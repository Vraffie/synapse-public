# Decision needed: should the daemon outlive your login session?

**Status: recommendation only. The owner has not decided. Nothing in the repository enables lingering, and the package never runs `loginctl enable-linger` (`arch-package.md`, "Not covered").**

## Options

A. **Plain user unit, no lingering (current behaviour).** The unit runs under the user manager, which by standard systemd behaviour exists while you have a login session (`[UNVERIFIED]` on this machine: not run). `systemctl --user enable` alone does not change that.

B. **User unit plus `loginctl enable-linger "$USER"`.** The user manager, and so the enabled unit, starts at boot and survives logout. Printed as optional in `systemd-user-service.md` and the package `.install` script; never done for you.

## Trade-offs

| | A: no linger | B: linger |
| --- | --- | --- |
| Runs after logout | No: the daemon stops | Yes |
| Interrupted work | Logout is a stop; interrupted runs are handled by startup recovery (`recover_interrupted`) at next start | Fewer interruptions |
| Attack surface | Daemon exists only while you are present | Daemon (and any enabled agent runs) can act unattended; approvals still gate effects, but the approver token is a same-user file (`systemd-user-service.md`) |
| Parked approvals | Resume only while you are logged in | Wait unattended; expiry rules in `approval-expiry.md` |
| Resource use | None when logged out | Always on |
| Reversibility | n/a | `loginctl disable-linger "$USER"`; changes login-manager state only, no data migration |
| Backup and restore | Daemon is gone at logout, simplifying offline restore | Must stop the unit explicitly before restore |

Real agent runs are not supported inside the shipped unit today (`systemd-user-service.md`), so lingering would currently only keep the mock/daemon-only configuration alive.

## Recommendation (needs the owner's decision)

Default to **A (no lingering)**. Reasons: nothing in Synapse today needs unattended execution; production deployment is disabled (`AGENTS.md`); agent runs are unsupported under the unit; and the approver token is same-user, so an always-on daemon widens the unattended window for no current benefit. Offer B as an explicit per-user opt-in once a feature needs overnight runs, and revisit when M7.1/M7.2 land. Document the opt-in command; never run it from packaging.

Owner action: record the choice here (replace "Status"), and if B becomes the default, state it in `arch-package.md`.
