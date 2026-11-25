# ADR 026: Multi-repository runs and the candidate manifest as the binding object

Status: Proposed (design only; no code, no migration in this change). Owner decisions of 2026-11-23 and B's review of 2026-11-24 are folded in. This ADR describes the FIRST SLICE; everything else is under "Later". Covers backlog M6.1 (multi-repository registry, baseline refs, editable paths, one worktree per task attempt per repo) and M6.2 wiring (the candidate manifest stored immutably as the single object that approvals, review and deployment bind to). Builds on ADR 019 (privilege boundary), ADR 021 (side-effect ledger), ADR 022 (approval environment binding), ADR 023 (spawn journal and worker fence), ADR 024 (re-queue of never-spawned attempts) and ADR 025 (independent review and evidence verifier). Local single machine only, Linux only; no network or multi-host design inside Synapse.

## Context

Today a run works on ONE repository. `agent_service.start` takes one `repo` and one `base_ref`, calls `WorktreeManager.check_base`, then `WorktreeManager.create(run_id, task_id, repo, base_ref)` per task. The worktree path is `<worktree_root>/<run_id>/<task_id>` and the branch is `synapse/<run_id>/<task_id>`; neither carries a repository. A retry reuses the same worktree. The task spec records `repo`, `base_ref`, `worktree` (path) and `worktree_head` once, at creation, and three consumers depend on that fixed pair:

- the approval subject (`persistence/approval_subject.py`) shows `repo`, `base_ref` and `worktree_head` and hashes the full spec;
- the spawn-time recheck (`orchestration/agent_engine.py`, `_worktree_problem`) refuses a spawn unless the tree is at the recorded path and HEAD;
- the ADR 025 evidence verifier (`orchestration/evidence_verifier.py`) refuses `worktree_mismatch` / `head_moved` against the same spec fields.

The allowlist is a flat list of paths in `agent.json` (`repos` plus `worktree_root`), written only by the operator and read once at daemon start. Commits are host-side and operator-requested (`agent-worktree-commit`), never made by the agent. Only one run is active at a time (ADR 015 defers parallel runs).

The candidate manifest library (`application/candidate_manifest.py`, format v1) hashes `{run_id, repos[{repo_id, baseline_sha, candidate_sha, diff_sha256, diff_bytes, validations}]}` and publishes it idempotently to the immutable `artifacts` table. `verify_manifest_artifact` checks only that the candidate objects exist and the diff hash matches; it does not re-run any content rule. `approval_requests.candidate_manifest_sha256` exists and is immutable (0010), but nothing creates an approval from a manifest. No XML parser exists in `src/`.

The target use is a Domain change across three repositories (`product-config`, `portfolio`, `tenant-specification`): usually 2 to 3 per run, 2 to 3 runs wanted in parallel, agents editing XML only, the owner reading the diff, approving, and deploying to a development environment with an operator-configured upload tool.

Facts about that deploy tool (from a local, owner-private analysis; described generically here): it has no dry-run and no status mode; every mode writes to the target; exit status 0 alone does not prove success (it is also returned for help output, bad arguments and a declined prompt), so success needs its success marker on stdout and no usage text; some modes are destructive and a failure leaves earlier steps applied with no rollback; it reads credentials from a configuration directory found through its working directory or an environment variable, so a scratch cwd with a scrubbed environment loses them; it ignores SIGINT; it renames folders inside its input tree while running; its error output can contain server details; it builds a shell string internally, so its input paths must be simple. One of the three repositories (`tenant-specification`) has no deploy command.

Threat model (ADR 019): one uid. Agents run in the bwrap sandbox: `/` read-only, only the scratch HOME and the task worktree writable, each repository's common `.git` read-only with `config`/`credentials` masked. With `--allow-same-user-agents` and the sandbox off, an agent can edit anything the daemon can. Nothing below defends against that case; it is stated where it matters.

## Owner decisions (2026-11-23)

| Question | Decision |
|---|---|
| Which files agents may change | Only `**/*.xml`, in the three repositories. Everything else is refused at the host-side commit. |
| Size | Usually 2 to 3 repositories per run; 2 to 3 runs in parallel. |
| Commit | Automatic host-side commit to the local `synapse/` attempt branch at attempt end. NEVER push. |
| Read repositories | Read-only worktrees at the run's pinned baseline. |
| Baseline moved during a run | The manifest is invalidated. |
| Writers | One writer task per repository per run; one task may write several repositories. |
| Registry location | Extend `agent.json` `repos`. |
| Protected refs | Not configurable: Synapse writes only `refs/heads/synapse/`. |
| Validation in v1 | No Catalog Validator. XML well-formedness plus path checks; the owner reads the diff. |
| Deploy | Operator-configured command(s), explicit approval on every deploy, intent recorded before and outcome after, `unknown` when unclear. Deploys read a disposable export of the candidate, never a worktree. Only repositories that have a deploy command are deployed. |
| Remote contact | The deploy command may contact the configured remote environment. Synapse itself never contacts a remote and never pushes. |
| Approver | The owner only. No passphrase in this slice. |
| Profiles | Three Claude profiles, chosen per run (per-task override), no fallback. |

## Decision (first slice)

### D1. Registry: `agent.json` `repos`, operator-only, snapshotted per run

`repos` accepts, in addition to the current plain path strings, objects:

```json
{"repos": [
  {"id": "product-config", "path": "/home/op/work/product-config",
   "baseline_refs": ["refs/heads/main"], "default_baseline_ref": "refs/heads/main"},
  "/home/op/work/legacy-repo"
], "worktree_root": "/home/op/.local/state/synapse/worktrees"}
```

- `id` matches the existing safe-id rule, is unique and is what manifests, approvals and the API use; it is required in the object form so it never changes when a folder is renamed. A plain string keeps working for single-repo runs; its id is the basename, and two strings with the same basename refuse daemon start (fail closed). The manifest builder no longer derives `repo_id` from a directory name for multi-repo runs; it takes it from the binding.
- An optional `"deploy": "none"` marks a repository whose output is generated by the owner's CI pipeline rather than deployed by Synapse (D9). Its `entry_sha256` covers the marker.
- No per-repository path or ref configuration in this slice: the editable-path rule (D3) and the ref rule (D3) are fixed in code by owner decision; changing them is a reviewed code change.
- All existing allowlist rules apply unchanged (absolute, normalized, no symlinks, owned by the daemon uid, real `.git` directory, not nested, no overlap with `worktree_root` or the state dir, `refuse_unsafe_repo_config` at start, before each run and before every git call on a tree). At most 16 entries (the manifest's limit).
- **Two hashes, two purposes.** `registry_sha256`: SHA-256 of the canonical JSON (sorted keys, no whitespace, UTF-8, the manifest's canonical encoder) of ALL validated entries, computed at daemon start and recorded only in the startup audit event; it answers "what registry was loaded". `bindings_sha256`: SHA-256 of the canonical JSON of THIS run's `run_repo_bindings` rows sorted by `repo_id`, each `{repo_id, entry_sha256, baseline_ref, baseline_sha, writer_task_id}` (`writer_task_id` null for a repository no task writes); `entry_sha256` is the canonical hash of that one registry entry. `bindings_sha256` is what the manifest carries (D8).
- **Who may edit:** only the operator, by editing the file (or the operator-only `synapse workspace discover --write`) and restarting the daemon. No route, MCP tool, desktop action or agent run can change an entry; the daemon never writes the file.
- **Snapshot per run:** at run creation the entries the run uses are copied into `run_repo_bindings` (D2). A registry edit plus restart never changes a created run; a run whose repository was removed can start no new attempts (`repo_not_registered`), but its manifests still verify.

Why not a database table: an editable table invites an edit route, and SQLite is same-uid writable anyway (F1). The config directory is outside the sandbox's writable binds; with the sandbox off an agent could edit it, and the snapshot and startup hash make that visible, not impossible.

### D2. Baseline refs are pinned to SHAs once, at run creation

- A run request names, per repository, a `baseline_ref` that must be one of that entry's `baseline_refs` (default: only `default_baseline_ref`; an entry with neither cannot be used in a multi-repo run). Free-form refs, remote-tracking refs and raw SHAs are refused.
- The daemon resolves each ref once with the existing `_resolve_base` and writes the bindings in the same transaction that queues the run. All resolutions happen before the transaction; if any fails, nothing is written and no worktree exists.
- Every attempt of every task cuts its worktrees from the pinned `baseline_sha`, never from the ref again.
- **Baseline moved ⇒ manifest invalidated** (owner decision). When a manifest is built (before collecting, and again after publishing and before its row is indexed), when a deploy approval is requested, and immediately before the deploy runs (pre-check step 2, mandatory), the daemon re-resolves each `baseline_ref` and compares it with the pinned SHA. A difference inserts a `baseline_moved` row in `candidate_manifest_invalidations` for every manifest the run already has and denies the action. The approval decision does not re-resolve refs; it refuses only on invalidation rows already recorded. Nothing is rebased; the owner starts a new run. Once a `baseline_moved` row exists for one of the run's manifests, a trigger refuses every new manifest row of that run, even if the ref is moved back. If the run had no manifest yet when the move was seen, nothing is recorded: every build refuses while the ref differs from the pinned SHA, but if the ref returns to exactly the pinned SHA a manifest can still be built (it is then built from the pinned baseline, so it is the same manifest it would have been).

### D3. Editable paths, file modes and refs

**Refs.** Synapse writes refs in exactly one namespace per repository: `refs/heads/synapse/<run_id>/<task_id>/<attempt_no>` (D4). `WorktreeManager` gains a ref-write guard that refuses any branch create, update or delete outside `refs/heads/synapse/`. Synapse never pushes, fetches, merges or touches the operator's checkout. Not configurable.

**Paths and modes (the commit rule).** A change is committable only if every changed path (added, modified or deleted; renames seen as delete plus add with `--no-renames`) satisfies all of:

1. matches `:(glob)**/*.xml` (case-sensitive, repository-relative, a plain relative path without `..`, control characters or a leading `-`);
2. if the task's node has an ADR 025 `EvidencePolicy`, is also inside its `allowed_paths`; the effective rule is the INTERSECTION of the two (both must allow; neither widens the other);
3. for an added or modified path, the new tree entry is a regular non-executable file, mode `100644` (no symlink `120000`, no executable `100755`, no gitlink `160000`);
4. for an added or modified path, the file is well-formed XML (below).

Everything else is refused. All git listings used for these checks are parsed NUL-separated (`-z`), never line-split.

**Untracked and ignored files.** Before staging, the commit runs `git status --porcelain=v1 -z --untracked-files=all --ignored=matching` (the hardened git of `WorktreeManager`). Any IGNORED path present in the worktree refuses the commit (`ignored_path_present`): a `.gitignore` must never hide a file from the rule. Untracked, non-ignored paths are part of the change and go through rules 1 to 4. After staging and before `git commit`, the same status must show no untracked path left (`untracked_path_present`). Independently, the deploy never reads a worktree (D9), so nothing outside the committed tree can reach it.

**XML well-formedness.** A new pure core module (`application/xml_wellformed.py`, stdlib only, no new dependency) checks one blob:

- a byte pre-scan rejects any `<!DOCTYPE` or `<!ENTITY` (and any other `<!` declaration except comments and `CDATA`) before parsing, so XXE and entity expansion are excluded by construction;
- parsing uses `xml.parsers.expat` with parameter-entity parsing set to never, and doctype/entity declaration handlers that raise;
- caps: 8 MiB per file, element depth 256, at most the existing 1000 paths per commit; exceeding a cap is a refusal, never a truncation;
- encoding: as declared by the XML declaration, default UTF-8; an undecodable file is malformed.

It reads blob bytes the caller gives it (from the git object store at commit time and at manifest verification), never paths the agent chose. Tests cover the rejected constructs, the caps and well-formed samples. ADR 020-M6's index may reuse it later.

**Enforcement points.**

1. **Deciding: the host-side commit** (D4 step 5). Any violation refuses the whole commit for that repository with a reason code (`path_not_editable`, `path_not_allowed`, `mode_not_allowed`, `xml_not_well_formed`, `ignored_path_present`, `untracked_path_present`) and a count; paths never appear in events.
2. **Detecting: manifest build and every consumer.** A new `verify_manifest_candidates` re-runs rules 1 to 4 for every write repository on `git diff-tree -r -z --no-renames baseline_sha candidate_sha` and the candidate blobs from the object store. It is called when the manifest is built, when a deploy approval is requested, and before deploy (pre-check step 2, mandatory); the approval decision does not call it. This catches a candidate produced any other way (a same-uid agent with the sandbox off writing objects and refs itself).

In the sandbox the common `.git` is read-only, so an agent cannot write objects or refs at all.

### D4. Worktree lifecycle: one worktree per (task attempt, repository)

Layout and names:

- Path: `<worktree_root>/<run_id>/<task_id>/<attempt_no>/<repo_id>`.
- Branch, in each WRITE repository: `synapse/<run_id>/<task_id>/<attempt_no>`. Read repositories get a detached worktree at the pinned baseline and no branch. Legacy single-repo runs keep `synapse/<run_id>/<task_id>`; a new run has a new id, so the shapes never collide (no D/F ref conflict).
- `_contained` (which assumes depth 2) and `worktree_path` gain the attempt depth for multi-repo runs; legacy depth-2 trees keep working.

Per attempt, in order:

1. **Create all, or none.** After the attempt is reserved and before the ADR 024 spawn mark, the daemon creates one worktree per repository the task declares (D5): write repositories on the attempt branch, read repositories detached at the baseline. Per-repo locks (in-process lock plus `flock`) are taken in ascending `repo_id` order. If any `create` fails, the ones created for this attempt are removed and the attempt fails `worktree_create_failed:<repo_id>` without spawning. One `attempt_worktrees` row per worktree is committed before the spawn mark.
2. **Spawn-time recheck (replaces the single-path recheck for multi-repo specs).** For each `attempt_worktrees` row of the attempt: the path equals the computed `worktree_path(run, task, attempt, repo)`; `_repo_of(path)` is the bound repository; HEAD equals the pinned `baseline_sha`; the tree is clean. Any failure refuses the spawn (`worktree_mismatch`, `head_moved`, `worktree_dirty`) before the spawn mark. The spec no longer carries a path or HEAD for multi-repo tasks (D5), so the recheck reads them from `attempt_worktrees` and `run_repo_bindings`.
3. **Spawn once.** One process per attempt, as today, with the task's profile (D7). Its cwd is the attempt directory `<...>/<attempt_no>/`, which is NOT itself a worktree; the prompt names the repository subdirectories. The sandbox gets one writable bind per write worktree and a read-only bind per read worktree; `git_readonly_dirs` and the secret masks are computed per worktree against that repository's entry only. The spawn journal (ADR 023) is unchanged: one entry per spawn.
4. **Evidence, then commit.** When the process ends `succeeded`, verification runs FIRST, against the uncommitted worktrees: the process-outcome check, then (for nodes with an evidence policy) the ADR 025 evidence verifier per write repository, with the expected path from `attempt_worktrees` and the expected HEAD equal to the pinned `baseline_sha` (so `head_moved` still means "the agent committed or reset"), and changed paths checked against the D3 intersection. Only an attempt whose verification passes becomes `completed`.
5. **Automatic host commit at attempt end** (owner decision). For a `completed` attempt the daemon commits each write worktree on its local attempt branch with the D3 rule, in ascending `repo_id` order, writing a commit intent before and the result after (D6). A write repository with no changes gets an `unchanged` row with `candidate_sha = baseline_sha`; no git commit is attempted (today `commit` raises on a clean tree; the multi-repo path checks status first). Nothing is pushed. Single-repo legacy runs keep today's operator-requested commit.
6. **Retry is a new attempt from the baseline**, with new worktrees at `attempt_no + 1`. The previous attempt's trees stay for the owner to read.
7. **Failed, cancelled and unknown attempts** are never committed. Their trees stay until the operator removes them (existing removal, now per attempt and repository, recorded in `attempt_worktree_removals`).
8. **Never-spawned attempts re-queued by ADR 024.** Such an attempt is closed `cancelled` and the next attempt gets new worktrees. Because no process ever ran for it (no spawn mark), recovery removes its worktrees right away with a non-forced removal and records `reason = never_spawned`; a tree that is unexpectedly dirty is kept and reported, never force-removed.

**Parallel runs.** The owner wants 2 to 3 runs in parallel. This design is parallel-safe at the worktree level (distinct paths and branches, ordered locks), but lifting the daemon's one-active-run rule is a separate change under ADR 015 and is NOT part of this slice. Two runs that edit the same repository produce independent candidates; if the owner merges one onto the baseline ref, the other run's manifest is invalidated by D2.

### D5. How a task declares repositories, and why an agent cannot widen the set

- The run request declares the run set: `repos: [{repo_id, baseline_ref}]`, 1 to 16. Each task declares `repos: [{repo_id, access: "write" | "read"}]`, a non-empty subset of the run set.
- **Exactly one writer task per repository per run**: the run is refused at creation if two tasks declare `write` on the same repository. One task may write several repositories. A repository no task writes is read-only for the whole run.
- **Read semantics.** A read worktree always shows the run's pinned `baseline_sha`, never another task's candidate, even when another task of the same run writes that repository. A task that needs a sibling task's output in another repository is not supported in v1. Review of a run's result binds to the manifest (D8), not to what any read worktree showed.
- **Spec shape.** A multi-repo task spec carries `repos: [{repo_id, access}]` and no `worktree`, `worktree_head`, `repo` or `base_ref` fields. The per-repository `baseline_ref` and `baseline_sha` live in `run_repo_bindings`, and paths and heads per attempt in `attempt_worktrees`.
- **Approval subject.** For a multi-repo task the subject becomes `{kind: "agent_run_multi", node_id, profile, repos: [{repo_id, access, baseline_ref, baseline_sha}], allowed_tools, permission_mode, timeout_seconds, prompt_bytes, prompt preview, spec_sha256}`. `spec_sha256` hashes the spec together with `bindings_sha256`, so an approval binds the declared repositories, their access modes and their pinned baselines. No path or attempt HEAD is part of the subject; the attempt's trees are checked by D4 step 2 against the same bindings.
- **No widening.** Every path that could widen the set reads only those immutable rows: the sandbox mounts only declared worktrees; the MCP gateway has no repository tool; agent-created subtasks or handoffs (if they exist) may only name a subset of their parent's set with no new `write`; the host commit loops over the task's declared write repositories; the manifest builder refuses a `repo_id` not in `run_repo_bindings` and a candidate from a task that is not the repository's writer. With the sandbox off an agent could write elsewhere on disk, but nothing outside the declared set becomes a candidate.

### D6. Cross-repository atomicity and failure

Git cannot commit several repositories atomically, so the unit of atomicity is the **manifest**. Commits on local `synapse/` branches are harmless when partial; only a complete manifest can be approved or deployed.

- **Commit phase state.** `attempt_repo_commits` is append-only: an `intent` row `(attempt, repo_id, head_before)` before `git commit`, then `done (candidate_sha)`, `unchanged (candidate_sha = baseline_sha)` or `failed (reason)`. The phase is complete only when every write repository of the attempt is `done` or `unchanged`, and at least one is `done`; an attempt whose write repositories are all `unchanged` completes but cannot produce a manifest (`no_changes`).
- **Partial success.** If repository 2 of 3 fails to commit, the attempt becomes `failed` with `commit_failed:<repo_id>:<reason>`; repository 1's commit stays on its branch as evidence; no manifest can be built from that attempt. Recovery is a new attempt.
- **Crash during the commit phase.** After the fence, an `intent` without a result: if the fence was `all_clear` and the branch HEAD is `head_before`, it is `not_done` and the commit may be re-run with the full D3 rule; if HEAD is a direct child of `head_before` whose tree equals the worktree's index, the commit landed and is recorded `done` (then re-checked by `verify_manifest_candidates`); anything else is `unknown`, the attempt is blocked and the operator decides (new attempt or abandon).
- **Crash during the agent run.** Unchanged from ADR 023/024: fence, attempt `unknown`, task blocked, run `recovering`, nothing resumed except ADR 024's provably never-spawned attempts (D4 step 8), nothing committed. One multi-repo attempt is one journal entry and one fence verdict.
- **Crash during a deploy**: D9.

### D7. Profiles (aligned with PR #45)

- The run request may name a run-level `profile` and each task may override it with its own `profile`; with neither, the implicit `default` profile is used (current behaviour).
- An unknown, disabled or cooling-down profile is refused with 422 at run creation, before any run row, worktree or process exists.
- No fallback: if the named profile cannot be used at spawn, the attempt fails with the existing reason; the daemon never substitutes another profile and never falls back to the user's own CLI configuration.
- Audit: `agent.run_profile_selected` in the run-start transaction (ids only, `fallback: null`) and `agent.profile_selected` per attempt, as PR #45 defines. The task's profile is part of the spec and so of the approval subject.

### D8. Manifest v2: one object binding every repository's baseline and candidate

```json
{"v": 2, "run_id": "...", "bindings_sha256": "...",
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

- Every repository in the run set appears exactly once, sorted by `repo_id`. Read repositories carry only their baseline. A write repository with no change has `candidate_sha == baseline_sha` and empty `diff_sha256` input; at least one write repository must differ.
- `source` names the attempt whose committed HEAD is the candidate; it must have a `done` or `unchanged` row and belong to the repository's writer task. All write repositories come from the same writer's attempts; for a writer task that writes several repositories they come from ONE attempt (no mixing attempts of one task).
- v1 validations are exactly `paths_editable` and `xml_well_formed`, both `passed`. No Catalog Validator result in v1.
- No timestamps, paths or prompts; existing limits (16 repos, 32 validations each, 48 KiB) stay; `manifest_sha256` is SHA-256 of the canonical bytes. v1 manifests stay verifiable; multi-repo approvals require v2.
- Storage: bytes in the immutable `artifacts` table (via `publish_manifest`), indexed by an immutable `candidate_manifests` row. Built by A's service on an operator request (or a workflow node), never by an agent, and only after D2's baseline check and `verify_manifest_candidates` pass.
- Review (ADR 025) for a multi-repo run binds its verdict to `manifest_sha256`.

### D9. Approval and deploy

**Environments.** ADR 022's allowlist is extended from `STAGING` to `STAGING` and `DEV` (a code change in `approval_environment.py`, reviewed like any allowlist change). The owner's first environment is `DEV`.

**Deploy configuration (operator-only, in `agent.json`).** Per environment, an ordered list of deploy targets, one per repository that has a deploy command:

```json
{"deploy": {"DEV": {
   "targets": [
     {"repo_id": "portfolio", "argv": ["/abs/path/to/runtime", "/abs/path/to/tool", "{export}/sub/dir", "..."],
      "cwd": "/abs/operator/dir", "env_passthrough": ["SOME_VAR"],
      "success_marker": "<regex on one stdout line>", "failure_text": ["<regex>"], "timeout_seconds": 1800},
     {"repo_id": "product-config", "...": "..."}
   ]}}}
```

- `argv` is an exact argument list with an absolute executable; no shell, no string splitting. The only placeholder is `{export}`, replaced by the export directory of that repository (D9 export); export paths are built by Synapse from safe ids and contain no spaces or shell metacharacters. Arguments are otherwise literal; the operator chooses the tool's flags and owns their consequences.
- `cwd` (optional) and `env_passthrough` (optional) exist because the tool finds its credentials through its working directory or an environment variable. Synapse stores only the directory path and the variable NAMES; it never opens the directory, never reads a credential file and never stores a variable's value. At deploy time the named variables are copied from the daemon's own environment into the child's.
- The child environment is otherwise scrubbed to a fixed minimum (`PATH` set to a fixed system value, `LC_ALL=C`, `HOME` set to a per-deploy scratch directory unless `cwd` requires otherwise). These are always removed and cannot be passed through: `SSH_AUTH_SOCK`, `GIT_*`, `GIT_ASKPASS`, `SSH_ASKPASS`, `CLAUDE_*`, `ANTHROPIC_*`, `OPENAI_*`, any Synapse bearer or approver token variable.
- `command_sha256` = canonical hash of the target list for the environment (argv, cwd, passthrough names, marker, failure text, timeout). It is bound into the approval (below); a config change after approval denies the deploy with `command_changed`.
- A repository marked `"deploy": "none"` in the registry (`tenant-specification`, by owner design: its output is generated by the owner's CI pipeline) is never a deploy target: a deploy configuration naming it refuses daemon start. When it has changes, the approval subject and the deploy view show it as "generated by the pipeline, not deployed by Synapse", and it does not block deploying the repositories that have targets. Synapse never pushes, so the owner moves such a change through the normal pipeline themselves (from the local `synapse/` attempt branch).
- A changed write repository that has neither a target for the environment nor the `deploy: none` marker refuses the deploy (`deploy_target_missing`), so a forgotten target fails closed instead of being skipped silently.
- A repository without changes (`unchanged`) is not deployed.

**Approval.** Every deploy needs its own explicit approval:

- **Requester:** the daemon, through the existing `DaemonRequesterAuthority` (subject `daemon:workflow`, kind `agent`), when the operator asks for a deploy approval of a manifest and environment. The operator bearer token cannot request or decide by itself (unchanged).
- **Approver:** the owner, through the existing local approver authority (`X-Approver-Token`). No passphrase mode in this slice, by owner decision; the passphrase authority that already exists stays available but is not enabled. Self-approval refusal is unchanged: `ApprovalService.decide` denies when the actor's subject equals `requester_subject`, and the human approver subject is never `daemon:workflow`.
- **Binding:** `operation = "deploy"`, `target = <environment>`, `candidate_manifest_sha256 = manifest_sha256`, plus the ADR 022 environment binding. The approval's displayed subject includes the per-repository baseline and candidate SHAs, the targets in order with their `command_sha256`, and the "generated by the pipeline, not deployed by Synapse" list. A trigger in migration 0019 refuses the insert when the run has `run_repo_bindings` rows unless the manifest exists for the same run and has no `candidate_manifest_invalidations` row. The one other approval such a run can hold is the `agent_run_multi` spawn gate (D5): an approval whose operation, target, policy version and `candidate_sha256` equal a multi-repo task's recorded `approval_step` (bound to the spec hash, which covers `bindings_sha256`); the trigger allows exactly that, and only for the operation `agent.run` (an allowlist, `MULTI_REPO_SPAWN_OPERATION` in `persistence/agent_tasks.py`, which also refuses any other operation in a multi-repo `approval_step`), so no deploy-like name such as `DEPLOY` or `deploy:DEV` can use it. A second trigger refuses the transition to `approved` for an approval bound to an invalidated manifest, matched by `manifest_sha256` alone (not joined on the approval's run).
- **At decision:** the decision path refuses only on recorded invalidations: if the manifest has any invalidation row (`baseline_moved`, `candidate_invalid` or `operator`), `ApprovalService.decide` denies `manifest_invalidated` and the 0019 trigger refuses the transition. It does **not** re-run D2 or `verify_manifest_candidates` (no git in the decision path), so a baseline that moved after the request is only caught if something recorded it. The git re-checks are therefore the job of the deploy pre-check (step 2 below), which must call `ManifestBuilder.recheck` and is mandatory: an approved manifest is never deployable on the strength of the approval alone.
- **Single use:** the deploy's ledger keys include the approval id; a second deploy needs a new approval. A retry produces a new manifest hash, so no approval transfers between candidates. A deploy refused by a pre-check before any target started does not consume the approval.

**Pre-checks, immediately before the first target.** In order; any failure refuses with nothing started:

1. `verify_approval_for_environment` (approved, unexpired, unused, environment equal, `command_sha256` equal);
2. `ManifestBuilder.recheck` (mandatory; the decision did not re-run git): the manifest has no invalidation row; D2 baseline check; `verify_manifest_candidates`;
3. only one deploy per environment runs at a time (per-environment lock).

**Export.** For each target repository, the daemon materializes `candidate_sha` into a fresh directory `<state>/deploy-exports/<deploy_id>/<repo_id>` (0700): a temporary index is filled with `git read-tree candidate_sha` and written with `git checkout-index -a` using the hardened git of `WorktreeManager` (attributes pinned to the empty tree, so no `export-ignore`, `export-subst`, filters or hooks apply). The export's file list and blob hashes are then compared with `git ls-tree -r -z candidate_sha`; any difference refuses the deploy (`export_mismatch`). The deploy never reads an agent worktree or the operator's checkout: untracked or ignored files cannot reach it, and the tool's in-place renames happen in a disposable copy.

**Run.** For each target in configured order:

1. insert a side-effect ledger intent (ADR 021) keyed `(task = "deploy:<run_id>", attempt = <approval id>, candidate = manifest_sha256, step = "deploy:<repo_id>", environment)`; if the insert does not return `PROCEED`, stop;
2. run `argv` once: own session and process group, the D9 environment, `cwd` as configured (else the export's parent), stdin closed (`/dev/null`), stdout and stderr captured;
3. on timeout: SIGTERM to the process group, a 10 s grace, then SIGKILL to the group and a check that no member survives. SIGINT is never relied on;
4. outcome: `done` only if exit status 0 AND the success marker matched a stdout line AND no `failure_text` pattern (usage text) matched; `not_done` only if the process provably never started (exec failed); everything else (non-zero exit, missing marker, usage text, timeout, signal, daemon crash, kill not verified) is `unknown`, because the tool has no rollback and earlier steps may be applied;
5. stop at the first target that is not `done`; the remaining targets are recorded `not_started` and get no ledger intent.

A daemon crash during a deploy leaves `intent` rows that recovery turns into `unknown` (ADR 021). `unknown` leaves only through an explicit owner resolution (`done` or `not_done`, with a note whose text is hashed, not stored) after checking the environment; nothing is retried automatically. A status probe is optional per target (`status_argv`, same rules, read-only by operator assertion); the owner's current tool has none, so in v1 the post-run evidence is the outcome rule above plus the owner's own check.

**Output.** Raw stdout and stderr go to a 0600 file under `<state>/deploy-output/<deploy_id>/` that stays local and private: it is never stored in SQLite, never returned by the API and never put in events. SQLite stores only a redacted summary per target: exit status, signal, marker seen, usage text seen, byte counts, `output_sha256`, and a bounded tail (last 20 lines, 4 KiB) after redaction of URLs, host names, IP addresses, e-mail addresses and anything that looks like a token or key. Exports are deleted after a target ends `done` or `not_done`, and kept for inspection when it is `unknown` (removed with the deploy's output on operator request).

### D10. Migrations 0018 (registry) and 0019 (manifests)

The runner needs a contiguous sequence. The registry tables (`run_repo_bindings`, `attempt_worktrees`, `attempt_worktree_removals`, `attempt_repo_commits`) landed as **migration 0018** (`0018_multi_repo_runs.sql`, slice 1). The candidate manifest tables and the approval triggers are **migration 0019** (`0019_candidate_manifests.sql`). The deploy tables (`deploys`, `deploy_target_results`) are not in 0019; they come with the deploy service in the next free migration.

| Table | Shape | Mutability |
|---|---|---|
| `run_repo_bindings` (0018) | `run_id, repo_id, path, baseline_ref, baseline_sha, writer_task_id, entry_sha256, bindings_sha256`, PK `(run_id, repo_id)` | insert-only (UPDATE/DELETE triggers abort) |
| `attempt_worktrees` (0018) | `attempt_id, repo_id, access, path, branch, created_at`, PK `(attempt_id, repo_id)` | insert-only |
| `attempt_worktree_removals` (0018) | `attempt_id, repo_id, removed_at, reason, forced` | insert-only |
| `attempt_repo_commits` (0018) | `attempt_id, repo_id, seq, state (intent/done/unchanged/failed/not_done/unknown), head_before, candidate_sha, reason, at` | append-only transitions |
| `candidate_manifests` (0019) | `manifest_sha256` PK, `run_id, artifact_id, bindings_sha256, version, seq, created_at` | insert-only |
| `candidate_manifest_invalidations` (0019) | `manifest_sha256, reason (baseline_moved/candidate_invalid/operator), actor, at` | insert-only |
| `deploys` (later) | `deploy_id` PK, `run_id, manifest_sha256, approval_id, environment, command_sha256, created_at` | insert-only |
| `deploy_target_results` (later) | `deploy_id, repo_id, seq, state (started/done/not_done/unknown/not_started/resolved_done/resolved_not_done), exit_status, signal, marker_seen, usage_seen, output_bytes, output_sha256, redacted_tail, resolver, note_sha256, at` | append-only transitions |
| triggers on `approval_requests` (0019) | BEFORE INSERT: as in D9; BEFORE UPDATE OF state: no `approved` for an invalidated manifest | n/a |

The side-effect ledger and environment-binding tables (ADR 021/022) keep their inline `ensure_schema`; they are not touched by 0018 or 0019. No column is added to `runs`, `tasks` or `task_attempts`. 0019 must apply from empty and from 0018, contain no `PRAGMA` or transaction statements, and pass the migration fingerprint round trip. Single-repo runs have no `run_repo_bindings` rows and keep working on the legacy path.

## What A builds and what B builds

A (core, daemon, persistence):

- Registry loader and validator for the object form, `registry_sha256`, `bindings_sha256`, startup audit, legacy-string compatibility; deploy configuration validation and `command_sha256`.
- `WorktreeManager`: attempt-depth paths and branches, detached read worktrees, ref-write guard, the D3 commit rule (paths, intersection with `allowed_paths`, mode `100644`, ignored/untracked refusal, `-z` parsing), no-op handling, ordered multi-repo locking, all-or-none create.
- `application/xml_wellformed.py` and its tests; `verify_manifest_candidates`.
- Sandbox: one writable bind per write worktree, read-only binds for read worktrees, per-repo `git_readonly_dirs`/masks.
- Engine and runtime: multi-repo spec shape, approval subject `agent_run_multi`, spawn-time recheck from `attempt_worktrees`, evidence verifier per repository (expected path and HEAD from the bindings), evidence-then-commit order, commit phase with intents, D6 recovery, D4 step 8 cleanup, one-writer and subset checks, profile rules aligned with PR #45.
- Manifest v2 builder and verifier, `candidate_manifests`, invalidations, D2 baseline check.
- ADR 022 allowlist extension (`DEV`), approval trigger and decision-time checks, deploy service (pre-checks, export and verification, ledger intents, command runner with SIGTERM/SIGKILL, outcome rule, output file and redaction, resolution), migration 0019 and its tests.
- Publish the contract B needs before B merges routes: request and response shapes, the refusal-code catalogue, read schemas, and which routes are operator-only versus approver-only.

B (clients, CLI, API routes, docs), against A's published contract:

- Run creation route: `repos: [{repo_id, baseline_ref}]`, per-task `repos: [{repo_id, access}]`, run-level and per-task `profile`; OpenAPI contracts and refusal messages.
- Read routes: `GET /runs/{id}/repos` (bindings, per-attempt worktrees, commit states), `GET /runs/{id}/candidate-manifests` and `GET .../{sha}` (with the per-repository diff for the owner to read), `GET /repos` (read-only registry view with `registry_sha256`), `GET /runs/{id}/deploys` and `GET /deploys/{id}` (redacted summaries only).
- Operator routes calling A's services: build a manifest; request a deploy approval for `(manifest, environment)`; start a deploy with an approved approval; resolve an `unknown` target (owner only).
- `agentctl`: `agent-start --repo ID=REF ... --profile P` and per-task repo flags, `repos list`, `manifest build/show/diff`, `deploy request/start/show/resolve`.
- `synapse workspace discover --write`: emit the object form with explicit ids (operator-only).
- Desktop: per-repository diff and status, manifest view with every repository's baseline and candidate, deploy approval view (targets, "generated by the pipeline, not deployed by Synapse" list, fixed warning that the tool has no rollback), deploy outcome view.
- Docs: `worktrees.md`, `candidate-manifest.md`, `agent-runs.md`, `workspace.md`, API reference, a deploy operations page written generically (no hosts or commands).

## Later (out of the first slice)

- **Parallel runs** (owner wants 2 to 3): lifting the one-active-run rule (ADR 015). This ADR's worktree design is ready for it.
- **Retention sweep**: per-repository `keep_days`, never removing trees of non-terminal runs, of runs with an uncleared unverified fence entry, or backing an unused approval; keep branches of deployed candidates. Until then trees are removed on operator request.
- **Cross-repository integration barrier (M6.6)** with dependency-compatibility checks from the XML dependency index; D2's invalidation is the only barrier in v1.
- **Deploy-order derivation** from the dependency index; v1 uses the operator's configured order.
- **Catalog Validator** results in the manifest (ADR 020-M6 adapter).
- **A status probe** for the owner's tool (it has none) and a preview of what a deploy will change beyond the git diff.
- **Passphrase or stronger approver authentication** (ADR 019).
- **Read access to a sibling task's candidate** in another repository.
- Per-repository configurable editable paths, if a repository ever needs non-XML edits.

## Alternatives rejected

- **One worktree per task, reused across attempts** (today's shape): a retry inherits the failed attempt's edits and the manifest cannot name the attempt.
- **Keeping path and HEAD in the spec for multi-repo tasks**: per-attempt trees and auto-commit make a creation-time path and HEAD wrong for every attempt after the first and after the commit.
- **Commit first, then run the evidence verifier on the commit**: the verifier's `head_moved` signal ("the agent committed") would be lost; evidence-then-commit keeps it, and the commit rule re-checks paths anyway.
- **Deploying from the worktree**: untracked, ignored and post-commit files would reach the tool, and its in-place renames would damage the tree.
- **`git archive` for the export**: it honours `export-ignore`/`export-subst` attributes the agent could influence; `read-tree` plus `checkout-index` with pinned attributes does not.
- **Exit status alone as success**: the tool returns 0 for help output and declined prompts.
- **A scrubbed environment with no passthrough**: the tool would lose its credentials; passing the operator-named variables by name keeps Synapse from ever holding them.
- **One super-repository or submodules; agent commits in the sandbox; a cross-repo two-phase commit; registry in SQLite; re-resolving the baseline per attempt; a deny-list of protected paths**: as argued in the first draft (changes the owner's repositories, needs a writable `.git`, impossible in git and unnecessary, adds an edit surface, moving target, fails open on new file types).

## Consequences and limits

- Same-uid caveat (ADR 019): with the sandbox off, an agent can edit the config file, SQLite, refs and objects. The design then detects (snapshots, re-checks, `verify_manifest_candidates`, export verification) but does not prevent.
- The deploy tool is destructive and has no rollback; a partial deploy is reported per target and needs the owner. Synapse cannot prove a remote state.
- The deploy child runs unsandboxed with network access and the operator-passed credentials, by owner decision; it reads only a verified export.
- Redaction of the stored tail is pattern-based, not a guarantee; the raw output file is the private record.
- Without the retention sweep, disk use grows with attempts × repositories until the operator removes trees and exports.
- XML well-formedness is not schema validity; the owner's diff review and, later, the Catalog Validator carry that.

## Open questions

All questions from the first draft were answered by the owner (table above). Remaining, to confirm at implementation time (not blocking this ADR):

1. The exact deploy target argv, success marker and usage-text patterns, the credential `cwd` or variable name, and the target order, entered by the operator in `agent.json` (kept out of the repository).
2. Closed (owner, 2026-11-24): the deploy proceeds for the repositories that have a target; a `deploy: none` repository is generated by the owner's pipeline and never blocks it (D9).
3. Default deploy timeout (proposed 30 minutes per target) and how long raw output files are kept.
