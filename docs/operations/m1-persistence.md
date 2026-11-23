# M1 persistence verification

Run `python -m pytest -q` on Python 3.13, as CI does.

- The migration runner takes the SQLite write lock before inspecting version history. All pending DDL, data changes, and version records commit together. Migration 0001 remains byte-for-byte compatible with the original schema.
- Standalone task transitions own their transaction. A daemon command can compose the same write with idempotency by calling `write_task_transition` inside `execute_idempotent`. Command callbacks must propagate errors, leave transaction control to the wrapper, and keep external effects out of the transaction.
- Event payloads are immutable snapshots. Domain consumers see read-only mappings and tuples for JSON objects and arrays.
- Regression coverage exercises database upgrades with existing rows, concurrent startup, concurrent task transitions, and rollback and replay of an idempotent transition.

The migration history currently stores version numbers without checksums. Released SQL files must be treated as immutable. Approval state transitions, daemon wiring, and message delivery leases still need implementation and verification.

## Outbox dispatch

Call `dispatch_batch` from the authoritative daemon's serialized loop with an autocommit writer connection. The dispatcher passes a committed event to the publisher. Callbacks run after the write transaction and should return only after accepting the event durably. They must deduplicate by event ID because a crash after delivery and before acknowledgment can replay it. A handler failure schedules a retry without blocking newer events in the batch. Consumers that need ordered history should use the event replay cursor.

Dispatch tests cover delayed retry, rejection of uncommitted state, and replay of the same event ID after a simulated crash and database reopen. The dispatcher assumes one daemon and one dispatcher instance. Cross-process dispatch claims, a daemon wake loop, and end-to-end subscriber recovery remain open.

## Task-state compatibility

Verification can move a task to retry wait, blocked, or cancelled as specified by the architecture. The existing terminal value `completed` remains the persisted spelling of the architecture's successful task outcome. Any future change to the stored value needs a versioned migration and an event compatibility plan.

The idempotency suite also submits the same command key over two connections and verifies that one caller executes the command and the other replays its response.

## Run transitions

Run commands validate lifecycle transitions in the domain and compare both state and version in SQLite. The daemon can call `write_run_transition` inside `execute_idempotent` to commit the new state, lifecycle event, outbox entry, and replay response together. The repository assumes the caller already authorized the command. The orchestration layer still needs to enforce scheduling, recovery, and approval conditions before requesting a transition.

Idempotent requests require string keys in JSON objects at every nesting level, preventing Python key coercion from creating hash collisions.

Task transition plans validate IDs, versions, and legal transitions at construction, including when constructed directly.

A pending outbox row whose event is missing causes dispatch to fail without acknowledging that row. Check database integrity and restore from a consistent backup before resuming delivery.

Migration 0002 makes event rows append-only with SQLite triggers. Updates, deletes, and replacement inserts are rejected while outbox delivery metadata remains mutable. Existing event rows are retained during upgrade. Removing events for retention or repair requires a future, explicitly designed maintenance procedure.

Event schema versions must be positive integers. Booleans, fractional values and strings are rejected at the domain boundary.

The versioned message envelope validates the supported message types, identifiers, timestamp, JSON payload and artifact references. It freezes payload and reference snapshots. Gateway authentication, recipient authorization, delivery and durable persistence remain to be implemented.

Immutable event payload snapshots can be serialized with `encode_payload`. Run transition persistence writes the event's frozen snapshot, preserving nested JSON values.

Message envelope creation normalizes timezone-aware creation timestamps to UTC with microsecond precision.

The context repository writes task/run revisions under a caller-owned transaction. Version zero creates a document and later versions use compare-and-swap. Project updates remain review-gated. The caller must authorize the principal and scope before invoking it. Context update events contain the content, so event consumers must enforce the same scope access before this repository is exposed through an API.

The idempotent context command binds scope, scope identifier, key, expected version and content to the idempotency key. The resulting event uses the context document ID as its aggregate ID.

`read_run_snapshot` reads run state, task state, dependencies and the global event high-water mark in one SQLite read transaction. Clients can replay events after that cursor to observe later commits. The caller must authorize access to the run before returning the snapshot. A missing or cross-run dependency causes an error instead of exposing a foreign task identifier. The operator API exposes this snapshot and authenticated event replay; agent-scoped authorization remains unfinished.

The Linux daemon lock uses advisory flock on a private, user-owned regular file in a private directory. All daemon instances for a database must use the same lock path; never replace the lock file while held. The lock coordinates cooperating local processes on filesystems supporting flock. Forked children may retain the descriptor until exec or exit. The daemon acquires it before opening or migrating SQLite and retains it until writes stop.

`daemon_database` derives a lock path beside the database, acquires it before opening or migrating SQLite, and closes the connection before release. It rejects symlinks, non-private files and hard-linked database aliases. SQLite reopens the database by path, so the private directory also protects it and its WAL sidecars. The daemon uses this lifecycle; real-worker fencing and reconciliation remain separate work.

Migration scripts may run SQLite's read-only `quick_check`, which SQLite also invokes internally when adding a checked column. Other PRAGMAs remain denied during migrations; a failed migration rolls back its pending changes and version record.

Task assignment must use `claim_task` inside a daemon-owned write transaction. The generic task transition repository rejects READY to ASSIGNED so callers cannot bypass the claim's dependency, run-state, unresolved-attempt, profile and capacity checks. This is an application-level invariant for cooperating daemon code; direct SQL writes are outside the repository boundary. Authorization, profile health, resource locks and budgets remain caller obligations.

The generic assigned-to-running task transition requires the run still be running and exactly one unresolved attempt: a newly reserved `created` attempt matching the task's current attempt number and assigned profile. Missing, terminal, unknown, stale or inconsistent attempts fail without a task event. This check records no process launch and does not fence workers; a future execution service must supervise startup and reconcile interrupted external effects before retry. The caller must run reservation and state changes through daemon-owned transactions.

The migration runner requires all discovered files to form a contiguous numbered sequence beginning at 0001 before opening its migration transaction. Missing or out-of-order files fail closed without changing database state; restore the full trusted migration set before retrying. Databases previously initialized with a missing migration remain an operator reconciliation case: do not insert history records or silently skip versions.
