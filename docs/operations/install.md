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

## Threat model

Everything here runs as one OS user. The opt-in user-scope sandbox (`sandbox` in the Claude CLI profile; needs `bwrap` and unprivileged user namespaces, and it fails closed when they are missing) and the opt-in passphrase verifier (`approver-passphrase.md`) are same-uid mitigations, not a security boundary. A sandboxed agent still runs as your uid with the network shared, and `--allow-same-user-agents` is still required with it. Same-uid code can brute-force the passphrase verifier offline. Do not describe approvals as human-only or as protecting you from a hostile agent; they catch mistakes and honest components. See `approver-identity.md` and ADR 019.

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

## One-command local start for real agent runs

From a checkout, `python scripts/synapse_up.py up /path/to/your/repo` sets up and starts everything for real,
sandboxed agent runs on this machine, and `python scripts/synapse_up.py down` stops it again:

- creates the private state directory (`~/.local/state/synapse`, or `$SYNAPSE_STATE`) and a sibling
  `...-worktrees` directory (the daemon refuses worktrees inside the state dir);
- finds `claude` (`$SYNAPSE_CLAUDE_EXE` or on `PATH`). The native install lives under `~/.local/share`, which the
  sandbox protects, so the binary is copied to `~/.local/lib/synapse/claude` and refreshed when Claude updates;
- writes `agent.json` (mode 0600, sandbox always on; a config you edited to turn the sandbox off is refused, not
  overwritten) and adds the repo to its `repos` allowlist;
- starts the daemon with `--allow-same-user-agents` (agents run as you; the sandbox is the boundary, see ADR 019)
  and, unless `--no-desktop`, the desktop console. Running `up` again for another repo is safe; restart with
  `down` then `up` for the daemon to pick up a new repo.

For a project folder that is not itself a repository but contains several (for example `~/Projects/BSS` with
sibling repos plus `data/`, `documentation/` ...), list what is in it first and then register the ones you want:

```sh
python scripts/synapse_up.py workspace ~/Projects/BSS          # read-only table: repo, branch, head, uncommitted, usable
python scripts/synapse_up.py up --workspace ~/Projects/BSS --repo api --repo web --dry-run   # shows the paths, writes nothing
python scripts/synapse_up.py up --workspace ~/Projects/BSS --repo api --repo web
```

Repositories that cannot be used (no commits, unsafe git config, bare, linked worktree) and names outside the
folder are refused. Agents only ever see committed refs; see `workspace.md`.

It prints the `agentctl agent-start` command to use. No approver token is needed for ordinary runs. Logs and pid
files are in the state directory (`daemon.log`, `desktop.log`, `*.pid`); `down` only signals a pid that is
verifiably a Synapse process.
