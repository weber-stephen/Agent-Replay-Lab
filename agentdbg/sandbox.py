"""Strict host replay controller and restricted Docker worker command."""

import hashlib
import json
from pathlib import Path
import shutil
import subprocess

from .runtime import canonical_request
from .privacy import environment_secrets


class RecordedFailure(Exception):
    def __init__(self, step):
        self.status = step["status"]
        self.error_type = step["error_type"]
        self.error_message = step["error_message"]
        super().__init__(self.error_message or self.error_type or self.status)


def docker_command(source: Path, workdir: Path) -> list[str]:
    source, workdir = Path(source).resolve(), Path(workdir).resolve()
    if source.name != "agentdbg" or workdir.parent.name != "work" or not workdir.is_dir():
        raise ValueError("worker mount must use package source and a fresh run directory")
    return ["docker", "run", "-i", "--rm", "--network", "none", "--read-only", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges", "--tmpfs", "/tmp", "-v", f"{source}:/app/agentdbg:ro",
            "-v", f"{workdir}:/run:rw", "-w", "/app", "python:3.11-slim", "python", "-m", "agentdbg.example", "--worker"]


class ReplayController:
    def __init__(self, store):
        self.store = store

    def prepare(self, parent_run_id, from_step, override=None):
        parent = self.store.get_run(parent_run_id)
        if parent["application"] != "example":
            raise ValueError("unsupported replay application")
        if not parent["replayable"]:
            raise ValueError("replay limitation: content was omitted, redacted, or truncated")
        self.steps = self.store.get_steps(parent_run_id)
        if from_step < 1 or from_step > len(self.steps) + 1:
            raise ValueError("invalid replay step")
        if override is not None:
            if (not isinstance(override, dict) or set(override) != {"step", "operation", "response"} or override["step"] != from_step or
                    from_step > len(self.steps) or override["operation"] != self.steps[from_step - 1]["operation"] or
                    not isinstance(override["response"], dict) or
                    (override["operation"] == "model" and "answer" not in override["response"])):
                raise ValueError("override step, operation, or response is invalid")
            expected_response = self.steps[from_step - 1]["response"]
            if isinstance(expected_response, dict) and any(
                key not in override["response"] or type(override["response"][key]) is not type(value)
                for key, value in expected_response.items() if key != "usage"):
                raise ValueError("override response is incompatible with recorded operation")
        self.cursor = from_step
        self.override = override
        self.last_origin = None

    def match(self, operation, request):
        if self.cursor > len(self.steps):
            raise ValueError("divergence: no next captured request")
        expected = self.steps[self.cursor - 1]
        if expected["limitation"] or not expected["request_hash"]:
            raise ValueError("replay limitation: required payload unavailable")
        digest = hashlib.sha256(canonical_request(operation, request)).hexdigest()
        if operation != expected["operation"] or digest != expected["request_hash"]:
            raise ValueError(f"divergence at step {self.cursor}: expected {expected['operation']} {expected['request_hash']}, got {operation} {digest}")
        override = self.override if self.override is not None and self.override["step"] == self.cursor else None
        if expected["effect"] == "external_write" and not override:
            raise ValueError("external_write requires an explicit mock response")
        response = override["response"] if override else expected["response"]
        self.last_origin = "overridden" if override else "recorded"
        self.cursor += 1
        if expected["status"] != "ok" and not override:
            raise RecordedFailure(expected)
        return response

    def replay(self, parent_run_id: str, from_step: int, override: dict | None = None) -> str:
        self.prepare(parent_run_id, from_step, override)
        checkpoint = self.store.checkpoint(parent_run_id, from_step - 1)
        if not checkpoint["replayable"]:
            raise ValueError("replay limitation: checkpoint unavailable")
        parent = self.store.get_run(parent_run_id)
        self.store = self.store.__class__(self.store.root,
                                           frozenset(parent["redact_fields"]) | self.store.fields,
                                           self.store.secrets | environment_secrets())
        fork = self.store.create_run("example", True, parent_run_id, from_step)
        workdir = self.store.workdir(fork)
        for step in self.steps[:from_step - 1]:
            self.store.append_step(fork, step)
        state = self.store.restore_checkpoint(parent_run_id, from_step - 1, workdir)
        self.store.save_checkpoint(fork, from_step - 1, state, workdir)
        try:
            docker_ready = bool(shutil.which("docker")) and subprocess.run(
                ["docker", "info"], capture_output=True, timeout=5).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            docker_ready = False
        if not docker_ready:
            self.store.append_step(fork, {"operation": "system", "request": {"from": from_step},
                                          "response": {"blocked": "Docker engine unavailable"}, "status": "blocked", "origin": "blocked"})
            self.store.set_status(fork, "blocked")
            return fork
        command = docker_command(Path(__file__).parent, workdir)
        if workdir != self.store.workdir(fork):
            raise ValueError("worker directory is outside this run")
        proc = None
        try:
            recorded_failure = False
            proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True)
            proc.stdin.write(json.dumps({"state": state}) + "\n")
            proc.stdin.flush()
            for line in proc.stdout:
                message = json.loads(line)
                if message["type"] == "boundary":
                    try:
                        response = self.match(message["operation"], message["request"])
                        self.store.append_step(fork, {"operation": message["operation"], "request": message["request"],
                                                      "response": response, "effect": message.get("effect"),
                                                      "origin": self.last_origin})
                        reply = {"response": response}
                    except RecordedFailure as exc:
                        source_step = self.steps[self.cursor - 2]
                        self.store.append_step(fork, source_step)
                        reply = {"error": {"type": exc.error_type, "message": exc.error_message}}
                        recorded_failure = True
                    except ValueError as exc:
                        self.store.append_step(fork, {"operation": message["operation"], "request": message["request"],
                                                      "response": {"blocked": str(exc)}, "status": "blocked", "origin": "blocked"})
                        reply = {"blocked": str(exc)}
                        self.store.set_status(fork, "blocked")
                elif message["type"] == "checkpoint":
                    self.store.save_checkpoint(fork, self.cursor - 1, message["state"], workdir)
                    reply = {}
                elif message["type"] == "final":
                    self.store.set_status(fork, "replayed")
                    break
                else:
                    raise ValueError("invalid worker protocol message")
                proc.stdin.write(json.dumps(reply) + "\n")
                proc.stdin.flush()
            proc.stdin.close()
            code = proc.wait()
            if code and recorded_failure and self.store.get_run(fork)["status"] != "blocked":
                self.store.set_status(fork, "replayed_error")
            if code and self.store.get_run(fork)["status"] != "blocked":
                if not recorded_failure:
                    self.store.append_step(fork, {"operation": "system", "request": {},
                                                  "response": {"blocked": "worker failed"}, "status": "blocked", "origin": "blocked"})
                    self.store.set_status(fork, "blocked")
        except (OSError, ValueError) as exc:
            self.store.append_step(fork, {"operation": "system", "request": {},
                                          "response": {"blocked": type(exc).__name__}, "status": "blocked", "origin": "blocked"})
            self.store.set_status(fork, "blocked")
        finally:
            if proc is not None:
                if getattr(proc, "poll", lambda: 0)() is None:
                    proc.kill()
                    proc.wait()
                for stream in (proc.stdin, proc.stdout, proc.stderr):
                    if stream is not None and not stream.closed:
                        stream.close()
        return fork
