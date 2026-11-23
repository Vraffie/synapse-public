# Making the repository public: checklist and plan

Status: plan only. The repository is private. Nothing here has been executed, and nothing in this document
authorises a visibility change, a history rewrite or a force-push. Each numbered gate needs the owner's explicit
go-ahead. `AGENTS.md` forbids agents from force-pushing or deleting protected branches, so the history step below
is prepared for the owner to run or to approve explicitly.

## Decisions recorded

| Topic | Decision |
| --- | --- |
| Licence | MIT (`LICENSE`, `NOTICE`). The owner must confirm they hold the rights to license the code (for example no employer or client claim) before publication. |
| Business names (`STAGING`, Catalog Validator, the Domain plugin) | Stay in the repository; the owner states they are theirs to publish. |
| Author emails in history | Must not be published as they are. Prefer a clean public copy (gate 3). |
| Visibility | Public, but only after gates 1 to 6. Making it public also gives free Actions minutes, which unblocks CI. |

## Gates

1. **Merge reviewed work first, while private.** Land the reviewed pull requests before any history change, so no open
   PR or branch has to be rewritten. This needs CI to run (the Actions usage limit) or an explicit owner decision to
   merge on recorded local test evidence for each reviewed PR. Update `IMPLEMENTATION_BACKLOG.md`.
2. **Secret scan of everything reachable.** (Done once on 2026-11-22 with `detect-secrets`: no findings in the added lines of
   every commit on every branch (about 71,000 lines); the tracked-file scan flagged only test sentinels, fake keys, a
   doc sentence and git's well-known empty-tree hash. This is a heuristic scanner, not an audit: repeat it with the
   tools below on the final tree.) Run a dedicated scanner (for example `gitleaks detect --log-opts="--all"`
   and `trufflehog git file://. --since-commit ""`) over all branches, tags and PR refs, plus GitHub secret scanning
   once enabled. A grep of the tree and history for common token shapes found nothing, but that is not a scan. Any
   hit means rotating the credential first; deleting it from history is not enough.
3. **Remove the personal emails from the public history.** Two author addresses (an employer-style one and a
   personal-domain one) appear on almost every commit. Options, in order of preference:
   1. **Fresh public repository (preferred, non-destructive).** Export the final tree of `main` into a new repository
      with one initial commit authored with the GitHub `noreply` address. The private repository stays untouched, so
      PR links and branches keep working there. Lose: per-commit history in the public copy.
   2. **Rewrite in a clone.** `git filter-repo --mailmap mailmap.txt` on a fresh clone, mapping both addresses to the
      `noreply` address, then push to a new empty public repository (never over the private one). Review the result
      before pushing; PR refs are not carried over.
   3. **Rewrite and force-push this repository.** Breaks every PR link and every local clone; not allowed for agents.
      Only if the owner explicitly wants it, and only after gate 1 with no open PRs.

   Going forward, set `user.email` to the `noreply` address, and stop adding `Claude-Session` links that point at
   private sessions if they are not wanted.
4. **Review what the documents say.** (`SECURITY.md` is added; it assumes GitHub private vulnerability reporting is turned on in the repository settings.) `docs/security/` lists open findings (same-user token and database exposure,
   agent containment, audit rate-limiting). The owner decides whether to publish them as they are; either way add a
   `SECURITY.md` with a private reporting route (GitHub private vulnerability reporting) before going public.
   Also skim `AUTONOMOUS_EXECUTION.md` and `AUTONOMOUS_PROGRESS.md` for operational detail that should not be public.
5. **Repository settings.**
   - Secret scanning and push protection on; Dependabot alerts on; private vulnerability reporting on.
   - Branch protection on `main`: required checks (`sqlite-schema`, `desktop-smoke`), required review, no force-push.
   - Actions: require approval for workflows from first-time contributors; keep the default read-only token. The
     workflow uses no secrets and only `contents: read`, so fork pull requests get no secrets.
6. **Contribution files.** `CONTRIBUTING.md` is added and states that outside contributions are not accepted yet; add a
   code of conduct only if that changes. Add a `.github/dependabot.yml` (weekly `pip` and `github-actions` updates) only once CI runs, because it opens pull requests
   as soon as it is on the default branch. State the real maturity: real provider execution is disabled by default, production
   deployment is disabled, and the same-user trust limits in `docs/security/` apply.

## After publishing

- Confirm the Actions jobs get runners and pass on a no-op pull request.
- Re-run the secret scan on the public copy and check `git log --format=%ae | sort -u` contains only the intended
  address.
- Update `IMPLEMENTATION_BACKLOG.md` (M0 licence and visibility item) and `docs/operations/install.md` if the clone
  URL changes.

## Rollback

Visibility can be set back to private at any time, but anything already cloned or indexed stays public. If a secret
is found after publication, rotate it first, then handle the history.

## Fresh public copy: exact commands (owner runs these; nothing here has been run)

Run from a clone of the private repository after gate 1, with the checkout on the commit you want to publish.
The public history is a single commit, so the old author emails never reach it.

```bash
# 1. Export the tracked files only (no .git, no untracked or ignored files).
git archive --format=tar HEAD | (mkdir ../synapse-public && tar -x -C ../synapse-public)
cd ../synapse-public

# 2. Check the export before committing: no stray files, no secrets.
git init -q -b main
git config user.name  "YOUR NAME OR HANDLE"
git config user.email "ID+USERNAME@users.noreply.github.com"   # your GitHub noreply address
gitleaks detect --no-git --source .                                # and trufflehog filesystem .

# 3. One commit, then push to a NEW empty repository (never over the private one).
git add -A && git commit -q -m "Initial public release"
git remote add origin git@github.com:OWNER/NEW-REPO.git
git push -u origin main
git log --format='%an <%ae>' | sort -u                             # must show only the noreply identity
```

Afterwards: enable secret scanning with push protection, private vulnerability reporting, Dependabot alerts and
branch protection on the new repository, and confirm the Actions jobs get runners. The private repository is
unchanged and keeps its history, branches and pull requests.
