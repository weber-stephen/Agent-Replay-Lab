"""Terminal entry point."""

import argparse
import json
import os
import subprocess
from pathlib import Path
import sys

from .report import format_diff, format_inspect, format_runs
from .store import RunStore
from .sandbox import ReplayController
from .privacy import environment_secrets


def main(argv=None):
    parser = argparse.ArgumentParser(prog="agentdbg")
    parser.add_argument("--store", default=os.environ.get("AGENTDBG_STORE", ".agentdbg"))
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("runs")
    record = commands.add_parser("record")
    record.add_argument("--capture-content", action="store_true")
    record.add_argument("--redact-field", action="append", default=[])
    record.add_argument("agent_command", nargs=argparse.REMAINDER)
    inspect = commands.add_parser("inspect")
    inspect.add_argument("run_id")
    inspect.add_argument("--step", type=int)
    replay = commands.add_parser("replay")
    replay.add_argument("run_id")
    replay.add_argument("--from", dest="from_step", type=int, required=True)
    fork = commands.add_parser("fork")
    fork.add_argument("run_id")
    fork.add_argument("--step", type=int, required=True)
    fork.add_argument("--response", type=Path, required=True)
    diff = commands.add_parser("diff")
    diff.add_argument("original_id")
    diff.add_argument("fork_id")
    args = parser.parse_args(argv)
    try:
        if args.command == "record":
            agent_command = args.agent_command[1:] if args.agent_command[:1] == ["--"] else args.agent_command
            integrated = (len(agent_command) == 3 and agent_command[0] in ("python", "python3", sys.executable)
                          and agent_command[1:] == ["-m", "agentdbg.example"])
            if not integrated and agent_command != ["py", "-3", "-m", "agentdbg.example"]:
                raise ValueError("record requires the integrated command: python -m agentdbg.example")
            store = RunStore(Path(args.store), frozenset(args.redact_field), environment_secrets())
            run_id = store.create_run("example", args.capture_content)
            env = dict(os.environ, AGENTDBG_RECORD_RUN=run_id, AGENTDBG_RECORD_STORE=str(store.root.resolve()),
                       AGENTDBG_CAPTURE_CONTENT="1" if args.capture_content else "0",
                       AGENTDBG_REDACT_FIELDS=json.dumps(args.redact_field))
            child = subprocess.run(agent_command, env=env, capture_output=True, text=True)
            if child.returncode:
                store.set_status(run_id, "error")
                raise ValueError(f"recording child failed (exit {child.returncode}); inspect the run for details")
            print(child.stdout.strip())
            return 0
        store = RunStore(Path(args.store))
        if args.command == "runs":
            print(format_runs(store))
        elif args.command == "inspect":
            print(format_inspect(store, args.run_id, args.step))
        elif args.command == "replay":
            fork = ReplayController(store).replay(args.run_id, args.from_step)
            print(f"{fork} {store.get_run(fork)['status']}")
        elif args.command == "fork":
            override = json.loads(args.response.read_text())
            if not isinstance(override, dict):
                raise ValueError("override file must contain an object")
            fork_id = ReplayController(store).replay(args.run_id, args.step, override)
            print(f"{fork_id} {store.get_run(fork_id)['status']}")
        elif args.command == "diff":
            print(format_diff(store, args.original_id, args.fork_id))
        return 0
    except (KeyError, ValueError, PermissionError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
