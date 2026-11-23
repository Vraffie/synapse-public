-- The event log is append-only; outbox delivery metadata remains mutable.
CREATE TRIGGER events_prevent_update
BEFORE UPDATE ON events
BEGIN
    SELECT RAISE(ABORT, 'events are immutable');
END;

CREATE TRIGGER events_prevent_delete
BEFORE DELETE ON events
BEGIN
    SELECT RAISE(ABORT, 'events are immutable');
END;

CREATE TRIGGER events_prevent_replacement
BEFORE INSERT ON events
WHEN EXISTS (SELECT 1 FROM events WHERE id = NEW.id OR sequence = NEW.sequence)
BEGIN
    SELECT RAISE(ABORT, 'events are immutable');
END;
