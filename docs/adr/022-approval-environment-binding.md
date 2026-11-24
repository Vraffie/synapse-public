# ADR-022: Approval environment binding (STAGING)

Status: Proposed (library in `src/synapse/persistence/approval_environment.py`; not wired into the daemon; no deployment code).

## Context

ARCHITECTURE.md requires approval for STAGING deployment bound to candidate manifest, target, expiry and audit, but never specifies how an environment is identified. Without an identity in the binding, an approval granted for one environment could be replayed for another.

## Decision

- An environment is an allowlisted opaque identifier (`ALLOWED_ENVIRONMENTS`, currently only `STAGING`). Matching is exact and case-sensitive. Anything else (missing, empty, unknown, wrong type) fails closed.
- An approval is bound at request time (only while `requested` at version 0) in the immutable table `approval_environment_binding`, with `bound_sha256 = sha256(domain || len-prefixed candidate_sha256 || len-prefixed environment)`.
- `verify_approval_for_environment` runs the existing execution-time recheck, then requires a binding, a recomputable bound hash and an environment equal to the one about to be acted on. No binding, a different environment or a tampered hash is denied (`binding_mismatch` / `malformed`). An approval for one environment is never usable for another.
- The side-effect ledger key (ADR-021) gains `environment` (`''` for legacy keys). It is validated against the allowlist when non-empty.
- Existing approvals are unchanged: `candidate_manifest_sha256` is not modified, so legacy hashes and `verify_approval_for_execution` behave exactly as before. A legacy approval simply has no binding and can never pass the environment check.
- The schema is inline (`ensure_schema`), like ADR-021, to avoid a non-contiguous migration; promote both to migrations once the sequence allows.

## Consequences

Callers must use the environment-aware verifier before any environment-specific action; the plain verifier does not check the environment, and `ApprovalGrant` does not carry it. The ledger change alters the ADR-021 primary key; the ledger `ensure_schema` migrates an older ledger in place (see "Enforcement added after review"). Allowlist changes are a code change by design.

## Enforcement added after review

- DB layer: a BEFORE INSERT trigger rejects a binding unless the approval is `requested` at `state_version=0` (plain SQL cannot bind a decided approval). Bindings stay immutable (no update/delete).
- Decision path: `write_approval_transition(..., environment=...)` refuses to approve a bound approval unless `environment` equals the stored one (`StateConflict`); approvals without a binding are unaffected. Wiring MUST pass the environment the approver was shown.
- Misuse guard: `tests/test_approval_environment.py::test_plain_verifier_not_used_outside_allowlist` scans `src` and fails if any module other than the allow-listed ones references `verify_approval_for_execution`. A deploy-class path must call `verify_approval_for_environment`; adding to the allow-list is a reviewed decision.
- Recovery: `recover_side_effects` and `StepKey.from_stored` work from raw rows without checking the current allowlist, so an environment removed from `ALLOWED_ENVIRONMENTS` cannot strand an intent. Caller-supplied keys still go through the validating constructor.
- Ledger upgrade: the ledger `ensure_schema` upgrades a pre-environment ledger (PR #138 shape) in one transaction (rename, copy with `environment=''`, drop; transitions gain the column), preserving rows.

## Wiring preconditions (documented, not enforced)

- The legacy key `''` and `'STAGING'` are different primary keys: a step recorded with `''` does not collide with the same step under `STAGING`. Deploy-class steps MUST therefore require a non-empty environment when wired; the ledger does not enforce it.
- `source` and `actor` in `resolve_unknown` are caller-supplied and unauthenticated provenance. Authenticating who may resolve is the wiring layer's job.
- The inline schemas and a future migration (next free number; 0017 is taken by ADR 024) must not conflict: use IF NOT EXISTS / rename-copy and cover both the fresh and the pre-environment shapes.
