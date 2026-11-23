# Context manifests (M5.4, record only)

`synapse.application.context_manifest` builds and records what the daemon assembled for one agent attempt (ADR 017 D5). It is a **record**, not a gate: nothing about what an agent is given, or whether it runs, depends on it. It exists so a later exact cache and the analytics views can bind to an exact input set. No cache exists yet (second half of M5.4).

## What is recorded

`ContextManifest(task_id, repo_head, worktree_clean, entries)`:

- `repo_head`: the 40-hex git SHA of the worktree head, or `None` for **unknown**.
- `worktree_clean`: `True`, `False` or `None` for **unknown**. The agent engine does not check cleanliness today, so it records `None`. Unknown is never recorded as clean and changes the hash, so a consumer that needs certainty (the cache) must treat unknown as "do not reuse".
- `entries`: `{kind, ref, sha256, byte_size, range?}` with `kind` one of `spec`, `requirement`, `file_range` (1-based inclusive `[start, end]`), `artifact`, `validation`. At most 1000 entries, `ref` up to 512 characters, `sha256` 64 lowercase hex.

The manifest hash is the SHA-256 of sorted-key compact JSON (`canonical_request_hash`). Entries are sorted, identical duplicates collapse, and two entries with the same `(kind, ref, range)` but different content are refused. The same inputs in any order give the same hash; any change to the head, the cleanliness (including unknown vs clean), a range, a hash or the task changes it.

## Where it is recorded

`AgentWorkflowEngine` records one manifest per attempt just before the provider is started, from sources the daemon owns: the task spec hash (`agent_tasks.spec_hash`), the prompt artifact id, hash and size, and the worktree head recorded at queueing. The agent never declares its own manifest. Requirement references, file ranges and validation results are supported by the module but not yet populated by any caller.

`record_manifest` runs inside the caller's transaction and writes the `context_manifests` row, a `context.manifest_recorded` event (`task_id`, `manifest_sha256`, `entry_count`) and its outbox row together. Re-recording the same manifest for the attempt is a no-op; a different one raises `ManifestConflict`. If recording fails, the engine logs and the attempt proceeds unchanged.

## Not done yet

- **No immutability triggers or unique index** on `context_manifests`. They need a migration (ADR 017 asks for the 0012/0014 pattern). Migration numbering has to be agreed with A first, because the loader requires contiguous numbers. Until then immutability is enforced only by `record_manifest`, not by the database.
- The exact result cache (`result_cache`, `cache.lookup`/`cache.stored` events, invalidation tests) is the second half of M5.4.
- Manifest-driven prompt changes and deduplication: none. Manifests do not change what is sent.
