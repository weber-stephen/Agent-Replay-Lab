"""Human-readable run and context reports."""

import json


def _display(value):
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False)


def format_runs(store):
    return "\n".join(f"{r['id']} parent={r['parent_id'] or '-'} {r['created']} {r['status']} replayable={r['replayable']}"
                     for r in store.list_runs()) or "No runs"


def analyze_context(model_steps):
    results = []
    previous = []
    for step in model_steps:
        messages = step.get("request", {}).get("messages", [])
        if not isinstance(messages, list):
            messages = []
        by_role = {}
        for message in messages:
            role = message.get("role", "unknown")
            size = len(_display(message.get("content", "")).encode())
            item = by_role.setdefault(role, {"count": 0, "bytes": 0})
            item["count"] += 1
            item["bytes"] += size
        common = min(len(previous), len(messages))
        changed = sum(previous[i] != messages[i] for i in range(common))
        repeated = [m for m in messages if m.get("role") == "tool" and
                    len(_display(m.get("content", "")).encode()) >= 512 and m in previous]
        results.append({"added": max(0, len(messages) - len(previous)),
                        "removed": max(0, len(previous) - len(messages)), "changed": changed,
                        "by_role": by_role, "repeated_large_tool_outputs": repeated,
                        "unchanged_content_candidates": [m for m in messages if m in previous],
                        "input_tokens": step.get("response", {}).get("usage", {}).get("input_tokens")
                        if isinstance(step.get("response"), dict) else None})
        previous = messages
    return results


def format_inspect(store, run_id, step=None):
    run = store.get_run(run_id)
    steps = store.get_steps(run_id)
    lines = [f"run {run_id}: {run['status']} replayable={run['replayable']}"]
    if step is None:
        lines += [f"step {s['sequence']}: {s['operation']} {s['status']} {s['origin']}" for s in steps]
        step = len(steps) if steps else None
    if step is None:
        return "\n".join(lines)
    selected = next((s for s in steps if s["sequence"] == step), None)
    if selected is None:
        raise KeyError(f"unknown step: {step}")
    lines += [f"step {step}: {selected['operation']} {selected['status']} origin={selected['origin']} ",
              f"duration: {selected['duration_ms'] or 0:.2f} ms", "request: " + _display(selected["request"]),
              "response: " + _display(selected["response"])]
    if selected["limitation"]:
        lines.append("limitation: " + selected["limitation"])
    try:
        before = store.checkpoint(run_id, step - 1)["state"]
        after = store.checkpoint(run_id, step)["state"]
        delta = {key: {"before": before.get(key), "after": after.get(key)} for key in after if before.get(key) != after.get(key)}
        lines.append("state delta: " + _display(delta))
    except KeyError:
        lines.append("state delta: unavailable")
    hints = {("example", "search_invoice"): "stale invoice status; expected paid",
             ("triage", "get_ticket"): "ticket priority is low; expected high",
             ("delivery", "model"): "model says in transit; expected delivered"}
    hint = hints.get((run["application"], selected["request"].get("name") if
                      selected["operation"] == "tool" else selected["operation"]))
    if hint:
        lines.append("seeded failure: " + hint)
    if selected["operation"] == "model":
        context = analyze_context([s for s in steps if s["operation"] == "model" and s["sequence"] <= step])[-1]
        lines.append("context: " + _display({k: v for k, v in context.items() if k != "input_tokens"}))
        if context["input_tokens"] is not None:
            lines.append(f"reported input tokens: {context['input_tokens']}")
    return "\n".join(lines)


def format_diff(store, original_id, fork_id):
    original = store.get_steps(original_id)
    fork = store.get_steps(fork_id)
    fork_run = store.get_run(fork_id)
    first = None
    for i in range(min(len(original), len(fork))):
        if original[i]["request"] != fork[i]["request"]:
            first = f"first changed request at step {i + 1}:\noriginal: {_display(original[i]['request'])}\nfork: {_display(fork[i]['request'])}"
            break
        if original[i]["response"] != fork[i]["response"]:
            first = f"first changed response at step {i + 1}:\noriginal: {_display(original[i]['response'])}\nfork: {_display(fork[i]['response'])}"
            break
    if first is None:
        first = f"first changed length: original {len(original)} steps, fork {len(fork)} steps" if len(original) != len(fork) else "no changed requests or responses"
    lines = [first]
    for i in range(min(len(original), len(fork))):
        try:
            old = store.checkpoint(original_id, i + 1)["state"]
            new = store.checkpoint(fork_id, i + 1)["state"]
        except KeyError:
            continue
        if old != new:
            lines.append(f"state changed at step {i + 1}: {_display(new)}")
            break
    if not any(line.startswith("state") for line in lines):
        lines.append("state: no captured change")
    for label, steps in (("original", original), ("fork", fork)):
        tokens = [s["response"]["usage"]["input_tokens"] for s in steps if isinstance(s["response"], dict)
                  and isinstance(s["response"].get("usage"), dict) and "input_tokens" in s["response"]["usage"]]
        if tokens:
            lines.append(f"{label} reported input tokens: {sum(tokens)}")
    lines.append(f"final outcome: {fork_run['status']}")
    if fork_run["status"] == "blocked":
        lines.append("blocked path; no final answer comparison")
    else:
        old_state = store.checkpoint(original_id, len(original))["state"]
        new_state = store.checkpoint(fork_id, len(fork))["state"]
        lines.append(f"answer: {old_state.get('answer')} → {new_state.get('answer')}")
    return "\n".join(lines)
