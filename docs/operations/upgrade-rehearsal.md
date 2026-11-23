# Upgrade rehearsal and rollback

Checked against `src/synapse/persistence/migrations.py`, `backup.py`, `infrastructure/daemon_database.py`, `daemon/__main__.py` and `arch-package.md`. Unverified items are marked `[UNVERIFIED]`.

## What the code guarantees

- Migrations are numbered SQL files `NNNN_name.sql`, contiguous from 1, shipped in the package. `apply_migrations` applies all pending ones **in one transaction**; a failure rolls the whole batch back and the version table is untouched.
- Released migrations are append-only by convention: history stores version numbers, not checksums, and "released SQL files must be treated as immutable" (`m1-persistence.md`). An edited old file is not flagged as such; only the schema fingerprint (below) can catch a resulting schema difference.
- `apply_migrations` raises `Database contains unknown migrations` when the database has a version the code lacks, so **an older binary refuses a database from a newer schema**. There are no down-migrations.
- **Schema fingerprint:** `backup.py` applies migrations `1..N` to an in-memory database and compares every table, index, trigger and view (normalised SQL) with the file. `backup-verify` and `restore` refuse a mismatch. At daemon start, after migrating, the same check against the code's newest version runs; a mismatch becomes an audit-chain problem and gated tasks are refused until resolved. `--accept-chain-reset` does not clear schema problems.

## Rehearse on a copy (never on the live state dir)

1. Take and verify a backup of the live database (`state-backup-runbook.md`). It is also your rollback point.
2. Build a scratch state dir from the backup, using the **new** code only:

   ```sh
   R=$(mktemp -d)    # mktemp -d is mode 0700
   agentctl --state-dir "$R" restore ~/synapse-backup-DATE/synapse.sqlite
   ```

   `restore_backup` has a `(none existed)` path for a state dir with no database, so this should publish the copy. `[UNVERIFIED]`: read, not run, that `restore` works end to end on a brand-new empty directory.
3. Run the new daemon against `$R` only, as a separate process with `--state-dir "$R"`, mock provider only (no `--claude-cli-config`, no real provider, no credentials). It applies the pending migrations to the copy. Do not use the packaged unit for this; it targets the real state dir.
4. Verify: the daemon starts; `agentctl --state-dir "$R" health`; stop it; then `agentctl --state-dir "$R" backup "$R/post.sqlite"` and `backup-verify "$R/post.sqlite"`. Success means integrity and foreign-key checks pass, history is contiguous to the new N and the fingerprint matches. Expect an audit-chain report needing `--accept-chain-reset` on the copy (restored database versus anchors); this is normal after a restore.
5. Optionally compare row counts of key tables between the backup and `post.sqlite` with `sqlite3` on the copies; migrations may add tables.
6. Delete `$R`. The real state dir was never opened.

Blobs are not needed to rehearse the schema; copy `artifacts/` into `$R` only to test reads (`state-backup-runbook.md`).

## Real upgrade

Follow `arch-package.md` (Upgrade and rollback): back up and verify, `pacman -U`, `systemctl --user daemon-reload && systemctl --user try-restart synapse-daemon.service`. Migrations apply at that restart and are one-way.

## What rollback means

Rollback is **restore, not downgrade**.

- If the new version never started, or its migration batch failed and rolled back, the database is unchanged: reinstall the previous package and restart.
- If the new version migrated the database, the old code refuses it (unknown migrations). Stop the daemon, reinstall the previous package, `agentctl --state-dir "$S" restore <pre-upgrade backup>`, start. Work done after the backup is lost (events, runs and idempotency keys revert) and restore revokes all pending approvals. Expect to need `--accept-chain-reset` on the first start.
- Rolling forward with a fix is the only alternative to restore; no tool removes a migration. `[UNVERIFIED]` beyond the absence of such a tool in the code read.
- Blobs written after the backup stay in `artifacts/` as unreferenced orphans after a restore; leaving them is safe (no GC exists).
- Keep the `synapse.sqlite.pre-restore-*` file the restore leaves: it is the post-upgrade state if you need to inspect it.
- Do not hand-edit the database or released migration files to "fix" a rollback: that breaks the fingerprint and the daemon will refuse gated work.

## Checklist

- [ ] Backup written and `backup-verify` ok; blobs copied and hashed
- [ ] Rehearsal on a scratch copy passes `backup-verify` after migration
- [ ] Previous package file kept for `pacman -U`
- [ ] Daemon stopped before any restore
