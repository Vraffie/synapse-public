# Arch Linux package

`packaging/arch/` builds `synapse-agent-control` as a system package. It installs the Python package, the four commands (`agentd`, `agentd-supervisor`, `agentctl`, `agent-desktop`), the operations docs and a **systemd user unit**. It never starts anything and never touches user data. No license has been chosen yet (`license=(LicenseRef-unlicensed)`): use it locally, do not redistribute.

## Design decision: system package, user unit

The per-user unit in `packaging/systemd/` runs `%h/.local/share/synapse/venv/bin/python`. A system package cannot provide a per-user venv, so two designs were possible:

1. **Chosen: install to `/usr` and ship the unit as `/usr/lib/systemd/user/synapse-daemon.service` with `ExecStart=/usr/bin/python -m synapse.supervisor ...`.** Every user on the machine can `systemctl --user enable` it; pacman owns the code, so upgrades and removal are normal pacman operations. The unit is the existing one with **only** `ExecStart` and `Documentation` changed; every hardening directive is identical (a test enforces this), and `systemd-analyze --user security` reports the same exposure (2.8 OK) for both. `ProtectSystem=strict` makes `/usr` read-only, which is fine because the package ships precompiled bytecode.
2. **Rejected: a package that installs a helper script building a per-user venv at first use.** It keeps the old unit unchanged but makes the package run `pip` against the network at user level, leaves code outside pacman's control (no `pacman -Qk`, no clean removal) and makes upgrades silent and per-user.

The per-user venv route in `systemd-user-service.md` still works and is unaffected; do not install both for the same user (the user unit in `~/.config/systemd/user/` overrides the packaged one).

## Build

```sh
packaging/arch/build.sh            # builds into a new mktemp -d and prints the package path
packaging/arch/build.sh /some/dir  # or into a directory you choose
```

`build.sh` exports the **current commit** with `git archive` (uncommitted changes are not packaged), computes the tarball's SHA-256, writes it into a scratch copy of the `PKGBUILD` and runs `makepkg -fd` there. The repository's `PKGBUILD` has `sha256sums=('SKIP')` only because that tarball is local and per-commit and does not exist until it is exported; the build substitutes the real digest before `makepkg` verifies it. `-d` skips dependency resolution: building a pure-Python wheel needs only the makedepends (`python-build`, `python-installer`, `python-setuptools`, `python-wheel`), which must already be installed. No sudo and no network are used.

To build by hand: put a `synapse-agent-control-0.1.0.tar.gz` (top directory `synapse-agent-control-0.1.0/`) next to the `PKGBUILD` and `.install` file, set its checksum, run `makepkg`.

## Install, enable, verify

```sh
sudo pacman -U synapse-agent-control-0.1.0-1-any.pkg.tar.zst    # installs the missing deps from the repos
systemctl --user daemon-reload
systemctl --user enable --now synapse-daemon.service            # as the user who will use it
agentctl --state-dir ~/.local/state/synapse health
```

The unit uses `StateDirectory=synapse`, so systemd creates the private (`0700`) state directory; creating it yourself first (`install -d -m 0700 ~/.local/state/synapse`) is also fine. Real agent runs stay disabled until you pass `--claude-cli-config` (`agent-runs.md`). Tokens: `operator.token` appears on first start; run `agentctl ... approver-init` once for the approver token (`systemd-user-service.md`, same caveats about a same-user approver token).

## Upgrade and rollback

The `.install` script prints these steps; it does nothing itself.

1. **Back up first**, per user running the daemon: `agentctl --state-dir ~/.local/state/synapse backup ~/synapse-backup-$(date +%F).sqlite`, then `backup-verify` it (`backup-restore.md`). The backup holds the database only, never tokens or artifact blobs.
2. `sudo pacman -U` the new package.
3. `systemctl --user daemon-reload && systemctl --user try-restart synapse-daemon.service`. Running daemons keep the old code until restarted. Migrations apply when the daemon next starts and are one-way.
4. **Rollback:** reinstall the previous package file with `pacman -U`, restart the daemon. If the new version already migrated the database, restore the pre-upgrade backup first with the daemon **stopped** (`agentctl ... restore FILE`); an older daemon refuses a database from a newer schema.

## Uninstall without data loss

`systemctl --user disable --now synapse-daemon.service` (per user), then `sudo pacman -R synapse-agent-control`. pacman removes only packaged files. `~/.local/state/synapse` (database, tokens, artifacts) and any worktrees are never touched; delete them yourself if you really want them gone.

## Dependencies and known incompatibility

Runtime `depends`: `python` (3.13 or newer; Arch currently ships 3.14, and the package installs under `/usr/lib/python3.14/site-packages`, so rebuild it after a Python minor upgrade), `python-fastapi`, `python-pydantic`, `uvicorn`, `python-websockets`. Optional: `pyside6` for `agent-desktop`.

- `pyproject.toml` allows `websockets>=13,<18` (was `<17`). Evidence: with websockets 17.2 (fastapi 0.165.0, uvicorn 0.54.0, mcp 1.30.0, Python 3.14) the non-desktop suite gave 2024 passed, 29 skipped, 1 failed; with 16.1.1 the same failure appears, so it does not depend on websockets (`docs/api/reference.md` is stale against newer fastapi OpenAPI output). The code only uses websockets indirectly (uvicorn) plus `websockets.sync.client.unix_connect` in a test. Untested: any 18.x.
- **MCP gateway is incompatible with Arch's `python-mcp` 2.x**: `synapse.mcp_gateway` calls `Server.list_tools`, which mcp 2.3.0 no longer has (2 tests fail). Synapse requires `mcp>=1.20,<2`, so `python-mcp` is intentionally not an optdepend (`pyproject.toml` pins `mcp>=1.20,<2`; a test checks this). Use the MCP pieces from a venv with a 1.x `mcp`, or wait for Synapse to support 2.x.

## Not covered

- **Lingering:** whether the daemon must keep running after logout is an open decision (`loginctl enable-linger "$USER"` is printed as optional and never done for you).
- Desktop notifications/autostart, a `.desktop` launcher, an AUR/repository publication, package signing, and a clean `namcap` run. `namcap` 3.6.0 on the built package reports: `E` missing python-pydantic dependency (fixed here), `E` `LicenseRef-unlicensed` needs a license file or a common identifier (open owner decision, left as is), plus false-positive `W` lines for PySide6/mcp (optional) and internal `synapse.*` modules. `namcap PKGBUILD` could not be run without the system `parsepkgbuild.sh`. The package ships no libalpm hooks; the only pacman hooks are the `.install` scriptlets, which are message-only (syntax checked, each function run in a shell, packaged `.INSTALL` identical).
- The packaged unit is untested with real agents enabled and probably breaks them; see `systemd-user-service.md`.
- Installing `python-fastapi`/`uvicorn`/`python-websockets` as hard `depends` means pacman, not pip, supplies them; the package never calls `pip`.
