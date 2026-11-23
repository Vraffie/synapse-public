# ADR 024: Re-queue agent attempts that were provably never spawned

Status: Accepted (owner decision 2026-11-23). Builds on ADR 023 (spawn journal and worker fencing). Local single machine only.

## Context

After a daemon crash, `AgentRuntime.recover_interrupted` marks every in-flight attempt `unknown`, blocks its task and
moves the run to `recovering`; only `agent-abandon` ends it. That is right for anything that may have run. It is
needlessly harsh for an attempt the engine reserved but never handed to a provider. Such an attempt cannot have
started a process. Until now, though, the database could not tell the two apart: the claim transaction moves the
attempt straight to `running`, long before the spawn. The spawn journal cannot tell them apart either, because its
keys are random and not tied to attempts.

## Decision

1. **A durable spawn mark (migration 0017).** The new append-only table `attempt_spawn_marks(attempt_id, marked_at)`
   is guarded by no-update, no-delete and no-replace triggers. The agent engine commits a row in its own transaction
   (`BEGIN IMMEDIATE`, after the run-still-running check) immediately before `provider.execute`, so before the router
   or provider is asked to start, before the spawn journal `begin` and before any `Popen`. SQLite runs in WAL with
   `synchronous=FULL` (the default, checked), so a committed mark survives a process crash and a power loss. If the mark cannot be written, the provider
   is never called.
2. **What the states mean after a crash:**
   - *Never spawned*: an attempt in `running` (task `running`) or `created` (task `assigned`) with **no** mark.
     Recovery may re-queue it.
   - *May have run*: any attempt with a mark, which covers the window after the mark but before the journal `begin`,
     a begin-only journal entry, a recorded pid, a live process, and a finished process whose result was not yet
     written. Such an attempt stays `unknown`, its task `blocked` and its run `recovering`. It is never re-spawned
     and still needs `agent-abandon`, exactly as before. The database has no `starting` attempt state: the mark
     plays that role without widening the attempt state machine.
   - Migration 0017 marks **every** attempt that exists before it, so a missing mark can only mean "created after
     the upgrade and never handed to a provider". An in-flight attempt from an older daemon is never re-queued.
3. **Re-queue (`persistence/attempt_resume.requeue_never_spawned`), all or nothing per run, in one transaction.**
   Re-queue happens only if all of these hold. Otherwise nothing changes and today's recovery runs:
   - the spawn journal is configured and the startup fence is `all_clear` (an unverified entry from any run is a doubt);
   - the run is `running`, with no `agent.cancellation_requested` or `agent.run_abandoned` event (`agent-abandon`
     only applies to `recovering` runs anyway);
   - every task either holds no in-flight work (`completed`, `created`, `waiting_dependencies`, `ready`,
     `awaiting_approval`, with no unresolved attempt) or holds exactly one never-spawned attempt;
   - for each re-queued task, `attempt_count < AGENT_MAX_ATTEMPTS` (2), the engine's existing budget. A gated task
     already spends one attempt on the approval wait, so in practice **gated tasks are not re-queued**;
   - at most `MAX_RESUMES_PER_RUN` (3) re-queues per run over its whole life.

   Each never-spawned attempt is closed as `cancelled` (its id is never reused), and its task goes `running ->
   retry_wait -> ready` (or `assigned -> ready`). An `agent.attempt_resumed` event (`run_id`, `task_id`,
   `attempt_id`, `principal`, no prompt text) and an outbox row are written. The run is then re-driven once through
   `resume_parked`. The next attempt is a normal new reservation and passes every normal check: the plan and profile
   (registry, enabled, cooldown, limits), the claim with per-profile concurrency and the budget policy, approval
   re-verification, the worktree HEAD recheck, the profile-selection audit and a new mark.
4. **Crash before reserve, or right after a re-queue commits.** A `running` run with no in-flight work at all is
   re-driven the same way, with no event (nothing is re-queued). Repeated restarts are idempotent: the event is
   written once per closed attempt, and a re-queued attempt that crashes again before its own mark is out of budget
   and recovers as before.
5. **Visibility.** `agent-show` (`GET /api/v1/runs/{id}/agent`) adds `attempts_resumed`, a count only.

## Alternatives rejected

- **A `starting` attempt state.** Every `set_attempt` caller, the cancellation and recovery queries and the state
  whitelist would change for the same information. A separate mark row keeps the attempt state machine unchanged.
- **Using the spawn journal alone.** Its keys are not attempts, and its `begin` is written on the provider's worker
  thread, after the point where the attempt has been handed over. It stays the evidence for the fence, and the fence
  must be `all_clear` for any re-queue.
- **Re-queuing per task inside a run that also has may-have-run work.** That run goes to `recovering` anyway, and
  re-driving part of it would race the operator's abandon decision.

## Consequences and limits

- One extra small transaction before each spawn. If it fails (lock timeout), the attempt never reaches the provider
  and ends `unknown` (conservative, as for other pre-spawn errors).
- A pre-spawn interruption that already makes the engine mark the attempt `unknown` (for example a cancellation
  racing the claim) is still treated as unknown. Only attempts left in `running`/`created` are candidates.
- `daemon_epoch` and stale-write rejection are still future work (ADR 023). Re-queue relies on the daemon ownership
  lock: no previous daemon can write after a restart.
- Codex runs are not wired into the daemon (ADR 016). The mark lives in the shared agent engine, so it applies once
  they are.
- Not verified against real `claude`/`codex` binaries: tests use the fake CLI and copied database and journal images
  for each crash point.
