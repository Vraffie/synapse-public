# ADR 023: Spawn journal and startup worker fencing

Status: Accepted (slice 1). Follows ADR 022. Builds on ADR 019 (privilege boundary); local single machine only, Linux only (`/proc`).

## Context

Startup recovery (`AgentRuntime.recover_interrupted`) marks every in-flight attempt `unknown`, blocks its task and moves the run to `recovering` via `recover_mock_run(provider_kind="claude")`. It used to pass `no_workers_survive=True` without verifying it. That claim is false when an agent is detached: the sandbox is off, or the agent escaped the tie to its parent. Such a process can outlive a crashed daemon and keep editing the worktree while recovery reports the attempt as unknown. The daemon kept no durable record of what it had spawned, so a restart had nothing to check.

## Decision (this slice)

1. **Per-spawn journal** under `<state>/spawn-journal` (directory 0700, files 0600, atomic write plus fsync of file and directory). Entries hold only ids, pid, start ticks and boot id, never a prompt, path or credential.
   - `begin`: an intent file (`state: starting`) is written before `Popen`. If the journal cannot be written, the spawn does not happen.
   - `record`: pid, start ticks and boot id are written right after `Popen`. The child is its own session leader, so its pid is its process group id.
   - `finish`: the file is removed after the process is reaped, unless the stop status was `UNKNOWN` (the process may still run), in which case the entry stays for the next startup. A failed spawn removes its entry.
2. **Startup fence**, run by `AgentRuntime.recover_interrupted` before any recovery (the daemon calls it under the ownership lock). For each leftover entry the outcome is one of:
   - `verified_dead`: provably gone. Boot id differs; no such pid and an empty process group; start ticks differ and no live member of that group; or only zombies remain.
   - Leader gone but members remain: killed only when every member has the session id we spawn with (`start_new_session`) and started no earlier than the recorded leader; otherwise `unverified`. This still cannot separate our orphans from an unrelated session leader that reused the pid after wrap-around and left children, which needs a pid wrap between crash and restart; that residual is accepted and stated here.
   - `killed`: alive and matching the recorded identity; the whole process group gets SIGKILL and is then verified to have no live member (5 s wait).
   - `unverified`: anything not provable. This covers a begin-only entry (a spawn may have happened with no pid recorded), a malformed, oversized or wrong-version file, a reused pid whose group is still live, a live process that is not the session leader we spawned, a signalling error, and a group that does not die within the wait. An ambiguous group is never signalled. Entries pointing at the daemon's own pid, process group or session are `unverified`, never signalled (a forged entry cannot turn the fence into a confused deputy against the daemon or the user's login session).
   Resolved entries are deleted. Unverified entries are renamed to `*.unverified`; existing `*.unverified` files count as unverified at EVERY startup until the operator deletes them after checking, so a doubt never silently disappears. Entries beyond the 1000-entry cap are counted unverified and left in place, and the fence has a 60 s total wait budget (an entry that would need more is unverified). Entry files are read with a bounded, non-following, non-blocking open of a regular file only (a FIFO, device or symlink named `*.json` is `unverified`, never read or waited on). Stray `*.tmp` files are deleted. If the fence itself raises, it is treated as one unverified.
3. **Recovery then proceeds exactly as before**: attempt `unknown`, task blocked, run `recovering`, nothing is re-spawned. `recover_mock_run` still receives `no_workers_survive=True`: that flag is the caller's attestation that recovery may not resume anything, and recovery never does. The fence does not change what is resumable; it kills surviving process groups and gives the operator evidence (the audit event) for the `agent-abandon` decision.
4. **Audit.** An `agent.worker_fence` event (payload `run_id`, `principal: agent-recovery`, `verified_dead`, `killed`, `unverified`, `all_clear`) is written, with an outbox row, on every run that has an unresolved attempt (`created`, `starting` or `running`) whenever the fence found at least one entry. `all_clear` is true when `unverified` is 0. A clean restart with no entries writes nothing.

### Why a journal file and not the originally sketched migration 0017

The provider spawns on a worker thread, and the SQLite connection must not be written from it. A synchronous fsynced file is thread-safe and durable at the moment of spawn, so the intent exists before the process does.

### Alternatives rejected

- DB columns written from the event loop after the first provider event: the first seconds of a process would be unrecorded, which is exactly the window a crash can hit.
- A pidfile per run: a run has several tasks and attempts, and a pidfile written after spawn has the same gap as the first alternative.

## Not done in this slice (future work)

- `daemon_epoch` and rejection of stale writes from a previous daemon.
- The `starting` attempt state in the database (the journal's `starting` entry is not mirrored in SQLite).
- Automatic resumption of attempts provably never spawned.
- Relaxing the `agent-abandon` operator assertion when the fence was `all_clear`. The assertion stays mandatory.
- Per-attempt association. The fence is global: its counts are written to every run with unresolved attempts, so an `unverified` entry from one run shows on all of them.

## Consequences and limits

- Local single machine only. Linux `/proc` only: the identity check reads `/proc/<pid>/stat` and `/proc/sys/kernel/random/boot_id`.
- ADR 019 same-uid caveat: with the sandbox off, the agent runs as the daemon's uid and can edit or delete journal files. The journal is a safety net for crashes, not a defence against a hostile same-uid agent.
- This is a PROCESS-GROUP fence. `all_clear` means no member of a recorded group survives, not that no descendant survives: a descendant that created its own session or group (`setsid`, `setpgid`, a detached helper) is not found, proved by the security review. A cgroup per spawn or a subreaper walk is future work. Sandbox runs are additionally tied to the daemon by `--die-with-parent` and a PID namespace.
- Codex spawns are journaled the same way as Claude spawns when the Codex provider is given a journal (same fail-closed `begin`, `finish` only after reap and never on an `unknown` stop status). Since the ADR 016 Codex wiring, the daemon passes its journal to every Codex provider it builds (only for tasks that name a Codex profile).
- A TOCTOU window exists between the `/proc` checks and `killpg` (no pidfd); exploiting it needs the leader to exit and the pid to wrap within microseconds.
- The journal's temp file is created with `O_EXCL|O_NOFOLLOW` after removing a stale one, so a planted symlink is never followed.
- `unverified` leaves the operator's existing duty unchanged: check for stray processes before `agent-abandon`.
