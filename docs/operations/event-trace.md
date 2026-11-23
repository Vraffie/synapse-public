# Event trace metadata

Migration 0006 adds nullable correlation and causation fields to events. Legacy events retain nulls; their historical values cannot be reconstructed. Message-created events copy correlation from the message and causation is a message ID in the same thread, not an event ID. Replay and outbox dispatch expose both fields. Other event producers do not yet supply trace IDs. Event consumers must authorize access to trace metadata and event payloads according to scope. Apply all earlier migrations, including 0005, before 0006.
