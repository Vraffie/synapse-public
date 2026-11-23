-- A managed run's fixed fixture and verifier version are immutable.
CREATE TABLE mock_run_config (
  run_id TEXT PRIMARY KEY REFERENCES runs(id),
  profile_id TEXT NOT NULL REFERENCES profiles(id),
  fixture TEXT NOT NULL CHECK(fixture = 'fixed-v1')
);
CREATE TRIGGER mock_config_no_update BEFORE UPDATE ON mock_run_config
BEGIN SELECT RAISE(ABORT, 'mock run configuration is immutable'); END;
CREATE TRIGGER mock_config_no_delete BEFORE DELETE ON mock_run_config
BEGIN SELECT RAISE(ABORT, 'mock run configuration is immutable'); END;
CREATE TRIGGER mock_config_no_replace BEFORE INSERT ON mock_run_config
WHEN EXISTS (SELECT 1 FROM mock_run_config WHERE run_id=NEW.run_id)
BEGIN SELECT RAISE(ABORT, 'mock run configuration is immutable'); END;
