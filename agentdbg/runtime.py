"""Agent boundary recording and canonical matching input."""

import time
import json
import sys

from .privacy import canonical


def canonical_request(operation, request):
    return canonical({"operation": operation, "request": request})


class Recorder:
    def __init__(self, store, run_id, workdir, capture_content, model, tool):
        self.store, self.run_id, self.workdir = store, run_id, workdir
        self.model, self.tool = model, tool
        self.sequence = len(store.get_steps(run_id))

    def checkpoint(self, state):
        self.store.save_checkpoint(self.run_id, self.sequence, state, self.workdir)

    def _call(self, operation, request, effect, invoke):
        started = time.monotonic()
        step = {"operation": operation, "request": request, "effect": effect, "origin": "recorded"}
        try:
            response = invoke()
            step.update(response=response, status="ok")
            return response
        except BaseException as exc:
            step.update(response=None, status="cancelled" if isinstance(exc, KeyboardInterrupt) else
                        "timeout" if isinstance(exc, TimeoutError) else "error",
                        error_type=type(exc).__name__, error_message=str(exc))
            raise
        finally:
            step["duration_ms"] = (time.monotonic() - started) * 1000
            self.sequence = self.store.append_step(self.run_id, step)

    def model_call(self, request):
        return self._call("model", request, None, lambda: self.model(request))

    def tool_call(self, name, arguments, effect):
        return self._call("tool", {"name": name, "arguments": arguments}, effect,
                          lambda: self.tool(name, arguments))


class WorkerBoundary:
    """One JSON object per line; stdout is reserved for protocol data."""

    def _exchange(self, message):
        print(json.dumps(message, separators=(",", ":")), flush=True)
        reply = json.loads(sys.stdin.readline())
        if "blocked" in reply:
            raise RuntimeError(reply["blocked"])
        if "error" in reply:
            error = reply["error"]
            exception = {"TimeoutError": TimeoutError, "KeyboardInterrupt": KeyboardInterrupt,
                         "ValueError": ValueError}.get(error["type"], RuntimeError)
            raise exception(error.get("message") or error["type"])
        return reply.get("response")

    def checkpoint(self, state):
        self._exchange({"type": "checkpoint", "state": state})

    def model_call(self, request):
        return self._exchange({"type": "boundary", "operation": "model", "request": request})

    def tool_call(self, name, arguments, effect):
        return self._exchange({"type": "boundary", "operation": "tool",
                               "request": {"name": name, "arguments": arguments}, "effect": effect})
