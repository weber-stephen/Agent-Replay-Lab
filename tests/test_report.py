import tempfile
from pathlib import Path
import unittest

from agentdbg.example import ExampleAgent, fixture_model, fixture_tool
from agentdbg.report import analyze_context, format_inspect, format_runs, format_diff
from agentdbg.runtime import Recorder
from agentdbg.store import RunStore


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = RunStore(Path(self.tmp.name) / "store")
        self.run = self.store.create_run("example", True)
        agent = ExampleAgent()
        agent.run(Recorder(self.store, self.run, self.store.workdir(self.run), True, fixture_model, fixture_tool))
        self.store.set_status(self.run, "wrong_answer")

    def test_inspect_stale_step(self):
        self.assertIn(self.run, format_runs(self.store))
        report = format_inspect(self.store, self.run, 2)
        for text in ("search_invoice", "unpaid", "2025-01-01", "expected paid", "recorded", "ms", "state delta"):
            self.assertIn(text, report)
        model = format_inspect(self.store, self.run, 4)
        self.assertIn("input tokens: 42", model)
        self.assertIn("bytes", model)
        self.assertNotIn("estimated exact tokens", model)

    def test_context_changes_and_candidates(self):
        steps = [
            {"request": {"messages": [{"role": "user", "content": "Q"},
                                        {"role": "tool", "name": "search", "content": "x" * 600}]}, "response": {}},
            {"request": {"messages": [{"role": "user", "content": "Q"},
                                        {"role": "tool", "name": "search", "content": "x" * 600},
                                        {"role": "assistant", "content": "thinking"}]}, "response": {"usage": {"input_tokens": 8}}},
            {"request": {"messages": [{"role": "user", "content": "changed"},
                                        {"role": "tool", "name": "search", "content": "x" * 600}]}, "response": {}},
        ]
        context = analyze_context(steps)
        self.assertEqual(context[1]["added"], 1)
        self.assertEqual(context[2]["removed"], 1)
        self.assertEqual(context[2]["changed"], 1)
        self.assertIn("tool", context[1]["by_role"])
        self.assertTrue(context[1]["repeated_large_tool_outputs"])
        self.assertTrue(context[1]["unchanged_content_candidates"])
        self.assertEqual(context[1]["input_tokens"], 8)
        self.assertIsNone(context[0]["input_tokens"])

    def test_diff_leads_with_first_change_and_blocked_status(self):
        fork = self.store.create_run("example", True, self.run, 2)
        for step in self.store.get_steps(self.run)[:1]:
            self.store.append_step(fork, step)
        self.store.append_step(fork, {"operation": "tool", "request": self.store.get_steps(self.run)[1]["request"],
                                      "response": {"status": "paid"}, "origin": "overridden"})
        self.store.append_step(fork, {"operation": "model", "request": {"changed": True},
                                      "response": {"blocked": "divergence"}, "status": "blocked", "origin": "blocked"})
        self.store.set_status(fork, "blocked")
        report = format_diff(self.store, self.run, fork)
        self.assertLess(report.index("first changed response"), report.index("state"))
        self.assertIn("blocked", report)
        self.assertNotIn("changed answer", report)
        self.assertIn("42", report)


if __name__ == "__main__":
    unittest.main()
