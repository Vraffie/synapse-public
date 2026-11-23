# ADR-025: Independent review enforcement and an evidence verifier

Status: Proposed. Library and engine hooks in `src/synapse/orchestration/review_independence.py`,
`src/synapse/orchestration/evidence_verifier.py` and `AgentWorkflowEngine(review_nodes=..., verify=EvidenceVerifier(...))`.
**Not wired into the daemon**: `AgentRuntime` still builds the engine without `review_nodes` and with
`process_outcome_verifier`, and the stored task-spec format (`persistence/agent_tasks.py`, a closed key set) has no
field for either. No migration.

## Context

The M2/M3 close-out item: "independent review enforcement (reviewer differs from author) and a verifier stronger than
`process_outcome_verifier`". State before this ADR (read, not assumed):

- `agents/roles.py::reviewer_is_independent(author_task_id, reviewer_task_id, reviewer_role)` was a pure rule
  (different task id, read-only role). Its docstring and `docs/operations/agent-roles.md` say the scheduler does
  not call it. A test pins that the role catalog is not imported by the runtime.
- `application/mock_review.py::MockReviewHandoff` checks only that two mock instance ids differ. Its finding carries
  `{task_id, attempt_id, approved}` and no candidate hash.
- `orchestration/agent_engine.py::process_outcome_verifier` approves any well-formed summary whose recorded process
  outcome is `succeeded`. It looks at no diff, path or check. The engine calls `verify(task_id, candidate)` and
  records the verdict with `record_verification`, which already binds the verdict to the candidate artifact's
  SHA-256 and to the task's current completed attempt.
- `task_attempts.session_id` exists but nothing writes it. The Claude adapter runs every attempt as a new process
  with `--no-session-persistence` and no resume. The engine never retries an agent attempt.
- ARCHITECTURE.md: "No worker self-certifies completion". ADR 017/020: budgets never skip a verifier, reviewer or
  gate.

## What "independent" can mean here

Everything runs on one machine as one OS user. Often there is one Claude profile, so author and reviewer are the
same model family behind the same account. Independence can therefore only be **procedural**:

| Property | Enforced |
|---|---|
| Different task (different worktree, prompt and spec) | yes |
| Different attempt (one provider process) | yes |
| Different provider session | yes: attempt = session today (see `AgentIdentity.session_key`); if a session id is ever recorded, equal ids are refused too |
| Fresh context: the reviewer prompt carries only the author's **verified summary artifact**, fenced as untrusted data, plus its SHA-256. It never carries the raw output artifact or a transcript | yes |
| Read-only reviewer (`permission_mode=plan`, tools ⊆ {Read, Glob, Grep}, the core `reviewer` role) | yes |
| Different registry profile (another account, or Codex) | only with `Independence.DISTINCT_PROFILE` |
| Different model, different vendor, different uid, isolation from a hostile author | **no** |

**A same-model reviewer is not model-independent.** It shares the author's blind spots and can be prompt-injected
by the candidate it reads. The verdict format (below) stops injected text from *flipping* a verdict mechanically. It
does not stop a model from being persuaded to approve. Same-uid code can also edit the database (see
`agent-runtime.md`, security limit). This is an accountability control, not a security boundary.

## Decision

### 1. Enforcement (`ReviewGate`, opt-in per workflow node)

`AgentWorkflowEngine(review_nodes={review_task_id: ReviewNode(author_task_id, independence)})`. The default level is
`DISTINCT_SESSION`, the strongest that works with a single Claude profile. `DISTINCT_PROFILE` also requires different
registry profiles. If neither task names a profile (both on the run's single default), it is refused as
`review_no_independent_reviewer`.

Before any spawn (`preflight`), the engine checks the following. Any failure **blocks** the review task with a
distinct reason, and the attempt is recorded as `failed` with no process started:

- the reviewer spec is read-only (`review_reviewer_not_read_only`);
- the author task is `completed` (`review_author_not_completed`) with **exactly one** approved
  `verification_results` row for its latest completed attempt (`review_author_candidate_unavailable` /
  `_ambiguous`). The candidate artifact's hash, provenance and blob bytes must match (`review_author_candidate_mismatch`);
- `independence_problem(author, reviewer, level)`: `review_cross_run`, `review_same_task`, `review_same_attempt`,
  `review_same_session`, `review_same_profile`, `review_no_independent_reviewer`.

The gate keeps a single-use ticket in memory: what this reviewer attempt was shown.

After the reviewer finishes successfully (`evaluate`), the ticket is consumed. Its absence is `review_ticket_missing`,
so an old summary cannot be replayed. The author candidate is re-read. A different hash or attempt is
`review_candidate_stale`. Independence is re-checked. Then the verdict is parsed from the reviewer's **own** summary:

- the output must be complete: not truncated, not clipped, no NUL replacement (`review_output_incomplete`);
- the token `SYNAPSE-REVIEW-VERDICT` must occur **exactly once** in the whole output
  (`review_verdict_missing` / `review_verdict_ambiguous`). It must sit on the last non-empty line, which must
  fully match `SYNAPSE-REVIEW-VERDICT v1 (approve|reject) <64 lowercase hex>` (`review_verdict_invalid`);
- the hash must equal the candidate hash the reviewer was given (`review_candidate_mismatch`).

A candidate that embeds a fake verdict line can only produce an ambiguous output, and so a block. The author cannot
choose the right hash in advance, because the summary hash covers its own attempt id and output.

A valid `approve` (AND the normal process-outcome verifier) completes the review task, recorded with verifier id
`builtin.agent.independent-review.v1`. A valid `reject` leaves it `changes_required`. The author task is already
`completed`, so workflows must make downstream work depend on the **review** node.

Audit trail: `review.refused` and `review.verdict_recorded` events (aggregate = reviewer attempt) plus audit-chain
rows with the same payload. The payloads hold ids, hashes, profile ids, level, reason or verdict: never prompt, output
or path text. One verdict per reviewer attempt; a second is a `StateConflict`. A refusal is written in the same
transaction as the BLOCKED transition. A verdict is written in the same transaction as `record_verification`.

### 2. Evidence verifier (`EvidenceVerifier`, opt-in per node)

`EvidenceVerifier` has the same callable interface as `process_outcome_verifier`. For nodes without an
`EvidencePolicy` it delegates unchanged and reports the fallback verifier id, so default behaviour is unchanged. For
configured nodes it always runs the process-outcome check first, then:

- **candidate hash**: SHA-256 of the bytes must equal exactly one `agent_summary` artifact of the task's current
  completed attempt (`candidate_hash_mismatch`, `candidate_not_current_attempt`);
- **HEAD**: if the spec records `worktree_head`, HEAD must still equal it (`head_moved`: the agent committed or reset);
- **diff**: `git status --porcelain=v1 -z --no-renames --untracked-files=all` through `WorktreeManager`'s hardened
  git (`GitWorktreeEvidence`). Non-empty when `require_changes` (`no_changes`). Every path, tracked or untracked,
  must be a plain relative path inside `allowed_paths`, matched component-wise (`path_outside_allowed`). Changed
  symlinks are refused (`symlink_changed`). Truncated status output raises (`evidence_unavailable`);
- **check command** (optional, run only if everything above passed): `CheckCommand(argv: tuple, timeout_seconds,
  max_output_bytes)`. The argv is a tuple with an absolute normalized executable, never a shell string. It runs host
  side with `shell=False`, stdin closed, a scrubbed environment (fixed `PATH`, `HOME=/nonexistent`, `LC_ALL=C`, git
  global/system config off, no inherited variables, so no OAuth token), cwd = the worktree, and its own process
  group. The group is SIGKILLed on timeout, on output overflow, and after exit (descendants). The leader is observed
  with `WNOWAIT` so its pgid cannot be reused before the kill. Reasons: `check_failed`, `check_timed_out`,
  `check_output_exceeded`, `check_not_started`.

Each evaluation publishes a bounded JSON evidence artifact (reasons, HEAD, up to 50 changed paths, last 4 KiB of
check output). It also writes a `verification.evidence` event with ids, hashes and reason codes only. The
`verification_results.evidence_artifact_id` column is **not** filled: that needs a change to
`persistence/mock_execution_store.py`, which was out of scope.

**Security limit:** a check command such as a test runner executes agent-written code as the daemon's uid, unsandboxed.
Point `argv` at a sandboxing launcher for untrusted worktrees. A pass is evidence, not proof.

## Alternatives considered

- *Wire `reviewer_is_independent` from `agents.roles`*: the runtime is deliberately not coupled to the role catalog
  (pinned by a test), and that rule checks task ids only. Its policy is re-expressed in orchestration instead. The
  candidate fence is a local copy of the same fixed-point idea.
- *Reviewer verdict as JSON (the core reviewer role's output contract)*: JSON quoted inside free text is easy to
  smuggle and hard to make unambiguous. One exact, final, single-occurrence line is mechanically checkable.
  Findings can still be free text above it.
- *A new table for review bindings*: avoided (another change may add migration 0017). Events plus the audit chain
  carry the trail, and the ticket is in memory. Consequence: a daemon restart between preflight and verdict loses
  the ticket. Recovery already blocks in-flight attempts, and a re-run gets a fresh preflight.
- *Block instead of `changes_required` on `reject`*: a well-formed reject is a legitimate outcome, not a fault.

## Consequences and follow-ups

- **Wiring** (needs `persistence/agent_tasks.py` and `daemon/agent_runtime.py`, both outside this change): add
  optional `review_of` / `independence` and `evidence` keys to the stored spec (they change `spec_hash` only when
  present), pass `review_nodes` and an `EvidenceVerifier` from the runtime, and let the planner/API create review
  nodes for every `requires_independent_review` role.
- Write `task_attempts.session_id` if an adapter ever reports a real session id. `DISTINCT_SESSION` then compares real ids.
- Bind the reviewer to the edit diff, not only the summary. For example, include the evidence artifact (diff hash)
  in the reviewed candidate.
- Fill `verification_results.evidence_artifact_id`.
- Run check commands inside the bubblewrap launcher (ADR 019 C2).
