# Agent runtime (real Claude CLI tasks, runtime-level)

> **Security limit (read first).** Against code running as the same OS user as the daemon (including any agent child), approvals are **not** a security boundary: that code can read `approver.token` and `operator.token` and write the SQLite file directly, so it can forge or delete approvals and audit rows. The checks described here catch mistakes and honest components, not a hostile same-user process. See the [security review](../security/review-2026-11-20.md) (F1, F2). The daemon refuses to enable agent runs unless the operator passes `--allow-same-user-agents`.

`synapse.daemon.agent_runtime.AgentRuntime` runs a queued run's tasks with the
real `ClaudeCliProvider` through the same claim / attempt / approval-gate
machinery as the mock path. Operators reach it through the HTTP/CLI surface described in [agent-runs](agent-runs.md); a caller in
the daemon process invokes `start(run_id, key, task_specs)`, `cancel(run_id,
key)`, and the daemon calls `recover_interrupted()` / `resume_parked()`.

## Operator enablement

`agentd --claude-cli-config <path>` reads a JSON file at startup (before any
state is touched; invalid config aborts startup):

```json
{"executable": "/usr/bin/claude", "allowed_tools": ["Read", "Edit", "Bash(git status)"],
 "enabled": true, "max_timeout_seconds": 1800}
```

Exactly `executable`, `allowed_tools`, `enabled` (a real boolean) plus optional
`max_timeout_seconds` (default 3600, at most 86400) and the `repos` / `worktree_root` pair (see [agent-runs](agent-runs.md)). Unknown keys, duplicate keys,
non-finite numbers, symlinked / non-regular / foreign-owned / group- or
world-writable files and files over 64 KiB are refused. `state_dir` is derived
from the daemon's state directory, never configured. API keys, extra CLI args and
`bypassPermissions` are not configurable here. Without the flag, or with
`enabled: false`, `AgentRuntime.start` raises `AgentDisabled`.

## Task specs

`start` takes `{node: spec}` that must name exactly the run's nodes:

| field | rule |
| --- | --- |
| `prompt` | non-empty, no NUL, at most 32 KiB |
| `worktree` | absolute path accepted by the adapter's `validate_worktree`; distinct per task; must not overlap the state dir |
| `allowed_tools` | non-empty subset of the operator allowlist |
| `permission_mode` | required; `acceptEdits`, `plan`, `dontAsk` or `manual`. `bypassPermissions` and `auto` are always refused |
| `timeout_seconds` | `0 < t <=` min(operator max, 3600) |
| `approval_step` | optional `{operation, target, policy_version[, ttl_seconds]}`; the approval is bound to the full-spec hash (see Approval gates; `candidate_sha256` is derived, not accepted) |
| `repo`, `base_ref`, `worktree_head` | optional identity of the worktree (absolute repo path, base ref, 40/64-hex HEAD recorded when it was created); the API always sets them. Bound by the spec hash; `worktree_head` also arms the spawn-time recheck |

`AgentSpecError` (a `ValueError`) is raised before any state change. Valid specs
are stored immutably in the start transaction as one hashed
`agent.tasks_configured` event (like mock scenarios); the prompt text is kept
only in a private artifact blob (event: SHA-256, byte length, artifact id).
`start` is idempotent: replays are receipts, a different spec under the same key
conflicts. The recorded specs are re-read after restart, so nothing is
re-supplied.

## Execution and outcome mapping

`AgentWorkflowEngine` subclasses `MockWorkflowEngine` (claim, attempts, gates,
run completion are shared) and replaces only the provider step. A narrowed
tool list / permission mode is applied by building a `ClaudeCliProvider` per
combination with `dataclasses.replace` on the operator profile (re-validated).

| provider terminal event | attempt | task |
| --- | --- | --- |
| `succeeded` | completed | verifying, then completed if the verifier approves (else changes_required, retained) |
| `failed`, `timed_out` | failed | failed (**no automatic retry**) |
| `cancelled` | cancelled | cancelled |
| `unknown`, or any error after a process may exist | unknown | blocked; **never re-spawned**, the run goes to `recovering` for an operator |
| provider `start` rejects (no process ever spawned) | failed | failed |

Per attempt the full assistant text (one output event per line, capped at 4 MiB) is published as a
`text/plain` artifact with provenance `kind=agent_raw_output`, then a bounded summary is published as an
`application/json` artifact: `output_text` is the deterministic [output reduction](output-reducer.md) of the raw
text (reduced to about 16 KiB; the reducer keeps every distinct failure line even past its budget, so the forwarded
text is **hard-clipped at 32 KiB** with a marker and `clipped: true`; the rest is in the raw artifact), `raw_sha256` and `raw_artifact_id` name the raw artifact
so the unreduced output stays recoverable, `raw_complete` is false when the 4 MiB cap cut it, `reduction` holds the
rules applied, dropped line count and kept-failure count, plus at most 100 tool names and never tool inputs. Reduction
only changes what is forwarded; the outcome and the verifier do not depend on it. If the raw artifact cannot be stored,
a succeeded attempt fails closed (the task is blocked for an operator, nothing re-spawns); a failed attempt still
records its summary with `raw_artifact_id` null and `raw_complete` false. The
raw artifact holds the provider's text **unredacted** (up to 4 MiB, the same private blob directory as other
artifacts, mode 0600). Approvers are not shown it: the approval reader filters on `kind='agent_summary'`. Anything
that can read `<state>/artifacts` or list artifacts by kind can read it, so treat it like the transcript it is. A NUL
byte in the output would make the reducer replace the whole text with "[binary output omitted]" and hide failure lines;
the engine reduces a copy with NUL replaced by U+FFFD (`nul_replaced: true`), while the raw artifact and `raw_sha256`
keep the real bytes. Output past the 4 MiB cap is stopped by the provider (`output_limit`, a failed attempt) and what
was captured is stored; `raw_complete` is false only if the engine itself had to cut the capture. The
provider's usage observation is recorded as an `agent.usage_observed` event and as an append-only
`usage_observations` ledger row, readable with `agentctl usage RUN` (see [usage-ledger](usage-ledger.md); missing
usage records nothing and is never shown as zero).

The default verifier (`process_outcome_verifier`) approves only a well-formed
summary whose recorded outcome is `succeeded`. It checks the engine-observed
process result, **not the quality of the work**; inject a stronger `verify`
callable for anything more.

## Approval gates

A gated task is parked (`awaiting_approval`) before any process exists, exactly
as in `workflow-approval-gates.md`. The provider starts only after the approval
is decided `approved` and the binding re-check passes. Rejection, expiry,
revocation or a binding mismatch fails the task and the process is never
spawned. A run with only parked gates stays `running` and is re-driven after
restart from the stored specs.

**Full-spec binding (F6).** `candidate_sha256` of the approval is `spec_hash(spec)`
(`persistence/agent_tasks.py`): SHA-256 of the canonical JSON of the prompt digest, repo, base_ref,
worktree path, worktree HEAD, sorted allowed_tools, permission_mode and timeout (version 1). It is
recomputed from the stored spec when the run is planned (and checked on load), so changing any of
these after the approval was requested makes the execution-time binding recheck fail closed
(`binding_mismatch`: task failed, no process); changing a field without recomputing the digest makes the
stored record untrustworthy and the run is routed to recovery. Operation/target/ttl are bound by the
approval row itself.

**Spawn-time worktree recheck.** `AgentRuntime(worktree_check=...)` is a blocking
`(run_id, task_id) -> (HEAD, clean)` (the daemon wires `WorktreeManager.status`). The API records HEAD
and requires a clean tree when it creates the worktree (immediately before the approval request is
made). Just before spawning, a spec with `worktree_head` is rechecked; if HEAD differs, the tree is
dirty (including untracked files), the check errors or no checker is configured, the attempt is
recorded `failed` with detail `worktree_head_changed_since_approval`, `worktree_dirty_since_approval`,
`worktree_check_failed` or `worktree_check_unavailable`, the run fails and nothing is spawned. The
detail is visible in the task result of `GET /api/v1/runs/{id}/agent`.

**Approval policy.** The optional operator config keys `require_approval` (bool),
`require_approval_for_modes` (list of permission modes) and `require_approval_for_tools` (subset of
`allowed_tools`) form `ApprovalPolicy` (default: nothing required). `AgentRuntime.start` raises
`AgentSpecError` for a task that matches the policy but has no `approval_step`; the API refuses earlier
(422). A run needs approval when `require_approval` is true, its mode is listed, or it requests a listed tool.

## Cancellation

`cancel` persists an `agent.cancellation_requested` event, halts scheduling, and
calls the provider's cancel for each active task; the adapter terminates the
process group (TERM, bounded grace, KILL) and reports `cancelled`. The run
becomes `cancelled` only when no attempt is unresolved. Unlike mock
cancellation, an `unknown` attempt is **not** discarded: such a run goes to
`recovering` instead.

## Restart

`recover_interrupted()` (after the daemon lock is held) marks every in-flight
attempt `unknown`, blocks its task and moves the run to `recovering` via the
shared `recover_mock_run(provider_kind="claude")`. Nothing that may have run is re-spawned.

### Never-spawned attempts are re-queued (ADR 024)

Right before handing an attempt to the provider, the engine commits an `attempt_spawn_marks` row (migration 0017).
An attempt with no mark was provably never spawned. At startup, a `running` run whose only in-flight work is such
attempts is re-driven instead of recovered: each attempt is closed `cancelled`, its task goes back to `ready`, an
`agent.attempt_resumed` event (ids only) is written, and the next attempt is a new reservation that passes every
normal check (profile, cooldown, per-profile concurrency, budget, approval, worktree HEAD). This needs the spawn
journal and an `all_clear` fence, no cancellation request, at most 3 re-queues per run, and room in the task's
attempt budget (2, so a gated task, which spends one attempt on its approval, is never re-queued). Anything with a
mark (mark written, journal `begin`, pid recorded, mid-run, finished but not yet written) recovers exactly as
above. A `running` run with no in-flight work at all (a crash before any claim) is re-driven the same way, with no
event. `agent-show` reports the count as `attempts_resumed`.

### Startup worker fence (ADR 023)

Every real spawn is journaled under `<state>/spawn-journal` (intent before `Popen`, pid, start ticks and boot id right
after, removed once reaped unless the stop status was `unknown`). The Claude and Codex providers journal identically; the
Codex provider is not wired into the daemon yet, so daemon runs today are Claude spawns. Before recovery, startup fences each leftover entry:
provably dead (other boot, no such pid and empty group, zombies only) or its whole process group is killed and checked
empty. Anything it cannot prove (begin-only entry, malformed file, reused pid with a live group, a group that will not
die) is `unverified` and is never signalled when ambiguous. Recovery then proceeds as above.

Each run with an unresolved attempt gets an `agent.worker_fence` event with `verified_dead`, `killed`, `unverified`
and `all_clear` (true when `unverified` is 0). The counts are global, not per run. `unverified` entries are kept as
`*.unverified` in the journal directory for inspection and are safe to delete once you have checked for stray
processes. Linux `/proc` only; with the sandbox off an agent can edit these files, so this is a crash safety net.

## Design notes and caveats

- Exclusion: an agent run owns a `mock_run_config` row (profile
  `synapse.builtin.agent.claude.v1`, provider `claude`), so a mock run and an
  agent run cannot be running at once. The `fixture` column (`fixed-v1`, CHECK
  constrained) is only a legacy constant. `mock_kind` returns `None` for these runs, so mock cancel,
  resume and recovery refuse them; `MockRuntime.recover_interrupted` skips them. A dedicated table
  would be cleaner but needs a migration.
- A child is started in its own session and **can outlive a daemon crash**. After a
  restart the attempt is `unknown` precisely because that process may still be
  running and editing the worktree; the operator must check before doing anything.
- There is no operator *resume* of `recovering` agent runs (`mock-resume` is fixed-fixture only); only
  provably never-spawned work is re-queued automatically, before a run ever reaches `recovering`. The only
  exit is abandonment, which fails the run; see [agent-runs](agent-runs.md#abandoning-a-recovering-run).
- The task's own prompt and transcript may contain sensitive text; summaries are
  artifacts, not events. The prompt text is never logged.
- Not verified against the real `claude` binary in this change: tests use a fake
  executable only. See `claude-cli-provider.md` for what was verified earlier.
