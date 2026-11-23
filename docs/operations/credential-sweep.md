# Credential sweep (leftover scratch HOMEs)

The Claude CLI and Codex adapters copy OAuth credentials into a per-run scratch root. Normal exit removes it,
but a SIGKILLed daemon leaves it on disk. `synapse.providers.credential_sweep` reclaims these at daemon
startup, after the daemon lock is held and before any run can start (`sweep_startup(state_dir)` in
`daemon/__main__.py`).

- Scanned roots only: `<tmp>/synapse-agent-<uid>/` (Claude) and `<state_dir>/codex-run/` (Codex).
- Each adapter writes a marker `.synapse-scratch-owner` (JSON: magic, owner pid, daemon boot id, process start
  time) into the scratch root right after creating it (`write_marker`).
- Candidates are direct children named as 32 hex chars that are real directories, owned by the current user,
  with a valid marker. Unmarked entries are never touched.
- Skipped: owner pid alive and (same boot id, or same process start time, e.g. a second daemon sharing `/tmp`).
  A reused pid with a different start time is treated as dead.
- Symlinks are never followed (roots, entries, contents); mount points are not crossed. Files are overwritten
  with zeros (only credential-named files, at most 1 MiB; everything else is just unlinked), fsynced and
  unlinked (best effort), then directories removed.
- The directory walk is iterative and holds at most 64 open directory fds. A scratch tree deeper than 64
  levels is not descended into: the failure is logged and the dir is left in place.
- Any failure (per entry, per scratch dir, or the whole sweep) is logged and never blocks startup:
  `sweep_startup` catches everything, and the daemon wraps the call in its own try/except as well.
