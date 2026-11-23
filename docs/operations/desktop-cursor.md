# Desktop event cursor persistence

The desktop saves how far it has read the event stream, so a restart resumes after the last delivered event instead of replaying from sequence zero.

- **File:** `desktop-events.cursor` in the daemon's state directory (so it is per daemon). Contents: `SEQUENCE EVENT_ID`, at most 256 bytes. No event bodies, tokens or payloads are stored or logged.
- **Binding:** the cursor is bound to the event id as well as the sequence. On the first successful poll after start the desktop fetches the event at the saved sequence and requires the same id. If the database was recreated or restored, or the state directory now belongs to a different daemon, the ids differ.
- **Delivery is at-least-once.** The cursor is saved after a page of events was delivered to the UI. A crash between delivery and save replays at most that page; it never skips events.
- **Writes** are atomic (temp file in the same directory, fsync, chmod 0600, rename, directory fsync).
- **Reads** refuse symlinks, non-regular files, files owned by another user and group/other-accessible files.

## Failure modes (always visible, never silent)

A notice appears under the connection line of the main window.

| Situation | Behaviour |
| --- | --- |
| No file | Replay from the start (first run). |
| Corrupt, oversized or unsafe-permission file | Ignored; replay from the start; notice; the next save repairs the file. |
| Cursor ahead of the event log | Replay from the start; notice. |
| Event id at the saved sequence differs | Replay from the start; notice ("different event log"). |
| Verification request fails (daemon down) | Normal reconnect backoff; the cursor is verified on the next success. No events are shown from a cursor that is not yet verified. |
| Save fails (read-only directory, disk full) | Polling continues; one notice that a restart will replay events; cleared when a save succeeds. |
| Event has no usable `id` | Not persisted; notice. |

A reopened desktop therefore lists only events after the saved cursor in its Events tab (run graphs and approvals are rebuilt from daemon snapshots, not from the event list). Replay on any doubt is deliberate: the event list is display-only, so duplicates are acceptable and skipping is not.

Not covered: the in-session daemon reset case still behaves as described in `desktop-console.md` (restart the desktop); deleting `desktop-events.cursor` forces a full replay.
