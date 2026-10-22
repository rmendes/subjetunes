#!/bin/sh
# Rebuild Subjetunes.exe (needs mingw-w64). The built exe is committed, so this is rarely needed.
set -e
cd "$(dirname "$0")"
x86_64-w64-mingw32-windres launcher.rc -O coff -o launcher.res
x86_64-w64-mingw32-gcc -O2 -municode -mwindows -s -o Subjetunes.exe launcher.c launcher.res -lshlwapi
