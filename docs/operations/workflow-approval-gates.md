# Workflow approval gates (mock path)

`MockWorkflowEngine.run(run_id, profile_id, scenarios, approval_steps={task_id: ApprovalStepSpec})` gates the named tasks behind a human approval. The engine needs an `approval_requester` (`DaemonApprovalRequester`); without one, `approval_steps` is refused. Logic lives in `orchestration/approval_gates.py` (`ApprovalGateCoordinator`).

## Flow

1. **Request and park.** When a gated task is READY and has no gate, the coordinator requests an approval as `daemon:workflow` (operation, target, candidate SHA-256, policy version from the spec; expiry = now + `ttl`, at most 24 hours). The approval id is derived from the run and task, so a retry reuses the stored request; after a crash between request and park the existing record is reused. Claim, a completed bookkeeping attempt for the wait, and the gate (`RUNNING -> AWAITING_APPROVAL`) commit in one transaction. The provider is not started. Siblings keep running.
2. **Wait.** While the approval is `requested`, each `run` pass leaves the task parked and writes nothing; the run reports `blocked`. A human decides through the existing approver endpoint, CLI or desktop dialog.
3. **Resume.** On a later `run` pass (same process or after restart; pass the same specs again) the coordinator calls `verify_approval_for_execution` with the spec values. Only on success, in one transaction, it resolves the gate with `binding_recheck_passed=True`, reserves a new attempt with `reserve_attempt_after_approval` (requires the `ApprovalGrant` (not forgeable by accident; forgeable by same-user code, see the [security review](../security/review-2026-11-20.md)) and a grant for this task's own consumed gate), and sets it running. The provider then runs.
4. **Denial.** Rejected, expired, revoked and lapsed approvals fail the task through gate resolution (`FAILED` is terminal; the run fails when no other work remains). A binding mismatch (operation, target, digest or policy differs from the spec), malformed record or wrong run on an *approved* approval fails the task too: the approval lifecycle has no approved -> revoked edge, so migration 0013 adds an immutable `task_gate_binding_denials` row that is the only extra evidence the failure trigger accepts. A still-pending approval keeps the task parked.

## Properties

- Transactional: park and resume are each one transaction; the approval request is its own idempotent transaction. Replays of `run` against an undecided or decided approval add no events, approvals or attempts; resolution is compare-and-swap and the gate is one-way consumable.
- No self-grant: the coordinator only holds the requester object, whose authority allows `approval.request` only. Deciding needs a distinct human with the approver token, which the engine never sees, stores or logs.
- The approval wait consumes one attempt (bookkeeping, no provider work), so `max_attempts >= 2` is required and the gated task's scenario tuple is indexed from its first real attempt. A retry of a gated task re-verifies the approval before reserving; if it no longer covers the step, the task is blocked.
- A resumed task whose process dies before its provider finishes is handled by `recover_mock_run` like any running task: unknown outcome, blocked, never silently retried.

## Limitations

- One gate per task lifetime (migration 0012). The engine polls only when `run` is called; there is no expiry sweep or notification here. A request that expired before it could be parked leaves the task READY and `run` raising/ignoring until an operator intervenes (the request id is deterministic, so it is not recreated).
- When driving the engine directly, the spec is supplied by the caller on every pass; a changed spec after restart fails the task closed rather than re-requesting. The daemon path below stores the spec instead.
- No HTTP route or CLI flag supplies approval steps; no real provider, deployment or STAGING binding is involved.

## Daemon-started gated runs

`MockRuntime.start(run_id, key, scenarios=None, approval_steps=None)` (runtime-level only: the HTTP `mock-start` endpoint has no body, so neither scenarios nor approval steps are exposed there or in the CLI) accepts a closed spec `{node: {operation, target, candidate_sha256, policy_version[, ttl_seconds]}}`:

- Validated server-side before any state change (`ScenarioError`): nodes must exist in the run, no unknown fields, `operation`/`target`/`policy_version` are 1-128 characters of `[A-Za-z0-9._:/@+-]`, `candidate_sha256` is 64 lowercase hex, `ttl_seconds` is an integer 1-86400 (default 3600). Approval steps imply the scenario kind; unlisted nodes use `succeed`.
- Recorded immutably in the start transaction as one `mock.approval_steps_configured` event (with a SHA-256 over the canonical form; events are immutable). It is part of the idempotency request, so replays are receipts and a different spec under the same key conflicts. A missing, duplicated or tampered record makes the run not understood (`mock_kind` returns `None`), so every guard refuses it.
- The daemon builds the engine with a `DaemonApprovalRequester` and the stored specs, so nothing is re-supplied after restart or `mock-resume`.

Parking and re-drive:

- When the engine reports `blocked` and the only open work is parked gates (plus tasks waiting on them), with no unresolved attempts, the run stays `running` and the daemon re-checks every 2 seconds. Parked tasks stay parked, writing nothing, until a decision, expiry or cancellation; then the coordinator resumes (fresh binding recheck, new attempt) or fails the task, and the run completes. Any other blocked, ready or in-flight work, or an unresolved attempt, still takes the conservative path: `recover_mock_run` then `recovering`, operator `mock-resume`, after which the stored specs apply.
- On daemon startup such a parked run is not moved to `recovering`; `recover_interrupted` marks it and the daemon re-drives it (`MockRuntime.resume_parked`) once its event loop exists, reloading the specs from the database. At most one run is re-driven; others take the conservative path.
- While parked the run is `running`, so, as for any running mock run, no other mock run can start until it finishes or is cancelled. Cancellation of a parked run cancels the parked task and the run; the approval stays `requested` until it expires.
- The expiry sweep fails a parked task whose approval expires (see `approval-expiry.md`); the re-drive then finishes the run.
