#!/usr/bin/env bash
# Rehearse an upgrade on a COPY of the state: back up, verify, restore into a scratch
# state directory, then apply this code's migrations to the copy and check integrity.
# The real state directory is only read (online backup); nothing is stopped or changed.
#
#   scripts/upgrade-rehearsal.sh STATE_DIR [WORK_DIR]
#
# WORK_DIR must not exist (default: a new mktemp -d). It keeps the verified backup,
# which is also your rollback point; the scratch copy is removed on success.
# Run it with the NEW code installed (the code whose migrations you want to rehearse).
set -euo pipefail

AGENTCTL=${AGENTCTL:-agentctl}
PYTHON=${PYTHON:-python3}
state=${1:?usage: upgrade-rehearsal.sh STATE_DIR [WORK_DIR]}
[ -d "$state" ] || { echo "no such state directory: $state" >&2; exit 2; }
umask 077
if [ $# -ge 2 ]; then
  work=$2
  [ ! -e "$work" ] || { echo "work directory already exists: $work" >&2; exit 2; }
  mkdir -m 0700 "$work"
else
  work=$(mktemp -d)
fi

ctl() { "$AGENTCTL" "$@"; }

echo "== 1/4 back up the live state (database, then the blobs that backup references)"
ctl --state-dir "$state" backup "$work/db.sqlite"
ctl --state-dir "$state" backup-verify "$work/db.sqlite"
ctl --state-dir "$state" blobs-backup "$work/blobs" --db-backup "$work/db.sqlite"
ctl --state-dir "$state" blobs-verify "$work/blobs" --db-backup "$work/db.sqlite"

echo "== 2/4 restore into a scratch state directory"
scratch="$work/scratch"
mkdir -m 0700 "$scratch" "$scratch/artifacts"
ctl --state-dir "$scratch" restore "$work/db.sqlite"
ctl --state-dir "$scratch" blobs-restore "$work/blobs" --db-backup "$work/db.sqlite"

echo "== 3/4 apply this code's migrations to the scratch copy"
"$PYTHON" - "$scratch/synapse.sqlite" <<'PY'
import sys
from pathlib import Path
from synapse.persistence.migrations import apply_migrations, default_migrations_dir, open_database

db = open_database(Path(sys.argv[1]))
applied = apply_migrations(db, default_migrations_dir())
integrity = db.execute("PRAGMA integrity_check").fetchall()
foreign = db.execute("PRAGMA foreign_key_check").fetchall()
version = db.execute("SELECT max(version) FROM schema_migrations").fetchone()[0]
db.close()
print(f"applied migrations: {applied or 'none (already current)'}; schema version now {version}")
if integrity != [("ok",)] or foreign:
    print(f"FAILED: integrity_check={integrity} foreign_key_check={foreign}", file=sys.stderr)
    sys.exit(1)
PY

echo "== 4/4 re-verify a backup of the migrated copy with this code"
ctl --state-dir "$scratch" backup "$work/migrated.sqlite"
ctl --state-dir "$scratch" backup-verify "$work/migrated.sqlite"

rm -rf "$scratch"
echo "rehearsal OK. Rollback point kept in: $work (db.sqlite + blobs/). Real state was not modified."
