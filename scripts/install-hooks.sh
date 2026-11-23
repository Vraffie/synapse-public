#!/usr/bin/env bash
# Opt-in: symlink scripts/pre-push into this repository's hooks directory. Idempotent; never overwrites a different
# existing hook unless --force is given (the old hook is then kept as pre-push.bak).
#
#   scripts/install-hooks.sh           install
#   scripts/install-hooks.sh --force   replace an existing, different pre-push hook
#   scripts/install-hooks.sh --remove  remove the hook if it is our symlink
set -eu
cd "$(dirname "$0")/.."
src="$(pwd -P)/scripts/pre-push"
hooks="$(git rev-parse --git-path hooks)"
mkdir -p "$hooks"
hooks="$(cd "$hooks" && pwd -P)"
dest="$hooks/pre-push"
mode="${1:-}"

if [ "$mode" = "--remove" ]; then
  if [ -L "$dest" ] && [ "$(readlink "$dest")" = "$src" ]; then
    rm "$dest"; echo "install-hooks: removed $dest"
  else
    echo "install-hooks: $dest is not our symlink, left alone"
  fi
  exit 0
fi

chmod +x "$src"
if [ -L "$dest" ] && [ "$(readlink "$dest")" = "$src" ]; then
  echo "install-hooks: already installed at $dest"
  exit 0
fi
if [ -e "$dest" ] || [ -L "$dest" ]; then
  if [ "$mode" != "--force" ]; then
    echo "install-hooks: $dest already exists; not overwriting (use --force)" >&2
    exit 1
  fi
  mv "$dest" "$dest.bak"
  echo "install-hooks: existing hook saved as $dest.bak"
fi
ln -s "$src" "$dest"
echo "install-hooks: installed $dest -> $src"
