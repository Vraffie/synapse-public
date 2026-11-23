# ADR 026: Multi-repository runs and the candidate manifest as the binding object

Status: Proposed (design only; no code, no migration in this change). Owner decisions of 2026-11-23 are folded in; this ADR describes the FIRST SLICE, and everything else is under "Later". Covers backlog M6.1 (multi-repository registry, baseline refs, protected paths, one worktree per task attempt per repo) and M6.2 wiring (the candidate manifest stored immutably as the single object that approvals, review and deployment bind to). Builds on ADR 019 (privilege boundary), ADR 021 (side-effect ledger), ADR 022 (approval environment binding), ADR 023 (spawn journal and worker fence), ADR 024 (resume of never-spawned attempts) and ADR 025 (independent review). Local single machine only, Linux only; no network or multi-host design.

## Context

Today a run works on ONE repository. `agent_service.start` takes one `repo` and one `base_ref`, calls `WorktreeManager.check_base`, then `WorktreeManager.create(run_id, task_id, repo, base_ref)` per task. The worktree path is `<worktree_root>/<run_id>/<task_id>` and the branch is `synapse/<run_id>/<task_id>`; neither carries a repository, so a second `create` for another repo under the same `(run, task)` is refused (`docs/operations/worktrees.md`). A retry reuses the same worktree. The allowlist is a flat list of paths in `agent.json` (`repos` plus `worktree_root`), written only by the operator (by hand or `synapse workspace discover`) and read once at daemon start. Commits are host-side and operator-requested (`agent-worktree-commit` → `WorktreeManager.commit`), never made by the agent. Only one run is active at a time.

The candidate manifest library (`application/candidate_manifest.py`, format v1) already hashes `{run_id, repos[{repo_id, baseline_sha, candidate_sha, diff_sha256, diff_bytes, validations}]}` and publishes it idempotently to the immutable `artifacts` table. `approval_requests.candidate_manifest_sha256` exists and is immutable by trigger (0010), but nothing creates an approval from a manifest, `repo_id` is caller-supplied, and no consumer rechecks the manifest before acting.

The target use is a Domain change across `product-config`, `portfolio` and `tenant-specification`. Owner's workload: runs usually touch 2 to 3 of these repositories, and 2 to 3 runs may be active in parallel. Agents edit only XML files. A change is reviewed by the owner reading the diff, approved by the owner, and deployed by one operator-configured command that takes all repositories together.

Threat model (unchanged from ADR 019): one uid. Agents run in the bwrap sandbox (`providers/sandbox_launcher.py`): `/` read-only, only the scratch HOME and the task worktree writable, the repository's common `.git` mounted read-only with `config`/`credentials` masked. With `--allow-same-user-agents` and the sandbox off, an agent can edit anything the daemon can, including SQLite, the config file and refs. Nothing below is a defence against that case; it is stated per decision where it matters.

## Owner decisions (2026-11-23)

| Question | Decision |
|---|---|
| Which files may agents change | Only `**/*.xml`, in the three repositories. Everything else is refused at the host-side commit. |
| Size | Usually 2 to 3 repositories per run; 2 to 3 runs in parallel. |
| Commit | Automatic host-side commit to the local `synapse/` attempt branch at attempt end. NEVER push. |
| Read repositories | Read-only worktrees at the pinned baseline. |
| Baseline moved during a run | The manifest is invalidated. |
| Writers | One writer task per repository per run; one task may write several repositories. |
| Registry location | Extend `agent.json` `repos`. |
| Protected refs | Not configurable: Synapse writes only `refs/heads/synapse/`. |
| Validation in v1 | No Catalog Validator. XML well-formedness plus path checks; the owner reads the diff. Catalog Validator later. |
| Deploy | ONE operator-configured command for all repositories together, reading the candidate worktrees from disk; a status command afterwards; explicit approval on every deploy; intent recorded before and outcome after; `unknown` when unclear. |
| Approver | The owner only. No passphrase in this slice. |
| Profiles | Three Claude profiles, chosen per run; no fallback. |

## Decision (first slice)

### D1. Registry: `agent.json` `repos`, operator-only, snapshotted per run

The `repos` key of `agent.json` accepts, in addition to the current plain path strings, objects:

```json
{"repos": [
  {"id": "product-config", "path": "/home/op/work/product-config",
   "baseline_refs": ["refs/heads/main"], "default_baseline_ref": "refs/heads/main"},
  "/home/op/work/legacy-repo"
], "worktree_root": "/home/op/.local/state/synapse/worktrees"}
```

- `id` matches the existing safe-id rule, is unique and is what manifests, approvals and the API use. It is required in the object form so it never changes when a folder is renamed. A plain string keeps working for single-repo runs; its id is derived from the basename, and two strings with the same basename refuse daemon start (fail closed).
- There is no per-repository path or ref configuration in this slice: the editable-path rule (`**/*.xml`, D3) and the ref rule (D3) are fixed in code, by owner decision. Changing them is a reviewed code change.
- All existing allowlist rules apply unchanged: absolute, normalized, no symlinks, owned by the daemon uid, real `.git` directory, not nested, no overlap with `worktree_root` or the state dir, `refuse_unsafe_repo_config` at start, before each run and before every git call on a tree. Maximum 16 entries (the manifest's limit).
- The daemon computes `registry_sha256` over the canonical JSON of the validated entries at start and records it in the startup audit event.
- **Who may edit:** only the operator, by editing the file (or the operator-only `synapse workspace discover --write`) and restarting the daemon. No route, MCP tool, desktop action or agent run can add, remove or change an entry; the daemon never writes the file.
- **Snapshot per run:** at run creation the entries the run uses are copied into `run_repo_bindings` (D2) with their entry hash. A registry edit plus restart never changes a run already created; a run whose repository was removed can start no new attempts (`repo_not_registered`), but its manifests still verify.

Why not a database table: an editable table invites an edit route, and SQLite is writable by the same uid anyway (F1), so it adds no protection over a file. The config directory is outside the sandbox's writable binds; with the sandbox off an agent could edit it, and the per-run snapshot and startup audit hash make that visible, not impossible.

### D2. Baseline refs are pinned to SHAs once, at run creation

- A run request names, per repository, a `baseline_ref` that must be one of that entry's `baseline_refs` (default: only `default_baseline_ref`; an entry with neither cannot be used in a multi-repo run). Free-form refs, remote-tracking refs and raw SHAs are refused.
- The daemon resolves each ref once with the existing `_resolve_base` (`rev-parse --verify --end-of-options <ref>^{commit}`) and writes `(run_id, repo_id, baseline_ref, baseline_sha)` to `run_repo_bindings` in the same transaction that queues the run. All resolutions happen before the transaction; if any fails, nothing is written and no worktree exists.
- Every attempt of every task cuts its worktrees from the pinned `baseline_sha`, never from the ref again.
- **Baseline moved ⇒ manifest invalidated** (owner decision). Before a manifest is built, before an approval is requested or decided, and immediately before the deploy command runs, the daemon re-resolves each `baseline_ref` and compares it with the pinned SHA. Any difference inserts a `baseline_moved` row in `candidate_manifest_invalidations` for every manifest of that run and denies the action. Nothing is rebased; the owner starts a new run. This is a simple per-run check, not the full M6.6 integration barrier (see Later).

### D3. Editable paths and refs: enforced at the host-side commit, re-checked later

**Refs.** Synapse writes refs in exactly one namespace per repository: `refs/heads/synapse/<run_id>/<task_id>/<attempt_no>` (D4). `WorktreeManager` gains a single ref-write guard that refuses any branch create, update or delete outside `refs/heads/synapse/`. It never pushes (no remote is ever contacted: `GIT_NO_LAZY_FETCH=1`, `protocol.ext.allow=never` already apply, and no push code exists or is added), never merges and never touches the operator's checkout. Not configurable (owner decision).

**Paths.** The only paths an agent may change are those matching `**/*.xml` (git pathspec `:(glob)**/*.xml`, case-sensitive, repository-relative). Additions, modifications and deletions of matching files are allowed; renames are seen as delete plus add (`--no-renames`) and both sides must match. Everything else (any non-XML file, symlinks, gitlinks, mode-only changes to non-XML files, `.gitmodules`) is refused.

Enforcement points:

1. **Deciding point: the host-side commit** (`WorktreeManager.commit`, D4 step 4). The staged path list (already computed for the gitlink and `.gitmodules` refusals) is checked against the rule; any non-matching path refuses the whole commit for that repository with `path_not_editable` and a count (never the paths in events). Each changed XML file is then parsed for well-formedness with the safe parser of ADR 020-M6 (any `<!DOCTYPE`/`<!ENTITY` rejected before parsing, size caps); a malformed file refuses the commit with `xml_not_well_formed`. A symlink whose name ends in `.xml` is refused (`symlink_changed`).
2. **Detecting point: manifest build.** `collect_repo_candidate` re-runs both checks on `git diff --name-only --no-renames baseline candidate` and on the candidate blobs. This catches a candidate produced any other way (a same-uid agent with the sandbox off writing objects and refs itself).
3. **Detecting point: before deploy.** The deploy step re-runs `verify_manifest_artifact(..., verify_diffs=True)`, which repeats check 2.

Prevention in the sandbox: the common `.git` is mounted read-only, so a sandboxed agent cannot write objects or refs at all. Non-XML files can still be edited in the working tree; they are refused at commit, which is when it matters.

### D4. Worktree lifecycle: one worktree per (task attempt, repository)

Layout and names:

- Path: `<worktree_root>/<run_id>/<task_id>/<attempt_no>/<repo_id>`.
- Branch, in each repository: `synapse/<run_id>/<task_id>/<attempt_no>`. Each repository has its own ref namespace, so the repository is not in the branch name. Legacy single-repo runs keep `synapse/<run_id>/<task_id>`; a new run has a new id, so the two shapes never collide (no D/F ref conflict).
- `_contained` (which assumes depth 2) and `worktree_path` change to the new depth for multi-repo runs; legacy depth-2 trees keep working.

Per attempt, in order:

1. **Create all, or none.** Before the spawn, the daemon creates one worktree per repository the task declares (D5): write repositories on the attempt branch, read repositories detached at the pinned baseline. With 2 to 3 runs in parallel, several runs may touch the same repository; per-repo locks (the existing in-process lock plus `flock`) are always taken in ascending `repo_id` order, so parallel runs cannot deadlock. If any `create` fails, the ones created for this attempt are removed and the attempt fails `worktree_create_failed:<repo_id>` without spawning. One `attempt_worktrees` row per worktree is written before the spawn.
2. **Spawn once.** One process per attempt, as today, using the run's chosen profile (D7). Its cwd is `<...>/<attempt_no>/`. The sandbox gets one writable bind per write worktree and a read-only bind per read worktree; each repository's common `.git` is read-only and masked as today (`git_readonly_dirs` is called per worktree with that repository's entry only). The spawn journal (ADR 023) is unchanged: one entry per spawn, not per repository.
3. **Run.** The agent edits files. It cannot reach a repository it was not given (not mounted) and cannot commit.
4. **Automatic host commit at attempt end** (owner decision). When the attempt ends `completed`, the daemon commits each write worktree on its local `synapse/` attempt branch (`WorktreeManager.commit` with the D3 checks), in ascending `repo_id` order, writing a commit intent before and the result after (D6). A write repository with no changes gets `candidate_sha = baseline_sha`. Nothing is pushed. Single-repo legacy runs keep today's operator-requested commit.
5. **Retry is a new attempt from the baseline.** A retry gets new worktrees at `attempt_no + 1`, cut from the pinned baseline. The previous attempt's trees stay for the owner to read.
6. **Failed, cancelled and unknown attempts** are never committed. Their trees stay until the operator removes them (existing `remove_worktree`, now per attempt and repository).

Parallel runs: the daemon's one-active-run rule is lifted only as far as the ADR 015/016 parallel-runs work allows; this ADR requires only that two runs may hold worktrees of the same repository at once (they do: different paths and branches) and that the lock order above is respected. Two runs editing the same repository produce independent candidates; whichever deploys first moves nothing in Synapse, but if the owner then merges it onto the baseline ref, the other run's manifest is invalidated by D2.

### D5. How a task declares repositories, and why an agent cannot widen the set

- The run request declares the run set: `repos: [{repo_id, baseline_ref}]`, 1 to 16 (usually 2 to 3). Each task declares `repos: [{repo_id, access: "write" | "read"}]`, a non-empty subset of the run set.
- **Exactly one writer task per repository per run** (owner decision): the run is refused at creation if two tasks declare `write` on the same repository. One task may write several repositories. A repository in the run set that no task writes is read-only for the whole run.
- The declared repositories and access modes are part of the task specification, so they are covered by the full-spec approval binding hash and the spawn-time recheck. They are written once at run creation (`run_repo_bindings`, task spec) and are immutable.
- An agent cannot widen the set because every path that could widen it reads only those immutable rows: the sandbox mounts only declared worktrees; the MCP gateway has no repository tool; agent-created subtasks or handoffs (if they exist) may only name a subset of their parent's set with no new `write`, checked by the engine; the host commit loops over the task's declared write repositories only; the manifest builder refuses a `repo_id` not in `run_repo_bindings` and a candidate from a task that does not hold `write` on it. With the sandbox off an agent could write elsewhere on disk, but nothing outside the declared set becomes a candidate.

### D6. Cross-repository atomicity and failure

Git cannot commit several repositories atomically, so the unit of atomicity is the **manifest**, not the commits. Commits on local `synapse/` branches are harmless when partial; only a complete manifest can be approved or deployed.

- **Commit phase state.** `attempt_repo_commits` is append-only (like the side-effect ledger, ADR 021): an `intent` row `(attempt, repo_id, head_before)` before `git commit`, then `done (candidate_sha)` or `failed (reason)`. The phase is complete only when every write repository of the attempt has `done`.
- **Partial success.** If repository 2 of 3 fails to commit (non-XML path, malformed XML, too many files, git error), the attempt becomes `failed` with `commit_failed:<repo_id>:<reason>`; repository 1's commit stays on its branch as evidence and is not undone; no manifest can be built from that attempt. Recovery is a new attempt, never a re-commit of the failed tree.
- **Crash during the commit phase.** At startup, after the fence, an `intent` without a result is resolved: if the fence was `all_clear` and the attempt branch HEAD is `head_before`, the intent is `not_done` and the commit may be re-run; if HEAD is a direct child of `head_before` whose tree equals the worktree's index, the commit landed and its SHA is recorded `done`; anything else is `unknown`, the attempt is blocked and the operator decides (retry as a new attempt, or abandon). Never guessed, never replayed blindly.
- **Crash during the agent run.** Unchanged from ADR 023/024: the fence runs, the attempt becomes `unknown`, the task is blocked, the run is `recovering`, nothing resumes (except ADR 024's provably never-spawned attempts), and nothing is committed from that attempt's worktrees. One multi-repo attempt is one journal entry and one fence verdict.
- **Deploy** is one step for all repositories together (D9), so there is no partial cross-repository deploy inside Synapse to reconcile; what the deploy command does across repositories is the command's own responsibility, and an unclear outcome is `unknown`.

### D7. Profiles: three Claude profiles, chosen per run, no fallback

The run request names one of the three operator-enabled Claude profiles (existing profile registry, ADR 015/016). It is recorded in the run's specs and covered by the spec binding. If that profile is unavailable at spawn (disabled, budget exceeded, provider failure), the attempt fails with the existing reason; the daemon never substitutes another profile.

### D8. Manifest v2: one object binding every repository's baseline and candidate

```json
{"v": 2, "run_id": "...", "registry_sha256": "<sha of this run's run_repo_bindings snapshot>",
 "repos": [
   {"repo_id": "portfolio", "access": "write",
    "baseline_ref": "refs/heads/main", "baseline_sha": "...", "candidate_sha": "...",
    "source": {"task_id": "...", "attempt_no": 2},
    "diff_sha256": "...", "diff_bytes": 1234, "changed_paths": 7,
    "validations": [{"name": "paths_editable", "outcome": "passed", "evidence_sha256": "..."},
                    {"name": "xml_well_formed", "outcome": "passed", "evidence_sha256": "..."}]},
   {"repo_id": "tenant-specification", "access": "read", "baseline_ref": "refs/heads/main", "baseline_sha": "..."}
 ]}
```

- Every repository in the run set appears exactly once, sorted by `repo_id`. Read repositories carry only their baseline (the owner must know what the agent read). A write repository with no change has `candidate_sha == baseline_sha`.
- `source` names the one attempt whose committed HEAD is the candidate; it must have a `done` commit row (D6) and must belong to the repository's single writer task.
- v1 validations are exactly `paths_editable` and `xml_well_formed`, both required to be `passed`. There is no Catalog Validator result in v1 (owner decision); the owner reads the diff.
- No timestamps, paths or prompts; existing size limits (16 repos, 32 validations each, 48 KiB) stay. `manifest_sha256` is SHA-256 of the canonical bytes, as in v1. v1 manifests stay verifiable; multi-repo approvals require v2.
- Storage: the bytes stay in the immutable `artifacts` table (via `publish_manifest`); a new immutable `candidate_manifests` row indexes them per run. A manifest is built by A's application service on an operator request (or a workflow node), never by an agent, and only after D2's baseline check passes.

### D9. Approval and deploy

**Approver.** The owner only (owner decision). In this slice the approver is the local operator identity that the existing approver-identity rules accept; no passphrase challenge is added (the ADR 019 passphrase verifier stays future work). Same-uid caveat: anything running as the owner's uid can approve; this is accepted for the first slice.

**Binding.** Every deploy needs its own explicit approval, created with `operation = "deploy"`, `candidate_manifest_sha256 = manifest_sha256` and the ADR 022 environment binding (`bound_sha256` over the manifest hash and environment). A trigger in the migration refuses the insert when the run has `run_repo_bindings` rows unless `candidate_manifest_sha256` names an existing, non-invalidated `candidate_manifests` row for the same run. Approvals are single-use for deploy: the ledger key below includes the approval id, so a second deploy needs a new approval. A retry produces a new manifest hash, so no approval transfers between candidates. Review verdicts (ADR 025) for a multi-repo run bind to `manifest_sha256`.

**Deploy step.** The operator configures, in `agent.json`, one deploy command and one status command, each as an argv list with an absolute executable (no shell strings, validated like the ADR 020-M6 planned-command rule). Immediately before running the deploy command the daemon:

1. verifies the approval with `verify_approval_for_environment`;
2. re-runs D2 (baseline unchanged) and `verify_manifest_artifact(..., verify_diffs=True)` (D3 re-check);
3. checks, for every write repository, that the source attempt is terminal (no live agent can still edit the tree), the worktree is clean and its HEAD equals `candidate_sha`; read repositories' worktrees are clean and at `baseline_sha`;
4. records a side-effect ledger intent (ADR 021) keyed `(task="deploy", attempt=<approval id>, candidate_sha256=manifest_sha256, step="deploy", environment)`.

It then runs the deploy command once, with the candidate worktree paths passed as arguments in `repo_id` order (`--repo <id>=<path>` per repository), cwd a scratch directory, scrubbed env, own process group, timeout, bounded output (the ADR 025 command-execution rules). Afterwards it runs the status command the same way. The ledger outcome is `done` only when the deploy command exited 0 AND the status command reports success; `not_done` only when the deploy command exited non-zero before acting according to the status command; everything else (timeout, signal, crash, ambiguous status, status command failure) is `unknown`. `unknown` is resolved only by the operator or a later status-command probe; nothing is retried automatically. Synapse never pushes; whatever the deploy command does with the worktrees is outside Synapse.

### D10. Migration 0019 (A's odd numbering; next after 0017)

One migration, **0019** (number final at merge; the runner needs a contiguous sequence, so it lands after B's 0018):

| Table | Shape | Mutability |
|---|---|---|
| `run_repo_bindings` | `run_id, repo_id, path, baseline_ref, baseline_sha, access_summary, entry_sha256, registry_sha256`, PK `(run_id, repo_id)` | insert-only (UPDATE/DELETE triggers abort) |
| `attempt_worktrees` | `attempt_id, repo_id, access, path, branch, created_at`, PK `(attempt_id, repo_id)` | insert-only |
| `attempt_worktree_removals` | `attempt_id, repo_id, removed_at, reason, forced` | insert-only (written by operator removal now, by the retention sweep later) |
| `attempt_repo_commits` | `attempt_id, repo_id, seq, state (intent/done/failed/not_done/unknown), head_before, candidate_sha, reason, at` | append-only transitions |
| `candidate_manifests` | `manifest_sha256` PK, `run_id, artifact_id, version, seq, created_at` | insert-only |
| `candidate_manifest_invalidations` | `manifest_sha256, reason (baseline_moved/candidate_missing/operator), actor, at` | insert-only |
| trigger on `approval_requests` | BEFORE INSERT: as in D9 | n/a |

The side-effect ledger and environment binding tables (ADR 021/022) are created by their inline `ensure_schema`; if they have been promoted to a migration by then, 0019 does not touch them. No column is added to `runs`, `tasks` or `task_attempts`. It must apply from empty and from 0018, contain no `PRAGMA` or transaction statements, and pass the migration fingerprint round trip. Existing single-repo runs have no `run_repo_bindings` rows and keep working on the legacy path.

## What A builds and what B builds

A (core, daemon, persistence):

- Registry loader and validator for the object form, `registry_sha256`, startup audit, legacy-string compatibility; deploy/status command config validation.
- `WorktreeManager`: attempt-depth paths and branches, read-only detached worktrees, ref-write guard, `**/*.xml` path rule and well-formedness check in `commit`, ordered multi-repo locking, all-or-none create.
- Sandbox: one writable bind per write worktree, read-only binds for read worktrees, per-repo `git_readonly_dirs`/masks.
- Engine and runtime: per-attempt multi-repo lifecycle, automatic commit phase with intents, recovery rules of D6, one-writer check, subset check for any agent-created subtask, per-run profile with no fallback.
- Manifest v2 builder and verifier, `candidate_manifests` store, invalidations, the D2 baseline check.
- Approval insert trigger, deploy step (pre-checks, ledger intent/outcome, command runner, status probe), ADR 025 and 022 adaptations.
- Migration 0019 and its tests.

B (clients, CLI, API routes, docs):

- Run creation route: accept `repos: [{repo_id, baseline_ref}]`, per-task `repos: [{repo_id, access}]` and the profile; OpenAPI contracts and refusal messages (reasons defined by A).
- Read routes: `GET /runs/{id}/repos` (bindings and per-attempt worktrees and commit states), `GET /runs/{id}/candidate-manifests` and `GET .../{sha}` (with per-repo diff for the owner to read), `GET /repos` (read-only registry view with `registry_sha256`).
- Operator routes calling A's services: `POST /runs/{id}/candidate-manifest`, deploy approval request/decision for a manifest, `POST /runs/{id}/deploy` (requires an approved, unused approval), `GET /runs/{id}/deploy` (ledger state, status output summary), resolution of an `unknown` deploy.
- `agentctl`: `run --repo ID=REF ... --profile P` and per-task repo flags, `repos list`, `manifest build/show/diff`, `deploy` and `deploy-status`.
- `synapse workspace discover --write`: emit the object form with explicit ids (operator-only, as today).
- Desktop: per-repository diff and status views, manifest view with every repository's baseline and candidate, deploy approval and outcome view.
- Docs: `worktrees.md`, `candidate-manifest.md`, `agent-runs.md`, `workspace.md`, API reference, a deploy operations page.

## Later (out of the first slice)

- **Retention sweep**: per-repository `keep_days`, never removing trees of non-terminal runs, of runs with an uncleared unverified fence entry, or backing an unused approval; startup and `--dry-run` operator command; keep branches of deployed candidates. Until then worktrees are removed only on operator request.
- **Cross-repository integration barrier (M6.6)**: dependency-compatibility checks across candidates using the XML dependency index; D2's moved-baseline invalidation is the only barrier in v1.
- **Deploy-order derivation**: per-repository deploy steps ordered from the dependency index. v1 has one command for all repositories, so there is no order to derive.
- **Catalog Validator** results in the manifest (ADR 020-M6 adapter, once its commands are known).
- **Passphrase or stronger approver authentication** (ADR 019).
- Per-repository configurable editable paths, if a repository ever needs non-XML edits.
- "Continue from attempt n" for retries.

## Alternatives rejected

- **One worktree per task, reused across attempts** (today's shape): a retry inherits the failed attempt's edits and the manifest cannot name which attempt produced a candidate.
- **One super-repository or submodules**: changes the owner's repositories; gitlinks are already refused by `commit`.
- **Agent commits inside the sandbox**: needs a writable common `.git`, which lets the agent write any ref.
- **Atomic cross-repo commit via a two-phase protocol**: impossible in git without a coordinator, and unnecessary when only the manifest is approved and deployed.
- **Registry in SQLite with an operator route**: adds the API surface the operator-only rule forbids; SQLite is same-uid writable anyway.
- **Re-resolving the baseline ref per attempt**: attempts would start from different commits and the manifest would bind to a moving target.
- **Deploying from the branches instead of the worktrees**: the owner's deploy tool reads files from disk; D9 step 3 makes the worktree provably equal to the candidate commit instead.
- **Deny-list of protected paths**: the owner's rule is an allow-list (`**/*.xml`), which fails closed on any new file type.

## Consequences and limits

- Same-uid caveat (ADR 019): with the sandbox off, an agent can edit the config file, SQLite, refs, objects and the worktrees between the D9 checks and the deploy command. The design then detects (snapshots, re-checks, audit hashes) but does not prevent; the D9 step-3 check narrows but does not close the window.
- Without the retention sweep, disk use grows with attempts × repositories until the operator removes trees.
- The global fence (ADR 023) still reports per daemon, not per attempt.
- The automatic host commit is new behaviour for multi-repo runs only.
- XML well-formedness is not schema validity; the owner's diff review and, later, the Catalog Validator carry that.

## Open questions

All questions from the first draft are answered by the owner decisions above:

1. Paths agents may change: only `**/*.xml` in the three repositories (repository paths and allowed baseline refs are supplied by the operator in `agent.json`).
2. Automatic commit at attempt end: yes, to the local `synapse/` branch, never pushed.
3. Read repositories: read-only worktrees.
4. Retention: moved to Later.
5. Cross-repository deploy order: not needed; one command for all repositories (derivation moved to Later).
6. Writers: one writer task per repository per run; one task may write several repositories.
7. Baseline moved: the manifest is invalidated.
8. Registry location: extend `agent.json`.
9. Protected refs: not configurable.

Remaining, to confirm at implementation time (not blocking this ADR): the exact argv contract of the owner's deploy and status commands (how they take repository paths and how status reports success), and the environment identifier the deploy approval binds to (`STAGING` is the only allowlisted value in ADR 022).
