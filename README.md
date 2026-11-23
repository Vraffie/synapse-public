# Agent Control Center

Local-first, general-purpose AI agent orchestration platform under active development. A local daemon, operator CLI and desktop shell are available alongside persistence and deterministic mock components. The daemon can execute and explicitly reconcile its fixed synthetic mock fixture, and can start opt-in real Claude CLI agent runs in git worktrees (disabled by default). Human approval decisions are available through the CLI and desktop with a separate approver token. Live agent messaging and real-provider recovery are not yet integrated; M1–M3 are incomplete.

## Architecture

- [Implementation architecture](docs/architecture/ARCHITECTURE.md)
- [Interactive architecture companion](docs/architecture/ARCHITECTURE_COMPANION.html) (open locally in a browser; simulated interactions only)
- [Implementation backlog](IMPLEMENTATION_BACKLOG.md)
- [Agent contribution instructions](AGENTS.md)
- [Operator API reference](docs/api/reference.md) (generated; regenerate with `scripts/gen_api_reference.py`)

## Scope

Python modular monolith, local FastAPI daemon, PySide6 desktop, CLI, MCP gateway, SQLite WAL, durable agent communication, 2–3 Claude Code subscription CLI profiles, 1 Codex CLI profile, up to 6 agent tasks, token optimization, Git worktrees, Domain plugin, and approval-gated deployment. Details and restrictions are in the architecture document.

## Quickstart

Requires Python 3.13 or newer and [uv](https://docs.astral.sh/uv/).

**Install and run the demo** (from a fresh checkout; no Claude quota, no real provider):

```
uv run --extra desktop --with-editable . python scripts/demo.py
```

(`scripts/demo.sh` is a thin wrapper for the same command.) The script creates a temporary private (0700) state directory, starts a real `agentd` daemon on the built-in MOCK provider, starts a five-step workflow (plan, then edit-config and update-docs in parallel, then review, then deploy-verify) and opens the desktop Control Center on the live daemon.

**What you will see:** the two middle steps running side by side; the review step sent back once ("changes needed") and passing on retry; then the run parked at "awaiting approval" with the Approve button live. Click Approve (the approver token is on your clipboard, paste it into the dialog; it is never printed), or run the `agentctl` command the script prints. The run then completes, and the script stops the daemon and deletes the temporary directory, also on Ctrl+C.

Flags: `--headless` (no window; approves through `agentctl` and prints the run states, for CI), `--keep-state`, `--no-approve-wait`, `--no-clipboard`, `--linger SECONDS`, `--pace SECONDS`.

**What is mock and what is real:** the daemon, HTTP API, database, workflow engine, approval gate, token separation and the desktop are the production code. The task work is synthetic mock output, and the only demo-specific code is in the demo script's own daemon child process (it attaches the approval gate and slows tasks down so you can watch them); the shipped `agentd` is unchanged. Real Claude runs are opt-in (`scripts/demo_agent.py`, needs the explicit `--allow-same-user-agents` acknowledgement once PR #116 merges) and consume subscription quota.

**Run the tests:**

```
QT_QPA_PLATFORM=offscreen uv run --extra test --extra desktop --with-editable . python -m pytest -q
```

To run the pieces by hand, see [install](docs/operations/install.md), [local API operations](docs/operations/local-api.md), [CLI](docs/operations/agentctl.md), [desktop](docs/operations/desktop.md) and [the demo](docs/operations/demo.md). The daemon must not be exposed to agents; its operator token does not establish human identity. See [progress](AUTONOMOUS_PROGRESS.md) for current validation and limitations. Do not enable real provider execution or deployment based on these partial components.

## Status and licensing

Design baseline dated 2026-11-19. Licensed under the MIT License (see `LICENSE`).