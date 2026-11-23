-- Old verification records do not identify the candidate artifact or attempt.
-- Their NULL bindings are deliberately ineligible for task completion.
ALTER TABLE verification_results ADD COLUMN candidate_artifact_id TEXT REFERENCES artifacts(id);
ALTER TABLE verification_results ADD COLUMN task_attempt_id TEXT REFERENCES task_attempts(id);
CREATE TRIGGER verification_results_no_update BEFORE UPDATE ON verification_results
BEGIN SELECT RAISE(ABORT, 'verification results are immutable'); END;
CREATE TRIGGER verification_results_no_delete BEFORE DELETE ON verification_results
BEGIN SELECT RAISE(ABORT, 'verification results are immutable'); END;
-- REPLACE can bypass delete triggers when recursive_triggers is disabled.
CREATE TRIGGER verification_results_no_replace BEFORE INSERT ON verification_results
WHEN EXISTS (SELECT 1 FROM verification_results WHERE id = NEW.id)
BEGIN SELECT RAISE(ABORT, 'verification results are immutable'); END;
