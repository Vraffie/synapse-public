-- Artifact metadata is immutable once published, including for REPLACE when
-- recursive triggers are disabled. Blob retention is a separate concern.
CREATE TRIGGER artifacts_prevent_update BEFORE UPDATE ON artifacts
BEGIN SELECT RAISE(ABORT, 'artifacts are immutable'); END;

CREATE TRIGGER artifacts_prevent_delete BEFORE DELETE ON artifacts
BEGIN SELECT RAISE(ABORT, 'artifacts are immutable'); END;

CREATE TRIGGER artifacts_prevent_replacement BEFORE INSERT ON artifacts
WHEN EXISTS (SELECT 1 FROM artifacts WHERE id = NEW.id)
BEGIN SELECT RAISE(ABORT, 'artifacts are immutable'); END;
