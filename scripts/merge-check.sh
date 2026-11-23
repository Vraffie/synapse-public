#!/usr/bin/env bash
# Run the suite on several open PRs merged together, the way main will see them after they land.
#
#   scripts/merge-check.sh 101 102 103
#
# In a temporary detached worktree of origin/main, merges each PR head in the given order (fetched with
# `git fetch origin pull/N/head`), runs scripts/check.sh once on the result, and on failure bisects by order to name
# the first PR whose addition breaks the suite. Never pushes and never writes a branch, tag or remote-tracking ref
# (fetches go to FETCH_HEAD only); the worktree is removed on exit.
#
# Environment: MERGE_CHECK_REMOTE (default origin), MERGE_CHECK_BASE (default main), MERGE_CHECK_CMD (default
# scripts/check.sh, run inside the worktree).
set -u
[ $# -ge 1 ] || { echo "usage: $0 <PR number>..." >&2; exit 2; }
for n in "$@"; do case "$n" in ''|*[!0-9]*) echo "not a PR number: $n" >&2; exit 2;; esac; done

remote="${MERGE_CHECK_REMOTE:-origin}"
base="${MERGE_CHECK_BASE:-main}"
cmd="${MERGE_CHECK_CMD:-scripts/check.sh}"
repo="$(git rev-parse --show-toplevel)" || exit 2
wt="$(mktemp -d "${TMPDIR:-/tmp}/merge-check.XXXXXX")"
cleanup() { git -C "$repo" worktree remove --force "$wt" >/dev/null 2>&1; rm -rf "$wt"; git -C "$repo" worktree prune; }
trap cleanup EXIT

git -C "$repo" fetch -q "$remote" "$base" || { echo "cannot fetch $remote $base" >&2; exit 2; }
base_sha="$(git -C "$repo" rev-parse FETCH_HEAD)"
git -C "$repo" worktree add -q --detach "$wt" "$base_sha" || exit 2

g() { git -C "$wt" -c user.name=merge-check -c user.email=merge-check@localhost -c commit.gpgsign=false "$@"; }
prs=("$@")
shas=("$base_sha")   # shas[i] = worktree HEAD after merging the first i PRs
for n in "${prs[@]}"; do
  git -C "$repo" fetch -q "$remote" "pull/$n/head" || { echo "merge-check: cannot fetch PR #$n" >&2; exit 2; }
  head="$(git -C "$repo" rev-parse FETCH_HEAD)"
  if ! g merge -q --no-ff --no-edit "$head" >/dev/null 2>&1; then
    g merge --abort >/dev/null 2>&1
    echo "merge-check: PR #$n does not merge cleanly onto $base plus the earlier PRs"
    exit 1
  fi
  shas+=("$(g rev-parse HEAD)")
  echo "merge-check: merged PR #$n (${head:0:9})"
done

run_at() { g checkout -q --detach "${shas[$1]}" && (cd "$wt" && $cmd); }

echo "merge-check: running $cmd on $base + PRs ${prs[*]}"
if run_at "${#prs[@]}"; then
  echo "merge-check: OK, all ${#prs[@]} PR(s) pass together"
  exit 0
fi

# Smallest k such that the first k PRs fail; assumes k=0 (plain base) passes.
lo=0; hi=${#prs[@]}
while [ $((hi - lo)) -gt 1 ]; do
  mid=$(((lo + hi) / 2))
  echo "merge-check: bisecting, trying first $mid PR(s)"
  if run_at "$mid" >/dev/null 2>&1; then lo=$mid; else hi=$mid; fi
done
echo "merge-check: FAILED. First breaking PR in the given order: #${prs[$((hi - 1))]} (suite passes with $lo PR(s), fails with $hi)"
[ "$hi" -eq 1 ] && echo "merge-check: note, this PR alone on $base fails; $base itself may be red"
exit 1
