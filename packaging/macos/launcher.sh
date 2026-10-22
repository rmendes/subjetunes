#!/bin/bash
# Subjetunes.app/Contents/MacOS/Subjetunes : starts the bundled Python on the bundled app.
# Your data never goes inside the .app (it's read-only, and macOS runs apps from Downloads out of a
# random read-only copy); it lives in ~/Library/Application Support/Subjetunes.
CONTENTS="$(cd "$(dirname "$0")/.." && pwd -P)"
RES="$CONTENTS/Resources"
export SUBJETUNES_HOME="${SUBJETUNES_HOME:-$HOME/Library/Application Support/Subjetunes}"
mkdir -p "$SUBJETUNES_HOME/data/logs"
export PYTHONHOME="$RES/python"
export PYTHONPATH="$RES/app"
export PYTHONNOUSERSITE=1
export PYTHONDONTWRITEBYTECODE=1           # the bundle is read-only; bytecode was compiled at build time
[ -d "$RES/tools/ffmpeg" ] && export PATH="$RES/tools/ffmpeg:$PATH"
# Finder starts apps with a minimal environment; a UTF-8 locale keeps non-ASCII file names intact.
export LANG="${LANG:-en_US.UTF-8}"
exec -a Subjetunes "$RES/python/bin/python3" -m subjetunes.desktop "$@" \
    >>"$SUBJETUNES_HOME/data/logs/launcher.log" 2>&1
