import json
import contextlib
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from agentdbg.cli import main
from agentdbg.report import format_diff, format_inspect
from agentdbg.runtime import Recorder
from agentdbg.sandbox import ReplayController, docker_command, linux_docker_available
from agentdbg.store import RunStore
from agentdbg.triage_example import TriageAgent, fixture_model as triage_model, fixture_tool as triage_tool
from agentdbg.delivery_example import DeliveryAgent, fixture_model as delivery_model, fixture_tool as delivery_tool


class MoreExampleTests(unittest.TestCase):
    def record(self, application, agent, model, tool):
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(Path(tmp) / "store")
            run = store.create_run(application, True)
            answer = agent.run(Recorder(store, run, store.workdir(run), True, model, tool))
            return answer, store.get_steps(run), [store.checkpoint(run, i)["state"] for i in range(len(store.get_steps(run)) + 1)]

    def test_triage_record_and_resume(self):
        answer, steps, states = self.record("triage", TriageAgent(), triage_model, triage_tool)
        self.assertEqual(answer, "routine")
        self.assertEqual([s["request"]["name"] for s in steps[:2]], ["get_ticket", "get_playbook"])
        self.assertEqual(steps[1]["request"]["arguments"]["priority"], "low")
        self.assertEqual(states[0]["next_operation"], 0)
        resumed = TriageAgent()
        resumed.restore_state(states[1])
        self.assertEqual(resumed.next_operation, 1)
        self.assertEqual(resumed.run(_Boundary(triage_model, triage_tool)), "routine")

    def test_delivery_record_and_resume(self):
        answer, steps, states = self.record("delivery", DeliveryAgent(), delivery_model, delivery_tool)
        self.assertEqual(answer, "in transit")
        self.assertEqual(steps[0]["request"]["name"], "track_package")
        self.assertEqual(steps[1]["operation"], "model")
        self.assertEqual(states[2]["answer"], "in transit")
        resumed = DeliveryAgent()
        resumed.restore_state(states[1])
        self.assertEqual(resumed.run(_Boundary(delivery_model, delivery_tool)), "in transit")

    def test_cli_records_only_named_examples(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(Path(tmp) / "store")
            for application in ("example", "triage", "delivery"):
                module = "agentdbg.example" if application == "example" else f"agentdbg.{application}_example"
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(main(["--store", str(store.root), "record", "--capture-content", "--",
                                           sys.executable, "-m", module]), 0)
                self.assertEqual(store.list_runs()[-1]["application"], application)
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertNotEqual(main(["--store", str(store.root), "record", "--",
                                          sys.executable, "-m", "agentdbg.other"]), 0)
            self.assertEqual(len(store.list_runs()), 3)

    def test_worker_module_is_fixed_by_application(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(Path(tmp) / "store")
            work = store.workdir(store.create_run("triage", True))
            source = Path(__file__).parents[1] / "agentdbg"
            self.assertEqual(docker_command(source, work, "triage")[-2:], ["agentdbg.triage_example", "--worker"])
            with self.assertRaises(ValueError):
                docker_command(source, work, "agentdbg.other")

    def test_local_worker_protocol_replay_and_fork(self):
        fixtures = Path(__file__).parent / "fixtures"
        for application, step, fixture, expected in (
            ("triage", 1, "corrected-triage-ticket.json", "blocked"),
            ("delivery", 2, "corrected-delivery-answer.json", "replayed"),
        ):
            with self.subTest(application=application), tempfile.TemporaryDirectory() as tmp:
                store = RunStore(Path(tmp) / "store")
                module = f"agentdbg.{application}_example"
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(main(["--store", str(store.root), "record", "--capture-content", "--",
                                           sys.executable, "-m", module]), 0)
                run = store.list_runs()[0]["id"]
                original = store.get_steps(run)
                with patch("agentdbg.sandbox.linux_docker_available", return_value=True), patch(
                    "agentdbg.sandbox.docker_command", return_value=[sys.executable, "-m", module, "--worker"]):
                    replay = ReplayController(store).replay(run, 1)
                    fork = ReplayController(store).replay(run, step, json.loads((fixtures / fixture).read_text()))
                self.assertEqual(store.get_run(replay)["status"], "replayed")
                self.assertEqual(store.get_run(fork)["status"], expected)
                self.assertEqual(store.get_steps(run), original)
                diff = format_diff(store, run, fork)
                self.assertIn("first changed response", diff)
                if application == "triage":
                    self.assertIn("get_playbook", format_inspect(store, fork, 2))
                    self.assertEqual(store.get_steps(fork)[-1]["status"], "blocked")
                else:
                    self.assertIn("in transit → delivered", diff)

    @unittest.skipUnless(linux_docker_available(), "Linux Docker engine unavailable")
    def test_cli_replay_and_fork_each_new_example(self):
        fixtures = Path(__file__).parent / "fixtures"
        for application, step, fixture, expected in (
            ("triage", 1, "corrected-triage-ticket.json", "blocked"),
            ("delivery", 2, "corrected-delivery-answer.json", "replayed"),
        ):
            with self.subTest(application=application), tempfile.TemporaryDirectory() as tmp:
                store = RunStore(Path(tmp) / "store")
                module = f"agentdbg.{application}_example"
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(main(["--store", str(store.root), "record", "--capture-content", "--",
                                           sys.executable, "-m", module]), 0)
                run = store.list_runs()[0]["id"]
                original = store.get_steps(run)
                self.assertIn(application, store.get_run(run)["application"])
                self.assertIn("seeded failure", format_inspect(store, run, step))
                replay = ReplayController(store).replay(run, 1)
                self.assertEqual(store.get_run(replay)["status"], "replayed")
                fork = ReplayController(store).replay(run, step, json.loads((fixtures / fixture).read_text()))
                self.assertEqual(store.get_run(fork)["status"], expected)
                self.assertEqual(store.get_steps(run), original)
                diff = format_diff(store, run, fork)
                self.assertIn("first changed response", diff)
                if application == "triage":
                    self.assertIn("get_playbook", format_inspect(store, fork, 2))
                    self.assertEqual(store.get_steps(fork)[-1]["status"], "blocked")
                else:
                    self.assertIn("in transit → delivered", diff)


class _Boundary:
    def __init__(self, model, tool):
        self.model, self.tool = model, tool

    def checkpoint(self, state):
        json.dumps(state)

    def tool_call(self, name, arguments, effect):
        return self.tool(name, arguments)

    def model_call(self, request):
        return self.model(request)
