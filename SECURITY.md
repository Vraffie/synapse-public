# Security policy

Synapse is early-stage software. Real provider execution and deployment are disabled by default, and the daemon
is designed for a single trusted local user. Read the trust limits before relying on any security property:

- [Security review](docs/security/review-2026-11-20.md) and its follow-ups (known, partly open findings).
- [Release gates](docs/security/release-gates.md).
- [Approver identity](docs/operations/approver-identity.md): approvals are not a security boundary against
  agent processes running as the same user.

## Reporting a vulnerability

Please do not open a public issue for a security problem.

Report it privately through GitHub: the repository's **Security** tab, then **Report a vulnerability** (GitHub
private vulnerability reporting). Include what you saw, the version or commit, and the smallest steps that
reproduce it. Do not include real credentials or tokens in a report.

You can expect an acknowledgement, and a status update once the report has been triaged. This project does not
currently run a bounty programme or promise fixed response times.

## Scope

In scope: authentication and authorisation of the local API, approval binding and verification, credential
handling, process and worktree isolation, and the backup and restore tooling.

Already documented as open (see the security review): the database and token files are readable by any process
of the same user, so a same-user agent is not contained by approvals alone; reports that restate these without a
new bypass are appreciated but are not new findings.

Out of scope: findings that need an attacker who already controls the same user account and are listed above,
and issues in third-party dependencies without a Synapse-specific exploit path.
