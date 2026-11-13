import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
from unittest.mock import patch
from pathlib import Path
import unittest

from agentdbg.cli import main
from agentdbg.store import RunStore
from agentdbg.sandbox import linux_docker_available
from agentdbg.report import format_inspect


class CliTests(unittest.TestCase):
    def test_runs_inspect_and_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(Path(tmp) / "store")
            run = store.create_run("example", True)
            store.append_step(run, {"operation": "tool", "request": {"name": "search_invoice"},
                                    "response": {"status": "unpaid", "as_of": "2025-01-01"}})
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(main(["--store", str(store.root), "runs"]), 0)
                self.assertEqual(main(["--store", str(store.root), "inspect", run, "--step", "1"]), 0)
            self.assertIn(run, output.getvalue())
            self.assertIn("unpaid", output.getvalue())
            with contextlib.redirect_stderr(io.StringIO()) as errors:
                self.assertNotEqual(main(["--store", str(store.root), "inspect", "missing"]), 0)
                self.assertNotEqual(main(["--store", str(store.root), "inspect", run, "--step", "99"]), 0)
            self.assertIn("unknown", errors.getvalue())

    def test_record_clean_directory_and_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "store"
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(main(["--store", str(root), "record", "--capture-content", "--",
                                       sys.executable, "-m", "agentdbg.example"]), 0)
            store = RunStore(root)
            run = store.list_runs()[0]
            self.assertEqual(len([s for s in store.get_steps(run["id"]) if s["operation"] == "tool"]), 3)
            self.assertIn("unpaid", output.getvalue())
            self.assertIn("2025-01-01", format_inspect(store, run["id"], 2))
            self.assertIn("tool_results", format_inspect(store, run["id"], 4))
            self.assertNotEqual(main(["--store", str(root), "record", "--", sys.executable, "-c", "print(1)"]), 0)
            self.assertEqual(len(store.list_runs()), 1)

    def test_module_entry_point(self):
        result = subprocess.run([sys.executable, "-m", "agentdbg.cli", "--help"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0)
        self.assertIn("record", result.stdout)

    def test_test_collection_without_docker_cli(self):
        env = dict(os.environ, PATH="")
        result = subprocess.run([sys.executable, "-c", "import tests.test_replay, tests.test_cli"],
                                env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_record_child_failure_does_not_print_secret(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"TEST_API_KEY": "SECRET123"}), patch(
            "agentdbg.cli.subprocess.run", return_value=subprocess.CompletedProcess([], 1, "", "SECRET123 failed")):
            with contextlib.redirect_stderr(io.StringIO()) as errors:
                self.assertNotEqual(main(["--store", str(Path(tmp) / "store"), "record", "--",
                                          sys.executable, "-m", "agentdbg.example"]), 0)
            self.assertNotIn("SECRET123", errors.getvalue())

    def test_record_accepts_windows_python_launcher_command(self):
        with tempfile.TemporaryDirectory() as tmp, patch(
            "agentdbg.cli.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "run example answer=unpaid", "")):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(["--store", str(Path(tmp) / "store"), "record", "--",
                                       "py", "-3", "-m", "agentdbg.example"]), 0)

    @unittest.skipUnless(linux_docker_available(), "Linux Docker engine unavailable")
    def test_clean_directory_replay_fork_diff(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "store"
            self.assertEqual(main(["--store", str(root), "record", "--capture-content", "--",
                                   sys.executable, "-m", "agentdbg.example"]), 0)
            store = RunStore(root)
            run = store.list_runs()[0]["id"]
            before = store.get_steps(run)
            files = {p.name: p.read_bytes() for p in (root / "payloads").iterdir()}
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(main(["--store", str(root), "replay", run, "--from", "1"]), 0)
            replay = store.list_runs()[-1]["id"]
            self.assertEqual(store.get_run(replay)["status"], "replayed")
            self.assertEqual(store.checkpoint(replay, 4)["state"]["answer"], "unpaid")
            fixture = Path(__file__).parent / "fixtures/corrected-tool-result.json"
            self.assertEqual(main(["--store", str(root), "fork", run, "--step", "2", "--response", str(fixture)]), 0)
            fork = store.list_runs()[-1]["id"]
            self.assertNotEqual(fork, run)
            self.assertEqual(store.get_run(fork)["status"], "blocked")
            with contextlib.redirect_stdout(io.StringIO()) as diff:
                self.assertEqual(main(["--store", str(root), "diff", run, fork]), 0)
            self.assertIn("first changed response", diff.getvalue().splitlines()[0])
            self.assertEqual(store.get_steps(run), before)
            self.assertTrue(all((root / "payloads" / name).read_bytes() == data for name, data in files.items()))


if __name__ == "__main__":
    unittest.main()
