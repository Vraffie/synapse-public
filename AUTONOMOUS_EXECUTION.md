# Autonomous implementation mission — Synapse M1–M3

> This is a durable execution brief for Codex and other coding agents. It does not override `AGENTS.md`, repository protection rules, or human approval requirements.

## Objective

Implement, verify, and document milestones **M1 → M2 → M3** in dependency order, producing a usable, local-first multi-agent orchestration system. Continue selecting bounded, useful work without asking for routine planning approval. Never claim a milestone is complete based only on code presence.

## Mandatory startup procedure (every session)

1. Read `AGENTS.md`, `IMPLEMENTATION_BACKLOG.md`, and `docs/architecture/ARCHITECTURE.md`.
2. Inspect current branches, open and recently closed PRs, working tree, and CI status. In particular, inspect historical M1 PRs before duplicating their work.
3. Run available baseline tests and report their actual outcome. If tests cannot run, record why and do not report them as passing.
4. Compare implemented functionality with acceptance criteria and identify the next eligible incomplete task.
5. Read `AUTONOMOUS_PROGRESS.md` if it exists; reconcile its claims with repository and CI evidence. Create it on the working feature branch if absent.

## Continuous bounded execution loop

Repeat until M1–M3 are accepted or a genuine blocking approval is required:

1. **Select:** choose the highest-priority task with satisfied dependencies. Fix correctness/security/data-integrity defects before adding features.
2. **Scope:** document acceptance criteria, ownership, files affected, architectural constraints, failure modes and test strategy. Keep increments reviewable.
3. **Implement:** use a dedicated nonprotected feature branch and isolated worktree. Avoid unrelated edits and placeholder implementations.
4. **Verify:** run focused tests and the relevant broader suite. Include invalid inputs, rollback, concurrent access, duplicate messages, crash/restart recovery, and compatibility when applicable.
5. **Review:** critically review the diff; obtain an independent reviewer for security-sensitive and deployment-related work. Fix findings.
6. **Record:** update `AUTONOMOUS_PROGRESS.md` with commit/PR links, executed commands and results, blockers, risks, and the next task. Only mark backlog checkboxes complete after demonstrated acceptance.
7. **Continue:** move to the next eligible task rather than stopping after a single commit or milestone. When execution time or quota runs out, leave a precise checkpoint for the next session.

Never invent CI results, runtime verification, or reviewer approval. A remote PR is not evidence that its tests passed.

## M1 — Domain, contracts and persistence

- Typed IDs and domain errors; run/task/message/approval event contracts.
- Transactional, upgrade-safe SQLite migrations and repositories, single authoritative daemon writer.
- Validated state machines with compare-and-swap transitions.
- Idempotency keys, immutable ordered event log, replay cursor, atomic transactional outbox.
- Correct at-least-once dispatch semantics, consumer deduplication contract, retries and crash recovery.
- Tests for schema upgrade and rollback, stale writes, concurrent writers, duplicate delivery, failure injection, and restart invariants.

**Exit gate:** full M1 test suite passes, including concurrency and simulated failure scenarios; documented compatibility and recovery behavior; reviewed diff.

## M2 — Communication and mock orchestration

- Authenticated message routing, scoped inboxes, leases, ACK/NACK, dead-letter behavior.
- Shared context CAS, immutable artifact references and access boundaries.
- Deterministic mock provider and supervised mock workers (no live provider credentials).
- Parallel tasks, review/changes-required loops, bounded retries, cancellation and restart recovery.
- Tests for duplicate delivery, worker crash, lease expiry, lost acknowledgements, replay and idempotency.

**Exit gate:** deterministic end-to-end mock workflow completes, survives a simulated worker crash and restart, and preserves state and message invariants.

## M3 — Local API, CLI and desktop

- Authenticated local FastAPI REST schemas/OpenAPI and WebSocket event replay/reconnection.
- CLI for health, runs, messages and approvals.
- PySide6 desktop for run graph, messages, approvals, state freshness and reconnect.
- Contract, integration, UI smoke and security-boundary tests.

**Exit gate:** operator can start a mock run, observe state, exchange messages and process approvals through supported interfaces; complete relevant tests and documentation.

## Safety and permissions

`AGENTS.md` is authoritative. The user has granted **standing authorization within the M1–M3 mission** to push to nonprotected feature branches, create pull requests, and merge verified M1–M3 PRs into `main`. This is conditional authorization, not permission to bypass GitHub branch protection or quality gates.

**Mandatory merge gate:** before merging, run the relevant focused tests and full available repository suite; verify required CI checks are green; inspect the complete diff for security, compatibility, data loss and migration risk; obtain any required independent reviews; confirm no unresolved blocking findings; and ensure the PR is scoped to M1–M3. If CI is absent/unavailable or any required check is inconclusive, leave the PR open and report the blocker. Do not self-approve a review that policy requires to be independent. Never force-push to `main` or disable protections.

Do not deploy to STAGING or production, alter external systems outside the approved mission, copy credentials, bypass quotas, or execute destructive operations without separate explicit approval. Production deployment is disabled. If one task is blocked, work on another eligible task without crossing the blocked gate.

Do not change architecture silently: record a proposed ADR when deviating from the baseline.

## Checkpoint format

Maintain `AUTONOMOUS_PROGRESS.md` on the active feature branch:

- Current milestone, task, branch/worktree and commit
- Last verified green commands and timestamps; distinguish not-run and failed
- Completed acceptance criteria and supporting evidence
- Open PRs, review findings, risks and blockers
- Exact next action and safe resumption instructions

## Stop conditions

Stop only when all M1–M3 exit gates are demonstrated, when an approval/security boundary blocks all eligible work, or when the execution environment cannot continue. In the latter cases, leave a durable checkpoint and a concise report; do not pretend to be continuously running after the session ends.
