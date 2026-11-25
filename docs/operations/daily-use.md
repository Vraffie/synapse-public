# Daily use (one operator, real Claude runs)

A short working guide for the sole operator. It repeats only what was run against a real daemon and the real
`claude` binary on 2026-11-25 (Claude Code 2.1.332, default profile); anything not run is listed at the end.
Details live in [agent-runs](agent-runs.md), [agentctl](agentctl.md), [install](install.md) and
[backup-restore](backup-restore.md).

## Start and stop

- Start: `python scripts/synapse_up.py up /abs/path/to/repo` (state dir `~/.local/state/synapse`, sandbox on, daemon
  plus desktop; add `--no-desktop` for no console). Stop: `python scripts/synapse_up.py down`.
- With no REPO, `up` registers your default repositories: the absolute paths, one per line (`#` for comments), in
  `~/.config/synapse/default-repos` (or the file named by `SYNAPSE_DEFAULT_REPOS`). The list is yours and is not part of
  this repository; each path must be a git checkout, and nested repositories are refused as usual.
- Check: `agentctl --state-dir ~/.local/state/synapse health` prints `{"status": "ok"}`.
- Keep the state directory path short (about 100 characters or fewer): the daemon's Unix socket `agentd.sock`
  lives in it, and a longer path fails with `AF_UNIX path too long`, visible only in the daemon log.

## A run, start to finish

```sh
agentctl --state-dir D agent-start --repo /abs/repo --base-ref main --prompt-file prompt.txt \
    --tool Read --tool Glob --tool Grep --tool Edit --permission-mode acceptEdits \
    --timeout-seconds 300 --idempotency-key K1 --correlation-id C1      # prints the run id
agentctl --state-dir D agent-show RUN        # state, summary, and the worktree diff
agentctl --state-dir D agent-worktree-commit RUN -m "message"   # commits onto branch synapse/<run>/<task>, never your checkout
agentctl --state-dir D agent-worktree-remove RUN                # or prune old ones, below
```

A small edit run took about 15 seconds. Merging the `synapse/<run>/<task>` branch into your own branch is yours to do
with plain git.

## Stopping a run

- `agentctl agent-cancel RUN`: the run reached `cancelled` about 6 seconds after the request (it can still read
  `running` for part of that time). The worktree is kept.
- `--timeout-seconds N`: a run past its timeout ends `failed` with outcome `timed_out`. No `claude` process remained after
  either.

## Two or three agents at once

The daemon allows one agent run at a time: a second `agent-start` while one is active answers
`409 Another run is active`. Parallelism is inside a run: put independent tasks in one `--tasks-file` (up to 6 tasks;
see agent-runs.md). The built-in profile and the engine both allow 2 at once, so with three independent tasks two start
together and the third starts when one finishes (checked live: two attempts started within a millisecond of each other).
Concurrent separate runs are not built.

## After a crash

If the daemon dies (even by `kill -9`), a `claude` child can keep running until the daemon starts again; the startup
fence then ends it. The run is left `recovering` with its attempt `unknown`, because the daemon never re-spawns work that
may already have run. Then:

1. `agentctl agent-show RUN` and look at the worktree diff.
2. Rerun it as a new run: `agentctl agent-restart RUN --prompt-file prompt.txt` (same repo, base ref, tools, mode and
   timeout; the prompt is stored only as a hash, so you pass it again and it must match).
3. End the old run: `agentctl agent-abandon RUN --expected-run-version N --assert-no-agent-process-remains` (N is the
   run's `state_version` from `agentctl run RUN`). Check first that no `claude` process for it remains.

## Cleaning up

`agentctl agent-worktree-prune --dry-run` lists, and `agentctl agent-worktree-prune` removes, the worktrees of finished
runs untouched for 7 days (`--older-than 12h` or `30d` to change it). It never forces: a tree with uncommitted changes is
kept and reported. Old runs themselves stay in the database (runs are never deleted).

## Backup

`agentctl --state-dir D backup /path/new.sqlite` (safe while the daemon runs), `backup-verify FILE`, and
`restore FILE` with the daemon stopped; restore revokes pending approvals. Tokens are not in a backup. The artifact blob
directory has its own commands (backup-restore.md).

## Approval-gated runs

Add `--approval-operation agent.run --approval-target repo/main` to `agent-start` and the task waits for a human decision
(`agentctl approvals` lists it, `approval-decide` decides it with the approver token in `SYNAPSE_APPROVER_TOKEN`; the
token is issued once by `agentctl approver-init`). Checked live through `up`: the operator token alone cannot decide
(`approval-decide` without the approver token ends in a bare `EOFError`, which is safe but unfriendly), and `--decision
approved` with the approver token released the task and the run succeeded. The choices are `approved` and `rejected`.

## What the sandbox does

`synapse_up.py up` turns the bubblewrap sandbox on (it needs `bwrap` and unprivileged user namespaces, and fails closed
without them). Checked live with a fake agent that detaches a child process into its own session: with the sandbox off
the child survived `agent-cancel`; with the sandbox on it was gone after the cancel. So do not start the daemon by hand
with a config that leaves `"sandbox"` off if you care about that.

## Checked through `up` (2026-11-25)

A small edit run through `synapse_up.py up --tools Read,Glob,Grep,Edit` (sandbox on, Claude Code 2.1.332) started,
edited and succeeded like the unsandboxed runs above. `up` defaults to read-only tools, so pass `--tools` for edits.

## Not covered by this guide

- Codex, and anything about the desktop console beyond its automated tests (a cancelled run showing as stopped is covered
  by `tests/test_desktop_live_daemon.py`, not by a manual session).
- The supported Claude versions are not yet documented: 2.1.329 is the only verified version in the code (a different
  version reports `unverified_version` but runs); 2.1.332 worked in every run above.
- Concurrency across separate runs, and an in-run retry of an `unknown` attempt, are not built.
