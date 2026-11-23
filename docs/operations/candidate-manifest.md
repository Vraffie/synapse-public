# Candidate manifest

`synapse.application.candidate_manifest` implements the multi-repository candidate manifest from
ARCHITECTURE.md section 10. It is the single object that approvals, reviews and (later) deployments
bind to: a manifest names what is being proposed, and its SHA-256 changes if anything about it changes
(invariants 5 and 9). It is a library: nothing in the daemon calls it yet.

## Format (version 1)

```json
{"repos":[{"baseline_sha":"<40|64 hex>","candidate_sha":"<40|64 hex>","diff_bytes":123,
           "diff_sha256":"<64 hex>","repo_id":"catalog",
           "validations":[{"evidence_sha256":"<64 hex>|null","name":"lens","outcome":"passed"}]}],
 "run_id":"run-1","v":1}
```

- Canonical form: sorted keys, no whitespace, ASCII escapes, UTF-8 (`CandidateManifest.canonical_bytes()`).
  `manifest_sha256` is the SHA-256 of exactly those bytes. There is no timestamp, so the hash is reproducible.
- Strict: exact keys (unknown or missing fields rejected), `repo_id`/validation `name` match
  `[A-Za-z0-9][A-Za-z0-9._-]{0,63}`, SHAs are full lowercase hex, `outcome` is `passed|failed|skipped`,
  repositories sorted by id and unique (1 to 16), validations sorted by name and unique (at most 32 per repo),
  whole manifest at most 48 KiB. Duplicate JSON keys, `NaN`/`Infinity`, booleans in integer fields and any
  non-canonical encoding are refused when parsing (`manifest_from_bytes`).
- `diff_sha256` is the SHA-256 of the UTF-8 text of `git diff --no-ext-diff --no-textconv baseline candidate`
  as the worktree manager returns it (invalid UTF-8 is decoded with replacement, binary files appear as git's
  "Binary files differ" line). The two commit SHAs are the authoritative binding; the diff hash covers the
  reviewable text. `diff_bytes` is its length.

## API

- `build_manifest(run_id, repos)`: pure; sorts repositories and validates. Facts are injected.
- `collect_repo_candidate(worktree_manager, run_id, task_id, repo, baseline_ref, *, repo_id=None, validations=(), max_diff_bytes=60000)`
  reads one task's worktree through `WorktreeManager` (read-only) and returns a `CollectedCandidate`
  (`candidate` plus the exact `diff` bytes). It refuses: a repository that does not own the worktree, a dirty
  tree (uncommitted **or untracked** files), a candidate that does not descend from the baseline, a diff over
  the limit (a truncated diff cannot be bound to) and a tree that changed while it was read. The candidate is
  the committed `HEAD`; uncommitted work is never part of a manifest. Validation results are supplied by the
  caller; nothing here runs validators.
- `publish_diff(...)` stores a collected diff as an artifact; its blob address equals `diff_sha256`.
- `publish_manifest(db, blobs, run_id, manifest, principal, idempotency_key)` stores the canonical bytes as an
  immutable `application/json` artifact with provenance `{kind: candidate_manifest, run_id, manifest_sha256, v}`.
  The artifact id is derived from `(run_id, idempotency_key)`, so a retry with the same key replays, and the
  same key with a different manifest conflicts. The artifact's `sha256` equals `manifest_sha256`.
- `verify_manifest_artifact(db, blobs, artifact_id, *, expected_manifest_sha256=None, run_id=None, verify_diffs=False)`
  re-reads the stored bytes and proves: the row is a candidate manifest, the blob matches its content address,
  the bytes parse strictly and are canonical, the recomputed hash equals the recorded one, the expected hash
  (what an approval recorded) and run when given, and, with `verify_diffs`, that every repository's diff blob
  exists with the recorded digest and size. Raises `ManifestError` or `ArtifactIntegrityError`.

No migration is needed: it uses the existing `artifacts` table, whose rows are immutable.

## What approvals should bind to

Record `manifest_sha256` as the approval's candidate hash (`candidate_manifest_sha256` on approval requests),
and have every consumer call `verify_manifest_artifact(..., expected_manifest_sha256=<recorded>)` immediately
before acting. Because the hash covers every repository's baseline and candidate commit, diff digest and
validation outcomes, any change after approval invalidates it.

Integration with the approval service is not done here: nothing yet creates approvals from a manifest or
rechecks one at execution time.

## Not done

- The cross-repository integration barrier (dependency compatibility, "baseline moved" invalidation).
- Running validators, the Catalog Validator adapter and the Domain plugin; this module only records their outcomes.
- Push, merge, protected-ref changes or deployment.
- A multi-repo registry (M6.1): `repo_id` is whatever the caller passes (default: the directory name).
- Wiring into the daemon, API, CLI or desktop.

## Added to `WorktreeManager`

Three small read-only methods used by `collect_repo_candidate`: `worktree_repo(run_id, task_id)`,
`descends_from(run_id, task_id, sha)` and `diff_since(run_id, task_id, sha, max_bytes=...)`. They go through
the same hardened git path (`_tree_git`: no external diff/textconv, attributes pinned to the empty tree).
An unknown commit makes `descends_from` raise `WorktreeError` (fails closed) rather than return `False`.
