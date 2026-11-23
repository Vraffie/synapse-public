# Backup and restore of daemon state

`agentctl` can snapshot and restore the daemon's SQLite database (`synapse.sqlite`). Implementation: `src/synapse/persistence/backup.py`.

## What is and is not backed up

- Included: the database only (runs, tasks, events, approvals, messages, workflows, idempotency records, `schema_migrations`).
- Never included: `operator.token`, `approver.token`, `synapse.sqlite.lock`, `agentd.sock` and the `artifacts/` blob directory. A backup file therefore contains no credentials. Artifact blobs referenced by the database must be preserved separately if you need them after a restore.
- Tokens are re-provisioned, not restored. The daemon creates a fresh `operator.token` on first start in a state directory that has none; run `agentctl --state-dir DIR approver-init` to issue a new approver token. A restore into an existing state directory leaves its current tokens untouched.
- Backup files contain run and message data and are as sensitive as the database. Store them accordingly.

## Commands

```
agentctl --state-dir DIR backup /path/to/new-backup.sqlite
agentctl --state-dir DIR backup-verify /path/to/new-backup.sqlite
agentctl --state-dir DIR restore /path/to/new-backup.sqlite     # daemon must be stopped
```

`--migrations PATH` (all three) selects the migration directory that defines the schema version this code supports; it defaults to the migrations packaged with Synapse (`synapse/persistence/migrations_sql`).

### backup (safe while the daemon runs)

- Uses SQLite's online backup API on a read-only connection, giving a transaction-consistent snapshot of the WAL database while writers continue. It does not take the daemon lock and does not block the daemon beyond normal SQLite read locking.
- The snapshot is written to a hidden temp file (mode 0600, `O_EXCL`, `O_NOFOLLOW`) in the destination directory, switched to a single-file journal mode, verified (below), fsynced, then published with `link(2)` so an existing name is never replaced. The temp file is always removed.
- Refuses if the destination name exists (including a symlink, even a dangling one), if the destination directory is not owned by the current user or is group/other-writable, or if the file name is outside `A-Za-z0-9._-`.

### backup-verify

Opens the file without following symlinks (must be a regular, user-owned file), then checks `PRAGMA integrity_check`, `PRAGMA foreign_key_check`, that `schema_migrations` exists with a contiguous history `1..N`, and that `N` is not newer than the newest migration known to this code. It then compares the file's schema objects to a **schema fingerprint**: the packaged migrations `1..N` are applied to a scratch in-memory database and every table, index, trigger and view (type, name, owning table, whitespace-normalised SQL) must match exactly. Missing objects (for example dropped immutability triggers), extra objects (planted triggers/views/indexes) and modified definitions are all refused, naming the first offenders. An older backup is compared against the schema of its own version, so it still verifies and is upgraded by normal migration at daemon start. It never modifies the file.

Limits: the fingerprint is computed from the migrations shipped with the code, so it defends against a tampered or damaged backup, not against someone who can also alter the code or the live database file (see the security review, F1). The daemon also runs the same fingerprint check at start (after migrating); a mismatch is reported as an audit-chain problem and gated tasks are refused (`daemon/__main__.py`, `audit_chain.startup_check`). Blobs and the manual backup procedure: `state-backup-runbook.md`.

### restore (offline only)

1. Takes the daemon ownership lock (`synapse.sqlite.lock`, the same lock the daemon holds). If it is held, restore refuses and changes nothing.
2. Verifies the backup (including the not-newer-than-code check and the schema fingerprint), copies it into a fresh private temp file via the backup API and re-verifies that copy.
   Then, still in that private copy and before it is published, **every approval that is still `requested` or `approved` is set to `revoked`** (`decision_principal=system:restore`, with an `approval.restore_revoke` audit row each). Reason: restore rewinds `task_approval_gates`, attempts and idempotency rows, so an approval consumed after the snapshot would otherwise reappear approved with an unresolved gate and authorise a second execution. Rejected/expired/revoked approvals are left as they are. Gated tasks are not edited: a task parked in `awaiting_approval` stays parked, and on the next gate pass its revoked approval fails it closed (a new approval must be requested by re-running the work). Approvals revoked this way also stop retries of already-resumed gated steps (`verify_gated` returns `revoked`); that is intentional fail-closed behaviour. The backup file itself is never modified. The copy is re-verified after neutralisation.

   Restore revocations are recorded only as `audit_entries` rows (`approval.restore_revoke`); unlike a normal transition they do not emit an `approval.state_changed` event or outbox row. This is deliberate: restore runs offline against a private copy, and nothing consumes those events to derive approval state (the desktop and API read `approval_requests` directly; the event stream is a log). Event cursors held by clients from before the restore are invalid in any case, since the restored `events` table is the snapshot's. Backups from a schema older than migration 0003 (no `state_version` column) are revoked without a version bump and are upgraded by the daemon on next open.
3. Moves the current `synapse.sqlite` and any `-wal`/`-shm` to `synapse.sqlite.pre-restore-<UTC time>-<random>[-wal|-shm]`. The previous database is kept, never deleted; remove it yourself once satisfied.
4. Publishes the restored file as `synapse.sqlite` (0600).

Stop the daemon first (the lock check is a safety net, not a substitute). On start the daemon applies any newer migrations to an older restored database as usual. Restoring a backup from a newer version of the code is refused; upgrade the code instead.

## Limits

- Restore cannot be undone automatically; to roll back, stop the daemon and move the `.pre-restore-*` files back.
- Events, run state and idempotency keys revert to the backup point; work done after it is lost. Any in-flight mock runs are handled by normal startup recovery.
- Locking relies on `flock`, so state must live on a local filesystem.
- A restored database is older than the audit-chain anchors the daemon published, which is indistinguishable from truncation (ADR 018). The daemon will report a broken chain and refuse gated tasks until you start it once with `--accept-chain-reset`, which records a reset anchor. Edited rows are never reset away.
