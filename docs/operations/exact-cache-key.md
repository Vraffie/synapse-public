# Exact cache key and cacheability rules (M5.4 cache 1/2)

`synapse.application.cache_key` decides whether a task result may be looked up in or stored to an exact cache, and if so derives the key. The design reference is [ADR 017](../adr/017-m5-token-optimization-and-analytics.md) D6.

**Nothing is cached today.** The module is pure: it stores nothing, does no I/O, and no runtime code calls it. The storage half (the `result_cache` table from ADR 017 D3 and the `cache.lookup` / `cache.stored` events from D2) needs a migration. That migration is blocked on numbering: 0017 belongs to another workstream and the loader requires contiguous version numbers (`persistence/migrations.py`). So you can't enable the cache: there is no flag and no storage. When the storage half lands, it will be off by default and behind an operator flag (ADR 017, first slice).

## API

- `derive_cache_key(CacheKeyInputs) -> CacheKey | CacheBypass`. It never raises. Malformed or oversized input returns `CacheBypass("invalid_input:<field>")`, so a caller that only acts on `CacheKey` fails closed.
- `store_decision(decision, outcome=..., budget_blocked=...) -> CacheBypass | None`. `None` means the finished attempt's result may be stored.
- `may_serve(decision, CacheHit) -> bool` is true only when the current attempt is cacheable and the stored key is exactly its key.
- `CacheHit(key_sha256, origin_attempt_id)` has class constants `counts_as_verification = False`, `satisfies_approval = False` and `requires_verification = True`. A hit is reused output, never evidence. Verifiers still run on the candidate, and approvals are still required.

All `CacheKeyInputs` fields are daemon-owned facts. The agent never supplies any of them.

## Bypass reasons (never cached, never served)

| reason | when |
| --- | --- |
| `edit_task`, `review_task`, `verification_task`, `independent_check_task` | the task class is one of these, even if an operator marked it cacheable |
| `unknown_task_class` | the class is not `read_only_analysis` or `validation` |
| `not_marked_cacheable` | the operator policy did not set `operator_marked_cacheable=True` (the default is no) |
| `approval_gated` | the task is approval-gated (any value other than `False`) |
| `bash_tool` | any `Bash` tool, including `Bash(...)` patterns |
| `write_capable_tool` | `Edit`, `Write`, `MultiEdit`, `NotebookEdit`, `WebFetch`, `WebSearch`, `Task` |
| `unknown_tool` | anything outside the allowlist `Read`, `Grep`, `Glob`, `LS` (scoped forms such as `Read(src/**)` included) |
| `write_capable_mode`, `unknown_mode` | permission mode `acceptEdits`, `bypassPermissions`, `auto`, or one that is not recognised. Allowed: `plan`, `dontAsk`, `manual` |
| `unknown_head`, `unknown_cleanliness`, `dirty_worktree` | the manifest head is unknown, or the worktree is not known to be clean |
| `unknown_tool_version` | `cli_version` is `None` |
| `invalid_input:settings_nodes` | settings exceed the node cap (breadth bound) |
| `credential_in_settings` | a settings key name looks like a credential (token, secret, password, api key, auth, cookie, session, private key) |
| `budget_blocked`, `outcome_unknown`, `outcome_failed`, `outcome_cancelled`, `outcome_timed_out`, `outcome_unrecognised` | `store_decision` only. Only a `succeeded` attempt that was not budget-blocked may be stored |

## Key

The key is the SHA-256 of canonical JSON over the following material:

- `cache_schema_version`
- `policy_version`
- `approval_policy_sha256`
- `provider`
- `profile_id`
- `cli_version`
- `model`: `None` means unknown, and that is distinct from any named model. This is a deliberate tradeoff, following ADR 017 D6 ("model (if known)"): an unknown model still caches, but only against other unknown-model attempts, so a hit can never cross to or from a named model. If the CLI silently changes its default model while reporting none, such attempts would share a key; set an explicit model where that matters
- the sorted, de-duplicated `allowed_tools`
- `permission_mode`
- `settings_sha256`
- `spec_sha256`
- `manifest_sha256` (see [context-manifest](context-manifest.md))
- `repo_head`
- `worktree_clean` (always true in a key)
- `task_class`
- the sorted, de-duplicated `validator_sha256s`

A change to any one of these gives a different key, and the tests check each one. Matching is exact: apart from set ordering, nothing is normalised, so case or whitespace changes miss. There is no semantic reuse. A key built from the prompt alone is impossible, because both the spec hash and the manifest are required.

Settings are hashed, and only the digest enters the material. A secret value under an innocent key name changes the key, but it never appears in the key. `CacheKey.material` holds only hashes, ids and versions, so it is safe for a `cache.lookup` event.

## Bounds

- Text fields: up to 256 characters, with no control characters.
- `allowed_tools`: 1 to 64 entries.
- `validator_sha256s`: up to 64 entries.
- Hashes: 64 lowercase hex characters.
- Settings: a JSON-compatible mapping with string keys, nesting depth up to 8, at most 2048 nodes (every mapping, list and scalar counts), at most 64 KiB encoded. The node cap is enforced during the walk, before any serialisation, so a huge flat mapping or list bypasses as `invalid_input:settings_nodes` after at most 2049 steps. NaN and non-JSON values are refused.

## Not done (the storage half, 2/2)

- The `result_cache` table, with the one-way `invalidated_at` trigger.
- The `cache.lookup` / `cache.stored` events.
- The audited operator invalidation.
- The TTL ceiling.
- Invalidating every entry when restoring from backup.
- Wiring into `AgentWorkflowEngine` and the operator flag.

The `outcome_*` and `budget_blocked` checks in `store_decision` are ready for that wiring. ADR 017 open question 1 still stands: which task classes, if any, will be cacheable in v1.
