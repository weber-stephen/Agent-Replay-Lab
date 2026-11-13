# agentdbg: offline invoice replay prototype

`agentdbg` records, inspects, replays, and forks **only its bundled offline invoice example**. It demonstrates how a stale tool result changes an agent's answer. It does not connect to a live model or replay arbitrary Python agents.

## Before you record

**Captured content may contain private data.** `--capture-content` saves full requests, responses, state, and checkpoint files. Redaction covers configured field names and recognized credentials, but an unexpected secret or personal detail can remain in the store. Use a disposable, user-private storage directory, inspect its contents, and never share a capture without reviewing it. On Windows, user-only ACL protection has not been verified.

## Prerequisites

- Python 3.11 or newer and `pip`. The package has no runtime dependencies outside the Python standard library.
- For `replay` and `fork`: a Docker CLI connected to a **Linux** container engine. `docker info` must succeed; the first replay may pull `python:3.11-slim`. Recording and inspection do not need Docker.
- A checkout of this repository for the corrected response fixture at `tests/fixtures/corrected-tool-result.json`.

## First run from an installed package

From the checkout directory, install and use the CLI entry point. The `.agentdbg` directory should be new and private. A plain `python -m pip install .` was tested in a clean environment with access to the package index for the build dependency. Offline installation needs a locally available setuptools package.

```sh
python -m pip install .
agentdbg --store .agentdbg record --capture-content -- python -m agentdbg.example
agentdbg --store .agentdbg runs
agentdbg --store .agentdbg inspect RUN_ID --step 2
agentdbg --store .agentdbg inspect RUN_ID --step 4
agentdbg --store .agentdbg replay RUN_ID --from 1
agentdbg --store .agentdbg fork RUN_ID --step 2 --response tests/fixtures/corrected-tool-result.json
agentdbg --store .agentdbg diff RUN_ID FORK_ID
```

Replace `RUN_ID` with the ID printed by `record`, and `FORK_ID` with the ID printed by `fork`. Use a different `--store` directory for another fresh run. Typical output is:

```text
run <RUN_ID> answer=unpaid expected=paid
<RUN_ID> parent=- <CREATED_AT> wrong_answer replayable=True
<REPLAY_ID> replayed
<FORK_ID> blocked
first changed response at step 2:
```

The exact IDs and report details vary. Step 2 shows the stale `search_invoice` result dated `2025-01-01`; step 4 shows that result in the model request. Unchanged replay uses captured responses and reaches `unpaid` again. The corrected step-2 response says `paid`. That changes the later model request, so strict replay **blocks at the first unmatched request** instead of making a live call or reusing a response for a different request. The original run stays intact; `diff` starts with the changed response and shows the blocked outcome.

## Run directly from the source tree

Without installing the package, run from the checkout directory and use `python -m agentdbg.cli` in place of `agentdbg`:

```sh
python -m agentdbg.cli --store .agentdbg record --capture-content -- python -m agentdbg.example
python -m agentdbg.cli --store .agentdbg inspect RUN_ID --step 2
python -m agentdbg.cli --store .agentdbg replay RUN_ID --from 1
python -m agentdbg.cli --store .agentdbg fork RUN_ID --step 2 --response tests/fixtures/corrected-tool-result.json
python -m agentdbg.cli --store .agentdbg diff RUN_ID FORK_ID
```

The installed `agentdbg` command works from another directory, but the fixture path above is relative to this checkout. On Windows with the Python launcher, the source-tree record command is `py -3 -m agentdbg.cli --store .agentdbg record --capture-content -- py -3 -m agentdbg.example`. Docker Desktop must use Linux containers. Windows CI passed package installation, CLI help, and the unit suite, including source-tree recording. Docker-backed replay and storage ACLs have not been verified on Windows.

## How it works and limits

The [example agent](agentdbg/example.py) calls the [recording boundary](agentdbg/runtime.py) for each model or tool operation and checkpoints its JSON state. The [store](agentdbg/store.py) keeps run metadata in SQLite and captured payloads in content-addressed files. The [replay controller](agentdbg/sandbox.py) starts the same example in Docker and supplies recorded responses only when the next operation and request hash match. The [reporter](agentdbg/report.py) reads saved runs for `inspect` and `diff`.

`record` accepts only the bundled `python -m agentdbg.example` command (or its Windows launcher form). The Python boundary is an integration example, not a supported general-agent replay interface: another host would need its own worker entry point and container integration. There are no live model or tool calls during replay. A matched `external_write` still needs an explicit mock response. The Docker worker has no network, a read-only root, and only a fresh run directory writable; this limits accidental effects and does not guarantee containment of hostile code.

Without `--capture-content`, runs are metadata-only and cannot replay. `--redact-field FIELD` can be repeated for additional keys. Known credentials from environment variables and common authorization headers are redacted; files containing a known secret are omitted. Redacted, omitted, or truncated content needed for matching blocks replay. Each payload is limited to 1 MiB and each run to 100 MiB. POSIX storage permissions are private; protect a Windows store with a user-only ACL. `--store ROOT` selects the store, or set `AGENTDBG_STORE`; no `.env` file is required.

## Contributing and security

Run `python -m unittest discover -s tests -v`; Docker integration tests run when a Linux engine is available and otherwise skip. See [CONTRIBUTING.md](CONTRIBUTING.md) for changes and [SECURITY.md](SECURITY.md) for private vulnerability reporting. The project is licensed under [MIT](LICENSE).
