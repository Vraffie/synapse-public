-- An approved, unexpired approval whose binding no longer matches what the
-- daemon is about to execute cannot be revoked (the approval lifecycle has no
-- approved -> revoked edge), yet the gated task must fail closed. A denial row
-- records that execution-time binding recheck refused the gate's approval and
-- is the only additional evidence that lets task_gate_fail_requires_denied
-- fail the task. Rows are immutable and bound to an unresolved, parked gate.
CREATE TABLE task_gate_binding_denials (
  task_id TEXT PRIMARY KEY REFERENCES tasks(id),
  approval_id TEXT NOT NULL UNIQUE REFERENCES approval_requests(id),
  reason TEXT NOT NULL CHECK (reason IN ('binding_mismatch','malformed','wrong_run')),
  recorded_at TEXT NOT NULL CHECK (length(recorded_at) > 0)
);
CREATE TRIGGER task_gate_denial_valid BEFORE INSERT ON task_gate_binding_denials
WHEN NOT EXISTS (SELECT 1 FROM tasks t JOIN task_approval_gates g ON g.task_id=t.id
                 WHERE t.id=NEW.task_id AND g.approval_id=NEW.approval_id
                 AND g.resolved_at IS NULL AND t.state='awaiting_approval')
BEGIN SELECT RAISE(ABORT, 'invalid gate binding denial'); END;
CREATE TRIGGER task_gate_denial_no_update BEFORE UPDATE ON task_gate_binding_denials
BEGIN SELECT RAISE(ABORT, 'gate binding denials are immutable'); END;
CREATE TRIGGER task_gate_denial_no_delete BEFORE DELETE ON task_gate_binding_denials
BEGIN SELECT RAISE(ABORT, 'gate binding denials are immutable'); END;
DROP TRIGGER task_gate_fail_requires_denied;
CREATE TRIGGER task_gate_fail_requires_denied BEFORE UPDATE OF state ON tasks
WHEN OLD.state='awaiting_approval' AND NEW.state='failed'
 AND NOT EXISTS (SELECT 1 FROM task_approval_gates g JOIN approval_requests a ON a.id=g.approval_id
                 WHERE g.task_id=OLD.id AND g.resolved_at IS NULL
                 AND (a.state IN ('rejected','expired','revoked')
                      OR (a.state IN ('approved','requested') AND julianday(a.expires_at) <= julianday('now'))
                      OR EXISTS (SELECT 1 FROM task_gate_binding_denials d
                                 WHERE d.task_id=g.task_id AND d.approval_id=g.approval_id)))
BEGIN SELECT RAISE(ABORT, 'failure requires an unresolved decided gate'); END;
