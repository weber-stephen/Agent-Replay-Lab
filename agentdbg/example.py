"""Offline seeded example with three instrumented tools."""

import copy
import json
import os
import sys

from .runtime import WorkerBoundary
from .privacy import environment_secrets


QUESTION = "Is invoice INV-42 paid?"
TOOLS = ("lookup_customer", "search_invoice", "read_policy")


def fixture_tool(name, arguments):
    return {
        "lookup_customer": {"customer_id": "C-7", "invoice_id": "INV-42"},
        "search_invoice": {"invoice_id": "INV-42", "status": "unpaid", "as_of": "2025-01-01"},
        "read_policy": {"policy": "Answer using the invoice status returned by search_invoice."},
    }[name]


def fixture_model(request):
    invoice = next(result for result in request["tool_results"] if result["name"] == "search_invoice")
    return {"answer": invoice["result"]["status"], "model": "local-fixture", "usage": {"input_tokens": 42}}


class ExampleAgent:
    expected_answer = "paid"

    def __init__(self):
        self.messages = [{"role": "user", "content": QUESTION}]
        self.next_operation = 0
        self.tool_results = []
        self.answer = None

    def get_state(self):
        return copy.deepcopy({"messages": self.messages, "next_operation": self.next_operation,
                              "tool_results": self.tool_results, "answer": self.answer})

    def restore_state(self, snapshot):
        self.messages = copy.deepcopy(snapshot["messages"])
        self.next_operation = snapshot["next_operation"]
        self.tool_results = copy.deepcopy(snapshot["tool_results"])
        self.answer = snapshot["answer"]

    def run(self, boundary):
        if self.next_operation == 0:
            boundary.checkpoint(self.get_state())
        while self.next_operation < len(TOOLS):
            name = TOOLS[self.next_operation]
            result = boundary.tool_call(name, {"invoice_id": "INV-42"}, "read")
            self.tool_results.append({"name": name, "result": result})
            self.messages.append({"role": "tool", "name": name, "content": result})
            self.next_operation += 1
            boundary.checkpoint(self.get_state())
        if self.next_operation == len(TOOLS):
            response = boundary.model_call({"model": "local-fixture", "messages": self.messages,
                                            "tool_results": self.tool_results})
            self.answer = response["answer"]
            self.messages.append({"role": "assistant", "content": self.answer})
            self.next_operation += 1
            boundary.checkpoint(self.get_state())
        return self.answer


def worker_main(agent_type=ExampleAgent):
    init = json.loads(sys.stdin.readline())
    agent = agent_type()
    agent.restore_state(init["state"])
    try:
        answer = agent.run(WorkerBoundary())
        print(json.dumps({"type": "final", "answer": answer}), flush=True)
    except BaseException as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


def record_main(agent_type=ExampleAgent, model=fixture_model, tool=fixture_tool, application="example"):
    if not all(os.environ.get(key) for key in ("AGENTDBG_RECORD_RUN", "AGENTDBG_RECORD_STORE", "AGENTDBG_CAPTURE_CONTENT")):
        print("example recording requires agentdbg record", file=sys.stderr)
        return 2
    from pathlib import Path
    from .store import RunStore
    from .runtime import Recorder
    store = RunStore(Path(os.environ["AGENTDBG_RECORD_STORE"]),
                     frozenset(json.loads(os.environ.get("AGENTDBG_REDACT_FIELDS", "[]"))),
                     environment_secrets())
    run_id = os.environ["AGENTDBG_RECORD_RUN"]
    agent = agent_type()
    try:
        answer = agent.run(Recorder(store, run_id, store.workdir(run_id),
                                    os.environ["AGENTDBG_CAPTURE_CONTENT"] == "1", model, tool))
        if store.get_run(run_id)["application"] != application:
            raise ValueError("recording application mismatch")
        store.set_status(run_id, "wrong_answer" if answer != agent.expected_answer else "complete")
        print(f"run {run_id} answer={answer} expected={agent.expected_answer}")
        return 0
    except BaseException:
        store.set_status(run_id, "error")
        raise


if __name__ == "__main__":
    sys.exit(worker_main() if "--worker" in sys.argv else record_main())
