CREATE TABLE message_artifacts (
  message_id TEXT NOT NULL REFERENCES messages(id),
  artifact_id TEXT NOT NULL REFERENCES artifacts(id),
  ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
  PRIMARY KEY(message_id, ordinal),
  UNIQUE(message_id, artifact_id)
);
CREATE TRIGGER messages_prevent_update BEFORE UPDATE ON messages
BEGIN SELECT RAISE(ABORT, 'messages are immutable'); END;
CREATE TRIGGER messages_prevent_delete BEFORE DELETE ON messages
BEGIN SELECT RAISE(ABORT, 'messages are immutable'); END;
CREATE TRIGGER messages_prevent_replacement BEFORE INSERT ON messages
WHEN EXISTS (SELECT 1 FROM messages WHERE id = NEW.id)
BEGIN SELECT RAISE(ABORT, 'messages are immutable'); END;
CREATE TRIGGER message_artifacts_prevent_update BEFORE UPDATE ON message_artifacts
BEGIN SELECT RAISE(ABORT, 'message artifacts are immutable'); END;
CREATE TRIGGER message_artifacts_prevent_delete BEFORE DELETE ON message_artifacts
BEGIN SELECT RAISE(ABORT, 'message artifacts are immutable'); END;
CREATE TRIGGER message_artifacts_prevent_replacement BEFORE INSERT ON message_artifacts
WHEN EXISTS (
  SELECT 1 FROM message_artifacts
  WHERE message_id = NEW.message_id AND (ordinal = NEW.ordinal OR artifact_id = NEW.artifact_id)
)
BEGIN SELECT RAISE(ABORT, 'message artifacts are immutable'); END;
CREATE TABLE message_artifact_seals (
  message_id TEXT PRIMARY KEY REFERENCES messages(id)
);
INSERT INTO message_artifact_seals(message_id) SELECT id FROM messages;
CREATE TRIGGER message_artifacts_prevent_late_insert BEFORE INSERT ON message_artifacts
WHEN EXISTS (SELECT 1 FROM message_artifact_seals WHERE message_id=NEW.message_id)
BEGIN SELECT RAISE(ABORT, 'message artifacts are sealed'); END;
CREATE TRIGGER message_artifact_seals_prevent_delete BEFORE DELETE ON message_artifact_seals
BEGIN SELECT RAISE(ABORT, 'message artifact seals are immutable'); END;
CREATE TRIGGER message_artifact_seals_prevent_update BEFORE UPDATE ON message_artifact_seals
BEGIN SELECT RAISE(ABORT, 'message artifact seals are immutable'); END;
CREATE TRIGGER message_artifact_seals_prevent_replacement BEFORE INSERT ON message_artifact_seals
WHEN EXISTS (SELECT 1 FROM message_artifact_seals WHERE message_id=NEW.message_id)
BEGIN SELECT RAISE(ABORT, 'message artifact seals are immutable'); END;
