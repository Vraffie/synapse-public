# systemd user service

`packaging/systemd/synapse-daemon.service` runs the supervised daemon (`python -m synapse.supervisor`, see `supervisor.md`) as a systemd **user** unit. It needs no root.

## Layout

| Item | Location |
| --- | --- |
| Python venv | `~/.local/share/synapse/venv` |
| Migrations | packaged in the wheel (`synapse/persistence/migrations_sql`); no copy needed |
| State dir (mode 0700, created by systemd) | `$XDG_STATE_HOME/synapse`, default `~/.local/state/synapse` |
| Socket / tokens | `<state dir>/agentd.sock`, `operator.token`, `approver.token` |

The unit uses `StateDirectory=synapse` so systemd creates the private state directory the daemon insists on. If you change the paths, edit `ExecStart` in your installed copy.

## Install

```sh
python -m venv ~/.local/share/synapse/venv
~/.local/share/synapse/venv/bin/pip install /path/to/synapse   # add .[desktop] if wanted
mkdir -p ~/.config/systemd/user
cp packaging/systemd/synapse-daemon.service ~/.config/systemd/user/
systemctl --user daemon-reload
```

Migrations ship inside the package, so upgrading the package upgrades them. See `install.md`.

## Start, status, logs

```sh
systemctl --user enable --now synapse-daemon.service
systemctl --user status synapse-daemon.service
journalctl --user -u synapse-daemon.service -f
agentctl --state-dir ~/.local/state/synapse health
```

To keep it running after logout: `sudo loginctl enable-linger "$USER"`. This is an explicit opt-in and off by default; see the lingering decision in [arch-package.md](arch-package.md#lingering-decision).

`Restart=on-failure` adds a second layer on top of the supervisor's own bounded restarts (five by default); the unit stops retrying after five starts in five minutes (`StartLimit*`).

## Tokens

- The **operator token** is created by the daemon on first start as `<state dir>/operator.token` (0600). `agentctl --state-dir ...` reads it.
- The **approver token** is not created automatically. Provision it once:

  ```sh
  agentctl --state-dir ~/.local/state/synapse approver-init          # prints once
  agentctl --state-dir ~/.local/state/synapse approver-init --rotate # revokes the old one
  ```

  Store the printed value somewhere you control. Approvals are decided with `agentctl approval-decide` (token via `SYNAPSE_APPROVER_TOKEN` or a hidden prompt) or in the desktop dialog.
- Caveat: the approver token is **same-user**. It is a 0600 file owned by your user, so any process running as you (including agents) can read it. The unit's hardening does not change that, and it does not isolate a human approver from same-user processes.

## Hardening

`NoNewPrivileges`, `PrivateTmp`, `PrivateDevices`, `ProtectSystem=strict` with `ReadWritePaths` limited to the state dir, `ProtectHome=read-only`, kernel/cgroup/clock protections, `RestrictAddressFamilies=AF_UNIX`, `IPAddressDeny=any`, `UMask=0077`, an empty capability set, `MemoryDenyWriteExecute` and a syscall-architecture lock. The daemon only uses Unix sockets, so network access is deliberately **not** enabled. Do not add `AF_INET*` unless a future feature needs it. `ProtectHome=read-only` still lets the daemon read the venv under `~`. Because of `PrivateTmp`, the venv must not live under `/tmp`.

Verify a modified unit with `systemd-analyze --user verify` (it warns about a missing venv until installed).

## Hardening and real agent runs (not verified, do not weaken the unit)

The unit is hardened for the mock/daemon-only case and is **not tested with real agents enabled** (`--claude-cli-config`). It will probably break the agent child; nothing here has been run against a real `claude` or `codex`. Likely conflicts, from reading the unit and `providers/sandbox_launcher.py`:

- `IPAddressDeny=any` and `RestrictAddressFamilies=AF_UNIX`: the CLI must reach its model API, and `IPAddressDeny` also blocks loopback. Both inherit to every child, so the agent cannot connect at all.
- `MemoryDenyWriteExecute=yes`: Node.js (the Claude CLI) and other JIT runtimes need writable+executable memory and typically abort at start.
- `RestrictNamespaces=yes`: the opt-in bubblewrap launcher creates mount/PID/IPC/UTS namespaces, so `bwrap` fails (fail-closed, run refused).
- `ProtectHome=read-only` and `ProtectSystem=strict`: the CLI writes its own config/cache under `~` and the agent writes in its worktree; both are read-only unless listed in `ReadWritePaths`. `PrivateTmp` also hides the host `/tmp`.
- `NoNewPrivileges=yes` and an empty `CapabilityBoundingSet=` can additionally interfere with the bubblewrap user-namespace setup.

Do not loosen the shipped unit to make agents work. Enable agents through a separate drop-in (`systemctl --user edit synapse-daemon.service`) that relaxes only what a verified agent run needs, after a threat-model review. Until that is tested, run agent-enabled daemons outside this unit.

## Uninstall

```sh
systemctl --user disable --now synapse-daemon.service
rm ~/.config/systemd/user/synapse-daemon.service
systemctl --user daemon-reload
rm -rf ~/.local/share/synapse            # venv
# Destroys data and tokens; skip it to retain runs and approvals:
rm -rf ~/.local/state/synapse
```
