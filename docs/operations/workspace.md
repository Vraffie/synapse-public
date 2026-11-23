# Workspace discovery: pick repositories out of a project folder

A run works on **one** git repository, through a worktree cut from a committed base ref. Many projects live in
a folder that is not itself a repository: for example `~/Projects/BSS` with several sibling repos, a `tools`
repo, and plain folders such as `data/`, `documentation/`, `reports/`. Workspace discovery lists the
repositories in such a folder so the operator can register the ones agents may work on.

```sh
python scripts/synapse_up.py workspace ~/Projects/BSS
python scripts/synapse_up.py up --workspace ~/Projects/BSS --repo api --repo tools --dry-run
python scripts/synapse_up.py up --workspace ~/Projects/BSS --repo api --repo tools
```

Code: `synapse.application.workspace` (`discover_repos`, `inspect_repo`, `select_repos`); the CLI is
`scripts/synapse_up.py`. Tests: `tests/test_workspace_discovery.py`, `tests/test_synapse_up.py`.

## The allowlist stays operator-only

Registering writes the chosen absolute paths into the `repos` list of `agent.json` (the same merge as
`up REPO`: sandbox forced on and never turned off, a config with the sandbox off is refused, mode `0600`, no
duplicates, no repo nested in another registered one, at most 32). The daemon reads that list once at start;
restart it (`down`, then `up`) to pick up new repos. Nothing in the daemon API, the desktop or an agent run can
discover or add repositories: discovery is a local operator tool only.

## What is discovered

- The folder itself, if it is a repository, is reported as one repository (`.`) and nothing else.
- Otherwise every directory down to `max_depth` (default 2: the folder's children and grandchildren) is
  checked for a `.git` entry (or the `HEAD`/`objects`/`refs` layout of a bare repo).
- A repository found at depth N stops the walk there: nested repositories (vendored checkouts, submodules)
  are not reported separately.
- Each repository is described with: absolute resolved path, name, current branch (or detached), HEAD sha,
  up to 50 local branch names (current first, then sorted), commit count up to 10000 (shown as `10000+`
  beyond), uncommitted entries (`git status --porcelain`, tracked changes plus untracked files, capped at
  10000), shallow flag, and `usable` with a reason when not.

Not usable, with the reason shown:

| Case | Why |
|---|---|
| no commits | a worktree needs a committed base ref |
| unsafe config (`core.fsmonitor`, `core.hooksPath`, filters, `include.path`, `!` aliases, `config.worktree`, `info/attributes`, ...) | exactly the rule `WorktreeManager` applies before every run, so the daemon would refuse it anyway |
| bare repository | no working tree |
| `.git` is a file (linked worktree or submodule) | register the main repository instead |
| `.git` is a symlink, or not owned by you | the daemon's allowlist rules refuse these |
| git query failed or timed out | reported, never retried silently |

## Bounds and what is NOT read

- The walk uses `os.scandir`/`lstat` and looks at **names only**. It never opens a file in the folder.
- Symlinks are never followed. A symlink to a directory (for example to a repo elsewhere) and a symlink loop
  are listed as *skipped*; symlinks to files are ignored.
- Hidden directories (`.git`, `.venv`, `.cache`, ...) and `node_modules`, `__pycache__`, `site-packages`,
  `venv` are not descended into.
- At most `max_entries` (2000) directory entries and `max_repos` (200) repositories; when a bound is hit the
  scan says so (`truncated`), and `depth_limited` says some folders below `max_depth` were not looked into.
  An unreadable directory is listed as skipped and the walk continues.
- The input folder must be an existing, absolute, resolved directory without control characters (the CLI
  expands `~` and resolves it first). Names with control characters are skipped; names printed in the table
  are escaped if not printable.

Inside each found repository only read-only git queries run (`config --local --list`, `symbolic-ref`,
`rev-parse`, `for-each-ref`, `rev-list --count`, `status --porcelain`). Repository contents and config are
untrusted input: every call goes through the hardened runner in `synapse.infrastructure.worktrees`
(`-c core.hooksPath=/dev/null -c core.fsmonitor=false -c core.pager=cat -c protocol.ext.allow=never
-c diff.external= ...`, `GIT_CONFIG_NOSYSTEM=1`, `GIT_CONFIG_GLOBAL=/dev/null`, `HOME=/nonexistent`,
`GIT_TERMINAL_PROMPT=0`, `GIT_OPTIONAL_LOCKS=0` so `status` does not rewrite the index, argv lists without a
shell, a wall-clock timeout per call and an output cap). `--git-dir`/`--work-tree` are passed explicitly so git
never walks up into a parent repository, and `status` uses attributes from the empty tree and
`--ignore-submodules=all`. The unsafe-config check runs first; for a refused repository `git status` (the only
query that could reach filters) is never run. The config check also reads `.git/info/attributes` (as
`WorktreeManager` does); no other file is read by Synapse itself.

## Uncommitted changes are not visible to agents

The table marks repositories with uncommitted changes. A run's worktree is cut from a **committed** ref
(`--base-ref`), so uncommitted or untracked work in your checkout is **not** visible to the agent, and the
agent's results land on its own worktree branch (`synapse/<run>/<task>`), never in your working tree. Commit
(or push to a branch) what the agent should see, and pick the base ref accordingly: many repos use `develop`
or a feature branch rather than `main`.
