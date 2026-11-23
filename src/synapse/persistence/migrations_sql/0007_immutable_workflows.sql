-- Published workflow versions are immutable, including INSERT OR REPLACE.
CREATE TRIGGER workflow_versions_prevent_update BEFORE UPDATE ON workflow_versions
BEGIN SELECT RAISE(ABORT, 'workflow versions are immutable'); END;
CREATE TRIGGER workflow_versions_prevent_delete BEFORE DELETE ON workflow_versions
BEGIN SELECT RAISE(ABORT, 'workflow versions are immutable'); END;
CREATE TRIGGER workflow_versions_prevent_replacement BEFORE INSERT ON workflow_versions
WHEN EXISTS (SELECT 1 FROM workflow_versions WHERE workflow_id=NEW.workflow_id AND version=NEW.version)
BEGIN SELECT RAISE(ABORT, 'workflow versions are immutable'); END;
