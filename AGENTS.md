# Instructions for Claude Code and Codex

Read `docs/architecture/ARCHITECTURE.md` and `IMPLEMENTATION_BACKLOG.md` before making changes. Treat the architecture as the baseline; propose changes through a documented ADR rather than silently altering requirements.

## Non-negotiable constraints

- General-purpose core; Domain remains an optional plugin.
- Core domain modules must not import Qt, FastAPI, SQLite, or provider-specific SDKs.
- Only daemon-owned repositories may mutate authoritative SQLite state.
- Never copy, export, inspect, or embed subscription credentials; support only documented CLI profile isolation.
- Never rotate accounts to circumvent rate limits, quotas, or provider restrictions.
- Use isolated managed Git worktrees for agent edits; never alter unrelated user files.
- **Standing user authorization for the Synapse M1–M3 autonomous mission:** agents may push to dedicated nonprotected feature branches, open pull requests, and merge their own M1–M3 pull requests into `main` after all required tests and reviews pass. Do not bypass branch protection, required checks, required reviewers, or GitHub permissions. Never force-push or delete protected branches. If verification is unavailable, failing, or inconclusive, do not merge.
- Destructive operations, STAGING deployment, production deployment, credential handling outside documented flows, and changes outside the M1–M3 mission still require separate explicit human approval.
- Production deployment is disabled.
- Keep validation evidence complete; do not optimize tokens at the expense of correctness.
- Implement mock adapters and tests before enabling live CLI execution.

## Collaboration protocol

- One issue/task per agent; record ownership and file scope before parallel editing.
- Communicate through structured messages/artifact references once the communication API exists.
- Until then, coordinate using pull requests and task notes; do not claim MCP communication is implemented.
- Require independent review for security-sensitive and deployment code.
- For every change: tests, migration compatibility, failure modes, and updated documentation.

## Initial assignment

Implement milestone M1 in `IMPLEMENTATION_BACKLOG.md`. Do not jump directly to provider authentication or deployment. The current repository is a specification scaffold, not a functioning orchestrator.