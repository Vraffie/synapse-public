# `agentctl events --follow`

Tail the daemon's event log from a terminal and resume after a restart of either side.

```
agentctl --state-dir STATE events --follow                 # print new events as JSON lines until Ctrl-C
agentctl --state-dir STATE events --follow --stop-when-idle   # catch up, then exit 0 (scripts, cron)
agentctl --state-dir STATE events --follow --after 0        # ignore the saved cursor, replay from the start
```

Options: `--limit` (page size, 1..1000, default 100), `--interval` (seconds between polls when caught up,
0.05..60, default 1), `--cursor-file` (default `STATE/events.cursor`). Without `--follow`, `events` is unchanged:
one page, one JSON document.

## Recovery model

Events are durable in the daemon database, so recovery is "ask again after the last sequence handled".
`STATE/events.cursor` holds that one integer (mode 0600, replaced atomically, fsynced).

- **At-least-once to stdout, never skipping.** A page is printed first, then the cursor is saved. A crash between
  the two replays that page on the next run. A consumer that needs exactly-once should dedupe on `sequence`.
- **Daemon down or restarting** (connection refused, missing socket, timeout, HTTP 5xx): retried with backoff
  0.5 s doubling to 10 s, message on stderr, giving up with exit 1 after 60 consecutive failures.
- **Stops instead of guessing** (exit 1, cursor not moved): HTTP 4xx (for example a rotated token), a permission
  error on the socket, a malformed response, or events that are not strictly increasing.
- **A reader that goes away** (`| head`): the unwritten page is not marked delivered.
- A cursor file that is a symlink, not owned by you, group/other accessible, not a plain integer or larger than the
  maximum sequence is refused with a message (delete it or pass `--after`); it is never silently treated as 0.

## Limits

- Polling, not the WebSocket: one request per interval while idle. The WebSocket stream stays for the desktop.
- Output is the same event objects as the API; they may include payloads, so treat the terminal and any redirect as
  sensitive as the event log.
- The desktop still keeps its cursor in memory only (restart replays from 0): not changed here.
- Tested against a fake daemon (retry, backoff, order, cursor safety) and a real daemon across a restart
  (`tests/test_cli_follow.py`). Not tested: a daemon that restarts while a poll is in flight, very large logs.
