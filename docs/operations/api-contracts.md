# Operator HTTP contracts

The authenticated `/openapi.json` describes the currently implemented health, event replay, run and task queries, thread and message queries, approval listing, workflow publication, queued run creation and fixed mock start/cancel commands. Successful payloads retain their existing shapes. Response schemas document the production repositories' current fields; the operator event stream is a separate WebSocket and is not described as a REST endpoint. Implementations still require their documented scoped authorization and daemon ownership.

HTTP errors now share `code`, `message`, `details`, `correlation_id`, and `detail`. The legacy `detail` member is retained for clients that read FastAPI-style errors; validation detail entries retain `loc`, `type`, and `msg` but use a generic message and exclude supplied input values and context. A valid `X-Correlation-ID` is returned on all responses; otherwise the server generates one. Mutations still require exactly one valid caller-supplied correlation ID and idempotency key. Errors and successes use `Cache-Control: no-store`; unexpected exceptions yield a generic 500, with only the exception class logged. WebSocket authentication and close behavior are unchanged. Command acceptance remains distinct from completed execution or cancellation.

Contract tests validate real Unix-socket daemon responses against the published JSON schemas and exercise validation, authentication, not-found and internal-error envelopes. API access still uses a same-user bearer token and does not establish a human identity or isolate worker processes. No approval decision endpoint or live provider capability is added.

`POST /api/v1/agent-runs` (201, replay 200), `GET /api/v1/runs/{run_id}/agent` and `POST /api/v1/runs/{run_id}/agent-cancel` are documented and contract-tested like the other operator endpoints; see [agent runs](agent-runs.md).

Run snapshots include `mock_config`, either null or the immutable fixture, profile ID and current profile provider read in the same SQLite snapshot as the run and tasks. The field is additive. It provides operator visibility only: the daemon rechecks the fixture, profile, exact versions and worker state transactionally before any mock reconciliation.

A generated, human-readable reference for every route is in [API reference](../api/reference.md); run `scripts/gen_api_reference.py` after changing a route (a test fails when it is stale).
