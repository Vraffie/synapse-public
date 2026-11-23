# Per-task git worktrees

`synapse.infrastructure.worktrees.WorktreeManager(repo_allowlist, root_dir)`
gives each task an isolated checkout so a real agent (for example
`ClaudeCliProvider`) can work without touching the main repository checkout.
The agent-runs API uses it for the per-run worktree.

## API

- `create(run_id, task_id, repo, base_ref) -> str`: runs
  `git worktree add -b synapse/<run>/<task> <root>/<run>/<task> <sha>` and
  returns the absolute path. Idempotent: an existing worktree for the same
  repo/branch is returned unchanged. A leftover branch (from a removed
  worktree) is resumed rather than reset.
- `status(run_id, task_id)`: clean/dirty, HEAD, branch, changed path count and
  `diff --stat`, output bounded (default 64 KiB, `truncated` flag).
- `diff(run_id, task_id)`: bounded `git diff HEAD` (tracked changes only).
- `remove(run_id, task_id, force=False)`: refuses a dirty tree (including
  untracked files) unless `force`. Returns `False` if already gone. The branch
  is deleted with `-d` (kept if unmerged and not forced; `-D` when forced).

## Safety properties

- Repos must be on an explicit allowlist of absolute, normalized,
  symlink-free, user-owned paths with a real `.git` directory. `root_dir` has
  the same checks, is created `0700`, and must not overlap an allowlisted repo.
- Multi-repository allowlist: the operator lists repositories in the agent config
  (`repos`, with `worktree_root`; both or neither). No config means no manager and no
  worktrees (fail closed); an empty list is refused. Allowlisted entries must not be nested
  inside each other. Every `create`/`check_base` call re-validates the requested path: it
  must equal an allowlisted entry exactly and still be its own realpath, so `..`, relative,
  trailing-slash, child-directory and symlink spellings (including a symlink swapped over
  an allowlisted path after startup) are refused. A repository not on the list raises
  `RepoNotAllowlistedError` (a `ValueError` subclass) with no filesystem side effects.
- Per-repo isolation: each worktree records its owning repository; `status`/`diff`/`remove`
  act only through that repository's git dir, branches (`synapse/<run>/<task>`) live only
  in the owning repo, and locks are per repository. A worktree whose repo has left the
  allowlist is refused by every operation. The layout is `<root>/<run>/<task>`, so the same
  (run, task) pair cannot span two repositories: a second `create` for another repo is
  refused with `WorktreeError` rather than shared. Use distinct task ids per repo.
- `run_id`/`task_id` pass `validate_id` and `[A-Za-z0-9][A-Za-z0-9._-]{0,127}`
  (no `..`, no `.lock`/`.` suffix), so they are safe path and ref components.
- `base_ref` must not start with `-`, and must resolve with
  `git rev-parse --verify --end-of-options <ref>^{commit}`; the resolved SHA is
  what is checked out.
- git is run with an argv list (no shell), a scrubbed environment (`PATH`,
  `HOME=/nonexistent`, no system/global config, no prompts), its own process
  group, `GIT_NO_LAZY_FETCH=1` and the global option `--no-lazy-fetch` (a partial
  clone never fetches, which would run `remote.*.uploadpack`; a git too old to know
  the option rejects it, so calls fail closed), and `-c core.hooksPath=/dev/null -c core.fsmonitor=false
  -c core.pager=cat -c core.attributesFile=/dev/null -c diff.external=`.
  Diffs use `--no-ext-diff --no-textconv`.
- Unsafe repository state is refused with a clear `WorktreeError`, and the check is
  **re-run before every git call that touches a work tree** (`status`, `diff`,
  `remove`) as well as at `create`, because a linked worktree shares its repo
  config with whatever shell the agent runs. Refused: `filter.*.clean|smudge|process`,
  `include.path`/`includeIf.*.path`, `alias.*` with a `!` shell value, `core.fsmonitor`
  other than false, `core.hooksPath`, `core.sshCommand`, `core.gitProxy`,
  `core.attributesFile`, `core.askPass`, `core.editor`, `diff.external`,
  `diff.*.command|textconv`, `merge.*.driver`, `credential.*helper`, `gpg.*program`,
  `remote.*.uploadpack|receivepack|vcs|proxy`, `core.alternateRefsCommand`,
  `uploadpack.*`, `http.*proxy`, config-defined hooks `hook.*` (git 2.54+ runs them even
  with `core.hooksPath=/dev/null`), `gpg.ssh.defaultKeyCommand`,
  `extensions.worktreeConfig`, any `config.worktree`
  file, a non-comment `info/attributes` file in the repository's git directory, and a
  `.git/config` too large (over 1 MiB listed) to check completely, and any
  `.git/commondir` (it makes git take config, `info/attributes`, objects and refs
  from another directory; also refused when the allowlist is loaded). The check lists
  the effective config with an explicit `--git-dir=<repo>/.git config --local`, so it
  never walks up to another repo. The check runs first in `check_base` (before any other
  git query) and also before `remove`, including `remove(force=True)`.
- Host-side `status`/`diff` in a worktree pass `--ignore-submodules=all`, so git never
  starts a child process inside a nested repository the agent created (its own config
  and `info/attributes` are not checked by the guard). A linked worktree's
  `.git/worktrees/<id>/commondir` must point back at the owning repository's `.git`.
- Work-tree git additionally runs with `--attr-source=<empty tree>`, so the tree's
  `.gitattributes` cannot enable a filter or diff driver even if a config plant
  slips in between the check and the call.
- Every git call has a wall-clock deadline (60 s) enforced by reader threads; on
  expiry (or output overflow) the whole process group is killed and the call
  raises `git timed out`. A hung filter cannot hold a call open.
- The agent API runs all worktree git in worker threads (`asyncio.to_thread`)
  and serializes starts with a lock, so a slow or hostile worktree cannot block
  the event loop (approval decisions, cancel, expiry sweep).
- `remove`/`status`/`diff` resolve the owning repo from the worktree's `.git`
  file, require it to be allowlisted and the backlink to match, and refuse
  paths with symlinks or outside `root_dir`.
- One lock per repo (in-process `threading.Lock` plus `flock` on a file in
  `root_dir`) serializes create/remove.

## Commit

`WorktreeManager.commit` stages everything in a task's worktree and commits it onto the task's own branch with the same hardened host git as the other operations (hooks, filters, attributes and config guard). It refuses a nested repository (gitlink), any `.gitmodules` change and more than 1000 changed files, judged on the staged index; any refusal resets the index. The operator-facing action is `agent-worktree-commit` (see [agent runs](agent-runs.md#getting-the-edits-out-agent-worktree-commit)).

## Provider compatibility

`git worktree add` writes a `.git` *file* (`gitdir: ...`), not a directory.
`ClaudeCliProvider`'s `validate_worktree` accepts any non-symlink existing
`.git` entry, so it accepts these worktrees unchanged (covered by a test). The
caller must keep `root_dir` outside the daemon state directory.

## Caveats

- This is mitigation, not containment. A residual race (TOCTOU) exists between the
  config check and the git call; the empty attr-source pin, disabled hooks/fsmonitor and
  `--no-ext-diff --no-textconv` narrow it, but the repository-level `info/attributes`
  file cannot be overridden by git and is covered only by the refusal check and the
  deadline.
- The agent can still edit shared config, hooks and refs. A refused check surfaces as
  `exists: false` in the agent API view and as a refused `remove` without `force`.
  The agent's own use of the git CLI (for example a `Bash(git:*)` tool allowance) is
  arbitrary code execution as the same user: do not treat a worktree as a trust
  boundary and only allowlist repos you trust as much as the user account.
- `create` for an already existing worktree only reads the branch name (no work-tree
  hashing) and does not run the full tree check.
- Branch `-d` may keep a branch not merged into the repo HEAD; it is reused on
  the next `create`.
