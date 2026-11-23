# Exact cache: lookup and record logic (M5.4 cache 2/3)

`synapse.application.result_cache` holds the lookup and record logic for the exact result cache. It works against a storage port (`CacheStore`). The design reference is [ADR 017](../adr/017-m5-token-optimization-and-analytics.md) D6. The key and the cacheability rules come only from [`cache_key`](exact-cache-key.md).

**Nothing is cached today.** The module is pure: no SQLite, no file or network I/O, no clock, no daemon. No runtime code calls it. The only store is `InMemoryCacheStore`, which exists for tests. The SQL storage needs migration 0017, which belongs to another workstream and does not exist yet. This change adds no migration.

## Flow

`lookup(store, request, *, verifier=None) -> CacheDecision`

1. `derive_cache_key(request)`. If it returns a bypass, the result is `outcome=bypass` with the `cache_key` reason passed through, for example `edit_task`, `approval_gated`, `bash_tool`, `dirty_worktree`, `unknown_tool_version` or `invalid_input:<field>`. The store is never read.
2. `store.get(key)`. If the store raises or is not a store, the result is `miss` / `store_error`. If it returns `None`, the result is `miss` / `not_found`.
3. Entry checks. Any failure is a `miss`:
   - `malformed_entry`: wrong type, wrong `entry_schema_version`, a bad hash, size, attempt id or marker, undecodable or wrong-shaped material, or an attribute that raises.
   - `key_mismatch`: the entry's `key_sha256` is not the requested key.
   - `entry_too_large`: the result is over `MAX_RESULT_BYTES` (64 MiB), the material is over 16 KiB, or the entry document is over 32 KiB.
   - `invalidated`: the entry has its one-way invalidation marker set.
   - `material_mismatch`: the stored key components do not hash to the key, or are not exactly the current request's components.
4. Verifier (optional). `verifier(result_sha256)` returns the artifact's current SHA-256, or `None` if the artifact is missing. The caller computes it, for example by re-hashing through `ArtifactBlobStore`. A missing artifact gives `artifact_missing`. A different value gives `corrupt_entry`. A verifier that raises gives `verifier_error`. When the verifier agrees, the hit has `artifact_hash_checked=True`.
5. `may_serve(key, CacheHit(...))` must hold. The result is then `outcome=hit` with `hit` (a `CacheHit` carrying `origin_attempt_id`), `result_sha256` and `result_size`.

`record(store, request, result: CacheResult) -> RecordOutcome`

1. If the key derivation bypasses, the result is `refused` with the same reason. Nothing is written.
2. The result fields are validated. A bad field gives `refused` / `invalid_result:<field>`, and an artifact over the size bound gives `result_too_large`.
3. `store_decision` runs. Only a `succeeded` attempt that was not budget-blocked is stored. Otherwise the result is `refused` / `budget_blocked` or `outcome_<x>`.
4. `store.put(key, entry)`. If it returns `True`, the result is `stored`. If it returns `False`, the result is `already_present` / `first_writer_wins`, and the existing entry is kept unchanged. If it raises or returns a non-bool, the result is `store_error`.

Neither function raises into the caller. A `BaseException` such as `KeyboardInterrupt` still propagates.

## What a hit does NOT mean

- A hit is **not** a verification, review, approval or mandatory check (invariants 5, 9 and 10). `CacheDecision` has the class constants `counts_as_verification`, `counts_as_review`, `satisfies_approval` and `counts_as_mandatory_check`, all `False`, plus `requires_verification = True`. They are not constructor fields and cannot be set on an instance. A test also checks that no code in the module assigns them.
- A hit returns a **reference** (artifact SHA-256 and size, plus the originating attempt), not data. The caller reads the artifact through the blob store, which re-verifies the bytes on read. It must then run every verifier, reviewer, approval gate and binding recheck on that output, exactly as it would for a fresh attempt.
- Without a verifier, `artifact_hash_checked` is `False`. The entry's internal consistency was checked, but the artifact bytes were not.

## Invalidation

Every key component is part of the key, so a change to any of them misses. The tests cover each component separately: `policy_version`, `approval_policy_sha256`, `provider`, `profile_id`, `cli_version` (tool version), `model` (including the unknown model), the tool list, `permission_mode`, settings (config), `spec_sha256`, the manifest (content and task), the repo head (base commit), `task_class`, the validators and `cache_schema_version`. Unchanged inputs hit, including when the order of the tool and validator sets is shuffled or the order of the settings keys changes.

The one change an entry may get is a one-way invalidation marker. `InMemoryCacheStore.invalidate` sets it once and never clears it. A later `record` cannot resurrect the entry, because the first writer wins.

## Not done

- No SQL storage and no migration. A `CacheStore` backed by the `result_cache` table needs migration 0017 (A's).
- Not wired into `AgentWorkflowEngine`. There is no operator flag and no `cache.lookup` / `cache.stored` events yet.
- No TTL ceiling and no eviction or retention policy. `created_marker` is an opaque caller-supplied string, because this module has no clock.
- No audited operator invalidate. `invalidate` exists only on the in-memory test store and is not part of the port.
- Restored-from-backup databases are not yet invalidated as a whole.

## Schema the storage needs (for migration 0017; not written here)

This matches ADR 017 D3:

| column | type | notes |
| --- | --- | --- |
| `key_sha256` | `TEXT PRIMARY KEY` | 64 lowercase hex, `CHECK (length(key_sha256) = 64)` |
| `entry_json` | `TEXT NOT NULL` | `CacheEntry.document()` as canonical JSON, at most `MAX_ENTRY_BYTES` (32 KiB). It holds `entry_schema_version`, `key_sha256`, `result_sha256`, `result_size`, `origin_attempt_id`, `created_marker` and `key_material` (16 fields: hashes, ids and versions only, no settings values) |
| `result_artifact_id` | `TEXT NOT NULL` | the result artifact's SHA-256 (`result_sha256`) |
| `origin_attempt_id` | `TEXT NOT NULL` | the attempt that produced the result |
| `created_at` | `TEXT NOT NULL` | the creation marker |
| `invalidated_at` | `TEXT NULL` | the one-way invalidation marker |

Required behaviour:

- `put` is `INSERT ... ON CONFLICT(key_sha256) DO NOTHING`, and it reports whether a row was inserted. That gives first-writer-wins.
- A `BEFORE UPDATE` trigger refuses every update except `invalidated_at` going from `NULL` to non-`NULL`, following the `task_gate_no_update` pattern.
- A `BEFORE DELETE` trigger refuses deletes. Entries are invalidated, never deleted.
- `get` maps a row back to a `CacheEntry` with `invalidated_marker = invalidated_at`. Any decode error should surface as an exception or as a non-`CacheEntry`, and `lookup` turns either into a miss.
- The repository is daemon-owned, like every other writer of authoritative SQLite state.
