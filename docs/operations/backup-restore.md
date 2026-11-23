# Backup and restore of daemon state

`agentctl` can snapshot and restore the daemon's SQLite database (`synapse.sqlite`). Implementation: `src/synapse/persistence/backup.py`.

## What is and is not backed up

- Included by `backup`: the database only (runs, tasks, events, approvals, messages, workflows, idempotency records, `schema_migrations`). The artifact blobs have their own commands, [below](#artifact-blobs).
- Never included: `operator.token`, `approver.token`, `synapse.sqlite.lock`, `agentd.sock`. A backup file therefore contains no credentials. The `artifacts/` blob directory is not part of the database backup; back it up with the blob commands below, or the restored database will reference blobs that do not exist (raw agent output, candidate diffs, manifests).
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

Limits: the fingerprint is computed from the migrations shipped with the code, so it defends against a tampered or damaged backup, not against someone who can also alter the code or the live database file (see the security review, F1). The daemon runs the same schema check at start and reports a mismatch as a broken audit chain (gated tasks are refused until resolved).

### restore (offline only)

1. Takes the daemon ownership lock (`synapse.sqlite.lock`, the same lock the daemon holds). If it is held, restore refuses and changes nothing.
2. Verifies the backup (including the not-newer-than-code check and the schema fingerprint), copies it into a fresh private temp file via the backup API and re-verifies that copy.
   Then, still in that private copy and before it is published, **every approval that is still `requested` or `approved` is set to `revoked`** (`decision_principal=system:restore`, with an `approval.restore_revoke` audit row each). Reason: restore rewinds `task_approval_gates`, attempts and idempotency rows, so an approval consumed after the snapshot would otherwise reappear approved with an unresolved gate and authorise a second execution. Rejected/expired/revoked approvals are left as they are. Gated tasks are not edited: a task parked in `awaiting_approval` stays parked, and on the next gate pass its revoked approval fails it closed (a new approval must be requested by re-running the work). Approvals revoked this way also stop retries of already-resumed gated steps (`verify_gated` returns `revoked`); that is intentional fail-closed behaviour. The backup file itself is never modified. The copy is re-verified after neutralisation.

   Restore revocations are recorded only as `audit_entries` rows (`approval.restore_revoke`); unlike a normal transition they do not emit an `approval.state_changed` event or outbox row. This is deliberate: restore runs offline against a private copy, and nothing consumes those events to derive approval state (the desktop and API read `approval_requests` directly; the event stream is a log). Event cursors held by clients from before the restore are invalid in any case, since the restored `events` table is the snapshot's. Backups from a schema older than migration 0003 (no `state_version` column) are revoked without a version bump and are upgraded by the daemon on next open.
3. Moves the current `synapse.sqlite` and any `-wal`/`-shm` to `synapse.sqlite.pre-restore-<UTC time>-<random>[-wal|-shm]`. The previous database is kept, never deleted; remove it yourself once satisfied.
4. Publishes the restored file as `synapse.sqlite` (0600).

Stop the daemon first (the lock check is a safety net, not a substitute). On start the daemon applies any newer migrations to an older restored database as usual. Restoring a backup from a newer version of the code is refused; upgrade the code instead.

## Artifact blobs

Blobs are content-addressed files in `STATE_DIR/artifacts/` (`artifact-blobs.md`). A blob is written before the transaction that references it commits, so a database snapshot never references a blob that does not exist yet. That gives the rule: **take the database backup first, then back up the blobs that backup references.** Never derive the blob list from the live database.

```
agentctl --state-dir DIR backup /path/db.sqlite
agentctl --state-dir DIR backup-verify /path/db.sqlite
agentctl --state-dir DIR blobs-backup /path/blobs --db-backup /path/db.sqlite   # new directory, must not exist
agentctl --state-dir DIR blobs-verify /path/blobs --db-backup /path/db.sqlite
# after `restore` (daemon stopped):
agentctl --state-dir DIR blobs-restore /path/blobs --db-backup /path/db.sqlite
```

- `blobs-backup` reads the `artifacts` table of the given backup file (read-only, no symlinks) and copies exactly those blobs through the blob store, re-checking every digest and the recorded size. A missing, corrupt or wrongly sized source blob fails the whole backup and removes the directory it created, naming the first offenders. The new directory is `0700`, files `0600`. It writes `MANIFEST.json` (blob digests and sizes plus the SHA-256 of the database backup file).
- `blobs-verify` refuses a backup whose manifest is missing or malformed, was taken from a different database backup file, lists different blobs than the database backup references, or holds a blob that does not match its digest or size. Extra files in the directory are ignored.
- `blobs-restore` verifies first, then adds the blobs to `STATE_DIR/artifacts/` (which must already exist, mode `0700`). Existing intact blobs are kept; a corrupt blob with the same name is repaired by the blob store, which preserves the damaged copy as `.corrupt-*`. It never deletes anything. Blobs created after the snapshot stay in place, unreferenced and harmless.
- A blob larger than 64 MiB is refused (agent raw output is capped at 4 MiB).
- Blob backups hold agent output and diffs and are as sensitive as the database. Keep the database backup and its blob directory together; the manifest binds them.

Full upgrade rehearsal, including these steps, is `scripts/upgrade-rehearsal.sh` (see [arch-package.md](arch-package.md#upgrade-and-rollback)).

## Limits

- Restore cannot be undone automatically; to roll back, stop the daemon and move the `.pre-restore-*` files back.
- Events, run state and idempotency keys revert to the backup point; work done after it is lost. Any in-flight mock runs are handled by normal startup recovery.
- Locking relies on `flock`, so state must live on a local filesystem.
- A restored database is older than the audit-chain anchors the daemon published, which is indistinguishable from truncation (ADR 018). The daemon will report a broken chain and refuse gated tasks until you start it once with `--accept-chain-reset`, which records a reset anchor. Edited rows are never reset away.
