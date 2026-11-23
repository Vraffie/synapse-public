# ADR 026: Multi-repository runs and the candidate manifest as the binding object

Status: Proposed (design only; no code, no migration in this change). Covers backlog M6.1 (multi-repository registry, baseline refs, protected paths, one worktree per task attempt per repo, retention cleanup) and M6.2 wiring (the candidate manifest stored immutably as the single object that approvals, review and deployment bind to). Builds on ADR 019 (privilege boundary), ADR 021 (side-effect ledger), ADR 022 (approval environment binding), ADR 023 (spawn journal and worker fence) and ADR 025 (independent review, proposed on `a/review-independence`). Local single machine only, Linux only; no network or multi-host design.

## Context

Today a run works on ONE repository. `agent_service.start` takes one `repo` and one `base_ref`, calls `WorktreeManager.check_base`, then `WorktreeManager.create(run_id, task_id, repo, base_ref)` per task. The worktree path is `<worktree_root>/<run_id>/<task_id>` and the branch is `synapse/<run_id>/<task_id>`; neither carries a repository, so a second `create` for another repo under the same `(run, task)` is refused (`docs/operations/worktrees.md`). A retry of a task reuses the same worktree; there is no per-attempt tree. The allowlist is a flat list of paths in `agent.json` (`repos` plus `worktree_root`), written only by the operator (by hand or `synapse workspace discover`) and read once at daemon start. Commits are host-side and operator-requested (`agent-worktree-commit` → `WorktreeManager.commit`), never made by the agent. Removal is on request only; nothing expires.

The candidate manifest library (`application/candidate_manifest.py`, format v1) already hashes `{run_id, repos[{repo_id, baseline_sha, candidate_sha, diff_sha256, diff_bytes, validations}]}` and is idempotently published to the immutable `artifacts` table. `approval_requests.candidate_manifest_sha256` exists and is immutable by trigger (0010), but nothing creates an approval from a manifest, `repo_id` is caller-supplied (default: directory name), and no consumer rechecks the manifest before acting.

The target use is a Domain change that spans three repositories: `product-config`, `portfolio` and `tenant-specification`. One change must be reviewed, approved and later deployed as one thing, even though git has no cross-repository commit.

Threat model (unchanged from ADR 019): one uid. Agents run in the bwrap sandbox (`providers/sandbox_launcher.py`): `/` read-only, only the scratch HOME and the task worktree writable, the repository's common `.git` mounted read-only with `config`/`credentials` masked. With `--allow-same-user-agents` and the sandbox off, an agent can edit anything the daemon can, including SQLite, the config file and refs. Nothing below is a defence against that case; it is stated per decision where it matters.

## Decision

### D1. Registry storage: operator-owned config file, snapshotted per run into SQLite

The registry stays in the operator's config file, not in the database and not behind any API. The `repos` key of `agent.json` accepts, in addition to the current plain path strings, objects:

```json
{"repos": [
  {"id": "product-config", "path": "/home/op/work/product-config",
   "baseline_refs": ["refs/heads/main"], "default_baseline_ref": "refs/heads/main",
   "protected_paths": [".github/**", "CODEOWNERS", ".gitmodules"],
   "retention": {"keep_days": 14}},
  "/home/op/work/legacy-repo"
], "worktree_root": "/home/op/.local/state/synapse/worktrees"}
```

- `id` matches the existing safe-id rule, is unique and is what manifests, approvals and the API use. It is required in the object form so it never silently changes when a folder is renamed. A plain string keeps working for single-repo runs; its id is derived from the basename and two strings with the same basename refuse daemon start (fail closed) instead of being disambiguated.
- All existing allowlist rules apply unchanged: absolute, normalized, no symlinks, owned by the daemon uid, real `.git` directory, not nested, no overlap with `worktree_root` or the state dir, `refuse_unsafe_repo_config` at start, before each run and before every git call on a tree. Maximum 16 repositories (the manifest's limit; discovery's 32 is lowered for object entries).
- The daemon computes `registry_sha256` over the canonical JSON of the validated entries at start and logs it in the startup audit event.
- **Who may edit:** only the operator, by editing the file (or the operator-only `synapse workspace discover --write`) and restarting the daemon. No route, MCP tool, desktop action or agent run can add, remove or change an entry; the daemon never writes the file. This keeps the invariant `docs/operations/workspace.md` already states.
- **Snapshot per run:** at run creation the entries the run uses are copied into `run_repo_bindings` (D2) together with their own entry hash. A registry edit plus restart never changes a run already created; a run whose repository was removed from the registry can no longer start new attempts (refused `repo_not_registered`) but its manifests still verify.

Why not a database table as the source of truth: an editable table invites an edit route, and the API surface is the thing we want to keep at zero. SQLite is writable by the same uid anyway (F1), so it adds no protection over a file. Why not a separate `repos.json`: one file, one reload rule, one place the operator already edits; an open question asks the owner.

Sandbox note: the config directory is outside the sandbox's writable binds, so a sandboxed agent cannot edit the registry. With the sandbox off it can, and the next restart would load the change; the per-run snapshot and the startup audit hash make such a change visible, not impossible.

### D2. Baseline refs are pinned to SHAs once, at run creation

- A run request names, per repository, a `baseline_ref` that must be one of that entry's `baseline_refs` (default: only `default_baseline_ref`; an entry with neither refuses multi-repo runs). Free-form refs, remote-tracking refs and SHAs are refused for multi-repo runs in the first slice.
- The daemon resolves each ref once with the existing `_resolve_base` (`rev-parse --verify --end-of-options <ref>^{commit}`) and writes `(run_id, repo_id, baseline_ref, baseline_sha)` to `run_repo_bindings` in the same transaction that queues the run. All resolutions happen before the transaction; if any repository fails, nothing is written and no worktree exists.
- Every attempt of every task cuts its worktrees from the pinned `baseline_sha`, never from the ref again. The manifest carries both `baseline_ref` and `baseline_sha`.
- If the ref later moves (the owner commits on `main` during a run), nothing is rebased. The M6.6 integration barrier compares the current ref value with the pinned SHA and marks the manifest stale (D8), which stops new approvals on it. Detection, not prevention: the owner's own commits are legitimate and indistinguishable from a same-uid agent's.

### D3. Protected refs and protected paths: one enforcement point that decides, others that detect

Synapse writes refs in exactly one namespace per repository: `refs/heads/synapse/<run_id>/<task_id>/<attempt_no>` (D4). Every other ref is protected by construction: `WorktreeManager` gains a single ref-write guard that refuses any branch create, update or delete outside `refs/heads/synapse/`, and it never pushes, merges or touches the operator's checkout. That covers protected refs without a per-repo list; there is deliberately no `protected_refs` registry key in the first slice.

Protected paths (`protected_paths`, gitignore-style globs, matched with git's own pathspec matching, `:(glob)` magic, against repository-relative paths) are enforced at these points:

1. **Deciding point: the host-side commit** (`WorktreeManager.commit`, D4 step 4). The staged path list (it is already computed for the gitlink and `.gitmodules` refusals) is matched against the repository's protected paths; any match refuses the whole commit for that repository with `protected_path_changed` and the paths' count (never the paths in events). This is the right place because the host commit is the only way a change becomes a candidate SHA: the agent never commits.
2. **Detecting point: manifest build.** `collect_repo_candidate` additionally runs `git diff --name-only --no-renames baseline candidate` (bounded) and refuses if any changed path is protected. This catches a candidate produced any other way (a same-uid agent with the sandbox off writing objects and refs itself).
3. **Detecting point: consumption.** Every consumer (approval decision, review ticket, future deploy step) re-runs `verify_manifest_artifact(..., verify_diffs=True)`, which repeats check 2 against the registry snapshot in `run_repo_bindings`.

Prevention in the sandbox: the common `.git` is already mounted read-only, so a sandboxed agent cannot write objects or refs in the shared repository at all (`git add`/`git commit` inside the sandbox fail; this is already true today and is why commits are host-side). Protected paths can still be edited in the working tree; they are refused at commit, which is when it matters.

### D4. Worktree lifecycle: one worktree per (task attempt, repository)

Layout and names:

- Path: `<worktree_root>/<run_id>/<task_id>/<attempt_no>/<repo_id>`.
- Branch, in each repository: `synapse/<run_id>/<task_id>/<attempt_no>`. The repository is not in the branch name because each repository has its own ref namespace. Legacy single-repo runs keep `synapse/<run_id>/<task_id>`; a new run has a new id, so the two shapes never meet in one ref directory (no D/F ref conflict).
- `_contained` (which assumes depth 2) and `worktree_path` change to the new depth for multi-repo runs; legacy depth-2 trees keep working.

Per attempt, in order:

1. **Create all, or none.** Before the attempt is spawned, the daemon creates one worktree per repository the task declares (D5), from the pinned baseline. Per-repo locks are taken in ascending `repo_id` order (deadlock-free; the current per-repo `flock` is kept). If any `create` fails, the ones created for this attempt are removed and the attempt fails `worktree_create_failed:<repo_id>` without spawning. A row per created worktree goes to `attempt_worktrees` before the spawn.
2. **Spawn once.** One process per attempt, as today. Its cwd is `<...>/<attempt_no>/`, and the sandbox gets one writable bind per write-access repository worktree and a read-only bind for each read-access one; each repository's common `.git` is read-only and masked as today (`git_readonly_dirs` is called per worktree with that repository's entry only). The spawn journal (ADR 023) is unchanged: one entry per spawn, not per repository.
3. **Run.** The agent edits files only. It cannot reach a repository it was not given (not mounted) and cannot commit.
4. **Host commit on completion.** When the attempt ends `completed`, the daemon commits each write-access worktree on its attempt branch (`WorktreeManager.commit`, with the D3 check), in ascending `repo_id` order, writing a commit intent before and the result after (D6). A repository with no changes gets `candidate_sha = baseline_sha`. This is a change from today's operator-requested commit, and it is safe for the same reason today's is: the attempt branch is Synapse-owned and nothing outside `refs/heads/synapse/` moves. Single-repo legacy runs keep the operator-requested behaviour. (Open question 2.)
5. **Retry is a new attempt from the baseline.** A retry gets new worktrees at `attempt_no + 1`, cut from the pinned baseline, not from the previous attempt's tree. The previous attempt's trees stay for evidence and review until retention (D10). "Continue from attempt n" is future work and would be an explicit field, never implicit.
6. **Failed, cancelled, unknown attempts** are never committed automatically. Their trees stay for inspection.

### D5. How a task declares repositories, and why an agent cannot widen the set

- The run request declares the run set: `repos: [{repo_id, baseline_ref}]`, 1 to 16, each registered. Each task declares `repos: [{repo_id, access: "write" | "read"}]`, a non-empty subset of the run set with at least one `write` entry for an editing task (a read-only reviewer task may have only `read`). In the first slice at most one task in a run may have `write` on a given repository (open question 6).
- The declared repositories and access modes are part of the task specification, so they are covered by the full-spec approval binding hash and the spawn-time recheck that already exist. They are written once at run creation (`run_repo_bindings`, task spec) and are immutable.
- An agent cannot widen the set because every path that could widen it reads only those immutable rows: the sandbox mounts only declared worktrees; the MCP gateway has no repository tool; agent-created subtasks or handoffs (when they exist) may only name a subset of their parent's set, checked by the engine; the host commit loops over declared write repositories only; the manifest builder refuses a `repo_id` not in `run_repo_bindings` for that run, and a candidate whose `repo_id` is read-only for its source task. With the sandbox off an agent could write elsewhere on disk, but nothing outside the declared set becomes a candidate.

### D6. Cross-repository atomicity and failure

Git cannot commit three repositories atomically, so the unit of atomicity is the **manifest**, not the commits. Commits on Synapse branches are harmless when partial; only a complete manifest can be approved, reviewed or deployed.

- **Commit phase state.** `attempt_repo_commits` is append-only (like the side-effect ledger, ADR 021): an `intent` row `(attempt, repo_id, head_before)` before `git commit`, then `done (candidate_sha)` or `failed (reason)`. The phase is complete only when every write repository of the attempt has `done`.
- **Partial success.** If repository 2 of 3 fails to commit (protected path, too many files, git error), the attempt becomes `failed` with `commit_failed:<repo_id>:<reason>`; repository 1's commit stays on its branch as evidence and is not undone; no manifest can be built from that attempt (the builder requires a `done` row for every write repository of the source attempt). Recovery is a new attempt (D4.5), never a re-commit of the failed tree.
- **Crash during the commit phase.** At startup, after the fence, an `intent` without a result is resolved as follows: if the fence was `all_clear` (no surviving writer) and the attempt branch HEAD is `head_before`, the intent is `not_done` and the daemon may re-run the commit; if HEAD is a direct child of `head_before` whose tree equals the worktree's index, the commit landed and its SHA is recorded as `done`; anything else is `unknown`, the attempt is blocked and an operator decides (retry as a new attempt or abandon). Never guessed, never replayed blindly.
- **Crash during the agent run.** Unchanged from ADR 023: the fence runs, the attempt becomes `unknown`, the task is blocked, the run is `recovering`, nothing resumes, and nothing is committed from any of that attempt's worktrees. `agent-abandon` keeps its mandatory assertion. The fence is global and per spawn, so a multi-repo attempt is one journal entry and one verdict; there is no per-repository fence.
- **Deployment (M7, outline only).** Each per-repository irreversible step is a separate side-effect ledger key `(task, attempt, manifest_sha256, step="deploy:<repo_id>", environment)`. A partial deployment is visible per repository as `done`/`unknown`/`not_done`; recovery never replays (ADR 021); the order across repositories is fixed in the deployment plan (open question 5), not chosen at runtime.

### D7. Manifest v2: one object binding every repository's baseline and candidate

Version 2 of the canonical manifest (v1 stays verifiable; multi-repo approvals require v2):

```json
{"v": 2, "run_id": "...", "registry_sha256": "<sha of this run's run_repo_bindings snapshot>",
 "repos": [
   {"repo_id": "portfolio", "access": "write",
    "baseline_ref": "refs/heads/main", "baseline_sha": "...", "candidate_sha": "...",
    "source": {"task_id": "...", "attempt_no": 2},
    "diff_sha256": "...", "diff_bytes": 1234, "changed_paths": 7,
    "protected_paths_sha256": "...", "validations": [{"name": "...", "outcome": "passed", "evidence_sha256": "..."}]},
   {"repo_id": "tenant-specification", "access": "read", "baseline_ref": "refs/heads/main", "baseline_sha": "..."}
 ]}
```

- Every repository in the run set appears exactly once, sorted by `repo_id`. Read-only repositories carry only their baseline (a reviewer must know what the agent read). A write repository with no change has `candidate_sha == baseline_sha`.
- `source` names the one attempt whose committed HEAD is the candidate; it must have a `done` commit row (D6). `protected_paths_sha256` is the hash of the protected-path list that was enforced, so a later registry change is visible.
- No timestamps, paths or prompts; the existing size limits (16 repos, 32 validations each, 48 KiB) stay. `manifest_sha256` is SHA-256 of the canonical bytes, as in v1.
- Storage: the bytes stay in the immutable `artifacts` table (via `publish_manifest`); a new immutable `candidate_manifests` row indexes them per run (D9). A manifest is built by A's application service when asked (workflow node or operator route), never automatically by the agent.

### D8. Approval, review and environment binding

- An approval for any multi-repo operation (`review`, `merge`, `deploy`) is created with `candidate_manifest_sha256 = manifest_sha256`. A trigger in the migration refuses the insert unless a `candidate_manifests` row with that hash exists for the same `run_id` and is not invalidated. The binding is then immutable (existing 0010 trigger).
- Before acting, every consumer calls `verify_manifest_artifact(expected_manifest_sha256=..., verify_diffs=True)` and checks that the manifest has no invalidation row. A stale or invalid manifest denies with `manifest_stale` / `manifest_invalid`.
- Review (ADR 025): for a multi-repo run, `record_verification` binds the verdict to `manifest_sha256` instead of a single candidate artifact hash, and the independence rules apply unchanged; the evidence verifier runs per repository against the manifest's SHAs.
- Environment (ADR 022): the bound hash uses `manifest_sha256` as the candidate; nothing else changes.
- A retry produces a new manifest with a new hash, so no approval ever transfers from one candidate to another. Invalidation (`baseline_moved`, `candidate_missing`, `registry_changed`, `operator`) is an insert into `candidate_manifest_invalidations`; manifests are never updated or deleted.

### D9. Migration (A's numbering: odd; next after auto-resume's 0017 is 0019)

One migration, **0019** (number final at merge; the runner needs a contiguous sequence, so it lands after B's 0018 or takes the next free odd number):

| Table | Shape | Mutability |
|---|---|---|
| `run_repo_bindings` | `run_id, repo_id, path, baseline_ref, baseline_sha, entry_sha256, registry_sha256`, PK `(run_id, repo_id)` | insert-only (UPDATE/DELETE triggers abort) |
| `attempt_worktrees` | `attempt_id, repo_id, access, path, branch, created_at`, PK `(attempt_id, repo_id)` | insert-only |
| `attempt_worktree_removals` | `attempt_id, repo_id, removed_at, reason, forced` | insert-only |
| `attempt_repo_commits` | `attempt_id, repo_id, seq, state (intent/done/failed/not_done/unknown), head_before, candidate_sha, reason, at` | append-only transitions |
| `candidate_manifests` | `manifest_sha256` PK, `run_id, artifact_id, version, seq, created_at` | insert-only |
| `candidate_manifest_invalidations` | `manifest_sha256, reason, actor, at` | insert-only |
| trigger on `approval_requests` | BEFORE INSERT: when the run has `run_repo_bindings` rows, `candidate_manifest_sha256` must name an existing, non-invalidated `candidate_manifests` row for the same run (legacy single-repo runs have no bindings and are unaffected) | n/a |

No column is added to `runs`, `tasks` or `task_attempts` (hot tables, fingerprint churn). It must apply from empty and from the previous head, contain no `PRAGMA` or transaction statements, and pass the migration fingerprint round trip. Existing single-repo runs have no `run_repo_bindings` rows and keep working on the legacy path. If ADR 021/022's inline schemas have been promoted by then, this migration does not touch them.

### D10. Retention cleanup

- Policy per repository entry (`retention.keep_days`, default 14) counted from the run reaching a terminal state (`completed`, `failed`, `cancelled`, or abandoned).
- A worktree is never removed while: its run is not terminal (including `recovering`); the last fence had an `unverified` entry that is not cleared; its attempt is the `source` of a non-invalidated manifest that has a requested or approved, not yet consumed approval.
- The sweep runs at daemon start (after the fence and recovery) and on an operator command with `--dry-run`. It calls `WorktreeManager.remove` per `(attempt, repo)`; `force` is used only for terminal runs and is audited; each removal inserts into `attempt_worktree_removals` and writes one `agent.worktree_retention` audit event per run (counts only). Then `git worktree prune` per registered repository.
- Branches: removing a worktree deletes its attempt branch with `-D` (Synapse namespace only, D3) unless its SHA is the candidate of a manifest that is approved or deployed; those branches are kept so the diff can still be re-verified. The operator can remove them explicitly.

## What A builds and what B builds

A (core, daemon, persistence):

- Registry loader and validator for the object form, `registry_sha256`, startup audit, legacy-string compatibility.
- `WorktreeManager`: attempt-depth paths and branches, ref-write guard, protected-path check in `commit`, ordered multi-repo locking, all-or-none create.
- Sandbox: one bind per declared worktree, read-only binds for `read` repositories, per-repo `git_readonly_dirs`/masks.
- Engine and runtime: per-attempt multi-repo lifecycle, commit phase with intents, recovery rules of D6, subset check for any agent-created subtask.
- Manifest v2 builder and verifier, the `candidate_manifests` store and invalidations, the M6.6 baseline-moved check (when M6.6 lands).
- Approval insert trigger, consumer rechecks, ADR 025 and 022 adaptations.
- Retention sweep (engine and audit), migration 0019 and its tests.

B (clients, CLI, API routes, docs):

- Run creation route: accept `repos: [{repo_id, baseline_ref}]` and per-task `repos: [{repo_id, access}]`; OpenAPI contracts and validation messages (refusals defined by A).
- Read-only routes: `GET /runs/{id}/repos` (bindings and per-attempt worktrees), `GET /runs/{id}/candidate-manifests` and `GET .../{sha}`; `POST /runs/{id}/candidate-manifest` (operator-only, calls A's service); `GET /repos` (read-only registry view with `registry_sha256`).
- `agentctl`: `run --repo ID=REF ...` and per-task repo flags, `repos list`, `manifest show/build`, `worktrees prune [--dry-run]`, multi-repo `agent-worktree-commit` display.
- `synapse workspace discover --write`: emit the object form with explicit ids (operator-only, as today).
- Desktop: per-repository diff and status views, manifest view showing every repository's baseline and candidate.
- Docs: `worktrees.md`, `candidate-manifest.md`, `agent-runs.md`, `workspace.md`, API reference.

## Alternatives rejected

- **One worktree per task, reused across attempts** (today's shape): a retry inherits the failed attempt's edits and the manifest cannot name which attempt produced a candidate.
- **One combined super-repository or submodules** for the three repositories: changes the owner's repositories, and gitlinks are already refused by `commit`.
- **Agent commits inside the sandbox**: needs a writable common `.git`, which lets the agent write any ref, including protected ones.
- **Atomic cross-repo commit via a two-phase protocol**: impossible in git without a coordinator, and unnecessary when only the manifest is approved.
- **Registry in SQLite with an operator route**: adds the API surface that the operator-only rule forbids, and SQLite is same-uid writable anyway.
- **Re-resolving the baseline ref per attempt**: attempts of one run would start from different commits, and the manifest would bind to a moving target.

## Consequences and limits

- Same-uid caveat (ADR 019): with the sandbox off, an agent can edit the config file, SQLite, refs and objects. The design then detects (manifest-build and consume-time checks, snapshots, audit hashes) but does not prevent.
- Disk use grows with attempts × repositories until retention runs; the 16-repository cap and per-attempt trees bound it per run but not across runs.
- The global fence (ADR 023) still reports per daemon, not per attempt; an unverified entry blocks retention for every run until cleared.
- Automatic host commit at attempt completion (D4.4) is new behaviour for multi-repo runs only.
- M6 remains blocked on owner inputs (real repository paths, refs, protected paths, validator commands); this ADR fixes the shape so those inputs are configuration, not code.

## Open questions for the owner

1. Real `id`, path, allowed baseline refs and protected paths for `product-config`, `portfolio` and `tenant-specification`. Are there paths in them an agent must never change beyond the proposed defaults (`.github/**`, `CODEOWNERS`, `.gitmodules`)?
2. Automatic host commit to the Synapse attempt branch when an attempt completes (proposed for multi-repo runs), or keep today's operator-requested commit?
3. Read-access repositories: a read-only worktree at the pinned baseline (proposed), or no checkout at all?
4. Retention default: 14 days after the run ends? Keep branches of approved or deployed candidates indefinitely (proposed) or for a fixed period?
5. Cross-repository deployment order: fixed per workflow (for example `product-config` before `tenant-specification`), or derived from the XML dependency index (ADR 020-M6) once it exists?
6. May one task write several repositories at once (proposed: yes), and may two tasks in one run write the same repository (proposed: no in the first slice)?
7. When the owner commits on a baseline ref during a run: invalidate the manifest (proposed) or only warn?
8. Registry location: extend `agent.json` `repos` (proposed) or a separate `repos.json`?
9. Should protected refs ever be configurable per repository, or is "Synapse writes only `refs/heads/synapse/`" enough?
