# Artifact blobs

`ArtifactBlobStore` stores bytes under a SHA-256 name in an existing private user-owned directory on a local filesystem. It limits blob size, writes and fsyncs a temporary file, atomically publishes a digest-named file, and verifies the content hash when reading or reusing an existing blob. A failed upload removes its temporary file when the process remains alive. Process termination can leave a temporary file; safe cleanup and garbage collection are not yet implemented.

The caller must authenticate and authorize access, persist artifact metadata and provenance, and coordinate database and blob-store backups. Publishing a blob and committing SQLite metadata are separate operations. A failed or interrupted metadata transaction can leave an unreferenced blob; do not delete blobs without a future reference-aware recovery procedure. Reads return bounded bytes in memory and require the same or a larger configured limit as the stored blob.

When a digest-named regular file is corrupt, a subsequent upload of verified matching content preserves the damaged inode under a unique `.corrupt-<digest>-<id>` name, fsyncs that directory entry, then atomically replaces the digest name. The returned reference sets `repaired` and a warning identifies the preserved copy. If preservation fails, the existing blob is left in place and the upload fails. Corrupt copies need an explicit future retention and cleanup policy.

Concurrent repairers can preserve a blob another writer has already repaired, so a `.corrupt-*` copy may contain valid bytes. Verify a preserved copy's digest before diagnosing it or applying a future retention policy.

If a concurrent replacement makes a quarantine link fail with a missing-file error, the writer rechecks the digest and retries preservation up to three times. Persistent races fail without replacing an unpreserved blob; other preservation errors also fail closed.

Artifact publication persists metadata, event, outbox and idempotent response atomically after blob publication. A failed transaction can leave an orphan. The caller must authorize every invocation, including retries; retries may republish or repair bytes before the idempotency lookup. Migration 0005 enforces immutable artifact metadata, including against INSERT OR REPLACE. Creation events lack run scope, so do not expose them without scoped authorization and event filtering.
