-- A running task may park in awaiting_approval, linked to exactly one approval.
-- tasks.state has no CHECK constraint, so no table rebuild is needed; the new
-- state is enforced by the link table and triggers below. Existing rows are
-- untouched (no task can already be gated).
CREATE TABLE task_approval_gates (
  task_id TEXT PRIMARY KEY REFERENCES tasks(id),
  approval_id TEXT NOT NULL UNIQUE REFERENCES approval_requests(id),
  run_id TEXT NOT NULL REFERENCES runs(id),
  created_at TEXT NOT NULL,
  resolved_at TEXT
);
CREATE TRIGGER task_gate_valid BEFORE INSERT ON task_approval_gates
WHEN NOT EXISTS (SELECT 1 FROM tasks t JOIN approval_requests a ON a.run_id=t.run_id
                 WHERE t.id=NEW.task_id AND t.run_id=NEW.run_id AND a.id=NEW.approval_id
                 AND t.state='running' AND a.state='requested')
BEGIN SELECT RAISE(ABORT, 'invalid task approval gate'); END;
-- The only allowed update is the one-way consumption of the gate.
CREATE TRIGGER task_gate_no_update BEFORE UPDATE ON task_approval_gates
WHEN NOT (OLD.resolved_at IS NULL AND NEW.resolved_at IS NOT NULL AND length(NEW.resolved_at)>0
          AND NEW.task_id=OLD.task_id AND NEW.approval_id=OLD.approval_id
          AND NEW.run_id=OLD.run_id AND NEW.created_at=OLD.created_at)
BEGIN SELECT RAISE(ABORT, 'task approval gates are immutable'); END;
CREATE TRIGGER task_gate_insert_unresolved BEFORE INSERT ON task_approval_gates
WHEN NEW.resolved_at IS NOT NULL
BEGIN SELECT RAISE(ABORT, 'invalid task approval gate'); END;
CREATE TRIGGER task_gate_no_delete BEFORE DELETE ON task_approval_gates
BEGIN SELECT RAISE(ABORT, 'task approval gates are immutable'); END;
CREATE TRIGGER task_gate_no_replace BEFORE INSERT ON task_approval_gates
WHEN EXISTS (SELECT 1 FROM task_approval_gates WHERE task_id=NEW.task_id OR approval_id=NEW.approval_id)
BEGIN SELECT RAISE(ABORT, 'task approval gates are immutable'); END;
-- running -> awaiting_approval requires a gate whose approval is still requested.
CREATE TRIGGER task_gate_park_requires_gate BEFORE UPDATE OF state ON tasks
WHEN OLD.state='running' AND NEW.state='awaiting_approval'
 AND NOT EXISTS (SELECT 1 FROM task_approval_gates g JOIN approval_requests a ON a.id=g.approval_id
                 WHERE g.task_id=OLD.id AND a.state='requested')
BEGIN SELECT RAISE(ABORT, 'awaiting_approval requires a requested approval gate'); END;
-- A gated task leaves awaiting_approval only through an unresolved (unconsumed)
-- gate: to running after approval; to failed after the approval was rejected,
-- expired or revoked, or is approved/requested but past its expiry (fail closed).
-- A consumed gate never authorizes anything, so a later post-verification
-- awaiting_approval wait is unaffected by an old gate.
CREATE TRIGGER task_gate_resume_requires_approved BEFORE UPDATE OF state ON tasks
WHEN OLD.state='awaiting_approval' AND NEW.state='running'
 AND NOT EXISTS (SELECT 1 FROM task_approval_gates g JOIN approval_requests a ON a.id=g.approval_id
                 WHERE g.task_id=OLD.id AND g.resolved_at IS NULL AND a.state='approved'
                 AND julianday(a.expires_at) > julianday('now'))
BEGIN SELECT RAISE(ABORT, 'resume requires an unresolved approved gate'); END;
CREATE TRIGGER task_gate_fail_requires_denied BEFORE UPDATE OF state ON tasks
WHEN OLD.state='awaiting_approval' AND NEW.state='failed'
 AND NOT EXISTS (SELECT 1 FROM task_approval_gates g JOIN approval_requests a ON a.id=g.approval_id
                 WHERE g.task_id=OLD.id AND g.resolved_at IS NULL
                 AND (a.state IN ('rejected','expired','revoked')
                      OR (a.state IN ('approved','requested') AND julianday(a.expires_at) <= julianday('now'))))
BEGIN SELECT RAISE(ABORT, 'failure requires an unresolved decided gate'); END;
