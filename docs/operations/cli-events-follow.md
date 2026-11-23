# `agentctl events --follow`

Tail the daemon's event log from a terminal and resume after a restart of either side.

```
agentctl --state-dir STATE events --follow                 # print new events as JSON lines until Ctrl-C
agentctl --state-dir STATE events --follow --stop-when-idle   # catch up, then exit 0 (scripts, cron)
agentctl --state-dir STATE events --follow --cursor-file replay.cursor --after 0   # replay from the start into a separate cursor file
```

Options: `--limit` (page size, 1..1000, default 100), `--interval` (seconds between polls when caught up,
0.05..60, default 1), `--cursor-file` (default `STATE/events.cursor`). Without `--follow`, `events` is unchanged:
one page, one JSON document.

## Recovery model

Events are durable in the daemon database, so recovery is "ask again after the last sequence handled".
`STATE/events.cursor` holds one line, `SEQUENCE EVENT_ID` (mode 0600, replaced atomically, fsynced): the last event
handled and its id, which binds the cursor to one database.

- **At-least-once to stdout, never skipping.** A page is printed first, then the cursor is saved. A crash between
  the two replays that page on the next run. A consumer that needs exactly-once should dedupe on `sequence`.
- **Cursor bound to the database.** On resume the event AT the cursor is fetched (`after=cursor-1&limit=1`). If the
  log is shorter than the cursor, or the event there has a different id (database recreated or restored), it exits 1
  with a message and prints nothing; to start over, delete the cursor file. The same check runs after every
  reconnect and on every idle poll while following, so a daemon restarted with a wiped or restored database stops the
  follower with exit 1 instead of polling an empty log forever. `--after N` (N > 0) gets the same check: an N beyond the
  end of the log exits 1 instead of stalling. A legacy cursor file (a
  bare integer, written by the previous version) is accepted once: the log must still contain that sequence, a
  warning goes to stderr, and the file is rewritten in the new format with the id of that event.
- **`--after` never moves a saved cursor backwards.** `--after N` below the saved cursor
  exits 2 with a message and changes nothing, unless `--cursor-file` names a file other than the default
  `STATE/events.cursor` (compared by resolved path), for example a separate file for a replay. `--after` at or above the saved cursor is allowed.
- **Cursor cannot be saved** (full disk, missing directory, read-only): one line on stderr, exit 1. The page was
  already printed, so the next run replays it.
- **Daemon down or restarting** (connection refused, missing socket, timeout, HTTP 5xx): retried with backoff
  0.5 s doubling to 10 s, message on stderr, giving up with exit 1 after 60 consecutive failures.
- **Wrong `--state-dir` or missing operator token** is a configuration error, not a daemon that is down: exit 1
  at once, no retries. Only the socket being missing or refusing connections is retried.
- **Stops instead of guessing** (exit 1, cursor not moved): HTTP 4xx (for example a rotated token), a permission
  error on the socket, a malformed response, or events that are not strictly increasing.
- **A reader that goes away** (`| head`): the unwritten page is not marked delivered; exit 1 with no traceback.
- **Output the terminal encoding cannot represent** (for example an ASCII locale with non-ASCII event text): one line on stderr, exit 1, cursor not moved; use a UTF-8 locale or `PYTHONIOENCODING=utf-8`.
- **Any other stdout write error** (for example `/dev/full`): one line on stderr, exit 1, cursor not moved.
- **Two followers on one cursor file** are not locked against each other; they can overwrite each other's cursor,
  which causes duplicates, never skipped events. Use one cursor file per follower.
- **SIGTERM** is handled like Ctrl-C: exit 0 (the cursor is saved after every page).
- `--interval` outside 0.05..60 is rejected with exit 2.
- A cursor file that is a symlink, not owned by you, group/other accessible, not `SEQUENCE EVENT_ID` (or a bare integer) or larger than the
  maximum sequence is refused with a message (delete it); it is never silently treated as 0.

## Limits

- Polling, not the WebSocket: one request per interval while idle. The WebSocket stream stays for the desktop.
- Output is the same event objects as the API; they may include payloads, so treat the terminal and any redirect as
  sensitive as the event log.
- The desktop still keeps its cursor in memory only (restart replays from 0): not changed here.
- Tested against a fake daemon (retry, backoff, order, cursor safety) and a real daemon across a restart
  (`tests/test_cli_follow.py`). Not tested: a daemon that restarts while a poll is in flight, very large logs.
