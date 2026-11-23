-- Existing event rows retain unknown trace IDs.
ALTER TABLE events ADD COLUMN correlation_id TEXT;
ALTER TABLE events ADD COLUMN causation_id TEXT;
