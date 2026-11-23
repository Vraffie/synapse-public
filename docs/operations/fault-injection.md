# Fault-injection harness (M7)

Deterministic fault injection against the mock engine/runtime (ARCHITECTURE.md section 13).
Every scenario ends by running the database invariant checker `tests/invariants.py`.
Synchronisation is by polling state, never by fixed sleeps. No real `claude` or `codex` is run.

The older SIGKILL/disk-full suite is `tests/test_fault_injection.py`, documented in
[fault-injection-tests.md](fault-injection-tests.md). This harness complements it.

## Running

```
uv run --extra test python -m pytest tests/test_invariants.py tests/test_fault_injection_*.py
```

Use `python -m pytest` (not bare `pytest`): `tests/test_fault_injection.py` imports `tests.*`.

## Faults injected

| File | Faults |
|---|---|
| `test_fault_injection_workers.py` | worker killed mid-attempt; crash image at each stage (parametrised); corrupted provider output; disk full on artifact write; crash between blob write and metadata commit; SQLite lock held by a second writer (at start, mid-run, short wait absorbed by busy timeout); resume after a crash |
| `test_fault_injection_approvals.py` | parked approval across a crash and forward-only expiry; decision made while the daemon was down; rejected approval; decision bound to a changed candidate or policy |
| `test_fault_injection_messaging.py` | lease expiry and clock jumps; exhausted deliveries dead-letter; duplicate completion; publisher crash before ACK; duplicate event rows |
| `test_fault_injection_processes.py` | killed lock owner; real daemon SIGKILLed while parked |
| `test_fault_injection_clients.py` | HTTP and WebSocket clients dropped abruptly while a run is parked: no cancellation, run still finishes after approval (invariant 8) |
| `test_invariants.py` | the checker itself: each invariant is shown to fire on a hand-corrupted database |

## Invariants checked (`check_invariants`, one SQLite snapshot)

Schema and immutability triggers present; events/outbox consistency; run, task and attempt
state histories match their events and terminal states; no attempt live in a quiescent database;
deliveries (leases, dead letters); approvals (decision matches binding); verification results;
usage ledger; artifact blobs match recorded hashes. `quiescent=True` additionally asserts nothing
can still be running. Violations are returned, not raised; `assert_invariants` raises.

See [invariant-coverage.md](invariant-coverage.md) for which test asserts each of the ten section 13 invariants.

## Known gaps

Not decidable from the database alone, so covered elsewhere or not at all:
single daemon owner (see `test_daemon_lock.py`, `test_socket_ownership_f11.py`), spawn counts,
approval invalidation at execution time, clock behaviour beyond the injected jumps, and real
provider CLIs (never run). Network and partial-write faults on the Unix socket are not injected.

## Results on the merged branch

All harness tests pass and found no invariant violations. See the PR for full-suite counts and
the unrelated failures observed on main.
