# Agent Control Center — Implementation Architecture v1.0

Status: Approved design baseline, 2026-11-19. Scope: general-purpose local-first orchestration platform; Domain catalog as a plugin.

## 1. Decision record

- ADR-001: Python modular monolith; internal ports/adapters, not microservices.
- ADR-002: `agentd` systemd user daemon owns state; Qt desktop and CLI are API-only clients.
- ADR-003: FastAPI REST `/api/v1`, OpenAPI, WebSocket event stream; Unix socket preferred for local clients, optional authenticated loopback TCP.
- ADR-004: SQLite WAL with foreign keys, single controlled write path, atomic event/outbox writes; content-addressed filesystem for large artifacts.
- ADR-005: Claude Code (2–3 distinct authorized subscription CLI contexts), Codex (1), deterministic mock; primary assignment with eligible fallback; no account rotation to evade limits.
- ADR-006: maximum six simultaneously running agent tasks; separate tool-process limits and per-profile caps; default conservative per-profile concurrency until verified.
- ADR-007: direct agent messaging via authenticated MCP tools, with orchestrator-mediated fallback when active sessions cannot receive messages.
- ADR-008: each editing attempt gets an isolated Git worktree; multi-repository changes use per-repo worktrees and an integration barrier.
- ADR-009: approval for STAGING deployment, remote push, protected branch mutation, destructive operations; production deployment disabled.
- ADR-010: balanced token optimization: scoped context, exact caching, deterministic output reduction, structured handoffs; never suppress mandatory verification.
- ADR-011: trusted versioned plugin packages; Domain catalog plugin has no special access to core persistence or policies.

## 2. Runtime topology and trust boundaries

```
agent-desktop (PySide6)     agentctl CLI        local integrations
          \                   |                    /
            REST / WebSocket over protected local transport
                              |
                    agentd (systemd --user)
     API/Auth -> Command Bus -> Domain Services -> SQLite + outbox
                     |                 |
             Scheduler/Policies   Messaging/Context/Analytics
                     |
               Worker Supervisor
             /         |          \
      Claude CLI     Codex CLI    deterministic tools
             \         |          /
             restricted MCP gateway (per-session identity)
                              |
           Git worktrees / validators / plugin workers
```

- Only daemon mutates authoritative state. Workers never write SQLite directly.
- Worker stdout/stderr are untrusted; parsed events must be schema-validated.
- MCP gateway grants per-session capabilities and derives identity from an ephemeral credential, not an `agent_id` supplied in tool parameters.
- Desktop closure does not stop daemon. For persistence after logout, optionally enable user lingering with explicit user consent.
- Keep the first version on one host. Remote workers are a future ADR requiring mTLS, worker identity and distributed leases.

## 3. Package boundaries and dependency rules

```
apps/{daemon,desktop,cli,mcp_gateway}/
packages/
  contracts/{api,events,mcp,providers,plugins}/
  domain/{workflows,tasks,agents,communication,approvals,policies}/
  application/{commands,queries,services}/
  orchestration/{scheduler,engine,recovery,worker_supervisor}/
  communication/{routing,delivery,context}/
  providers/{claude,codex,mock,registry}/
  optimization/{context_builder,cache,output_reducer,budgets}/
  analytics/{usage,aggregation,experiments}/
  infrastructure/{sqlite,artifacts,git,process,logging}/
plugins/domain_catalog/{manifest,agents,workflows,validators,integrations,tests}/
tests/{unit,contract,integration,e2e,fault_injection,qt}/
packaging/{arch,systemd}/
docs/{adr,api,operations,security}/
```

Dependency direction: contracts/domain <- application <- adapters/API/UI. Domain never imports Qt, FastAPI, SQLite, Claude or Codex. Modules own repository interfaces and tables; no cross-module direct SQL. In-process communication is through typed services and durable events, not self-HTTP. Public schemas are versioned and tested for backward compatibility.

## 4. Contracts

```python
from typing import Protocol, AsyncIterator

class ProviderAdapter(Protocol):
    async def probe(self, profile_id: str) -> 'ProviderHealth': ...
    async def capabilities(self, profile_id: str) -> 'ProviderCapabilities': ...
    async def start(self, request: 'ExecutionRequest') -> 'ExecutionHandle': ...
    async def stream(self, handle: 'ExecutionHandle') -> AsyncIterator['ProviderEvent']: ...
    async def cancel(self, handle: 'ExecutionHandle') -> None: ...
    async def usage(self, handle: 'ExecutionHandle') -> 'UsageObservation | None': ...

class MessagePort(Protocol):
    async def send(self, sender_identity: str, command: 'SendMessage') -> 'MessageReceipt': ...
    async def claim(self, recipient_identity: str, limit: int) -> list['Delivery']: ...
    async def ack(self, recipient_identity: str, delivery_id: str, result_ref: str) -> None: ...

class ArtifactPort(Protocol):
    async def put(self, stream, media_type: str, provenance: dict) -> 'ArtifactRef': ...
    async def open(self, artifact_id: str): ...

class PolicyPort(Protocol):
    async def authorize(self, principal: str, action: str, resource: dict) -> 'Decision': ...

class ValidatorPort(Protocol):
    async def validate(self, candidate: 'CandidateRevision') -> 'VerificationEvidence': ...
```

Capabilities are reported, never guessed: streaming, cancellation, structured events, resume, context limits, usage telemetry, quota telemetry, tool/MCP support. Provider adapters isolate CLI flags and JSON parsing by detected version. A CLI capability absence results in `unsupported`, not fabricated data.

## 5. Execution and scheduling state machines

Run: `CREATED -> QUEUED -> RUNNING -> {AWAITING_APPROVAL, PAUSED, RECOVERING, SUCCEEDED, FAILED, CANCELLED}`. Terminal states immutable. Task: `CREATED -> WAITING_DEPENDENCIES -> READY -> ASSIGNED -> RUNNING -> VERIFYING -> {SUCCEEDED, CHANGES_REQUIRED, RETRY_WAIT, BLOCKED, FAILED, CANCELLED}`. Approval: `REQUESTED -> {APPROVED, REJECTED, EXPIRED, REVOKED}`. Session: `CREATED -> STARTING -> RUNNING -> {EXITED, CANCELLED, LOST, UNKNOWN}`.

All transitions are compare-and-swap against current state/version and emitted atomically with an event. No worker self-certifies completion. Scheduler selects READY tasks by dependency, priority, eligibility, resource locks, profile health, configured caps, and budget. Preferred profile first; fallback only if policy permits. Never bypass provider rate limits or authentication rules. Reviewers must not review their own changes where independence is required. Every retry is a new attempt with a new identity and bounded retry count. Timeout and cancellation use process-group supervision; reconcile unknown external effects before retry.

A task that needs human approval enters AWAITING_APPROVAL and releases expensive worker slots. Approval is bound to candidate SHA(s), artifact hashes, operation, environment, policy version and expiry. Changed candidate or policy invalidates approval.

## 6. Communication semantics

Messages are immutable; recipients have independent delivery records. `send` writes message, recipients and outbox event in one transaction. Dispatcher wakes recipient workers and websocket subscribers. `claim` atomically assigns a lease, increments attempt count, and returns a delivery; `ack` requires the current lease owner and durable result reference. Expired leases can be redelivered; at-least-once delivery means consumers must be idempotent. Acknowledged != work completed != verified. Retry exhaustion moves delivery to dead-letter status and escalates.

Types: `task.request`, `question`, `answer`, `review.request`, `review.finding`, `change.request`, `change.completed`, `verification.result`, `escalation`, `ack`. Message envelope: id, schema_version, run_id, thread_id, sender_instance, type, correlation_id, causation_id, payload, artifact_refs, created_at. Sender identity comes from gateway. Allowlist recipients by run/capability. Bound messages per thread, unanswered question TTL and circular-wait detection. No message can override system policy.

MCP tools: `send_message`, `get_inbox`, `read_thread`, `reply`, `ack_delivery`, `read_context`, `propose_context_update`, `get_artifact`, `publish_artifact`, `request_review`, `report_blocker`. The gateway validates task/run scope and payload sizes. If active CLI MCP support is absent or disabled, orchestrator injects pending messages at the next checkpoint/new attempt.

Shared context: task/run/project scopes. Task/run updates use versioned CAS. Project-level knowledge changes require reviewed promotion. Store source revision and provenance; large content lives in artifacts. FTS5 optional for search.

## 7. SQLite DDL — initial migration

```sql
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;
PRAGMA busy_timeout = 5000;

CREATE TABLE schema_migrations (
  version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL
);
CREATE TABLE profiles (
  id TEXT PRIMARY KEY, provider TEXT NOT NULL CHECK(provider IN ('claude','codex','mock')),
  display_name TEXT NOT NULL, config_ref TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
  priority INTEGER NOT NULL DEFAULT 100, max_concurrency INTEGER NOT NULL DEFAULT 1,
  health_state TEXT NOT NULL DEFAULT 'unknown', version INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL
);
CREATE TABLE agent_definitions (
  id TEXT NOT NULL, version TEXT NOT NULL, plugin_id TEXT,
  definition_json TEXT NOT NULL, PRIMARY KEY(id,version)
);
CREATE TABLE workflow_versions (
  workflow_id TEXT NOT NULL, version TEXT NOT NULL, definition_json TEXT NOT NULL,
  definition_sha256 TEXT NOT NULL, published_at TEXT NOT NULL,
  PRIMARY KEY(workflow_id,version)
);
CREATE TABLE runs (
  id TEXT PRIMARY KEY, workflow_id TEXT NOT NULL, workflow_version TEXT NOT NULL,
  state TEXT NOT NULL, state_version INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  FOREIGN KEY(workflow_id,workflow_version) REFERENCES workflow_versions(workflow_id,version)
);
CREATE TABLE tasks (
  id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
  node_id TEXT NOT NULL, state TEXT NOT NULL, state_version INTEGER NOT NULL DEFAULT 0,
  assigned_profile_id TEXT REFERENCES profiles(id), attempt_count INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  UNIQUE(run_id,node_id)
);
CREATE TABLE task_dependencies (
  task_id TEXT NOT NULL REFERENCES tasks(id), depends_on_id TEXT NOT NULL REFERENCES tasks(id),
  PRIMARY KEY(task_id,depends_on_id), CHECK(task_id <> depends_on_id)
);
CREATE TABLE task_attempts (
  id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id), attempt_no INTEGER NOT NULL,
  profile_id TEXT REFERENCES profiles(id), state TEXT NOT NULL, session_id TEXT,
  worktree_path TEXT, started_at TEXT, ended_at TEXT, exit_code INTEGER,
  UNIQUE(task_id,attempt_no)
);
CREATE TABLE agent_instances (
  id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
  definition_id TEXT NOT NULL, definition_version TEXT NOT NULL,
  task_attempt_id TEXT REFERENCES task_attempts(id), state TEXT NOT NULL,
  last_heartbeat_at TEXT, created_at TEXT NOT NULL,
  FOREIGN KEY(definition_id,definition_version) REFERENCES agent_definitions(id,version)
);
CREATE TABLE threads (
  id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id), subject TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'open', created_at TEXT NOT NULL
);
CREATE TABLE messages (
  id TEXT PRIMARY KEY, thread_id TEXT NOT NULL REFERENCES threads(id),
  sender_instance_id TEXT NOT NULL REFERENCES agent_instances(id),
  type TEXT NOT NULL, schema_version INTEGER NOT NULL,
  correlation_id TEXT, causation_id TEXT REFERENCES messages(id),
  payload_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE message_deliveries (
  id TEXT PRIMARY KEY, message_id TEXT NOT NULL REFERENCES messages(id),
  recipient_instance_id TEXT NOT NULL REFERENCES agent_instances(id),
  state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
  lease_owner TEXT, lease_until TEXT, acknowledged_at TEXT, result_artifact_id TEXT,
  UNIQUE(message_id,recipient_instance_id)
);
CREATE INDEX idx_deliveries_inbox ON message_deliveries(recipient_instance_id,state,lease_until);
CREATE INDEX idx_messages_thread ON messages(thread_id,created_at);
CREATE TABLE context_documents (
  id TEXT PRIMARY KEY, scope TEXT NOT NULL CHECK(scope IN ('task','run','project')),
  scope_id TEXT NOT NULL, context_key TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1,
  content_json TEXT NOT NULL, updated_by TEXT NOT NULL, updated_at TEXT NOT NULL,
  UNIQUE(scope,scope_id,context_key)
);
CREATE TABLE context_revisions (
  document_id TEXT NOT NULL REFERENCES context_documents(id), version INTEGER NOT NULL,
  content_json TEXT NOT NULL, updated_by TEXT NOT NULL, updated_at TEXT NOT NULL,
  PRIMARY KEY(document_id,version)
);
CREATE TABLE artifacts (
  id TEXT PRIMARY KEY, sha256 TEXT NOT NULL, media_type TEXT NOT NULL,
  byte_size INTEGER NOT NULL, storage_path TEXT NOT NULL,
  provenance_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE events (
  sequence INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE,
  aggregate_type TEXT NOT NULL, aggregate_id TEXT NOT NULL,
  run_id TEXT REFERENCES runs(id), type TEXT NOT NULL, schema_version INTEGER NOT NULL,
  payload_json TEXT NOT NULL, occurred_at TEXT NOT NULL
);
CREATE INDEX idx_events_run_seq ON events(run_id,sequence);
CREATE TABLE outbox (
  event_id TEXT PRIMARY KEY REFERENCES events(id), dispatched_at TEXT,
  attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at TEXT
);
CREATE TABLE verification_results (
  id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
  task_id TEXT REFERENCES tasks(id), candidate_sha256 TEXT NOT NULL,
  verifier_id TEXT NOT NULL, outcome TEXT NOT NULL, evidence_artifact_id TEXT REFERENCES artifacts(id),
  created_at TEXT NOT NULL
);
CREATE TABLE approval_requests (
  id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
  operation TEXT NOT NULL, target TEXT NOT NULL, candidate_manifest_sha256 TEXT NOT NULL,
  policy_version TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'requested',
  expires_at TEXT NOT NULL, requested_at TEXT NOT NULL, decided_at TEXT,
  decision_principal TEXT, decision_reason TEXT
);
CREATE TABLE usage_observations (
  id TEXT PRIMARY KEY, attempt_id TEXT NOT NULL REFERENCES task_attempts(id),
  profile_id TEXT NOT NULL REFERENCES profiles(id), source TEXT NOT NULL,
  input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER,
  cache_write_tokens INTEGER, reported_total_tokens INTEGER,
  confidence TEXT NOT NULL, observed_at TEXT NOT NULL, source_event_id TEXT,
  UNIQUE(attempt_id,source_event_id)
);
CREATE TABLE quota_observations (
  id TEXT PRIMARY KEY, profile_id TEXT NOT NULL REFERENCES profiles(id),
  metric TEXT NOT NULL, value REAL, unit TEXT, reset_at TEXT,
  source TEXT NOT NULL, confidence TEXT NOT NULL, observed_at TEXT NOT NULL
);
CREATE TABLE context_manifests (
  id TEXT PRIMARY KEY, task_attempt_id TEXT NOT NULL REFERENCES task_attempts(id),
  manifest_json TEXT NOT NULL, manifest_sha256 TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE optimization_decisions (
  id TEXT PRIMARY KEY, task_attempt_id TEXT NOT NULL REFERENCES task_attempts(id),
  policy_version TEXT NOT NULL, decision_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE idempotency_keys (
  principal TEXT NOT NULL, operation TEXT NOT NULL, key TEXT NOT NULL,
  request_sha256 TEXT NOT NULL, response_json TEXT NOT NULL, created_at TEXT NOT NULL,
  PRIMARY KEY(principal,operation,key)
);
CREATE TABLE audit_entries (
  sequence INTEGER PRIMARY KEY AUTOINCREMENT, principal TEXT NOT NULL,
  action TEXT NOT NULL, resource TEXT NOT NULL, decision TEXT NOT NULL,
  correlation_id TEXT, details_json TEXT NOT NULL, created_at TEXT NOT NULL
);
```

Production migration notes: add CHECK constraints/enums and indexes for all state fields, `sessions` and process reconciliation tables, profile health history, integration configs, cache entries, resource locks, artifact references, retry policies, and audit tamper-evidence in subsequent numbered migrations. DDL is an executable starting schema, not a substitute for migrations and constraints validated by tests. Protect SQLite with user-only filesystem permissions; backups must include database and artifact store as a consistent set.

## 8. Public API contracts

Base `/api/v1`. All mutation requests: authenticated principal, `Idempotency-Key`, correlation ID; JSON schema validation; consistent errors (`code`, `message`, `details`, `correlation_id`). Use ISO 8601 UTC, UUID/ULID, cursor pagination and ETag/state-version optimistic concurrency.

| Method | Path | Purpose |
|---|---|---|
| GET | `/system/health` | liveness/readiness |
| GET | `/system/capabilities` | feature and adapter availability |
| GET/POST | `/profiles` | list/create profiles |
| POST | `/profiles/{id}/probe` | health probe, not authentication bypass |
| GET | `/agents` | versioned agent catalog |
| GET/POST | `/workflows` | list/create draft |
| POST | `/workflows/{id}/publish` | immutable workflow version |
| POST | `/runs` | create run; returns 202 and operation/run ID |
| GET | `/runs/{id}` | consistent run snapshot |
| POST | `/runs/{id}/commands` | pause/resume/cancel/retry via authorized command |
| GET | `/runs/{id}/tasks` | tasks and attempts |
| POST | `/messages` | send typed message |
| GET | `/agents/{instance_id}/inbox` | inbox, scoped |
| POST | `/deliveries/{id}/claim` | atomic lease |
| POST | `/deliveries/{id}/ack` | acknowledge processing |
| GET | `/threads/{id}` | thread history |
| GET/PUT | `/context/{scope}/{scope_id}/{key}` | versioned shared context |
| GET | `/artifacts/{id}` | artifact metadata/stream |
| GET | `/approvals` | pending approvals |
| POST | `/approvals/{id}/decision` | approve/reject, authenticated human |
| GET | `/analytics/tokens` | measured usage grouped by profile/agent/run |
| GET | `/analytics/optimization` | estimates and experiments separately |
| GET | `/events?after={sequence}` | replay events |
| WS | `/events/stream?after={sequence}` | live events and reconnect |

Example `POST /runs`:
```json
{
  "workflow_id": "domain.catalog-change", "workflow_version": "1.0.0",
  "input": {"request": "Update notification configuration", "repositories": ["catalog", "products"]},
  "policy": {"max_parallel_agents": 6, "require_independent_review": true}
}
```
Response `202`:
```json
{"run_id":"run-001","operation_id":"op-001","state":"queued"}
```

Event envelope:
```json
{"event_id":"evt-001","sequence":42,"schema_version":1,"type":"message.created",
 "run_id":"run-001","timestamp":"2026-11-19T18:00:00Z","correlation_id":"corr-001",
 "payload":{"message_id":"msg-001","thread_id":"thread-001"}}
```

WebSocket client flow: obtain snapshot with `last_sequence`, subscribe with `after=last_sequence`, dedupe event IDs, detect sequence gaps, refetch snapshot if retention gap. Event sequence is global, not guaranteed gapless per run.

## 9. Profile isolation and worker supervision

Profile config stores provider, supported config root reference, CLI executable, model preference, max concurrency, role allowlist, primary/fallback order, allowed repos, health/cooldown; never store raw auth tokens. Authentication setup is a user-initiated CLI flow. Launch each session with explicit supported config environment and minimal inherited environment. Do not infer support for arbitrary profile directories or CLI flags: verify installed versions and document supported mechanisms before enabling a profile.

Worker supervisor creates process group, controls cwd/env, captures stdout/stderr to redacted bounded logs, applies timeouts and resource ceilings, tracks PID plus start time and supervisor identity to avoid PID reuse confusion. A restart reconciles processes; ambiguous state becomes `UNKNOWN`, not blindly retried. Cancel sends graceful termination then bounded kill escalation. Quota is `unknown` if CLI does not provide a reliable authorized source. Profile failover never attempts to evade rate limits or subscription policies.

## 10. Multi-repository Git and Domain plugin

Registry of allowed repository roots, baseline refs and protected paths. Create a unique worktree per task attempt per repo; check for uncommitted state and forbid destructive cleanup outside managed worktrees. Multi-repo candidate manifest contains repository ID, baseline SHA, candidate SHA, diff artifact hash and validation results. Integration barrier verifies all repo candidates and dependency compatibility. No push, protected branch mutation or deployment without explicit human approval. Deploy verifier rechecks manifest hashes immediately before action and records smoke-test evidence. Production deployment disabled by default.

Domain plugin registers agents (architect, requirement analyst, XML editor, schema expert, reviewer, Lens runner, QA verifier, deployment verifier), workflow definitions, XML indexer, validators, catalog dependency resolver, and STAGING adapter. All privileged operations go through central PolicyPort and approval service; plugin cannot write core DB directly.

## 11. Balanced token optimization

Context builder produces a manifest of exact repo SHA, requirement references, file ranges, validation results, context fragments and hashes. First run deterministic parsers/validators. Use task-scoped retrieval and deduplication; include raw artifact pointers rather than repeating large outputs. Tool output reducer preserves all unique failures and raw output. Exact cache keys include input hashes, tool version, configuration, policy version; semantic reuse disabled for edits/reviews initially. Compaction preserves hard requirements, unresolved issues, decisions and citations. Provider usage is normalized with source and confidence; do not double count cache categories. Report measured consumption separately from estimated avoided tokens. Quality gate is tokens per independently verified completed task, not simply fewer tokens.

## 12. Security policy matrix

| Operation | Default | Enforcement |
|---|---|---|
| Read allowlisted repo | Allow | scoped capability, path validation |
| Edit isolated worktree | Allow for editor role | worktree-scoped filesystem permissions |
| Run allowlisted validators | Allow | exact executable/argument schema |
| Send agent messages | Allow within run | authenticated MCP identity + recipient policy |
| Modify shared project knowledge | Review required | proposal and versioned promotion |
| Push remote | Human approval | commit-bound approval check at execution |
| Protected branch write | Human approval | protected ref and commit verification |
| Destructive filesystem operation | Human approval or deny | policy + canonical path checks |
| STAGING deployment | Human approval | candidate manifest, target, expiry, audit |
| Production deployment | Deny | no production adapter capability initially |
| Change credentials/security policy | Deny to agents | operator-only administrative API |

Use local Unix socket permissions or loopback bearer token with secure local storage, request origin checks where relevant, least-privilege workers, no shell-string command construction, redaction, output size caps, signed/hashed artifacts, no unsafe implicit plugin loading. Agents cannot approve their own requests. Human approval identity must be distinct from an agent identity; do not accept approval through agent-accessible MCP tools.

## 13. Recovery, invariants and tests

Critical invariants:
1. Exactly one authoritative daemon writer controls state.
2. Every state change and its event/outbox record commit atomically.
3. Agent messages are immutable and delivery is at least once.
4. Every external side effect has idempotency/reconciliation logic or manual recovery.
5. An approval is invalid after any relevant candidate/policy change.
6. No agent can elevate its capabilities by message or prompt.
7. Unknown quota is displayed as unknown.
8. Desktop disconnection cannot cancel work unless explicitly requested.
9. Every verification result identifies its exact candidate artifact.
10. Token optimization cannot bypass mandatory checks.

Tests: unit (state machines, policy, context), contract (API, provider adapters, MCP), integration (SQLite transaction/outbox, leases, Git worktrees), e2e (parallel agents, review loop, approval), fault injection (worker kill, daemon restart, corrupted provider output, lease expiry, duplicate event, invalid approval), UI (Qt event reconnection), security (path traversal, prompt injection, forged MCP identity, secret redaction). Tests use deterministic mock provider; real CLI smoke tests are opt-in and never required in CI.

## 14. Engineering backlog and release gates

### Phase 0 — Discovery and baseline (P0)
- Inventory Claude/Codex CLI versions, auth/profile isolation support, streaming formats, MCP capabilities, quotas and terms.
- Inventory repo layouts, Catalog Validator invocation, STAGING deployment interface, safe command allowlists.
- Record ADRs and threat model. Exit: supported capability matrix, no guessed CLI flags.

### Phase 1 — Contracts and persistence (P0)
- Establish monorepo, uv, ruff, mypy, pytest, pre-commit, CI.
- Implement domain entities, state transition validators, OpenAPI schemas and event envelope.
- Implement SQLite migrations, repositories, atomic outbox and artifact store.
- Exit: migration rollback/upgrade tests, contract tests and atomicity tests green.

### Phase 2 — Messaging and mock orchestration (P0)
- Implement inbox/threads/delivery leases/CAS context and authenticated MCP gateway.
- Build deterministic mock provider, scheduler, DAG execution, task attempts, retries and approvals.
- Implement 10 scenario fixtures including failure, disagreement, crash and rejection.
- Exit: 6 concurrent mock tasks, bidirectional messages, deterministic replay and recovery.

### Phase 3 — Local API and native Qt (P0)
- Build daemon, API authentication, REST/WebSocket event replay, agentctl.
- Build Qt shell, workflow graph, communication view, run inspector and approvals.
- Exit: close/reopen desktop while run continues; UI reconstructs exact state.

### Phase 4 — Real providers and profiles (P1)
- Claude/Codex adapters with capability probing and structured event normalization.
- Support 2–3 Claude profiles and 1 Codex profile; fixed primary with policy fallback.
- Worker supervision, session isolation, cancellation, quota unknown handling.
- Exit: opt-in smoke runs, no credential leakage, safe failover behavior.

### Phase 5 — Optimization and analytics (P1)
- Context manifests, deterministic output reduction, exact caches, budgets.
- Token usage ingestion with provenance; graphs for agents, messages, tokens and retries.
- Exit: reproducible metrics, cache invalidation tests, no false savings claims.

### Phase 6 — Multi-repo and Domain plugin (P1)
- Git worktree manager, multi-repo manifest, XML index and Lens adapter.
- Domain specialist agents, catalog change workflow, independent review and QA.
- Exit: full catalog workflow completes without push or deployment.

### Phase 7 — Deployment and hardening (P1)
- Human approval for remote push/protected refs/destructive operations/STAGING.
- Candidate binding, expiry, audit, deployment reconciliation and smoke checks.
- systemd user service, Arch PKGBUILD, backups, upgrade/recovery runbooks.
- Exit: red-team/fault-injection tests green; deployment cannot bypass approval.

### Phase 8 — Post-v1 extensions (P2)
- Advanced knowledge index, optional browser client, remote workers, additional plugins, optional semantic caching with evaluation.

## 15. First implementation slice

Deliver a vertical slice with `agentd`, SQLite, a mock provider, two specialist mock agents, an authenticated message API, one workflow, one reviewer correction loop, a minimal Qt graph, and `agentctl`. This must run offline, persist events, survive Qt closure, and pass deterministic e2e tests before real CLI integration.

## 16. Open implementation questions (nonblocking)

- Exact Claude Code/Codex CLI versions and supported isolated login/config mechanisms.
- Exact repository roots, protected refs, and Catalog Validator commands.
- Whether daemon must continue after OS logout (requires lingering setup).
- Exact STAGING deployment transport and authoritative smoke tests.
- Whether CLI subscription telemetry exposes reliable per-session usage or quota observations.

These are discovery inputs, not excuses to assume unsafe defaults. Keep adapters disabled until capabilities are verified.