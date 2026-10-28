#!/bin/sh
# Install frontend dependencies from WSL when the checkout lives on a Windows drive (/mnt/<x>).
#
# drvfs mounted with uid=0 refuses chmod for normal users, and pnpm chmods its shims, lockfile and
# copied files, so `pnpm install` cannot write node_modules there. This script installs into a
# mirror on the Linux filesystem (package.json + lockfile only), copies the lockfile back and
# symlinks ./node_modules to the mirror. Every other script (`pnpm dev`, `pnpm build`, ...) runs
# in place. On Windows or a Linux filesystem, use plain `pnpm install` instead.
#
# Usage: sh scripts/wsl-install.sh [pnpm install args...]   e.g. --frozen-lockfile
set -eu

here=$(cd "$(dirname "$0")/.." && pwd)
mirror=${ASR_FRONTEND_MIRROR:-$HOME/.local/share/asr/frontend-env}
pnpm_bin=${PNPM:-pnpm}

mkdir -p "$mirror"
cp "$here/package.json" "$mirror/package.json"
for f in pnpm-lock.yaml pnpm-workspace.yaml .npmrc; do
  if [ -f "$here/$f" ]; then cp "$here/$f" "$mirror/$f"; else rm -f "$mirror/$f"; fi
done

(cd "$mirror" && "$pnpm_bin" install "$@")

cp "$mirror/pnpm-lock.yaml" "$here/pnpm-lock.yaml"
if [ ! -L "$here/node_modules" ]; then
  rm -rf "$here/node_modules"
  ln -s "$mirror/node_modules" "$here/node_modules"
fi
echo "node_modules -> $mirror/node_modules"
