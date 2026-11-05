#!/usr/bin/env bash
# Set up a user-space toolchain on a Mac (Apple Silicon, Xcode command line tools, no Homebrew,
# no sudo) and build / test / run ASR Recorder. Safe to run over SSH; re-running is cheap.
#
#   desktop/scripts/macos-dev.sh setup            rustup + Rust, Node.js, pnpm, uv (user space)
#   desktop/scripts/macos-dev.sh test             cargo fmt --check, clippy -D warnings, lib tests
#   desktop/scripts/macos-dev.sh build [args]     release .app + zips (scripts/macos-build.sh args,
#                                                  e.g. --edition plain --version 0.2.0)
#   desktop/scripts/macos-dev.sh run [secs]       start the built app from the terminal (log on
#                                                  stdout); with secs: quit after secs seconds
#   desktop/scripts/macos-dev.sh dev              pnpm tauri dev (Vite + debug shell)
#   desktop/scripts/macos-dev.sh env              print the PATH line to use the tools yourself
#
# Installs into ~/.rustup, ~/.cargo, ~/.local/asr-dev/{node,bin} and uv's Python 3.12 under
# ~/.local/share/uv; nothing system-wide. Written for macOS's /bin/bash 3.2.
# Downloads are checked: Node.js against nodejs.org's SHASUMS256.txt, uv against the SHA-256
# pinned in scripts/stage-backend.py (and its release .sha256), rustup via its TLS installer.
# GUI note: `run` / `dev` need the user to be logged in at the Mac's screen (WindowServer), and
# the first recording asks for the Screen Recording / Microphone permission there.
set -euo pipefail

DESKTOP="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO="$(dirname "$DESKTOP")"
TAURI="$DESKTOP/src-tauri"
PREFIX="$HOME/.local/asr-dev"
NODE_MAJOR=24        # LTS; frontend/package.json needs >= 22.12
PNPM_VERSION="$(/usr/bin/python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["packageManager"].split("@")[1])' "$REPO/frontend/package.json")"

export PATH="$PREFIX/node/bin:$PREFIX/bin:$HOME/.cargo/bin:$PATH"
export CARGO_TARGET_DIR="${CARGO_TARGET_DIR:-$TAURI/target}"

step() { printf '\n== %s\n' "$*"; }
fail() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

[[ "$(uname -s)" == Darwin ]] || fail "macOS only"
[[ "$(uname -m)" == arm64 ]] || fail "Apple Silicon (arm64) only"

check_clt() {
  if ! xcode-select -p >/dev/null 2>&1 || ! xcrun --find clang >/dev/null 2>&1; then
    fail "Xcode command line tools missing. At the Mac: xcode-select --install
  (over SSH, with admin rights: softwareupdate --list, then
   sudo softwareupdate --install 'Command Line Tools for Xcode-<version>')"
  fi
  echo "Xcode CLT: $(xcode-select -p)"
}

setup_rust() {
  if ! command -v rustup >/dev/null 2>&1; then
    step "rustup (into ~/.rustup, ~/.cargo)"
    curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs \
      | sh -s -- -y --no-modify-path --profile minimal --default-toolchain stable
  fi
  rustup toolchain install stable --profile minimal --component clippy,rustfmt
  rustup default stable
  rustc --version
}

setup_node() {
  if [[ -x "$PREFIX/node/bin/node" ]] && "$PREFIX/node/bin/node" -e \
    "process.exit(+process.versions.node.split('.')[0] === $NODE_MAJOR ? 0 : 1)"; then
    echo "node $("$PREFIX/node/bin/node" --version)"
    return
  fi
  step "Node.js $NODE_MAJOR (official tarball, into $PREFIX/node)"
  local ver tmp
  ver="$(curl -fsSL https://nodejs.org/dist/index.json | python3 -c '
import json, sys
major = sys.argv[1]
print(next(r["version"] for r in json.load(sys.stdin) if r["version"].startswith("v" + major + ".")))' "$NODE_MAJOR")"
  tmp="$(mktemp -d)"
  local tarball="node-$ver-darwin-arm64.tar.gz"
  curl -fsSL -o "$tmp/$tarball" "https://nodejs.org/dist/$ver/$tarball"
  curl -fsSL -o "$tmp/SHASUMS256.txt" "https://nodejs.org/dist/$ver/SHASUMS256.txt"
  (cd "$tmp" && grep " $tarball\$" SHASUMS256.txt | shasum -a 256 -c -) || fail "Node.js checksum mismatch"
  rm -rf "$PREFIX/node" && mkdir -p "$PREFIX/node"
  tar -xzf "$tmp/$tarball" -C "$PREFIX/node" --strip-components 1
  rm -rf "$tmp"
  node --version
}

setup_pnpm() {
  if command -v pnpm >/dev/null 2>&1 && [[ "$(pnpm --version)" == "$PNPM_VERSION" ]]; then
    echo "pnpm $PNPM_VERSION"
    return
  fi
  step "pnpm $PNPM_VERSION (npm -g into $PREFIX/node)"
  npm install -g "pnpm@$PNPM_VERSION"
  pnpm --version
}

# uv: the version and SHA-256 pinned in stage-backend.py (read with ast: the system python3 of
# the command line tools is 3.9, too old to run the script itself).
setup_uv() {
  if [[ ! -x "$PREFIX/bin/uv" ]]; then
    step "uv (the version pinned for the bundled backend, into $PREFIX/bin)"
    local pin ver sha tmp
    pin="$(python3 -I - "$DESKTOP/scripts/stage-backend.py" <<'PY'
import ast, sys
tree = ast.parse(open(sys.argv[1], encoding="utf-8").read())
vals = {t.id: n.value for n in tree.body if isinstance(n, ast.Assign) for t in n.targets
        if isinstance(t, ast.Name) and t.id in ("UV_VERSION", "UV_BUILDS")}
builds = ast.literal_eval(vals["UV_BUILDS"])
print(ast.literal_eval(vals["UV_VERSION"]), builds["aarch64-apple-darwin"][2])
PY
)"
    ver="${pin% *}"; sha="${pin#* }"
    tmp="$(mktemp -d)"
    curl -fsSL -o "$tmp/uv.tar.gz" \
      "https://github.com/astral-sh/uv/releases/download/$ver/uv-aarch64-apple-darwin.tar.gz"
    echo "$sha  $tmp/uv.tar.gz" | shasum -a 256 -c - || fail "uv checksum mismatch"
    tar -xzf "$tmp/uv.tar.gz" -C "$tmp"
    mkdir -p "$PREFIX/bin"
    cp "$tmp/uv-aarch64-apple-darwin/uv" "$PREFIX/bin/uv"
    rm -rf "$tmp"
  fi
  uv --version
  # Python 3.12 for the build scripts (uv-managed, user space), first on PATH as python3.
  uv python install 3.12
  ln -sf "$(uv python find 3.12)" "$PREFIX/bin/python3"
  echo "python3 -> $(python3 --version)"
}

need_tools() {
  if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null \
    || ! command -v cargo >/dev/null || ! command -v pnpm >/dev/null; then
    fail "toolchain incomplete: run $0 setup first"
  fi
}

app_path() {
  local app="$CARGO_TARGET_DIR/release/bundle/macos/ASR Recorder.app"
  [[ -d "$app" ]] || fail "no app at $app; run: $0 build --edition plain"
  printf '%s' "$app"
}

cmd="${1:-help}"
[[ $# -gt 0 ]] && shift
case "$cmd" in
  setup)
    check_clt
    setup_uv
    setup_rust
    setup_node
    setup_pnpm
    step "frontend + tauri CLI dependencies"
    pnpm --dir "$REPO/frontend" install --frozen-lockfile
    pnpm --dir "$DESKTOP" install --frozen-lockfile
    step "ready"
    echo "export PATH=\"$PREFIX/node/bin:$PREFIX/bin:\$HOME/.cargo/bin:\$PATH\""
    ;;
  test)
    need_tools
    cd "$TAURI"
    step "cargo fmt --check"; cargo fmt --check
    step "clippy (app)"; cargo clippy --all-targets -- -D warnings
    step "clippy (lib)"; cargo clippy --no-default-features --all-targets -- -D warnings
    step "tests (lib)"; cargo test --no-default-features
    step "tests OK"
    ;;
  build)
    need_tools
    exec "$DESKTOP/scripts/macos-build.sh" "$@"
    ;;
  run)
    app="$(app_path)"
    if [[ -n "${1:-}" ]]; then export ASR_RECORDER_AUTO_EXIT="$1"; fi
    # The binary itself (not `open`): it inherits this environment and logs to the terminal.
    exec "$app/Contents/MacOS/asr-recorder"
    ;;
  dev)
    need_tools
    cd "$DESKTOP" && exec pnpm tauri dev
    ;;
  env)
    echo "export PATH=\"$PREFIX/node/bin:$PREFIX/bin:\$HOME/.cargo/bin:\$PATH\""
    ;;
  *)
    sed -n '2,20p' "$0"
    ;;
esac
