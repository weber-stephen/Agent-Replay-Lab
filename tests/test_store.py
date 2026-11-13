import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest

from agentdbg.store import RunStore
from agentdbg.runtime import Recorder, canonical_request
from agentdbg.privacy import sanitize


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "store"
        self.store = RunStore(self.root)
        self.run = self.store.create_run("example", True)
        self.work = self.store.workdir(self.run)

    def test_round_trip_dedup_and_private(self):
        self.assertEqual(canonical_request("tool", {"b": 2, "a": [2, 1]}),
                         b'{"operation":"tool","request":{"a":[2,1],"b":2}}')
        step = {"operation": "tool", "request": {"name": "lookup_customer"},
                "response": {"x": 1}, "origin": "recorded", "status": "ok"}
        self.store.append_step(self.run, step)
        self.store.append_step(self.run, step)
        self.assertEqual(len(list((self.root / "payloads").iterdir())), 2)
        self.store.save_checkpoint(self.run, 0, {"position": 0}, self.work)
        self.assertEqual(self.store.restore_checkpoint(self.run, 0, self.work / "copy"), {"position": 0})
        self.assertEqual(self.store.get_steps(self.run)[0]["response"], {"x": 1})
        if os.name != "nt":
            self.assertEqual(self.root.stat().st_mode & 0o077, 0)
            self.assertTrue(all(p.stat().st_mode & 0o077 == 0 for p in (self.root / "payloads").iterdir()))

    def test_recorder_preserves_values_and_errors(self):
        seen = []
        rec = Recorder(self.store, self.run, self.work, True,
                       lambda request: seen.append(request) or {},
                       lambda name, args: seen.append(args) or None)
        self.assertEqual(rec.model_call({"messages": []}), {})
        self.assertIsNone(rec.tool_call("x", {}, "read"))
        self.assertEqual(seen, [{"messages": []}, {}])
        self.assertEqual([s["response"] for s in self.store.get_steps(self.run)], [{}, None])
        for exc in (ValueError("bad"), TimeoutError("late"), KeyboardInterrupt()):
            rec.model = lambda request, exc=exc: (_ for _ in ()).throw(exc)
            with self.assertRaises(type(exc)):
                rec.model_call({"messages": []})
        self.assertEqual([s["status"] for s in self.store.get_steps(self.run)][2:],
                         ["error", "timeout", "cancelled"])

    def test_bad_state_keeps_checkpoint(self):
        self.store.save_checkpoint(self.run, 0, {"ok": True}, self.work)
        with self.assertRaises((TypeError, ValueError)):
            self.store.save_checkpoint(self.run, 1, {"bad": object()}, self.work)
        self.assertEqual(self.store.restore_checkpoint(self.run, 0, self.work / "copy"), {"ok": True})

    def test_escaping_symlink(self):
        outside = Path(self.tmp.name) / "outside"
        outside.write_text("outside")
        try:
            (self.work / "link").symlink_to(outside)
        except OSError:
            self.skipTest("symlink creation unavailable on this host")
        with self.assertRaises(ValueError):
            self.store.save_checkpoint(self.run, 1, {"ok": True}, self.work)

    def test_redaction_and_limits(self):
        secret = "SECRET123"
        store = RunStore(Path(self.tmp.name) / "private", frozenset({"password"}), frozenset({secret}))
        run = store.create_run("example", True)
        work = store.workdir(run)
        (work / "secret.txt").write_text(secret)
        store.append_step(run, {"operation": "model", "request": {"headers": {"Authorization": "Bearer " + secret},
                           "password": "abc", "text": secret}, "response": {"answer": "ok"}})
        store.save_checkpoint(run, 0, {"nested": {"password": secret}}, work)
        payload_text = "".join(p.read_text(errors="ignore") for p in (store.root / "payloads").iterdir())
        db_bytes = (store.root / "runs.sqlite3").read_bytes()
        self.assertNotIn(secret, payload_text)
        self.assertNotIn(secret.encode(), db_bytes)
        self.assertNotIn("Bearer", payload_text)
        self.assertFalse(store.get_run(run)["replayable"])
        self.assertEqual(store.checkpoint(run, 0)["files"]["secret.txt"], {"omitted": "known secret"})
        store.append_step(run, {"operation": "tool", "request": {"huge": "x" * (1024 * 1024)}, "response": {}})
        self.assertIn("truncated", json.dumps(store.get_steps(run)[-1]))
        self.assertFalse(store.get_run(run)["replayable"])
        metadata = store.create_run("example", False)
        self.assertFalse(store.get_run(metadata)["replayable"])
        store.run_limit = 8
        store.append_step(run, {"operation": "tool", "request": {"x": "larger"}, "response": {}})
        self.assertIn("run_size_limit", json.dumps(store.get_steps(run)[-1]))

    def test_metadata_only_checkpoint_keeps_no_content(self):
        run = self.store.create_run("example", False)
        work = self.store.workdir(run)
        (work / "note.txt").write_text("private note")
        self.store.save_checkpoint(run, 0, {"messages": ["private conversation"]}, work)
        self.assertNotIn(b"private conversation", self.store.db.read_bytes())
        payloads = b"".join(p.read_bytes() for p in (self.root / "payloads").iterdir())
        self.assertNotIn(b"private conversation", payloads)
        self.assertNotIn(b"private note", payloads)
        self.assertFalse(self.store.checkpoint(run, 0)["replayable"])

    def test_secret_keys_and_tuple_values_are_redacted(self):
        safe, changed = sanitize({"SECRET123": "x", "tuple": ("SECRET123",)}, frozenset(), frozenset({"SECRET123"}))
        self.assertTrue(changed)
        self.assertNotIn("SECRET123", json.dumps(safe))
        secret_store = RunStore(Path(self.tmp.name) / "secret-keys", secret_values=frozenset({"SECRET123"}))
        run = secret_store.create_run("example", True)
        secret_store.append_step(run, {"operation": "model", "request": {"SECRET123": ("SECRET123",)}, "response": {}})
        self.assertNotIn(b"SECRET123", secret_store.db.read_bytes())
        self.assertNotIn(b"SECRET123", b"".join(p.read_bytes() for p in (secret_store.root / "payloads").iterdir()))

    def test_checkpoint_files_count_toward_budget_without_raw_overflow(self):
        self.store.run_limit = 100
        work = self.work
        for index in range(3):
            (work / f"{index}.txt").write_text(chr(65 + index) * 60)
        self.store.save_checkpoint(self.run, 0, {"state": "small"}, work)
        self.assertFalse(self.store.get_run(self.run)["replayable"])
        self.assertLessEqual(self.store.get_run(self.run)["bytes"], 100)
        payloads = b"".join(p.read_bytes() for p in (self.root / "payloads").iterdir())
        self.assertEqual(sum(bytes([65 + i]) * 60 in payloads for i in range(3)), 1)
        self.assertIn("run_size_limit", json.dumps(self.store.checkpoint(self.run, 0)))

    def test_state_over_budget_never_writes_raw_payload(self):
        self.store.run_limit = 20
        self.store.save_checkpoint(self.run, 0, {"state": "private-state-content"}, self.work)
        payloads = b"".join(p.read_bytes() for p in (self.root / "payloads").iterdir())
        self.assertNotIn(b"private-state-content", payloads)
        self.assertLessEqual(self.store.get_run(self.run)["bytes"], 20)
        self.assertIn("run_size_limit", json.dumps(self.store.checkpoint(self.run, 0)))

    def test_step_over_budget_keeps_marker_within_accounting_limit(self):
        self.store.run_limit = 1
        self.store.append_step(self.run, {"operation": "tool", "request": {"secret": "private-content"},
                                          "response": {"ok": True}})
        self.assertLessEqual(self.store.get_run(self.run)["bytes"], 1)
        self.assertIn("run_size_limit", json.dumps(self.store.get_steps(self.run)[0]))


if __name__ == "__main__":
    unittest.main()
