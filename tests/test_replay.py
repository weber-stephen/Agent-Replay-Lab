import json
import io
from pathlib import Path
import tempfile
import subprocess
import shutil
import unittest
from unittest.mock import patch

from agentdbg.example import ExampleAgent, fixture_model, fixture_tool
from agentdbg.runtime import Recorder, WorkerBoundary
from agentdbg.sandbox import ReplayController, RecordedFailure, docker_command
from agentdbg.store import RunStore
from agentdbg.cli import main


class ReplayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = RunStore(Path(self.tmp.name) / "store")
        self.run = self.store.create_run("example", True)
        ExampleAgent().run(Recorder(self.store, self.run, self.store.workdir(self.run), True, fixture_model, fixture_tool))

    def test_strict_next_request(self):
        controller = ReplayController(self.store)
        steps = self.store.get_steps(self.run)
        controller.prepare(self.run, 2)
        self.assertEqual(controller.match("tool", steps[1]["request"]), steps[1]["response"])
        self.assertEqual(controller.cursor, 3)
        for operation, request in (("tool", {"name": "other"}), ("model", steps[2]["request"]),
                                   ("tool", steps[3]["request"])):
            controller.prepare(self.run, 2)
            with self.assertRaisesRegex(ValueError, "divergence"):
                controller.match(operation, request)
            self.assertEqual(controller.cursor, 2)

    def test_redacted_and_external_write(self):
        secret_store = RunStore(Path(self.tmp.name) / "redacted", secret_values=frozenset({"secret"}))
        run = secret_store.create_run("example", True)
        secret_store.append_step(run, {"operation": "tool", "request": {"value": "secret"}, "response": {}})
        controller = ReplayController(secret_store)
        with self.assertRaisesRegex(ValueError, "replay limitation"):
            controller.prepare(run, 1)
        run2 = self.store.create_run("example", True)
        self.store.append_step(run2, {"operation": "tool", "request": {"name": "send"},
                                      "response": {"ok": True}, "effect": "external_write"})
        controller = ReplayController(self.store)
        controller.prepare(run2, 1)
        with self.assertRaisesRegex(ValueError, "external_write"):
            controller.match("tool", {"name": "send"})
        self.assertEqual(controller.cursor, 1)

    def test_docker_command_and_missing_engine(self):
        source = Path(__file__).parents[1] / "agentdbg"
        work = self.store.workdir(self.run)
        command = docker_command(source, work)
        self.assertEqual(command[:4], ["docker", "run", "-i", "--rm"])
        for flag in ("--network", "none", "--read-only", "--tmpfs", "/tmp", "-w", "/app"):
            self.assertIn(flag, command)
        mounts = [command[i + 1] for i, value in enumerate(command[:-1]) if value == "-v"]
        self.assertEqual(len(mounts), 2)
        self.assertIn(":/app/agentdbg:ro", mounts[0])
        self.assertIn(":/run:rw", mounts[1])
        self.assertNotIn(str(self.store.db), " ".join(command))
        with self.assertRaises(ValueError):
            docker_command(source, Path(self.tmp.name) / "outside")
        with patch("agentdbg.sandbox.shutil.which", return_value=None):
            fork = ReplayController(self.store).replay(self.run, 1)
        self.assertEqual(self.store.get_run(fork)["status"], "blocked")
        self.assertIn("Docker", json.dumps(self.store.get_steps(fork)))
        with patch("agentdbg.sandbox.shutil.which", return_value="docker"), patch(
            "agentdbg.sandbox.subprocess.run", return_value=subprocess.CompletedProcess([], 1, "", "no daemon")):
            fork = ReplayController(self.store).replay(self.run, 1)
        self.assertIn("Docker engine unavailable", json.dumps(self.store.get_steps(fork)))

    @unittest.skipUnless(shutil.which("docker") and subprocess.run(["docker", "info"], stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL).returncode == 0, "Docker engine unavailable")
    def test_container_blocks_network_and_outside_write(self):
        command = docker_command(Path(__file__).parents[1] / "agentdbg", self.store.workdir(self.run))
        script = ("import socket; from pathlib import Path; "
                  "assert not Path('/outside').exists(); "
                  "\ntry: Path('/outside').write_text('bad'); assert False\nexcept OSError: pass\n"
                  "try: socket.create_connection(('example.com', 80), timeout=2); assert False\nexcept OSError: pass")
        result = subprocess.run(command[:-4] + ["python", "-c", script], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_fork_override_stops_at_changed_model_request(self):
        original_steps = self.store.get_steps(self.run)
        original_payloads = {p.name: p.read_bytes() for p in (self.store.root / "payloads").iterdir()}
        changed = dict(original_steps[3]["request"])
        changed["tool_results"] = list(changed["tool_results"])
        changed["tool_results"][1] = {"name": "search_invoice", "result": {"status": "paid"}}
        events = [
            {"type": "boundary", "operation": "tool", "request": original_steps[1]["request"], "effect": "read"},
            {"type": "checkpoint", "state": self.store.checkpoint(self.run, 2)["state"]},
            {"type": "boundary", "operation": "tool", "request": original_steps[2]["request"], "effect": "read"},
            {"type": "checkpoint", "state": self.store.checkpoint(self.run, 3)["state"]},
            {"type": "boundary", "operation": "model", "request": changed},
        ]

        class FakeProcess:
            def __init__(self):
                self.stdin = io.StringIO()
                self.stdout = io.StringIO("".join(json.dumps(e) + "\n" for e in events))
                self.stderr = io.StringIO()

            def wait(self):
                return 1

        override = json.loads((Path(__file__).parent / "fixtures/corrected-tool-result.json").read_text())
        process = FakeProcess()
        with patch("agentdbg.sandbox.shutil.which", return_value="docker"), patch(
            "agentdbg.sandbox.subprocess.run", return_value=subprocess.CompletedProcess([], 0)), patch(
            "agentdbg.sandbox.subprocess.Popen", return_value=process):
            fork = ReplayController(self.store).replay(self.run, 2, override)
        self.assertTrue(process.stdout.closed)
        self.assertTrue(process.stderr.closed)
        self.assertEqual(self.store.get_run(fork)["parent_id"], self.run)
        self.assertEqual(self.store.get_run(fork)["fork_step"], 2)
        self.assertEqual(self.store.get_run(fork)["status"], "blocked")
        self.assertEqual(self.store.get_steps(fork)[1]["origin"], "overridden")
        self.assertEqual(self.store.get_steps(fork)[1]["response"]["status"], "paid")
        self.assertEqual(self.store.get_steps(fork)[-1]["status"], "blocked")
        self.assertEqual(self.store.get_steps(self.run), original_steps)
        for name, data in original_payloads.items():
            self.assertEqual((self.store.root / "payloads" / name).read_bytes(), data)

    def test_bad_override_rejected_before_fork(self):
        controller = ReplayController(self.store)
        for override in ({"step": 3, "operation": "tool", "response": {}},
                         {"step": 2, "operation": "model", "response": {}},
                         {"step": 2, "operation": "tool", "response": "bad"}):
            with self.assertRaises(ValueError):
                controller.replay(self.run, 2, override)
        self.assertEqual(len(self.store.list_runs()), 1)

    def test_recorded_failure_is_not_successful_none(self):
        run = self.store.create_run("example", True)
        self.store.append_step(run, {"operation": "tool", "request": {"name": "search_invoice"},
                                     "response": None, "status": "timeout", "error_type": "TimeoutError", "error_message": "late"})
        controller = ReplayController(self.store)
        controller.prepare(run, 1)
        with self.assertRaises(RecordedFailure) as captured:
            controller.match("tool", {"name": "search_invoice"})
        self.assertEqual(captured.exception.status, "timeout")
        self.assertEqual(controller.cursor, 2)
        with patch("agentdbg.runtime.sys.stdin", io.StringIO('{"error":{"type":"TimeoutError","message":"late"}}\n')):
            with patch("agentdbg.runtime.sys.stdout", io.StringIO()):
                with self.assertRaises(TimeoutError):
                    WorkerBoundary().tool_call("search_invoice", {}, "read")

    def test_invalid_override_shapes_and_fork_redaction(self):
        controller = ReplayController(self.store)
        for bad in ({}, [], {"step": 2, "operation": "tool", "response": {}}):
            with self.assertRaises(ValueError):
                controller.replay(self.run, 2, bad)
        self.assertEqual(len(self.store.list_runs()), 1)
        policy_store = RunStore(Path(self.tmp.name) / "policy", frozenset({"secret_field"}))
        policy_run = policy_store.create_run("example", True)
        ExampleAgent().run(Recorder(policy_store, policy_run, policy_store.workdir(policy_run), True, fixture_model, fixture_tool))
        override = {"step": 2, "operation": "tool", "response": {
            "invoice_id": "INV-42", "status": "paid", "as_of": "2026-11-12", "secret_field": "SECRET123"}}
        # A fork must reapply the saved capture policy before writing the replacement.
        events = [{"type": "boundary", "operation": "tool", "request": policy_store.get_steps(policy_run)[1]["request"]}]
        class FakeProcess:
            def __init__(self):
                self.stdin = io.StringIO()
                self.stdout = io.StringIO(json.dumps(events[0]) + "\n")
                self.stderr = io.StringIO()
            def wait(self): return 1
        with patch("agentdbg.sandbox.shutil.which", return_value="docker"), patch(
            "agentdbg.sandbox.subprocess.run", return_value=subprocess.CompletedProcess([], 0)), patch(
            "agentdbg.sandbox.subprocess.Popen", return_value=FakeProcess()):
            fork = ReplayController(RunStore(policy_store.root)).replay(policy_run, 2, override)
        payloads = b"".join(p.read_bytes() for p in (policy_store.root / "payloads").iterdir())
        self.assertNotIn(b"SECRET123", payloads)
        self.assertFalse(policy_store.get_run(fork)["replayable"])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.json"
            path.write_text("{")
            self.assertNotEqual(main(["--store", str(self.store.root), "fork", self.run, "--step", "2", "--response", str(path)]), 0)
        self.assertEqual(len(self.store.list_runs()), 1)


if __name__ == "__main__":
    unittest.main()
