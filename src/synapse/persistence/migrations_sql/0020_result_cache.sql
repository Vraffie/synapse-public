-- M5.4 exact result cache (ADR 017 D3/D6), storage for synapse.application.result_cache.
-- Schema verified in docs/operations/exact-cache-logic.md, plus typeof() = 'text' checks so a BLOB
-- cannot stand in for text and json_type() = 'object' on entry_json.
-- entry_json is the authoritative record; the other columns are query-only copies that the table
-- CHECK binds to it. invalidated_at is the one field outside entry_json, and the only one that may
-- change: once, from NULL to a value.
CREATE TABLE result_cache (
    key_sha256 TEXT NOT NULL PRIMARY KEY
        CHECK (typeof(key_sha256) = 'text' AND length(key_sha256) = 64
               AND key_sha256 NOT GLOB '*[^0-9a-f]*'),
    entry_json TEXT NOT NULL
        CHECK (typeof(entry_json) = 'text' AND length(CAST(entry_json AS BLOB)) <= 24576
               AND json_valid(entry_json) AND json_type(entry_json) = 'object'),
    result_artifact_id TEXT NOT NULL
        CHECK (typeof(result_artifact_id) = 'text' AND length(result_artifact_id) = 64
               AND result_artifact_id NOT GLOB '*[^0-9a-f]*'),
    origin_attempt_id TEXT NOT NULL
        CHECK (typeof(origin_attempt_id) = 'text' AND length(origin_attempt_id) BETWEEN 1 AND 256),
    created_at TEXT NOT NULL
        CHECK (typeof(created_at) = 'text' AND length(created_at) BETWEEN 1 AND 256),
    invalidated_at TEXT
        CHECK (invalidated_at IS NULL
               OR (typeof(invalidated_at) = 'text' AND length(invalidated_at) BETWEEN 1 AND 256)),
    CHECK (json_extract(entry_json, '$.key_sha256') IS key_sha256
       AND json_extract(entry_json, '$.result_sha256') IS result_artifact_id
       AND json_extract(entry_json, '$.origin_attempt_id') IS origin_attempt_id
       AND json_extract(entry_json, '$.created_marker') IS created_at)
);

-- First writer wins. RAISE(IGNORE) skips the new row, so the existing row is kept, the statement
-- succeeds and changes() is 0. It fires before conflict resolution, so INSERT OR REPLACE and
-- ON CONFLICT DO UPDATE cannot overwrite either (REPLACE would otherwise bypass the delete trigger).
-- Not RAISE(ABORT): a duplicate must read as already_present, not as a store error.
CREATE TRIGGER result_cache_first_writer_wins BEFORE INSERT ON result_cache
WHEN EXISTS (SELECT 1 FROM result_cache WHERE key_sha256 = NEW.key_sha256)
BEGIN SELECT RAISE(IGNORE); END;

-- The only permitted update: invalidated_at from NULL to a value, once, nothing else changing.
CREATE TRIGGER result_cache_no_update BEFORE UPDATE ON result_cache
WHEN NOT (OLD.invalidated_at IS NULL AND NEW.invalidated_at IS NOT NULL
          AND NEW.key_sha256 IS OLD.key_sha256 AND NEW.entry_json IS OLD.entry_json
          AND NEW.result_artifact_id IS OLD.result_artifact_id
          AND NEW.origin_attempt_id IS OLD.origin_attempt_id AND NEW.created_at IS OLD.created_at)
BEGIN SELECT RAISE(ABORT, 'result cache entries are append-only'); END;

CREATE TRIGGER result_cache_no_delete BEFORE DELETE ON result_cache
BEGIN SELECT RAISE(ABORT, 'result cache entries are append-only'); END;
