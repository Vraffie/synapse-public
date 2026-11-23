# ADR-012: Block interrupted assigned tasks during mock recovery

Status: Implemented (`ASSIGNED -> BLOCKED` in `src/synapse/domain/workflow.py`, used by `src/synapse/persistence/mock_recovery.py`). Originally proposed 2026-11-20.

## Context

The baseline task lifecycle lists `ASSIGNED → RUNNING` but does not specify a safe outcome when the daemon stops after assignment and before a worker begins. Returning such a task to READY could duplicate work if the worker's state is uncertain.

## Decision

Permit `ASSIGNED → BLOCKED` for conservative recovery. The mock-only recovery repository records this transition and its event atomically after a trusted daemon asserts that no in-process mock workers survive. It does not retry, establish process fencing, or imply that external effects are reconciled. Production and real-provider recovery still require verified worker fencing and a separate reconciliation policy.

## Alternatives and consequences

Leaving the task ASSIGNED indefinitely prevents an operator from seeing the explicit blocker; silently returning it to READY risks duplicate execution. The new edge makes uncertainty observable while preserving the requirement for reconciliation before retry. Callers must not treat the attestation flag as proof of daemon ownership or worker termination.
