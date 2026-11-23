# Operator message queries

The operator-only local API exposes `GET /api/v1/runs/{run_id}/threads` and `GET /api/v1/runs/{run_id}/threads/{thread_id}/messages`, with `after` row cursor (default zero) and `limit` (1–100). Pass the last returned `cursor` to get the next page. Messages include payload and artifact identifiers, so these endpoints are only for the existing all-access operator identity, never agent sessions. They do not permit sending, delivery changes or artifact download. Missing or cross-run threads return 404.

Cursors are local SQLite row IDs, not globally portable identifiers or event sequence numbers. Thread deletion and reinsertion can reuse row IDs; these pages are not a consistent snapshot. A full refresh is needed to detect changes behind a thread cursor. Message rows are immutable in this schema. No thread-history retention or gap detection is implemented.
