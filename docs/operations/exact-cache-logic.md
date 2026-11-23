# Exact cache: lookup, record and SQL storage (M5.4 cache 2/3 and 3/3)

`synapse.application.result_cache` holds the lookup and record logic for the exact result cache. It works against a storage port (`CacheStore`). The design reference is [ADR 017](../adr/017-m5-token-optimization-and-analytics.md) D6. The key and the cacheability rules come only from [`cache_key`](exact-cache-key.md).

**Nothing is cached today.** The logic module is pure: no SQLite, no file or network I/O, no clock, no daemon. The SQL storage now exists: migration `0018_result_cache.sql` creates the `result_cache` table, and `synapse.persistence.result_cache_store.SqliteCacheStore` implements the port over it (see [SQL store](#sql-store-migration-0018) below). No runtime code calls either one: the engine is not wired, there is no operator flag and no events. `InMemoryCacheStore` remains for the logic tests.

## Which task classes can be cached

Only `read_only_analysis`, and only when an operator policy marks the task cacheable. ADR 017 D6 allows caching only for deterministic derived work: reduced output, manifests, and read-only analysis that an operator marked cacheable. It forbids caching "reviews, verifications and anything whose purpose is an independent check". A `validation` task is a check on a candidate, so it falls under that rule. `cache_key` previously listed `validation` as cacheable. It now bypasses with `validation_task`, like `verification_task`.

## Flow

`lookup(store, request, *, verifier=None) -> CacheDecision`

1. `derive_cache_key(request)`. If it returns a bypass, the result is `outcome=bypass` with the `cache_key` reason passed through, for example `edit_task`, `validation_task`, `approval_gated`, `bash_tool`, `dirty_worktree`, `unknown_tool_version` or `invalid_input:<field>`. The store is never read.
2. `store.get(key)`. If the store raises or is not a store, the result is `miss` / `store_error`. If it returns `None`, the result is `miss` / `not_found`.
3. Entry checks. The entry must be exactly a `CacheEntry`, not a subclass, because a subclass could override fields with properties that change between reads. Each field is read once into a local. Only those locals are checked, given to the verifier and put in the decision. Any failure is a `miss`:
   - `malformed_entry`: wrong type, wrong `entry_schema_version`, a bad hash, size, attempt id, marker or digest, undecodable or wrong-shaped material, or an attribute that raises.
   - `key_mismatch`: the entry's `key_sha256` is not the requested key.
   - `entry_too_large`: the result is over `MAX_RESULT_BYTES` (64 MiB), or the material is over `MAX_MATERIAL_BYTES` (16 KiB).
   - `invalidated`: the entry has its one-way invalidation marker set.
   - `material_mismatch`: the stored key components do not hash to the key, or are not exactly the current request's components.
   - `entry_digest_mismatch`: `entry_digest` is not the SHA-256 of the canonical JSON of `entry_schema_version`, `key_sha256`, `result_sha256`, `result_size`, `origin_attempt_id` and `created_marker`.
4. Verifier (optional). `verifier(result_sha256, result_size)` returns `ArtifactFacts(sha256, size=None)`, or `None` if the artifact is missing. The caller computes the facts, for example by re-hashing through `ArtifactBlobStore`. Possible outcomes:
   - A missing artifact gives `artifact_missing`.
   - A different hash gives `corrupt_entry`.
   - A size that was supplied and differs gives `size_mismatch`.
   - A non-`ArtifactFacts` return, or a verifier that raises, gives `verifier_error`.
   - When the verifier agrees, the hit has `artifact_hash_checked=True`.
5. `may_serve(key, CacheHit(...))` must hold. The result is then `outcome=hit` with `hit` (a `CacheHit` carrying `origin_attempt_id`), `result_sha256` and `result_size`.

`record(store, request, result: CacheResult, *, expected_key_sha256) -> RecordOutcome`

1. If the key derivation bypasses, the result is `refused` with the same reason. Nothing is written.
2. `expected_key_sha256` is a required keyword. If it differs from the key the request derives now, the result is `refused` / `key_changed`. A malformed value gives `invalid_input:expected_key_sha256`. **The daemon must pass the `key_sha256` from the `lookup` that preceded the attempt.** If anything in the key moved while the attempt ran (head, spec, policy, settings and so on), the result is then refused rather than filed under inputs it was not produced from. It is required so that a caller cannot forget it.
3. The result fields are validated. A bad field gives `refused` / `invalid_result:<field>`, and an artifact over the size bound gives `result_too_large`.
4. `store_decision` runs. Only a `succeeded` attempt that was not budget-blocked is stored. Otherwise the result is `refused` / `budget_blocked` or `outcome_<x>`.
5. The entry is built with its digest. If the material or `entry_json()` exceeds its bound, the result is `refused` / `entry_too_large`. The `entry_json()` bound is `MAX_ENTRY_BYTES`, 24 KiB, which is also the storage CHECK value. With the field bounds, it can only be reached if the material bound changes. A test checks that the worst case fits.
6. `store.put(key, entry)`. If it returns `True`, the result is `stored`. If it returns `False`, the result is `already_present` / `first_writer_wins`, and the existing entry is kept unchanged. If it raises or returns a non-bool, the result is `store_error`.

Neither function raises into the caller. A `BaseException` such as `KeyboardInterrupt` still propagates.

## What a hit does NOT mean

- A hit is **not** a verification, review, approval or mandatory check (invariants 5, 9 and 10).
  - `CacheDecision` has the class attributes `counts_as_verification`, `counts_as_review`, `satisfies_approval` and `counts_as_mandatory_check`, all `False`, plus `requires_verification = True`.
  - They are not constructor fields and cannot be set on an instance. A test checks that no code in the module assigns them.
  - Code that edits the class itself (for example `CacheDecision.counts_as_verification = True`) is out of scope: no runtime code does that, and the guarantee is about this module's code paths.
- A hit returns a **reference** (artifact SHA-256 and size, plus the originating attempt), not data.
  - The caller reads the artifact through the blob store, which re-verifies the bytes on read.
  - It must then run every verifier, reviewer, approval gate and binding recheck on that output, exactly as it would for a fresh attempt.
- Without a verifier, `artifact_hash_checked` is `False`. The entry's internal consistency was checked, but the artifact bytes were not.

## What the entry checks do and do not protect

- The material check binds the stored key components to the key and to the current request.
- The entry digest binds the result reference (artifact hash, size and originating attempt) to the key.
  - Together they catch accidental corruption, a partial rewrite, or a row edited by hand without recomputing the digest. For example, an entry that was pointed at another blob and another attempt is a miss even when a verifier for that other blob agrees. The old code served it; a regression test covers this.
  - They do **not** stop anyone who can write the store and recompute the digest. The digest is not a MAC, and there is no secret.
- The real protection is that only the daemon writes the table, together with the append-only triggers below. A store that a hostile party can write to is out of scope.

## Invalidation

Every key component is part of the key, so a change to any of them misses. The tests cover each component separately:

- `policy_version`, `approval_policy_sha256`, `provider` and `profile_id`
- `cli_version` (tool version)
- `model`, including the unknown model
- the tool list and `permission_mode`
- settings (config)
- `spec_sha256`, the manifest (content and task) and the repo head (base commit)
- `task_class`, the validators and `cache_schema_version`

The `task_class` test temporarily adds a second cacheable class, since only one exists today. Unchanged inputs hit, including when the order of the tool and validator sets is shuffled or the order of the settings keys changes.

The one change an entry may get is a one-way invalidation marker. `InMemoryCacheStore.invalidate` sets it once and never clears it. A later `record` cannot resurrect the entry, because the first writer wins.

`InMemoryCacheStore` takes a test-only `race_hook` that runs between the existence check and the insert. The concurrency test uses it. A mutation test shows that the same test sees several winners once the lock is removed.

## Not done

- Not wired into `AgentWorkflowEngine`. There is no operator flag and no `cache.lookup` / `cache.stored` events yet.
- No TTL ceiling and no eviction or retention policy. `created_marker` is an opaque caller-supplied string, because this module has no clock.
- No audited operator invalidate. `invalidate` exists on `InMemoryCacheStore` and `SqliteCacheStore` but is not part of the port, writes no audit record and nothing calls it.
- Cache writes do not append to the audit hash chain. The append-only triggers are the protection.
- Restored-from-backup databases are not yet invalidated as a whole.

## SQL store (migration 0018)

The schema is `src/synapse/persistence/migrations_sql/0018_result_cache.sql` (0017 is `attempt_spawn_marks`, ADR 024). It is the statement set below, re-verified against the real migration by `tests/test_result_cache_store.py`, with three additions that the scratch version lacked:

- every text column has `typeof(col) = 'text'`, so a BLOB with the same bytes (which `json_valid` and `length` would otherwise accept) is refused;
- `json_type(entry_json) = 'object'`;
- `invalidated_at` is bounded the same way when set.

The statements were first run in a scratch SQLite 3.54.1 database and behaved as noted under them; the tests now check the same rows against the migration. They use the built-in JSON functions and `GLOB`.

```sql
CREATE TABLE result_cache (
    key_sha256 TEXT NOT NULL PRIMARY KEY
        CHECK (length(key_sha256) = 64 AND key_sha256 NOT GLOB '*[^0-9a-f]*'),
    entry_json TEXT NOT NULL
        CHECK (length(CAST(entry_json AS BLOB)) <= 24576 AND json_valid(entry_json)),
    result_artifact_id TEXT NOT NULL
        CHECK (length(result_artifact_id) = 64 AND result_artifact_id NOT GLOB '*[^0-9a-f]*'),
    origin_attempt_id TEXT NOT NULL CHECK (length(origin_attempt_id) BETWEEN 1 AND 256),
    created_at TEXT NOT NULL CHECK (length(created_at) BETWEEN 1 AND 256),
    invalidated_at TEXT CHECK (invalidated_at IS NULL OR length(invalidated_at) BETWEEN 1 AND 256),
    CHECK (json_extract(entry_json, '$.key_sha256') IS key_sha256
       AND json_extract(entry_json, '$.result_sha256') IS result_artifact_id
       AND json_extract(entry_json, '$.origin_attempt_id') IS origin_attempt_id
       AND json_extract(entry_json, '$.created_marker') IS created_at)
);
-- First writer wins. RAISE(IGNORE) skips the new row, so the existing row is kept, the statement
-- succeeds and changes() is 0. It fires before conflict resolution, so INSERT OR REPLACE and
-- ON CONFLICT DO UPDATE cannot overwrite either (REPLACE would otherwise bypass the delete trigger).
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
```

Observed in the scratch run, with `changes()` after each statement:

| statement | result |
| --- | --- |
| first insert | 1 |
| second plain insert, same key | 0, the first row is kept |
| `INSERT OR REPLACE` with another artifact and attempt `att-evil` | 0, the first row is kept |
| `INSERT OR IGNORE` | 0 |
| `INSERT ... ON CONFLICT(key_sha256) DO UPDATE` | 0, the first row is kept |
| set `invalidated_at` | 1 |
| set it again, clear it, change `result_artifact_id`, or `DELETE` | `ABORT: result cache entries are append-only` |
| `INSERT OR REPLACE` after invalidation | 0, the row stays invalidated with the original artifact and attempt |
| NULL key (twice) | `NOT NULL constraint failed` |
| `'Z'*64`, `'A'*64` or a 63-character key | `CHECK constraint failed` |
| `entry_json` disagreeing with `result_artifact_id` or `origin_attempt_id` | `CHECK constraint failed` |
| `entry_json` not JSON, or over 24576 bytes | `CHECK constraint failed` |

Required behaviour of the SQL store:

- `put` is a plain `INSERT`. It returns `changes() == 1`: `True` means stored, and `False` means the first writer won. It must not use `RAISE(ABORT)` for the duplicate case, as `0008_verification_bindings.sql` does, because that would turn `already_present` into `store_error`.
- `get` reads **`entry_json` only**. It parses it into a `CacheEntry` and sets `invalidated_marker` from the `invalidated_at` column. That column is the one field kept outside `entry_json`, because it is the only one that changes.
- The columns `result_artifact_id`, `origin_attempt_id` and `created_at` duplicate fields of `entry_json`. They exist only for queries and joins, as in ADR 017 D3. The table CHECK makes them agree with `entry_json`, so `get` never needs them.
- Any decode error in `get` should surface as an exception or as a non-`CacheEntry`. `lookup` turns either into a miss.
- The repository is daemon-owned, like every other writer of authoritative SQLite state.

What `SqliteCacheStore` does (tests in `tests/test_result_cache_store.py`):

- `put` validates that the entry is exactly a `CacheEntry` for that key and is not already invalidated, then runs one `INSERT` inside its own `BEGIN IMMEDIATE` (or inside the caller's transaction, if one is open). The cursor's row count is the return value. Two connections racing on one key give exactly one `stored` and one `already_present`, and a second writer cannot decide while a first writer's insert is uncommitted (it needs the write lock; with `busy_timeout` 0 it fails as `store_error`) and reports `already_present` once that commits. Inside a caller's transaction, a failing `put` or `invalidate` raises, SQLite undoes only that statement, and the caller's transaction stays open with its earlier work intact; if the caller rolls back, a `put` made inside it leaves no row. The connection must use `isolation_level=None`, as `open_database` sets.
- `get` reads `entry_json` and `invalidated_at` only. It parses strictly: duplicate object keys, `NaN`/`Infinity`, a number that overflows to infinity (`1e400`) in the key material, a non-object, or any field set other than the eight `CacheEntry.entry_json()` writes raise `CorruptCacheEntry`, which `lookup` reports as a `store_error` miss. Duplicate keys matter: SQLite's `json_extract` reads the first duplicate (so the column CHECK passes), Python's `json.loads` would read the last. Field values are left to `lookup` (wrong types give `malformed_entry`, a hand-edited field gives `entry_digest_mismatch`, edited material gives `material_mismatch`).
- A hit through the SQL store is the same `CacheDecision` as before: never a verification, review, approval or mandatory check, and a lookup writes nothing (`total_changes` is unchanged).

Migration numbering: 0017 is ADR 024's `attempt_spawn_marks` (PR #41). This branch is built on it. ADR 026 plans a later migration, which must be 0019 or later. The loader refuses gaps, so 0018 cannot merge before 0017.
