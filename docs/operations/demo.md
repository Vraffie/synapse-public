# Demos (mock path)

Two scripts share one launcher: `scripts/demo.py` (interactive Control Center demo) and `scripts/demo_mock.py` (CLI-only smoke harness).

## Control Center demo: `scripts/demo.py`

```
uv run --extra desktop --with-editable . python scripts/demo.py            # window + live daemon
uv run --extra desktop --with-editable . python scripts/demo.py --headless # no window, CI/smoke (about 12 s)
```

Steps: (1) a temporary 0700 state directory; (2) a real daemon subprocess on the MOCK provider (launched through `demo_mock.py _daemon`, below); (3) workflow `demo-pipeline` is published and started: `plan` -> `edit-config` + `update-docs` (parallel) -> `review` (first attempt rejected, so the task shows "changes required" and the retry passes) -> `deploy-verify`, which is gated on a human approval; (4) the desktop `MainWindow` opens on that state directory (the poller finds the socket and operator token there, exactly as `agent-desktop --state-dir` does) and the run is selected; (5) the script prints the gate banner and the exact `agentctl` command, then waits; (6) when the run reaches a terminal state it prints the final task states, keeps the window for `--linger` seconds, stops the daemon and removes the directory (also on Ctrl+C or SIGTERM).

Flags: `--headless` (approves through `agentctl approval-decide` and prints a line whenever the run or task states change), `--keep-state` (leave the directory; it holds both tokens), `--no-approve-wait` (stop at the gate; GUI: keep the window open until closed), `--no-clipboard`, `--linger SECONDS` (default 6), `--pace SECONDS` (how long each mock task stays running; default 2.5), `--screenshots DIR` (GUI: save PNGs of the window after each state change; development aid).

Token handling: the approver token is never printed. The printed command reads it with `$(cat <state>/approver.token)` into the `SYNAPSE_APPROVER_TOKEN` environment of that one command. In GUI mode the script also puts the token on the clipboard so it can be pasted into the Approve dialog, and clears the clipboard on exit if it still holds the token; `--no-clipboard` disables that (clipboard managers may keep history; the token is throwaway and dies with the demo).

What is mock and what is real: real are the daemon process, HTTP/Unix-socket API, SQLite database, migrations, workflow engine, approval service and expiry, token authentication and separation, the CLI and the desktop. Mock is the task execution: every step is the synthetic mock provider (fixed output, instant), so no agent does any work. Demo-only, inside the daemon child process only: the approval gate and scenarios are attached to workflow `demo-pipeline` at run start (no public interface can start a gated mock run), and `--pace` makes tasks and the "changes required" state last long enough to watch. Real Claude runs are opt-in via `scripts/demo_agent.py` (needs the explicit `--allow-same-user-agents` acknowledgement once PR #116 merges) and consume subscription quota; nothing here uses them.

Tests: `tests/test_demo_launcher.py` runs the headless flow, `--no-approve-wait`, Ctrl+C and a GUI (offscreen) run against real daemon subprocesses and checks that no process or directory is left behind and that token values never reach the output.

## CLI smoke harness: `scripts/demo_mock.py`

`scripts/demo_mock.py` drives a real daemon subprocess end to end using only the public `agentctl` CLI (`python -m synapse.cli`), a temporary private state directory and its socket. It is the first harness meant to be extended to the real agent later; today it covers the synthetic mock path only.

```
PYTHONPATH=src python scripts/demo_mock.py            # human timeline, fresh temp state dir
PYTHONPATH=src python scripts/demo_mock.py --json     # machine-readable report
PYTHONPATH=src python scripts/demo_mock.py --no-restart
```

Runs in about 5 seconds. Exit status is non-zero if any check fails.

## What it does

1. Starts the daemon, provisions tokens (`approver-init`; the operator token is created by the daemon) and publishes three two-node workflows (`build` -> `review`).
2. `demo-plain`: queued and started with `mock-start` (fixed fixture) and awaited.
3. `demo-gated`: `review` is gated on a human approval. Once the task is parked the daemon is killed with SIGKILL and restarted; startup recovery (`recover_interrupted` / `resume_parked`) must keep the run `running` with the task parked. The script first tries to decide with the OPERATOR token (must be refused and audited as denied), then decides as the human approver via `agentctl approval-decide`, and awaits success.
4. `demo-expiry`: same gating with a 1 second ttl and no decision. The expiry sweep must expire the approval and fail the gated task (audited as `daemon:expiry`), after which the park re-drive closes the run as `failed`.
5. `agentctl backup` of the live database followed by `backup-verify`.
6. Prints a compact timeline merged from the `events` table (run, approval and gate events) and `audit_entries` (approval records), read through a read-only SQLite connection.

## Gated runs: the one non-public path

Approval steps are runtime-level only (`MockRuntime.start(..., approval_steps=...)`); no HTTP or CLI surface exists, by design. The harness therefore launches the daemon through its own `_daemon` subcommand, which calls the real `synapse.daemon.__main__.run` after patching, in that child process only:

- `MockRuntime.start` attaches an `approval_steps` spec for node `review` when the run belongs to workflow `demo-gated` or `demo-expiry` (the debug path; `demo-plain` and any other workflow are untouched);
- for `demo-pipeline` (used by `demo.py`), the gate on `deploy-verify` and the scenarios (`review`: rejected once, then succeed); with a pace argument, mock tasks and the changes-required hold last that long (demo.py only);
- the expiry sweep interval (30 s) and park poll (2 s) are shortened so the demo is fast; `demo-expiry` uses a 3 s park poll so the sweep, not the engine, fails the task.

The shipped `agentd` has no such flag and nothing in the daemon changes. Everything else (HTTP API, approval service, sweep, startup hooks, backup) is the production code.

## Secrets

The approver token reaches `agentctl approval-decide` only through the `SYNAPSE_APPROVER_TOKEN` environment of that one child process; it is never printed or logged, and `approver-init`'s one-time stdout (the token) is not retained. The pytest smoke test (`tests/test_demo_mock_smoke.py`) scans every captured CLI output, the timeline, the daemon log, the database and WAL files, every table row and the backup for both token values.

## Limits

State and paths are temporary; the harness does not exercise the supervisor, the desktop console or live providers. Timing waits are bounded (12-20 s) polls, not fixed sleeps.

## Agent runs

`scripts/demo_agent.py` is the sibling for REAL agent runs (see [agent-runs](agent-runs.md)): fake `claude` executable by default; `SYNAPSE_REAL_CLAUDE=1` uses the real binary for a read-only prompt on a throwaway repo (small quota).
