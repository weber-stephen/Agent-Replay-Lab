"""Offline delivery example: correcting a final model response can finish replay."""

import copy
import sys

from .example import record_main, worker_main


def fixture_tool(name, arguments):
    if name != "track_package":
        raise ValueError(f"unknown tool: {name}")
    return {"package_id": "P-8", "status": "delivered"}


def fixture_model(request):
    return {"answer": "in transit", "model": "local-fixture"}


class DeliveryAgent:
    expected_answer = "delivered"

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
            result = boundary.tool_call("track_package", {"package_id": "P-8"}, "read")
            self.tool_results.append({"name": "track_package", "result": result})
            self.next_operation = 1
            boundary.checkpoint(self.get_state())
        if self.next_operation == 1:
            response = boundary.model_call({"model": "local-fixture", "tool_results": self.tool_results})
            self.answer = response["answer"]
            self.next_operation = 2
            boundary.checkpoint(self.get_state())
        return self.answer


if __name__ == "__main__":
    sys.exit(worker_main(DeliveryAgent) if "--worker" in sys.argv else
             record_main(DeliveryAgent, fixture_model, fixture_tool, "delivery"))
