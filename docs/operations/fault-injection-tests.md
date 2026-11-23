# Fault-injection tests

`tests/test_fault_injection.py` (SIGKILL suite; the broader harness is in fault-injection.md) kills or fails the system at precise points and asserts the
recovery invariants (no lost or duplicated side effects, no approval without a matching
decision, runs end recoverable, restart is idempotent). Kill points are real SIGKILLs: a child
process kills itself at the injected point and the test waits on its exit status (no sleeps).
Disk-full is simulated by a connection wrapper raising `SQLITE_FULL` at each write in turn.

Scenarios: kill mid-transaction / after event commit / after publish before ACK; write failure
in task transition, attempt reservation and approval decision; kill during fresh and upgrade
migrations; kill before attempt commit, before provider start and before spawn; kill after an
approval decision commits but before the engine reacts; crash while parked with no or a
mismatched decision; backup during an open write transaction; kill during backup and restore.
