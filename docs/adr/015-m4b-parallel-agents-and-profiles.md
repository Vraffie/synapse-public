# ADR 015: M4-B parallel agents, profiles and the Codex adapter

Status: proposed (design only; no code in this change). Date: 2026-11-20.

## Findings that reshape the plan

1. **Intra-run parallelism already exists in the engine.** `AgentWorkflowEngine` (via `MockWorkflowEngine`) claims tasks
   up to `max_concurrency` (default 2, at most 6; the `profiles.max_concurrency` row is enforced by `claim_task`), honours
   task dependencies and approval gates, and supervises each provider stream. What is missing is the *surface*:
   `POST /api/v1/agent-runs` builds a one-node workflow (`NODE = "agent"`), so users can never ask for more than one task.
2. **The "one run at a time" limit is a cross-run limit**: `AgentRuntime` holds a single `_active` task, `_busy()` refuses a
   second start, and an agent run excludes any running mock run. Lifting it touches cancellation, recovery and
   restart-time redrive, which all assume one active run. It is the riskiest change and the least valuable one.
3. **Codex is installed** (`codex-cli 0.170.0`). `codex exec` has what an adapter needs: prompt on stdin (`-` / piped),
   `--json` (JSONL events), `-s read-only|workspace-write` (sandbox), `-C <dir>`, `--ephemeral`, `--ignore-user-config`,
   `--ignore-rules`, `--skip-git-repo-check`, `-o <file>` (last message), `--output-schema`. Its event schema, exit codes,
   usage reporting and credential handling are **unverified**; nothing in this ADR may claim them until probed and tested
   against the real binary, as was done for Claude in `claude-cli-provider.md`.
4. **`--allowedTools` is not a restriction** (found by the real-binary tests, fixed in #84). Any new adapter must be tested
   for "a tool outside the allowlist cannot act", against the real binary, before it is enabled.

## Decision

Deliver parallelism where it is cheap and safe first; defer cross-run concurrency.

### B1. Multi-task runs through the API (first)
- `POST /api/v1/agent-runs` accepts either today's single-task body (unchanged, backward compatible) or
  `tasks: [{id, prompt, allowed_tools, permission_mode, timeout_seconds, depends_on: [id], approval_step?}]`, at most 6
  tasks, acyclic, ids `[A-Za-z0-9._-]{1,64}`.
- One worktree per task (`WorktreeManager.create(run_id, task_id, ...)`), all from the same validated `base_ref`; the
  per-task specs are stored immutably exactly as today (one hashed `agent.tasks_configured` event, prompts only in private
  blobs). `depends_on` becomes workflow edges; a dependent task starts only after its prerequisites complete.
- **Stacked work is explicitly out of scope**: a dependent task does not inherit its prerequisite's changes. Each worktree
  starts from `base_ref`. Merging task results is the M6 integration barrier's job, not B1's.
- Cancel, abandon, worktree removal and the desktop view are generalised from "the task" to "each task". Failure policy:
  a failed or unknown task blocks its dependents; the run goes to `recovering` on `unknown` exactly as today.
- Exit: three real tasks (two independent, one dependent) run in parallel on the real binary, a cancel stops all of them
  with no child left, and a crash mid-run leaves every in-flight attempt `unknown` with nothing re-spawned.

### B2. Usage provenance
- Append-only `usage_records` (M5.1 shape: run, task, attempt, profile, model, tokens incl. cache read/write, source,
  confidence). Ingest the Claude `result` usage and `total_cost_usd` already emitted. Missing usage is `unknown`, never 0.
  Per-profile aggregates feed the profile caps below. (Shared with M5.1: build once.)

### B3. Profiles, caps and fallback
- Replace the single operator profile with a small registry (2–3 Claude profiles, 1 Codex profile): `id`, provider,
  executable, `allowed_tools` ceiling, `max_concurrency`, `max_timeout_seconds`, optional model preference, enabled flag.
  Credentials stay out of the registry (scratch `HOME` plus the user's own `claude setup-token` / login, as today).
- A task names a profile (default: the primary). Concurrency is capped per profile *and* globally (<= 6).
- **Fallback is explicit and conservative**: only before any process has started (spawn refused, profile disabled, profile
  at cap) a task may move to the next profile in a fixed operator-configured order. After a process may exist (`unknown`)
  nothing moves or retries. Rate-limit or quota responses put a profile into a visible cooldown and fail the attempt;
  they are never worked around, rotated or retried to evade a limit.
- Quota stays `unknown` unless the CLI exposes a reliable authorised source.

### B4. Codex adapter
- A `CodexCliProvider` implementing the existing `ProviderAdapter`, reusing the Claude adapter's hardening (scrubbed env,
  scratch home, stdin prompt, process group, bounded output, rlimits, argv allowlist, worktree validation).
- Required before enabling, all against the real binary: version probe + verified-version list; event schema fixtures;
  a `read-only` run succeeds and cannot write; a `workspace-write` run changes only its worktree; cancel and timeout leave
  no children; repo-controlled config/hooks are not honoured (`--ignore-user-config`, `--ignore-rules`, no project config);
  credentials are not copied into logs, events or artifacts. Anything not demonstrated stays documented as unverified.

### B5. Cross-run concurrency (last, possibly never)
- Only if B1–B3 show a real need. Requires a per-run registry in `AgentRuntime` (active tasks, cancelling, redrive), a
  global cap, and a rewrite of recovery/redrive; the mock/agent exclusion must be re-examined. Needs its own ADR.

## Consequences
- Parallel real agents arrive with B1 without touching the runtime's single-run invariants.
- B2 doubles as M5.1; B3 doubles as M4 profile routing; neither blocks B1.
- The Codex adapter is gated on real-binary evidence, not on the schema being plausible.

## Non-goals
Agent-to-agent messaging, review enforcement and session credentials (M2/M3 close-out), model/effort routing (M5.5),
stacked or merged task results (M6), and remote execution.
