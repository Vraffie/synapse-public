# Autonomous progress

Current state, refreshed 2026-11-20 from `git log origin/main`, `gh pr list` and a full test run on origin/main at `e247f08`. Earlier per-slice integration notes (PR #39 integration branch, `/tmp` worktrees, session handoffs) were transient and have been removed; the commit history and merged PRs are the record. Anything not listed here is unverified.

## Test evidence

Full suite on `e247f08`: 1045 passed, 6 skipped, 1 warning in 126.52s, exit code 0. Command: `QT_QPA_PLATFORM=offscreen uv run --extra test --extra desktop --with-editable . python -m pytest -q`. The MCP extra was not requested in this run.

Re-run on `204f99c` (origin/main at this sync, `scripts/check.sh`): 2005 passed, 13 skipped, 2 xfailed, 2 failed in 223.88s. The two failures are `tests/test_api_contract_coverage.py::test_case_table_covers_exactly_the_documented_routes` and `::test_every_documented_status_has_a_case`: the `/api/v1/runs/{run_id}/usage` route has no case in the coverage table (PR #123 adds it).

## Merged on main

PRs #1-#73 are merged or closed (closed unmerged: #7, #14, #15, #18, #22-#24, #29, #31-#37, which were superseded by the integration in PR #39). Main includes:

- M1 persistence: migrations 0001-0014 (`src/synapse/persistence/migrations_sql`), transactional repositories, append-only events, consistent snapshots, approval persistence with requester binding.
- M2 mock orchestration: mock provider and scenarios, supervised in-process executions, daemon-owned mock runtime, approval gates in the mock engine (#54, #61), approval expiry sweep (#51), stale-socket reclaim under the daemon lock (#43, #56).
- M3 clients: local API, `agentctl`, PySide6 desktop with reconnect backoff and an approvals-first UI (#60, #66), human approval decision endpoint, CLI and desktop dialog (#42, #44-#46, #48), denied-decision audit (#59), `GET /api/v1/runs` (#63).
- Agent runs: Claude CLI adapter, disabled by default (#53, #55, #73), `AgentRuntime` (#67), real agent runs through the API, `agentctl` and desktop (#68, #69), per-task git worktree manager (#62) hardened in #71.
- Ops: systemd user service (#57), backup/restore (#58), packaged migrations (#64), end-to-end mock demo and live-daemon smoke test (#65), security review document (#72).

## Also merged since #73

Checked against `gh pr list --state merged`: #74 (desktop Control Center home), #75 (backup schema fingerprint, restore revokes approvals), #76/#89 (socket lock, umask, identity), #77 (full-spec approval binding and spawn-time worktree recheck), #80, #83, #85 (ADR 015), #91 (profile registry, not wired), #92 (usage ledger, migration 0014), #93 (Codex adapter, not wired), #95/#98/#112 (ADRs 016, 017, 019), #97 (candidate manifest), #100 (budget/routing policy, not wired), #102 (Arch package), #105 (agent roles), #106 (output reducer, not wired), #109, #111, #118 and the test PRs #101, #103, #104.

## Open PRs (not merged)

Open at the time of this sync (`gh pr list --state open`): #78 (backlog), #79 (Claude CLI version probe), #81 (agent worktree removal), #82 (abandon recovering agent runs), #84 (`--tools` restriction), #87, #88 (multi-task agent runs), #90, #96, #107, #108, #110, #113, #114, #115, #116, #117, #119, #120, #121, #122 and #123. Their review and CI status was not checked.

## Known open nits (checked against current main code)

- Desktop `closeEvent` (`src/synapse/desktop/main_window.py`) waits up to 11 s for the poller and ignores the close if it does not stop.
- Same-user approver token is a file, not isolation from same-user agent processes.

Items from the earlier list that are no longer true: stale `agentd.sock` blocks restart (reclaim added in #43), event stream pausing 250 ms after each full page (fixed, the loop continues without idling), `run_queries` mapping every `ValueError` to 404 (now only `RunNotFound` maps to 404).

## Remaining acceptance gaps

- M1-M3 exit gates are not demonstrated as complete (see `IMPLEMENTATION_BACKLOG.md`).
- Secure agent session provisioning and worker isolation; live MCP/fallback communication.
- Real-worker process fencing and automatic reconciliation; mock resume is fixed-fixture only and must not be generalised to subprocess or provider effects.
- Genuinely independent reviewer; stronger distinct-human identity.
- Wiring of the Codex adapter and profile registry, budget and routing policy, output reducer (wired since #1) and usage analytics (M4 remainder, M5; the adapter, registry, policy modules and usage ledger are merged but not wired or only partly wired), multi-repo plugin (M6), deployment and release gates (M7).
- No live provider is enabled by default and no deployment has occurred.

## Session 2026-11-23 (A, `claude-personal`)

Baseline: `main` at `3eb4143` (one squashed snapshot, no PRs, no other branches before this session). Full suite run on that commit: 2697 passed, 22 skipped, 0 failed, exit 0, 239 s (`QT_QPA_PLATFORM=offscreen uv run --extra test --extra desktop --with-editable . python -m pytest -q`). Migrations 0001-0016.

PR numbers elsewhere in this file (#1-#162) refer to the earlier private history and do not exist in this repository; treat them as provenance only. Whether each item they describe is present was checked against the code where noted below, not assumed.

- ADR 016 W0 (finished runs do not hold capacity slots) was already in the snapshot (`persistence/task_claim.py`).
- PR #4 (A7b W1): per-task `profile`, claims under the named profile's row, spawn through the registry executable (fails closed, never the default), `agent.profile_selected` before spawn, spawn-time rechecks (`profile_narrowed`, `profile_assignment_mismatch`). Full suite on the branch: 2710 passed, 22 skipped. Independent `security-reviewer` report: no critical findings; its two important findings were fixed with tests.
- PR #6 (A7b W2, stacked on #4): profile list, audited operator cooldowns, `agentctl profiles` and `profile-cooldown`. Full suite on the branch: 2750 passed, 22 skipped. Independent review: no important findings; nits fixed.
- Both PRs were reviewed by session B (#4: no blocking findings), passed CI, and were merged on 2026-11-23 with #10 (MCP IPC size-bound tests).
- Verified on this machine: `bwrap` is installed (`/usr/bin/bwrap`), unprivileged user namespaces work, and the 79 sandbox/bwrap tests run and pass (1 skipped: the opt-in real Codex test). This answers part of ADR 019 open question 7; the polkit, `systemd-run`, uvicorn peer-credential and `python-fido2` items remain unverified.
- Known limits that remain true: the `--agent-config` registry gives every profile the base executable, credentials and `state_dir`, so profiles do not isolate accounts; `per_profile` budgets keyed to the built-in id do not cover tasks naming a registry profile; operator and approver tokens are same-user files (F2), agents still need `--allow-same-user-agents`; the cooldown routes use the shared operator token.
- Session B started and opened #1-#3, #5, #7. A's independent reviews found blocking issues in #1 and #2 (posted on the PRs); neither is merged. Merging #8 was impossible because the `block main` ruleset requires CI checks that docs-only PRs never run, so these status files travel with the `profile_id` change instead.

## Session 2026-11-23, later (A)

- Merged: #1 (M5.3 wiring), #2 (M7.3 ops), #3 (M5.4 manifest record), #7/#15/#19/#21 (M7.4 coverage), #11-#14 (profile_id in run views, F10 counters, peer credentials, same-user gate), #18 (log why a sandboxed spawn failed, sandbox vs native Claude install path), #20 (`agent-worktree-commit`), #22 (`scripts/synapse_up.py`). Full suite on `main` after #22: 2867 passed, 22 skipped.
- First real, non-mock runs: a sandboxed read-only Claude run and a sandboxed edit run (fixed a bug in a throwaway repo; the edit was committed to its own branch with `agent-worktree-commit`, owner `main` untouched). Findings from the live runs: the sandbox refuses the native install path (`~/.local/share`), now documented and handled by `synapse_up.py`; the daemon refuses a worktree root inside the state dir.
- Independent security reviews found and fixed: #20 (a `.gitmodules` `ignore = all` hid a gitlink; truncated listings skipped the check) and the spawn journal (leader-gone group kill without identity check, forgotten unverified entries, silent entry cap, FIFO hang, no self-protection).
- In flight: spawn journal / startup worker fence (ADR 023, branch `a/spawn-journal`; process-group only, see the ADR limits), desktop "Commit changes..." (branch `a/desktop-commit`), B's #5, #16, #17, #23.
- Live check of the spawn journal (2026-11-23, real sandboxed Claude run, bwrap on): started a run, `kill -9` the daemon mid-run. The journal entry (real pid, start ticks, boot id) was present while the run was live. bwrap and the agent died with the daemon (`--die-with-parent`). On restart the fence found the entry provably dead (`verified_dead: 1, killed: 0, unverified: 0, all_clear: true`), wrote `agent.worker_fence` on the run, the journal emptied, and recovery was unchanged (attempt unknown, task blocked, run `recovering`, nothing re-spawned). Not exercised live: a surviving orphan being killed (covered by the fake-agent tests), a descendant that left the group.

