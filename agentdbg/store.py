"""Private SQLite metadata and content-addressed run payloads."""

import hashlib
from contextlib import closing, contextmanager
import json
import os
from pathlib import Path
import sqlite3
import uuid

from .privacy import RUN_LIMIT, canonical, prepare, sanitize


class RunStore:
    def __init__(self, root: Path, redact_fields=frozenset(), secret_values=frozenset()):
        self.root = Path(root)
        self.fields = frozenset(field.lower() for field in redact_fields)
        self.secrets = secret_values
        self.run_limit = RUN_LIMIT
        if self.root.exists() and os.name != "nt" and self.root.stat().st_mode & 0o077:
            raise PermissionError("storage root is shared")
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        for name in ("payloads", "work"):
            (self.root / name).mkdir(mode=0o700, exist_ok=True)
        self.db = self.root / "runs.sqlite3"
        with self._connect() as con:
            con.executescript("""
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY, parent_id TEXT, fork_step INTEGER, created TEXT DEFAULT CURRENT_TIMESTAMP,
                    status TEXT, application TEXT, capture_content INTEGER, replayable INTEGER, live_calls INTEGER DEFAULT 0,
                    schema_version INTEGER DEFAULT 1, bytes INTEGER DEFAULT 0, redact_fields TEXT DEFAULT '[]');
                CREATE TABLE IF NOT EXISTS steps (
                    run_id TEXT, sequence INTEGER, operation TEXT, request_ref TEXT, response_ref TEXT,
                    request_hash TEXT, response_hash TEXT, status TEXT, origin TEXT, effect TEXT,
                    started TEXT, duration_ms REAL, error_type TEXT, error_message TEXT, limitation TEXT,
                    PRIMARY KEY(run_id, sequence));
                CREATE TABLE IF NOT EXISTS checkpoints (
                    run_id TEXT, sequence INTEGER, state_ref TEXT, files TEXT, replayable INTEGER,
                    PRIMARY KEY(run_id, sequence));
            """)
            if "redact_fields" not in {row[1] for row in con.execute("PRAGMA table_info(runs)")}:
                con.execute("ALTER TABLE runs ADD COLUMN redact_fields TEXT DEFAULT '[]'")
        if os.name != "nt":
            self.db.chmod(0o600)

    @contextmanager
    def _connect(self):
        with closing(sqlite3.connect(self.db)) as con, con:
            yield con

    def _payload(self, value):
        data = canonical(value)
        digest = hashlib.sha256(data).hexdigest()
        target = self.root / "payloads" / digest
        if not target.exists():
            with target.open("xb") as handle:
                if os.name != "nt":
                    target.chmod(0o600)
                handle.write(data)
        return digest, len(data)

    def _load(self, digest):
        return json.loads((self.root / "payloads" / digest).read_bytes()) if digest else None

    def create_run(self, application, capture_content, parent_id=None, fork_step=None):
        run_id = uuid.uuid4().hex[:12]
        with self._connect() as con:
            con.execute("INSERT INTO runs(id,parent_id,fork_step,status,application,capture_content,replayable,redact_fields) VALUES(?,?,?,?,?,?,?,?)",
                        (run_id, parent_id, fork_step, "running", application, int(capture_content), int(capture_content), json.dumps(sorted(self.fields))))
        self.workdir(run_id)
        return run_id

    def workdir(self, run_id):
        self.get_run(run_id)
        path = self.root / "work" / run_id
        path.mkdir(mode=0o700, exist_ok=True)
        return path

    def get_run(self, run_id):
        with self._connect() as con:
            con.row_factory = sqlite3.Row
            row = con.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(f"unknown run: {run_id}")
        result = dict(row)
        result["replayable"] = bool(result["replayable"])
        result["capture_content"] = bool(result["capture_content"])
        result["redact_fields"] = json.loads(result["redact_fields"])
        return result

    def list_runs(self):
        with self._connect() as con:
            return [self.get_run(row[0]) for row in con.execute("SELECT id FROM runs ORDER BY rowid")]

    def set_status(self, run_id, status):
        with self._connect() as con:
            con.execute("UPDATE runs SET status=? WHERE id=?", (status, run_id))

    def append_step(self, run_id, step):
        run = self.get_run(run_id)
        with self._connect() as con:
            sequence = con.execute("SELECT COALESCE(MAX(sequence),0)+1 FROM steps WHERE run_id=?", (run_id,)).fetchone()[0]
        request = step.get("request")
        response = step.get("response")
        if run["capture_content"]:
            request, request_limited = prepare(request, self.fields, self.secrets)
            response, response_limited = prepare(response, self.fields, self.secrets)
            limitation = "redacted_or_truncated" if request_limited or response_limited else None
        else:
            request = response = {"metadata_only": True}
            limitation = "metadata_only"
        size = len(canonical(request)) + len(canonical(response))
        over_run_limit = run["bytes"] + size > self.run_limit
        if over_run_limit:
            request = response = {"run_size_limit": True, "original_bytes": size}
            limitation = "run_size_limit"
        request_ref, request_size = self._payload(request)
        response_ref, response_size = self._payload(response)
        from .runtime import canonical_request
        request_hash = hashlib.sha256(canonical_request(step["operation"], request)).hexdigest() if not limitation else None
        response_hash = hashlib.sha256(canonical(response)).hexdigest() if not limitation else None
        safe_error, error_limited = prepare(step.get("error_message"), self.fields, self.secrets)
        if error_limited:
            limitation = limitation or "redacted_error"
        with self._connect() as con:
            con.execute("""INSERT INTO steps VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (run_id, sequence, step["operation"], request_ref, response_ref, request_hash, response_hash,
                         step.get("status", "ok"), step.get("origin", "recorded"), step.get("effect"),
                         step.get("started"), step.get("duration_ms"), step.get("error_type"),
                         json.dumps(safe_error), limitation))
            con.execute("UPDATE runs SET bytes=bytes+?, replayable=replayable AND ? WHERE id=?",
                        (0 if over_run_limit else request_size + response_size, int(limitation is None), run_id))
        return sequence

    def get_steps(self, run_id):
        self.get_run(run_id)
        with self._connect() as con:
            con.row_factory = sqlite3.Row
            rows = con.execute("SELECT * FROM steps WHERE run_id=? ORDER BY sequence", (run_id,)).fetchall()
        result = []
        for row in rows:
            step = dict(row)
            step["request"] = self._load(step.pop("request_ref"))
            step["response"] = self._load(step.pop("response_ref"))
            step["error_message"] = json.loads(step["error_message"])
            result.append(step)
        return result

    def save_checkpoint(self, run_id, sequence, state, workdir):
        workdir = Path(workdir).resolve()
        if workdir != self.workdir(run_id).resolve():
            raise ValueError("checkpoint work directory must be this run's directory")
        raw_state = canonical(state)  # Reject non-JSON state before changing the prior checkpoint.
        run = self.get_run(run_id)
        if not run["capture_content"]:
            state_ref, size = self._payload({"metadata_only": True})
            with self._connect() as con:
                con.execute("INSERT OR REPLACE INTO checkpoints VALUES(?,?,?,?,?)",
                            (run_id, sequence, state_ref, "{}", 0))
                con.execute("UPDATE runs SET bytes=bytes+?, replayable=0 WHERE id=?", (size, run_id))
            return
        safe_state, limited = prepare(json.loads(raw_state), self.fields, self.secrets)
        files = {}
        staged = []
        state_bytes = canonical(safe_state)
        used = run["bytes"]
        if used + len(state_bytes) > self.run_limit:
            safe_state = None
            limited = True
        else:
            used += len(state_bytes)
        for path in workdir.rglob("*"):
            if path.is_symlink():
                raise ValueError("checkpoint symlink is not allowed")
            if not path.is_file():
                continue
            data = path.read_bytes()
            if any(secret and secret.encode() in data for secret in self.secrets):
                files[str(path.relative_to(workdir))] = {"omitted": "known secret"}
                limited = True
                continue
            if len(data) > 1024 * 1024 or used + len(data) > self.run_limit:
                files[str(path.relative_to(workdir))] = {"truncated": True, "original_bytes": len(data),
                                                       "reason": "run_size_limit" if used + len(data) > self.run_limit else "payload_limit"}
                limited = True
            else:
                digest = hashlib.sha256(data).hexdigest()
                staged.append((digest, data))
                files[str(path.relative_to(workdir))] = digest
                used += len(data)
        for digest, data in staged:
            target = self.root / "payloads" / digest
            if not target.exists():
                with target.open("xb") as handle:
                    if os.name != "nt": target.chmod(0o600)
                    handle.write(data)
        state_ref = self._payload(safe_state)[0] if safe_state is not None else None
        with self._connect() as con:
            con.execute("INSERT OR REPLACE INTO checkpoints VALUES(?,?,?,?,?)",
                        (run_id, sequence, state_ref, json.dumps(files), int(not limited)))
            con.execute("UPDATE runs SET bytes=bytes+?, replayable=replayable AND ? WHERE id=?",
                        (used - run["bytes"], int(not limited), run_id))

    def checkpoint(self, run_id, sequence):
        with self._connect() as con:
            row = con.execute("SELECT state_ref,files,replayable FROM checkpoints WHERE run_id=? AND sequence=?", (run_id, sequence)).fetchone()
        if not row:
            raise KeyError(f"missing checkpoint {sequence}")
        return {"state": self._load(row[0]) if row[0] else {"run_size_limit": True},
                "files": json.loads(row[1]), "replayable": bool(row[2])}

    def restore_checkpoint(self, run_id, sequence, destination):
        checkpoint = self.checkpoint(run_id, sequence)
        destination = Path(destination)
        destination.mkdir(mode=0o700, parents=True, exist_ok=True)
        for name, ref in checkpoint["files"].items():
            if not isinstance(ref, str):
                continue
            path = destination / name
            if not path.resolve().is_relative_to(destination.resolve()):
                raise ValueError("unsafe checkpoint path")
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            path.write_bytes((self.root / "payloads" / ref).read_bytes())
            if os.name != "nt": path.chmod(0o600)
        return checkpoint["state"]
