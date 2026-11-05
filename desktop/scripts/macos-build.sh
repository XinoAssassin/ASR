#!/usr/bin/env bash
# Build the macOS (Apple Silicon) release of ASR Recorder: the .app of one or both editions,
# signed, optionally notarised, zipped (and optionally a .dmg).
#
#   desktop/scripts/macos-build.sh [--version X.Y.Z] [--edition plain|backend|both]
#                                  [--skip-frontend] [--out DIR] [--dmg]
#
# Produces in desktop/build/dist/ (or --out):
#   ASR-Recorder-<v>-macos-arm64.zip               ASR Recorder.app (recorder only)
#   ASR-Recorder-<v>-macos-arm64-with-backend.zip  + Contents/Resources/managed-backend/
#   ASR-Recorder-<v>-macos-arm64[-with-backend].dmg  with --dmg
#
# Signing (environment, all optional):
#   APPLE_SIGNING_IDENTITY   "Developer ID Application: … (TEAMID)" from a keychain in the search
#                            list; unset or "-" = ad-hoc signature (Gatekeeper: "Open anyway").
#   APPLE_API_ISSUER, APPLE_API_KEY, APPLE_API_KEY_PATH
#                            App Store Connect API key: notarise (the Tauri bundler submits the
#                            .app; this script staples and verifies it, and notarises the .dmg).
#
# Needs: Xcode command line tools, rustup/cargo, node + pnpm, python3 (3.11+). Honours
# CARGO_TARGET_DIR. Run from anywhere inside the repo. Written for macOS's /bin/bash 3.2.
set -euo pipefail

DESKTOP="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO="$(dirname "$DESKTOP")"
TAURI="$DESKTOP/src-tauri"
VERSION=""
EDITION="both"
SKIP_FRONTEND=0
OUT="$DESKTOP/build/dist"
DMG=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --version) VERSION="$2"; shift 2 ;;
    --edition) EDITION="$2"; shift 2 ;;
    --skip-frontend) SKIP_FRONTEND=1; shift ;;
    --out) OUT="$2"; shift 2 ;;
    --dmg) DMG=1; shift ;;
    -h|--help) sed -n '2,24p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
case "$EDITION" in plain|backend|both) ;; *) echo "--edition: plain|backend|both" >&2; exit 2 ;; esac
[[ "$(uname -s)" == Darwin ]] || { echo "macOS only" >&2; exit 1; }

# In GitHub Actions: steps are log groups and errors become annotations.
step() {
  CURRENT_STEP="$*"
  if [[ -n "${GITHUB_ACTIONS:-}" ]]; then
    [[ -n "${IN_GROUP:-}" ]] && echo "::endgroup::"
    echo "::group::$*"; IN_GROUP=1
  else
    printf '\n== %s\n' "$*"
  fi
}
fail() {
  if [[ -n "${GITHUB_ACTIONS:-}" ]]; then echo "::error title=macos-build::$*"; fi
  printf '\nERROR: %s\n' "$*" >&2
  exit 1
}
CURRENT_STEP="start"
on_exit() {
  local rc=$?
  if [[ $rc -ne 0 && -n "${GITHUB_ACTIONS:-}" ]]; then
    echo "::error title=macos-build::step '$CURRENT_STEP' failed (exit $rc); see its log group"
  fi
}
trap on_exit EXIT

if [[ -z "$VERSION" ]]; then
  VERSION="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["version"])' "$TAURI/tauri.conf.json")"
fi
IDENTITY="${APPLE_SIGNING_IDENTITY:--}"
NOTARIZE=0
if [[ "$IDENTITY" != "-" && -n "${APPLE_API_ISSUER:-}" && -n "${APPLE_API_KEY:-}" && -n "${APPLE_API_KEY_PATH:-}" ]]; then
  NOTARIZE=1
fi
if [[ "$IDENTITY" == "-" ]]; then
  echo "signing: ad-hoc (not notarised)"
else
  echo "signing: $IDENTITY; notarise: $([[ $NOTARIZE == 1 ]] && echo yes || echo no)"
fi
TARGET_DIR="${CARGO_TARGET_DIR:-$TAURI/target}"
BUNDLE="$TARGET_DIR/release/bundle/macos/ASR Recorder.app"
STAGE="$OUT/stage"
mkdir -p "$OUT"

if [[ $SKIP_FRONTEND == 0 ]]; then
  step "frontend build"
  pnpm --dir "$REPO/frontend" install --frozen-lockfile
  pnpm --dir "$REPO/frontend" build
fi
[[ -f "$REPO/frontend/dist/index.html" ]] || fail "frontend/dist missing (build the SPA first)"

step "tauri CLI"
pnpm --dir "$DESKTOP" install --frozen-lockfile

# Build-time config: version, signing identity, no beforeBuildCommand (the SPA is built above).
CONF="$DESKTOP/build/tauri.macos.json"
mkdir -p "$DESKTOP/build"
python3 - "$CONF" "$VERSION" "$IDENTITY" <<'PY'
import json, sys
path, version, identity = sys.argv[1:]
json.dump({"version": version,
           "build": {"beforeBuildCommand": ""},
           "bundle": {"macOS": {"signingIdentity": identity}}}, open(path, "w"))
PY

verify_app() { # <app>
  local app="$1"
  step "verify $(basename "$app")"
  codesign --verify --deep --strict --verbose=2 "$app"
  codesign -dv --verbose=2 "$app" 2>&1 | grep -E "^(Identifier|Authority|TeamIdentifier|Signature|Runtime|Timestamp|flags)" || true
  if [[ $NOTARIZE == 1 ]]; then
    xcrun stapler staple "$app"
    xcrun stapler validate "$app"
    spctl -a -vv -t exec "$app"
  fi
}

package() { # <edition-suffix> <app>
  local suffix="$1" app="$2"
  local name="ASR-Recorder-$VERSION-macos-arm64$suffix"
  step "zip $name"
  rm -f "$OUT/$name.zip"
  # ditto keeps the bundle's symlinks, extended attributes and signature intact.
  ditto -c -k --sequesterRsrc --keepParent "$app" "$OUT/$name.zip"
  if [[ $DMG == 1 ]]; then
    step "dmg $name"
    local d="$STAGE/dmg$suffix"
    rm -rf "$d" && mkdir -p "$d"
    ditto "$app" "$d/ASR Recorder.app"
    ln -s /Applications "$d/Applications"
    rm -f "$OUT/$name.dmg"
    hdiutil create -volname "ASR Recorder $VERSION" -srcfolder "$d" -ov -format UDZO "$OUT/$name.dmg"
    rm -rf "$d"
    if [[ "$IDENTITY" != "-" ]]; then
      codesign --force --sign "$IDENTITY" --timestamp "$OUT/$name.dmg"
    fi
    if [[ $NOTARIZE == 1 ]]; then
      xcrun notarytool submit "$OUT/$name.dmg" --key "$APPLE_API_KEY_PATH" \
        --key-id "$APPLE_API_KEY" --issuer "$APPLE_API_ISSUER" --wait --timeout 30m
      xcrun stapler staple "$OUT/$name.dmg"
      spctl -a -vv -t open --context context:primary-signature "$OUT/$name.dmg"
    fi
  fi
  for f in "$OUT/$name".{zip,dmg}; do
    if [[ -f "$f" ]]; then
      printf '%s  %s\n' "$(shasum -a 256 "$f" | cut -d' ' -f1)" "$(basename "$f")"
    fi
  done
}

build_edition() { # plain|backend
  local edition="$1" suffix=""
  local extra=()
  if [[ "$edition" == backend ]]; then
    suffix="-with-backend"
    step "stage backend (uv for aarch64-apple-darwin)"
    python3 -I "$DESKTOP/scripts/stage-backend.py" --target aarch64-apple-darwin
    if [[ "$IDENTITY" != "-" ]]; then
      # Every Mach-O in the bundle needs a Developer ID signature with the hardened runtime and a
      # timestamp for notarisation; the bundler signs only the app itself.
      codesign --force --options runtime --timestamp --sign "$IDENTITY" "$DESKTOP/build/managed-backend/uv"
    fi
    extra=(--config "$TAURI/tauri.bundled.conf.json")
  fi
  step "tauri build ($edition)"
  rm -rf "$BUNDLE"
  (cd "$DESKTOP" && pnpm tauri build --bundles app --config "$CONF" ${extra[@]+"${extra[@]}"})
  [[ -d "$BUNDLE" ]] || fail "no app bundle at $BUNDLE"
  if [[ "$edition" == backend ]]; then
    [[ -x "$BUNDLE/Contents/Resources/managed-backend/uv" ]] || fail "managed-backend/uv missing in the bundle"
  fi
  local app="$STAGE/$edition/ASR Recorder.app"
  rm -rf "${STAGE:?}/$edition" && mkdir -p "$STAGE/$edition"
  ditto "$BUNDLE" "$app"
  verify_app "$app"
  package "$suffix" "$app"
}

[[ "$EDITION" == backend ]] || build_edition plain
[[ "$EDITION" == plain ]] || build_edition backend
rm -rf "$STAGE"
step "done"
ls -l "$OUT"
