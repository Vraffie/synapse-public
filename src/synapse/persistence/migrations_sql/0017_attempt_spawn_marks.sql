-- attempt_spawn_marks (ADR 024): durable evidence that a provider spawn MAY have begun for an attempt. The agent
-- engine commits a row immediately before it hands the attempt to the provider (before the spawn journal `begin`,
-- before any process exists). An attempt with NO row was provably never handed to a provider, which is the only
-- case startup recovery may re-queue (with a new attempt). Append-only: a mark can never be withdrawn.
CREATE TABLE attempt_spawn_marks (
  attempt_id TEXT PRIMARY KEY REFERENCES task_attempts(id),
  marked_at TEXT NOT NULL
);
CREATE TRIGGER attempt_spawn_marks_no_update BEFORE UPDATE ON attempt_spawn_marks
BEGIN SELECT RAISE(ABORT, 'attempt spawn marks are append-only'); END;
CREATE TRIGGER attempt_spawn_marks_no_delete BEFORE DELETE ON attempt_spawn_marks
BEGIN SELECT RAISE(ABORT, 'attempt spawn marks are append-only'); END;
-- REPLACE would bypass the delete trigger when recursive_triggers is off.
CREATE TRIGGER attempt_spawn_marks_no_replace BEFORE INSERT ON attempt_spawn_marks
WHEN EXISTS (SELECT 1 FROM attempt_spawn_marks WHERE attempt_id = NEW.attempt_id)
BEGIN SELECT RAISE(ABORT, 'attempt spawn marks are append-only'); END;
-- Conservative backfill: every attempt that exists before this migration may have been spawned by a daemon that
-- did not write marks, so each one is marked. Absence of a mark therefore only ever means "created after 0017 and
-- never handed to a provider".
INSERT INTO attempt_spawn_marks(attempt_id, marked_at)
  SELECT id, 'migration-0017' FROM task_attempts;
