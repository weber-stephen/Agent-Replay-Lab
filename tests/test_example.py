import json
import unittest

from agentdbg.example import ExampleAgent, fixture_model, fixture_tool


class FakeBoundary:
    def __init__(self):
        self.calls = []
        self.states = []
        self.requests = []

    def checkpoint(self, state):
        self.states.append(json.loads(json.dumps(state)))

    def tool_call(self, name, arguments, effect):
        self.calls.append(name)
        return fixture_tool(name, arguments)

    def model_call(self, request):
        self.requests.append(request)
        return fixture_model(request)


class ExampleTests(unittest.TestCase):
    def test_seeded_failure_and_resume(self):
        boundary = FakeBoundary()
        agent = ExampleAgent()
        self.assertEqual(agent.run(boundary), "unpaid")
        self.assertEqual(agent.expected_answer, "paid")
        self.assertEqual(boundary.calls, ["lookup_customer", "search_invoice", "read_policy"])
        self.assertEqual(boundary.states[0]["next_operation"], 0)
        self.assertEqual(boundary.states[1]["next_operation"], 1)
        self.assertIn('"as_of": "2025-01-01"', json.dumps(boundary.requests[-1]))
        resumed = ExampleAgent()
        resumed.restore_state(boundary.states[1])
        other = FakeBoundary()
        self.assertEqual(resumed.run(other), "unpaid")
        self.assertEqual(other.calls, ["search_invoice", "read_policy"])


if __name__ == "__main__":
    unittest.main()
