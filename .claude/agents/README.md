# Claude Code development subagents

These files are **development aids** for working on the Synapse repository inside Claude Code sessions. They are **not part of the Synapse runtime**: the daemon never loads them, and they are unrelated to the agent roles Synapse launches for users. Claude Code reads project subagents from this directory automatically.

| Agent | Use it when | Access |
|---|---|---|
| `security-reviewer` | independent security review of adapters, credential handling, process supervision, approval/API code | read-only |
| `migration-checker` | a migration (or a table/trigger a migration defines) changes; finds numbering collisions across open PRs | read-only (runs tests) |
| `test-runner` | you want the relevant tests and then the full suite run, with exact first errors and flaky detection | runs tests, never edits |
| `adapter-verifier` | a provider adapter is added or changed; checks the must-have fake and real tests, asks before any real run | read-only until you approve real runs |
| `docs-sync` | docs, backlog checkboxes or ADR status may be stale | proposes edits only |
| `pr-conflict-planner` | many PRs are open and you need a merge order and rebase plan | read-only; never merges or pushes |

Rules they all follow (see `AGENTS.md`): never touch another agent's or the user's PRs, never merge or comment on PRs, real-binary runs spend the user's subscription and need explicit approval, and every claim is marked proved or unverified.

`tests/test_claude_dev_subagents.py` validates the frontmatter of each file.
