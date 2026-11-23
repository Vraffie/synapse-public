# Installing Synapse outside a source checkout

Requires Python 3.13 or newer. The wheel is self-contained: the SQL migrations ship as package data (`synapse/persistence/migrations_sql`) and every command defaults to them, so no `--migrations` flag or manual copy is needed. The repository's top-level `migrations` is a symlink to the same directory (single source of truth).

## Install into a venv

```sh
python -m venv ~/.local/share/synapse/venv
~/.local/share/synapse/venv/bin/pip install /path/to/synapse        # or a built wheel
~/.local/share/synapse/venv/bin/pip install '/path/to/synapse[desktop]'   # adds the Qt desktop
```

Build a wheel with `python -m pip wheel --no-deps -w dist /path/to/synapse` (or `python -m build`).

Console entry points: `agentd` (daemon), `agentd-supervisor`, `agentctl` (CLI), `agent-desktop`. Equivalent modules: `python -m synapse.daemon`, `python -m synapse.supervisor`, `python -m synapse.cli`, `python -m synapse.desktop`.

## State directory

Create a user-owned directory with mode `0700` (the daemon refuses anything else):

```sh
install -d -m 0700 ~/.local/state/synapse
agentd-supervisor --state-dir ~/.local/state/synapse     # or: agentd --state-dir ...
```

To allow real agent runs, pass `--claude-cli-config FILE` with `repos` and `worktree_root` (see `agent-runs.md`; off by default). Because agents run as your user and approvals are not a security boundary against them, the daemon refuses unless you also pass `--allow-same-user-agents` ([security review](../security/review-2026-11-20.md)).

It holds `synapse.sqlite`, the socket `agentd.sock` and the token files. Back it up with `agentctl --state-dir ... backup DEST` (see `backup-restore.md`).

## Tokens

- `operator.token` is created by the daemon on first start (0600) and read by `agentctl` and the desktop.
- The approver token is provisioned once with `agentctl --state-dir ... approver-init` (add `--rotate` to replace it). It is a same-user file: any process running as you can read it.

## Check it works

```sh
agentctl --state-dir ~/.local/state/synapse health
```

## systemd user service

`packaging/systemd/synapse-daemon.service` runs the supervisor from the venv above. Copy it to `~/.config/systemd/user/`, run `systemctl --user daemon-reload` and `systemctl --user enable --now synapse-daemon.service`. Details: `systemd-user-service.md`.

## Desktop

```sh
agent-desktop --state-dir ~/.local/state/synapse
```

Needs the `desktop` extra and a graphical session (or `QT_QPA_PLATFORM=offscreen` for headless smoke tests). See `desktop.md`.
