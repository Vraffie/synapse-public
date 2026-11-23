-- A committed intent survives a crash before synthetic workers stop.
CREATE TABLE mock_cancellation_requests (
  run_id TEXT PRIMARY KEY REFERENCES mock_run_config(run_id),
  requested_at TEXT NOT NULL
);
CREATE TRIGGER mock_cancellation_no_update BEFORE UPDATE ON mock_cancellation_requests
BEGIN SELECT RAISE(ABORT, 'mock cancellation requests are immutable'); END;
CREATE TRIGGER mock_cancellation_no_delete BEFORE DELETE ON mock_cancellation_requests
BEGIN SELECT RAISE(ABORT, 'mock cancellation requests are immutable'); END;
CREATE TRIGGER mock_cancellation_no_replace BEFORE INSERT ON mock_cancellation_requests
WHEN EXISTS (SELECT 1 FROM mock_cancellation_requests WHERE run_id=NEW.run_id)
BEGIN SELECT RAISE(ABORT, 'mock cancellation requests are immutable'); END;
