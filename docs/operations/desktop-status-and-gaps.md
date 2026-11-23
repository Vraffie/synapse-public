# Desktop UI status and gaps (audit, 2026-11-21)

Audit of `src/synapse/desktop` at origin/main `7d39ebb`. Docs only; no feature code. Anything not
exercised is marked [UNVERIFIED]. Open desktop PRs by another agent: #115 (roster click cycle),
#117 (feed drop count), #122 (approval card always visible, topology fits), #129 (live-daemon
acceptance tests), #131 (demo launcher). Do not duplicate them.

## How it was checked

- `QT_QPA_PLATFORM=offscreen uv run --extra desktop --extra test python -m pytest -q tests/test_desktop*.py`:
  **138 passed, 0 failed, 0 skipped** (27 s). These are 14 files, including live-daemon, mock start/cancel/resume,
  approval decision and agent-run tests.
- `scripts/render_control_center.py` (fixture data): overview, empty, light PNGs viewed.
- `scripts/demo.py --no-approve-wait --pace 3 --screenshots DIR` (real daemon subprocess, MOCK provider only):
  six window PNGs viewed. Reached the approval gate. Approve/Reject click-through, reconnect after a daemon kill
  and a second run were not exercised [UNVERIFIED]. No real `claude`/`codex` was run, so the real-agent
  screens (agent_form submit, agent_view with a live DAG) are covered by tests only [UNVERIFIED visually].

## What works (and how verified)

| Area | Evidence |
|---|---|
| Control Center renders dark and light, empty and populated | render script PNGs; `img/control-center-*.png` |
| Real daemon end to end on mock provider: connect, run list, topology, phases, approval card with operation/target, status bar (socket, health, API v1, pending approvals) | demo screenshot `img/desktop-live-mock-gate.png`; `test_desktop_live_daemon.py` |
| Approval decision dialog with worktree diff, token entry, idempotency key | `test_desktop_approval_decision.py`, `test_desktop_approval_pages.py` |
| Approvals / Runs / Events / Messages developer tabs with cursor paging, Ctrl+1..4, F5, Ctrl+F, F1 shortcuts table | `shortcuts.py`, `test_desktop_operator_ui.py` |
| Fixed-mock start, cancel and operator resume with explicit evidence confirmation | `test_desktop_mock_*.py`, `desktop.md` |
| Real agent start (`POST /agent-runs`) and cancel (`agent-cancel`) from the form; run detail (`/runs/{id}/agent`): tools, worktree status, bounded diff | `agent_form.py`, `agent_view.py`, `test_desktop_agent_run.py` |
| Reconnect with capped backoff, redaction of tokens, reduced-motion switch (`SYNAPSE_REDUCED_MOTION`) | `connection.py`, `redaction.py`, `theme.py`; offline path in tests only |

## Broken or ugly (screenshots)

1. **Approval card and topology compete for height (also the live demo).** In `img/desktop-live-mock-gate.png` the
   bottom Prompt composer takes roughly 40% of the window; the topology shows a scrollbar and its last node
   (deploy-verify, the gated step) is cut off, and the agent roster and communication panels are below the fold.
   In the fixture overview the approval card text is cut at the panel edge. #122 targets exactly this; re-check after it merges.
2. **Run switcher is the only run navigation and loads 25 runs** (`connection.py`, `/runs?limit=25`); no "load more".
3. **Stat tiles can be misleading in the live demo:** with 5 steps and the run "Running", tiles read "Agents working 0 / 5"
   and "Messages exchanged 0" while a step was due to start. Probably a snapshot-timing effect; [UNVERIFIED] root cause.
4. **Topology text is tiny** (node sublabel ~7 px at default window) in both themes; legend collides with the scrollbar.
   Dependency edge labels/lines cross heavily for 8 nodes (fixture); the 8-step fixture already needs scrolling, and
   `control_center.py` only prints "N more steps are not drawn" beyond a cap, so large DAGs are truncated, not scrollable [verified by code, not drawn].
5. **Empty state shows Approve/Reject buttons** (disabled, "Locked until an agent asks"): acceptable but visually
   suggests an action. "Connecting" pill with an empty run combo has no timeout or error text if the daemon never appears
   (the status-bar string appears only in the main window).
6. Window title area in the demo shows menu bar, a bare "Connected" label and wasted vertical space above the tabs.
7. Light theme is legible; topology edge colors (dashed teal, purple) are low contrast against the white node fill.
   No automated contrast check exists [UNVERIFIED numerically].

## Missing, ranked

Backend routes that exist on main: `agent-runs`, `agent-cancel`, `agent-abandon`, `agent-worktree-remove`, `agent-worktree-commit`, `runs/{id}/usage`,
`runs/{id}/agent`, approvals, events, threads/messages, `mock-*`. The desktop client (`connection.py`) calls none of
`agent-abandon`, `agent-worktree-remove`, `usage` (grep: no match for abandon/cooldown/environment/audit/before_id in `src/synapse/desktop`).

### P0

| # | Gap | Scope (one line) | Files |
|---|---|---|---|
| 1 | Operator abandon of a recovering real agent run | Button + confirm dialog calling `POST /runs/{id}/agent-abandon` (see `ADR 012`, `persistence/agent_abandon.py`); show recovering state | `connection.py`, `agent_view.py`, `control_center.py` |
| 2 | Approval environment (ADR 022, PR #148, open) | Show bound environment (STAGING) in card and decision dialog; block approve on mismatch with clear text. Blocked on #148 landing | `approvals_view.py`, `approval_decision.py`, `narrative.py` |
| 3 | Side-effect ledger `unknown` steps needing reconcile (PR #138, open) | List unknown steps per run and an operator reconcile action; no API route on main yet, so needs backend first [UNVERIFIED API shape] | `connection.py`, new `ledger_view.py`, `agent_view.py` |
| 4 | Layout: gate and topology always visible, composer collapsible | Overlaps #122; do not start before it merges | `control_center.py`, `topology_view.py` |

### P1

| # | Gap | Scope | Files |
|---|---|---|---|
| 5 | Worktree removal after terminal run | Button for `agent-worktree-remove` with confirm and result (done; the matching "Commit changes..." action for `agent-worktree-commit` is also implemented) | `connection.py`, `agent_view.py` |
| 6 | Usage/token ledger | Panel from `GET /runs/{id}/usage` (tokens, cost, per profile); ADR 017/020 budget state | `connection.py`, `agent_view.py`, `narrative.py` |
| 7 | Multi-task DAG of real agent runs | `agent_view` shows one run detail only (single tools/worktree block); per-task status, tools and permission mode per task; form starts a single task | `agent_view.py`, `agent_form.py`, `topology_view.py` |
| 8 | Profile/registry display (ADR 015/016) | Profile column and cooldown/availability; `assigned_profile_id` is fetched (`runs_view.py`) but only shown for mock | `runs_view.py`, `narrative.py` |
| 9 | Repo selector fed by daemon allowlist | Form uses free-text path with locally remembered recents; daemon has `repos` allowlist (up to 32) but no list endpoint on main [UNVERIFIED]; needs health/config field | `agent_form.py`, `prompt_composer.py`, backend |
| 10 | Pagination of runs and events backlog | Load-more for runs (>25), "dropped rows" count belongs to #117 | `connection.py`, `control_center.py`, `runs_view.py` |

### P2

| # | Gap | Scope | Files |
|---|---|---|---|
| 11 | Audit-chain status (PR #119 open) | Status-bar badge "chain ok / broken" once a verify endpoint exists | `status_bar.py`, `connection.py` |
| 12 | Error and loading states | Explicit "daemon unreachable / token missing" empty states instead of silent "Connecting" | `control_center.py`, `status_bar.py` |
| 13 | Accessibility | Reduced motion exists; add accessible names to topology nodes, focus order test, contrast test for both themes, minimum node font size | `topology_view.py`, `theme.py`, tests |
| 14 | Large DAG scaling | Zoom/pan, collapse by phase, cap message instead of truncation | `topology_view.py` |
| 15 | Long text | Truncation/tooltips for long operation/target strings and summaries [UNVERIFIED visually] | `approvals_view.py`, `narrative.py` |

## Conflicts with open PRs

- #122 (`control_center.py`, `topology_view.py`): gaps 4, 14 and layout items 1 and 4 under "ugly". Wait for it.
- #117 and #115 (`control_center.py`, `narrative.py`): any change to the feed or roster handlers in gaps 10, 8.
- #129 (`tests/test_desktop_live_daemon.py`): new live tests for gaps 1, 5, 6 would collide; extend after merge.
- #131 (`scripts/demo*.py`, `docs/operations/demo.md`): none of the gaps touch these.
- Backend-dependent, not desktop PRs: #148 (gap 2), #138 (gap 3), #119 (gap 11).
- Gaps 1, 5, 6, 7, 12, 13, 15 touch `connection.py`, `agent_view.py` or `control_center.py`, so conflicts with #115/#117/#122 are textual; sequence after them.
