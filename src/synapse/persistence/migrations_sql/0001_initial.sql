-- Initial schema extracted from approved architecture v1.0
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
