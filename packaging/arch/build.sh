#!/bin/sh
# Build the Arch package from the CURRENT COMMIT in a clean scratch directory. No install, no sudo.
# Usage: packaging/arch/build.sh [OUTDIR]   (default: a new mktemp -d). Prints the package path.
#
# The source is `git archive HEAD`, so uncommitted changes are NOT packaged. makepkg runs with -d
# (no dependency resolution): a pure-Python wheel needs only the makedepends (python-build,
# python-installer, python-setuptools, python-wheel), which must already be installed; the runtime
# `depends` are not needed to build.
set -eu
root=$(git rev-parse --show-toplevel)
out=${1:-$(mktemp -d)}
name=synapse-agent-control
ver=$(sed -n 's/^pkgver=//p' "$root/packaging/arch/PKGBUILD")
work="$out/build"
mkdir -p "$work"
git -C "$root" archive --format=tar.gz --prefix="$name-$ver/" -o "$work/$name-$ver.tar.gz" HEAD
sum=$(sha256sum "$work/$name-$ver.tar.gz" | cut -d' ' -f1)
cp "$root/packaging/arch/PKGBUILD" "$root/packaging/arch/$name.install" "$work/"
sed -i "s/^sha256sums=('SKIP')/sha256sums=('$sum')/" "$work/PKGBUILD"
cd "$work"
makepkg -fd --noconfirm
ls "$work"/*.pkg.tar.*
