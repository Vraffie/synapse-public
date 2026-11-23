# Runbook: back up and restore the database plus artifact blobs

Companion to `backup-restore.md` (database commands) and `artifact-blobs.md` (blob store). Every statement below was checked against `src/synapse/persistence/backup.py`, `src/synapse/cli/__main__.py` (`_backup_command`), `src/synapse/infrastructure/artifact_blobs.py` and `src/synapse/daemon/__main__.py`. Items not checked are marked `[UNVERIFIED]`.

## What `agentctl backup` does and does not include today

| Item | In `agentctl backup`? |
| --- | --- |
| `synapse.sqlite` (online, consistent snapshot) | Yes |
| `artifacts/` blobs (SHA-256-named files) | **No.** `backup.py` never reads `artifacts/`; its module docstring says so. |
| `operator.token`, `approver.token` | No, by design |
| Worktrees (agent edits) | No `[UNVERIFIED]`: no code in `backup.py` touches them and they live under the configured `worktree_root`, not the state dir. |

Consequence: `agentctl backup` followed by `agentctl restore` brings the database back **without** any blob. Rows in the `artifacts` table (columns `sha256`, `byte_size`, `storage_path`) then point at blobs that exist only if you kept the old `artifacts/` directory or copied it yourself. There is no `agentctl` command that backs up, verifies or restores blobs, and no blob garbage collection (`artifact-blobs.md`).

## Manual procedure (manual: not automated by Synapse)

Blobs are immutable and content-addressed: a file named by its SHA-256 never changes (on a detected corruption the damaged copy is kept as `.corrupt-<digest>-<id>` and the digest name is replaced with verified bytes). That makes "database first, blobs second" a safe order: every blob a database snapshot references already exists when the blobs are copied, and the copy may also contain newer or orphaned blobs, which is harmless (orphans are possible by design, `artifact-blobs.md`).

### Backup

1. Database first (the daemon may keep running):

   ```sh
   S=~/.local/state/synapse            # your --state-dir
   B=~/synapse-backup-$(date +%F)
   install -d -m 0700 "$B"
   agentctl --state-dir "$S" backup "$B/synapse.sqlite"
   agentctl --state-dir "$S" backup-verify "$B/synapse.sqlite"
   ```

2. Blobs second. Copy regular files and keep them private (the store refuses a root that is not owner-only or a blob that is not an owner-only regular file owned by the daemon user):

   ```sh
   install -d -m 0700 "$B/artifacts"
   rsync -a --chmod=F600,D700 --exclude '.tmp-*' --exclude '.corrupt-*' "$S/artifacts/" "$B/artifacts/"
   ```

   `.tmp-*` files are in-flight or leftover uploads; `.corrupt-*` are quarantined damaged copies. Leaving them out is a choice, not a requirement. `rsync` is not part of Synapse; any byte-preserving copy that keeps private modes works.

3. Verify the blobs. No Synapse command exists; this manual check of the content address is the same test `ArtifactBlobStore.read` applies:

   ```sh
   cd "$B/artifacts"
   for f in [0-9a-f]*; do
     [ "$(sha256sum < "$f" | cut -d' ' -f1)" = "$f" ] || echo "MISMATCH $f"
   done
   ```

4. Optional cross-check: every `sha256` in the `artifacts` table of the backup copy should exist as a file here (`sqlite3 "$B/synapse.sqlite" 'select sha256 from artifacts'`; use the backup copy, never open the live database for writing). `message_artifacts` (migration 0004) may also reference blobs `[UNVERIFIED]`: its blob naming was not checked.
5. Treat `$B` as sensitive: it holds run and message data. It contains no tokens.

### Restore

1. Stop the daemon: `systemctl --user stop synapse-daemon.service`. `restore` refuses if the ownership lock is held.
2. Restore the database: `agentctl --state-dir "$S" restore "$B/synapse.sqlite"`. This verifies the backup, **revokes every approval still `requested` or `approved`**, moves the current database aside as `synapse.sqlite.pre-restore-<UTC>-<hex>` and publishes the restored file (details in `backup-restore.md`). The state dir must have no group/other permission bits (`restore_backup` checks this).
3. Blobs, manual. If `$S/artifacts` still exists and is intact, do nothing: blobs are additive, so it is a superset of what the snapshot needs. If it is lost, copy back with private modes:

   ```sh
   install -d -m 0700 "$S/artifacts"
   rsync -a --chmod=F600,D700 "$B/artifacts/" "$S/artifacts/"
   ```

   The daemon creates `artifacts/` (0700) itself if missing and refuses a directory that is not private.
4. Start the daemon: `systemctl --user start synapse-daemon.service`. Expect the audit-chain refusal described in `backup-restore.md` (Limits); start once with `--accept-chain-reset` to re-baseline anchors (the `agentd` flag was checked; passing it through `agentd-supervisor` or the unit is `[UNVERIFIED]`). `--accept-chain-reset` never clears a schema mismatch (`startup_check` only accepts a reset when no problem starts with `schema`).
5. Tokens: `operator.token` is recreated if absent; run `agentctl --state-dir "$S" approver-init` for a new approver token.
6. Check: `agentctl --state-dir "$S" health`, then spot-check that a run with an artifact still reads. Delete the `.pre-restore-*` files only once satisfied.

## Notes and gaps

- Blob size: the runtimes construct the store with `max_bytes=65536` (`mock_runtime.py`, `agent_runtime.py`), so blobs written there are at most 64 KiB.
- There is no atomic cross-copy of database and blobs. The order above is what makes it safe; reversing it (blobs, then database) can leave database rows whose blob is missing.
- `backup-restore.md` used to say the daemon does not re-check the schema fingerprint at start. It does: `daemon/__main__.py` runs `_check_schema` after migrating and feeds a mismatch into `audit_chain.startup_check`, which then refuses gated tasks. That line has been corrected.
- None of the manual shell above was executed against a real state directory.
