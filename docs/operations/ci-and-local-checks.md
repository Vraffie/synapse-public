# CI minutes and local checks

The repository is private on a GitHub Free account: about 2,000 included Linux Actions minutes a month, and a spending
limit of $0 unless a budget is set. When the allowance is used up, jobs fail within seconds with "the job was not
started because recent account payments have failed or your spending limit needs to be increased".

## How the workflow limits usage

- `Validate` runs on pull requests (every push to the PR branch) and on pushes to `main`; it no longer also runs on
  `push` for feature branches, which used to run every check twice.
- A newer push to the same branch cancels the older run. Runs on `main` are never cancelled.
- Documentation-only pull requests (`*.md`, `docs/**`) skip the run.

## Local checks

`scripts/check.sh` runs the same tests as CI, locally and for free:

| Command | What it does |
|---|---|
| `scripts/check.sh` | Full suite with the desktop extra, offscreen Qt (about 2 minutes) |
| `scripts/check.sh --quick` | Skips desktop and real-daemon tests (about 80 seconds, for small changes) |
| `scripts/check.sh <pytest args>` | Passes the arguments to pytest |

It exits non-zero if any test fails. Prefer it to `pytest ... | tail`, which hides the exit status.

## Pushing in bursts

Each push to an open PR starts a run. Batch commits locally and push once, rather than pushing every commit.

## Pre-push hook (opt-in)

`scripts/pre-push` runs `scripts/check.sh --quick` before a push and blocks the push if it fails. It only runs when the
push contains changes under `src/` or `tests/`; a docs-only push, and a branch deletion, skip it. It prints only the
output of `check.sh` and never the environment or remote URL.

Install it once per clone (nothing is installed automatically):

```
scripts/install-hooks.sh            # symlink into the hooks dir reported by `git rev-parse --git-path hooks`
scripts/install-hooks.sh --force    # replace a different existing pre-push hook (kept as pre-push.bak)
scripts/install-hooks.sh --remove   # remove it again
```

The installer is idempotent and refuses to overwrite an existing hook without `--force`. Worktrees share the hooks
directory of the main checkout, so one install covers them.

Bypass for one push: `SKIP_CHECK=1 git push` or `git push --no-verify`.

## Checking several PRs together

Main went red when PRs that each passed alone were merged without ever being run together. Before merging a batch:

```
scripts/merge-check.sh 109 111 113
```

It creates a temporary detached worktree of `origin/main`, fetches each PR head (`git fetch origin pull/N/head`, into
`FETCH_HEAD` only) and merges them in the given order, then runs `scripts/check.sh` once on the result. If a PR does not
merge cleanly it says so and stops. If the suite fails it bisects by order (re-running the suite on prefixes of the PR
list) and names the first PR whose addition breaks it; it assumes plain `origin/main` is green. The worktree is removed
on exit. The script never pushes and never creates or changes a branch, tag or remote-tracking ref. Put the PRs in the
order you intend to merge them. Optional environment: `MERGE_CHECK_REMOTE`, `MERGE_CHECK_BASE`, `MERGE_CHECK_CMD`.
