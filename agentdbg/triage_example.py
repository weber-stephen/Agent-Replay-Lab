"""Offline support triage example: a bad priority changes the next tool request."""

import copy
import sys

from .example import record_main, worker_main


def fixture_tool(name, arguments):
    if name == "get_ticket":
        return {"ticket_id": "T-17", "priority": "low", "symptom": "service unavailable"}
    if name == "get_playbook":
        return {"priority": arguments["priority"],
                "action": "urgent" if arguments["priority"] == "high" else "routine"}
    raise ValueError(f"unknown tool: {name}")


def fixture_model(request):
    return {"answer": request["tool_results"][1]["result"]["action"], "model": "local-fixture"}


class TriageAgent:
    expected_answer = "urgent"

    def __init__(self):
        self.next_operation = 0
        self.tool_results = []
        self.answer = None

    def get_state(self):
        return copy.deepcopy({"next_operation": self.next_operation, "tool_results": self.tool_results,
                              "answer": self.answer})

    def restore_state(self, state):
        self.next_operation = state["next_operation"]
        self.tool_results = copy.deepcopy(state["tool_results"])
        self.answer = state["answer"]

    def run(self, boundary):
        if self.next_operation == 0:
            boundary.checkpoint(self.get_state())
            result = boundary.tool_call("get_ticket", {"ticket_id": "T-17"}, "read")
            self.tool_results.append({"name": "get_ticket", "result": result})
            self.next_operation = 1
            boundary.checkpoint(self.get_state())
        if self.next_operation == 1:
            priority = self.tool_results[0]["result"]["priority"]
            result = boundary.tool_call("get_playbook", {"priority": priority}, "read")
            self.tool_results.append({"name": "get_playbook", "result": result})
            self.next_operation = 2
            boundary.checkpoint(self.get_state())
        if self.next_operation == 2:
            response = boundary.model_call({"model": "local-fixture", "tool_results": self.tool_results})
            self.answer = response["answer"]
            self.next_operation = 3
            boundary.checkpoint(self.get_state())
        return self.answer


if __name__ == "__main__":
    sys.exit(worker_main(TriageAgent) if "--worker" in sys.argv else
             record_main(TriageAgent, fixture_model, fixture_tool, "triage"))
