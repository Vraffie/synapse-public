-- run_budget_bindings (ADR 020 D2): the budget policy a run was started under, stored as an immutable copy so a
-- run's budget decisions stay reproducible after the operator's policy file changes or disappears. One row per
-- run, written in the transaction that starts the run; a run started with no policy has no row (unbudgeted).
CREATE TABLE run_budget_bindings (
  run_id TEXT PRIMARY KEY REFERENCES runs(id),
  policy_version TEXT NOT NULL,
  policy_sha256 TEXT NOT NULL CHECK (length(policy_sha256) = 64),
  policy_json TEXT NOT NULL,
  bound_at TEXT NOT NULL
);
CREATE TRIGGER run_budget_bindings_no_update BEFORE UPDATE ON run_budget_bindings
BEGIN SELECT RAISE(ABORT, 'run budget bindings are append-only'); END;
CREATE TRIGGER run_budget_bindings_no_delete BEFORE DELETE ON run_budget_bindings
BEGIN SELECT RAISE(ABORT, 'run budget bindings are append-only'); END;
