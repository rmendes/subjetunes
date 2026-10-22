#!/bin/bash
# packaging/macos/build_macos.sh : build Subjetunes.app and a zip (or dmg) for testers.
#
#   bash packaging/macos/build_macos.sh                 this Mac's architecture, full (CLAP: torch + transformers)
#   bash packaging/macos/build_macos.sh --lite          no torch (~400 MB smaller). Intel Macs always build lite,
#                                                       because PyTorch no longer publishes Intel-Mac wheels.
#   bash packaging/macos/build_macos.sh --arch x86_64   Intel build on an Apple-silicon Mac (runs under Rosetta)
#   ... --quick      reuse the bundled Python when nothing in requirements changed; only sync the app (no zip)
#   ... --dmg        also make a .dmg
#   ... --with-ffmpeg <url-of-zip-containing-ffmpeg>   bundle ffmpeg for AAC/M4A decoding (pick a build you trust)
#
# Output: dist/macos-<arch>/Subjetunes.app and dist/Subjetunes-<version>-macos-<arch>.zip (app + README-testers.txt)
#
# Signing:
#   default                ad-hoc signature (required for Apple silicon). Testers approve it once in
#                          System Settings > Privacy & Security > Open Anyway (see README-testers.txt).
#   SIGN_IDENTITY="Developer ID Application: Name (TEAMID)"   real signature with the hardened runtime.
#   NOTARY_PROFILE=<name>  also notarise and staple (profile made with `xcrun notarytool store-credentials`),
#                          so testers can open it with a double-click.
#
# Linux (CI or a quick check of the bundle layout only): --assemble-test builds the same tree with a
# Linux Python and skips signing. The result doesn't run on a Mac.
set -euo pipefail

PBS_TAG="20260929"          # python-build-standalone release; pinned, not "latest"
PY_VERSION="3.12.14"
VERSION="${VERSION:-0.9.1}"
BUNDLE_ID="${BUNDLE_ID:-com.subjetunes.app}"

ARCH="$(uname -m)"; LITE=0; QUICK=0; ZIP=1; DMG=0; FFMPEG_URL=""; TEST=0
while [ $# -gt 0 ]; do
  case "$1" in
    --arch) ARCH="$2"; shift ;;
    --lite) LITE=1 ;;
    --quick) QUICK=1; ZIP=0 ;;
    --no-zip) ZIP=0 ;;
    --dmg) DMG=1 ;;
    --with-ffmpeg) FFMPEG_URL="$2"; shift ;;
    --assemble-test) TEST=1 ;;
    -h|--help) sed -n '2,26p' "$0"; exit 0 ;;
    *) echo "unknown option $1" >&2; exit 2 ;;
  esac
  shift
done

HERE="$(cd "$(dirname "$0")" && pwd -P)"
REPO="$(cd "$HERE/../.." && pwd -P)"
DIST="$REPO/dist"
CACHE="$DIST/.cache"
OS="$(uname -s)"
[ "$ARCH" = "aarch64" ] && ARCH="arm64"

case "$OS:$ARCH:$TEST" in
  Darwin:arm64:0)  TRIPLE="aarch64-apple-darwin" ;;
  Darwin:x86_64:0) TRIPLE="x86_64-apple-darwin"; LITE=1 ;;
  Linux:x86_64:1)  TRIPLE="x86_64-unknown-linux-gnu"; LITE=1 ;;
  Linux:*:0)       echo "This builds a Mac app, so it runs on macOS (or use --assemble-test to check the layout)." >&2; exit 2 ;;
  *) echo "unsupported: $OS $ARCH" >&2; exit 2 ;;
esac
# Building for the other architecture on an Apple-silicon Mac: run the bundled Python under Rosetta.
RUN=""
if [ "$OS" = "Darwin" ] && [ "$ARCH" != "$(uname -m)" ]; then RUN="arch -$ARCH"; fi

APP="$DIST/macos-$ARCH/Subjetunes.app"
CONTENTS="$APP/Contents"
RES="$CONTENTS/Resources"
PY="$RES/python/bin/python3"
mkdir -p "$CACHE" "$RES" "$CONTENTS/MacOS"
START=$(date +%s)
say() { printf '== %s\n' "$*"; }

LOCK="$DIST/.build-macos-$ARCH.lock"
if [ -e "$LOCK" ] && [ -z "$(find "$LOCK" -mmin +60 2>/dev/null)" ]; then
  echo "Another build is running ($LOCK). Delete that file if it isn't." >&2; exit 1
fi
touch "$LOCK"; trap 'rm -f "$LOCK"' EXIT

if [ ! -f "$REPO/subjetunes/cli.py" ]; then
  echo "warning: subjetunes/cli.py is missing in $REPO. The app builds, but without the analysis engine." >&2
fi

sha() { if command -v shasum >/dev/null; then shasum -a 256 "$1" | cut -d' ' -f1; else sha256sum "$1" | cut -d' ' -f1; fi; }
REQS=("$REPO/requirements-app.txt"); [ -f "$REPO/requirements-ml.txt" ] && REQS+=("$REPO/requirements-ml.txt")
STAMP="pbs=$PBS_TAG py=$PY_VERSION triple=$TRIPLE lite=$LITE"
for f in "${REQS[@]}"; do STAMP="$STAMP $(sha "$f" | cut -c1-16)"; done
STAMP_FILE="$RES/python/.deps-stamp"

if [ "$QUICK" = 1 ] && [ -x "$PY" ] && [ -f "$STAMP_FILE" ] && [ "$(cat "$STAMP_FILE")" = "$STAMP" ]; then
  say "Runtime unchanged, reused"
else
  ASSET="cpython-${PY_VERSION}+${PBS_TAG}-${TRIPLE}-install_only.tar.gz"
  URL="https://github.com/astral-sh/python-build-standalone/releases/download/${PBS_TAG}/${ASSET/+/%2B}"
  say "Python $PY_VERSION ($TRIPLE)"
  [ -f "$CACHE/$ASSET" ] || curl -fsSL --retry 3 -o "$CACHE/$ASSET" "$URL"
  [ -f "$CACHE/SHA256SUMS-$PBS_TAG" ] || curl -fsSL --retry 3 -o "$CACHE/SHA256SUMS-$PBS_TAG" \
      "https://github.com/astral-sh/python-build-standalone/releases/download/${PBS_TAG}/SHA256SUMS"
  WANT="$(grep " ${ASSET}\$" "$CACHE/SHA256SUMS-$PBS_TAG" | cut -d' ' -f1 || true)"
  GOT="$(sha "$CACHE/$ASSET")"
  if [ -z "$WANT" ] || [ "$WANT" != "$GOT" ]; then
    rm -f "$CACHE/$ASSET"; echo "checksum mismatch for $ASSET (expected '$WANT', got '$GOT')" >&2; exit 1
  fi
  rm -rf "$RES/python"
  tar -xzf "$CACHE/$ASSET" -C "$RES"               # -> Resources/python
  export PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_WARN_SCRIPT_LOCATION=1
  say "Dependencies"
  $RUN "$PY" -m pip install --upgrade --quiet pip
  $RUN "$PY" -m pip install --quiet -r "$REPO/requirements-app.txt"
  if [ "$LITE" = 0 ]; then
    $RUN "$PY" -m pip install --quiet torch          # macOS wheels are CPU/MPS; no index URL needed
    if [ -f "$REPO/requirements-ml.txt" ]; then $RUN "$PY" -m pip install --quiet -r "$REPO/requirements-ml.txt"
    else $RUN "$PY" -m pip install --quiet "transformers>=4.40"; fi
  fi
  say "Trim"
  LIB="$RES/python/lib/python${PY_VERSION%.*}"
  rm -rf "$LIB/test" "$LIB/idlelib" "$LIB/tkinter" "$LIB/turtledemo" "$RES/python/share" \
         "$RES/python/lib/"libtcl* "$RES/python/lib/"libtk* "$RES/python/lib/"tcl* "$RES/python/lib/"tk*
  find "$RES/python" -type d \( -name __pycache__ -o -name tests \) -prune \
       ! -path "*numpy*testing*" -exec rm -rf {} + 2>/dev/null || true
  echo "$STAMP" > "$STAMP_FILE"
fi

say "App"
cp "$HERE/launcher.sh" "$CONTENTS/MacOS/Subjetunes"; chmod +x "$CONTENTS/MacOS/Subjetunes"
sed -e "s/@VERSION@/$VERSION/g" -e "s/@BUNDLE_ID@/$BUNDLE_ID/g" "$HERE/Info.plist" > "$CONTENTS/Info.plist"
printf 'APPL????' > "$CONTENTS/PkgInfo"
cp "$HERE/Subjetunes.icns" "$RES/Subjetunes.icns"
mkdir -p "$RES/app"
mirror() {   # mirror <from> <to> [names to leave out]: deletes what you removed from the repo
  local from="$1" to="$2"; shift 2
  if command -v rsync >/dev/null; then
    local ex=(); for n in "$@"; do ex+=(--exclude "$n"); done
    rsync -a --delete "${ex[@]}" "$from/" "$to/"
  else
    rm -rf "$to"; mkdir -p "$to"; cp -R "$from/." "$to/"
    for n in "$@"; do find "$to" -name "$n" -prune -exec rm -rf {} +; done
  fi
}
mirror "$REPO/subjetunes" "$RES/app/subjetunes" __pycache__ '*.pyc' data
mirror "$REPO/config" "$RES/config" user.yaml
for f in README.md TAGGING.md DESKTOP.md; do [ -f "$REPO/$f" ] && cp "$REPO/$f" "$RES/"; done
$RUN "$PY" -m compileall -q "$RES/app/subjetunes" >/dev/null

if [ -n "$FFMPEG_URL" ]; then
  say "ffmpeg"
  F="$CACHE/ffmpeg-$ARCH.zip"
  [ -f "$F" ] || curl -fsSL --retry 3 -o "$F" "$FFMPEG_URL"
  T="$(mktemp -d)"; unzip -q -o "$F" -d "$T"
  mkdir -p "$RES/tools/ffmpeg"; find "$T" -type f -name ffmpeg -exec cp {} "$RES/tools/ffmpeg/" \;
  chmod +x "$RES/tools/ffmpeg/ffmpeg"; rm -rf "$T"
fi

say "Smoke test"
SMOKE_HOME="$(mktemp -d)"
SUBJETUNES_HOME="$SMOKE_HOME" PYTHONPATH="$RES/app" $RUN "$PY" -c "
import subjetunes.desktop.server, subjetunes.tagging, numpy, yaml, pyrekordbox, sqlcipher3, soundfile, mutagen, requests, webview
from subjetunes.desktop import paths, rblocate
assert paths.config_dir().joinpath('tags.yaml').exists(), paths.config_dir()
print('imports OK; library lookup:', rblocate.report()['candidates'][:1])"
rm -rf "$SMOKE_HOME"

if [ "$OS" = "Darwin" ]; then
  if [ -n "${SIGN_IDENTITY:-}" ]; then
    say "Sign: $SIGN_IDENTITY"
    # inside out: every native file, then the bundle
    { find "$RES" -type f \( -name '*.so' -o -name '*.dylib' \) -print0
      find "$RES/python/bin" -type f -name 'python3*' -print0; } |
      xargs -0 -n 50 codesign --force --timestamp --options runtime --entitlements "$HERE/entitlements.plist" \
                              --sign "$SIGN_IDENTITY"
    codesign --force --timestamp --options runtime --entitlements "$HERE/entitlements.plist" --sign "$SIGN_IDENTITY" "$APP"
  else
    say "Sign: ad-hoc"
    codesign --force --deep --sign - "$APP"
  fi
  codesign --verify --deep --strict "$APP"
fi

if [ "$ZIP" = 1 ] || [ "$DMG" = 1 ]; then
  STAGE="$DIST/.stage-macos-$ARCH"; rm -rf "$STAGE"; mkdir -p "$STAGE"
  cp -R "$APP" "$STAGE/"; cp "$HERE/README-testers.txt" "$STAGE/"
  NAME="Subjetunes-$VERSION-macos-$ARCH"
  if [ "$OS" = "Darwin" ] && [ -n "${NOTARY_PROFILE:-}" ]; then
    say "Notarise"
    ditto -c -k --keepParent "$APP" "$DIST/$NAME-notary.zip"
    xcrun notarytool submit "$DIST/$NAME-notary.zip" --keychain-profile "$NOTARY_PROFILE" --wait
    xcrun stapler staple "$STAGE/Subjetunes.app"
    rm -f "$DIST/$NAME-notary.zip"
  fi
  if [ "$ZIP" = 1 ]; then
    say "Zip"
    rm -f "$DIST/$NAME.zip"
    if [ "$OS" = "Darwin" ]; then ditto -c -k --sequesterRsrc "$STAGE" "$DIST/$NAME.zip"   # keeps symlinks + signature
    else (cd "$STAGE" && zip -qry "$DIST/$NAME.zip" .); fi
    echo "   $DIST/$NAME.zip"
  fi
  if [ "$DMG" = 1 ] && [ "$OS" = "Darwin" ]; then
    say "DMG"
    ln -s /Applications "$STAGE/Applications"
    rm -f "$DIST/$NAME.dmg"
    hdiutil create -quiet -volname "Subjetunes" -srcfolder "$STAGE" -ov -format UDZO "$DIST/$NAME.dmg"
    echo "   $DIST/$NAME.dmg"
  fi
  rm -rf "$STAGE"
fi

SIZE="$(du -sm "$APP" | cut -f1)"
echo "Built $APP (${SIZE} MB) in $(( $(date +%s) - START )) s."
