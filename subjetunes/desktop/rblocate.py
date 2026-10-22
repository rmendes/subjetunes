# subjetunes/desktop/rblocate.py
"""Find the rekordbox library (master.db) and the installed rekordbox versions, on Windows and macOS.

pyrekordbox can find master.db itself, but only when rekordbox is installed in the default place
(/Applications or Program Files) and its two settings files agree. Testers' machines vary, so we
look ourselves and hand pyrekordbox an explicit path. In order:

  1. options.json, written by rekordboxAgent: "db-path" (follows a library you moved).
       macOS   ~/Library/Application Support/Pioneer/rekordboxAgent/storage/options.json
       Windows %APPDATA%\\Pioneer\\rekordboxAgent\\storage\\options.json
  2. rekordbox3.settings: "masterDbDirectory".
       macOS   ~/Library/Application Support/Pioneer/rekordbox6/rekordbox3.settings
       Windows %APPDATA%\\Pioneer\\rekordbox6\\rekordbox3.settings   (rekordbox 7 uses this folder too)
  3. The default location.
       macOS   ~/Library/Pioneer/rekordbox/master.db
       Windows %APPDATA%\\Pioneer\\rekordbox\\master.db

The key is not looked up here: pyrekordbox (>= 0.4.4) carries it, the same on both systems.
"""
from __future__ import annotations

import json
import os
import plistlib
import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Candidate:
    path: Path
    how: str                # where we learned about it, shown in Settings
    exists: bool


@dataclass
class Install:
    version: str
    path: Path


def _platform() -> str:
    return "darwin" if sys.platform == "darwin" else "win32" if sys.platform.startswith("win") else "other"


def pioneer_app_dir() -> Path | None:
    """Pioneer's per-user data: Application Support on macOS, Roaming AppData on Windows."""
    p = _platform()
    if p == "darwin":
        return Path.home() / "Library" / "Application Support" / "Pioneer"
    if p == "win32":
        base = os.environ.get("APPDATA")
        return Path(base) / "Pioneer" if base else None
    return None


def default_db() -> Path | None:
    p = _platform()
    if p == "darwin":
        return Path.home() / "Library" / "Pioneer" / "rekordbox" / "master.db"
    if p == "win32":
        base = os.environ.get("APPDATA")
        return Path(base) / "Pioneer" / "rekordbox" / "master.db" if base else None
    return None


def _from_options(app: Path) -> Path | None:
    f = app / "rekordboxAgent" / "storage" / "options.json"
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    # {"options": [["db-path", "/Users/x/Library/Pioneer/rekordbox/master.db"], ...]}
    for item in data.get("options", []) if isinstance(data, dict) else []:
        if isinstance(item, list) and len(item) == 2 and item[0] == "db-path" and item[1]:
            return Path(item[1])
    return None


def _from_settings(app: Path) -> Path | None:
    for sub in ("rekordbox6", "rekordbox7"):
        f = app / sub / "rekordbox3.settings"
        try:
            root = ET.parse(f).getroot()
        except (OSError, ET.ParseError):
            continue
        for v in root.iter("VALUE"):
            if v.get("name") == "masterDbDirectory" and v.get("val"):
                return Path(v.get("val")) / "master.db"
    return None


def candidates() -> list[Candidate]:
    out: list[Candidate] = []
    seen: set[str] = set()

    def add(p: Path | None, how: str) -> None:
        if p is None:
            return
        k = os.path.normcase(str(p))
        if k in seen:
            return
        seen.add(k)
        out.append(Candidate(p, how, p.is_file()))

    app = pioneer_app_dir()
    if app:
        add(_from_options(app), "rekordboxAgent options.json")
        add(_from_settings(app), "rekordbox3.settings")
    add(default_db(), "default location")
    return out


def find_master_db() -> Path | None:
    for c in candidates():
        if c.exists:
            return c.path
    return None


def _version(name: str) -> str:
    m = re.search(r"(\d+(?:\.\d+){0,3})", name)
    return m.group(1) if m else ""


def installs() -> list[Install]:
    """Installed rekordbox applications, newest first (shown in Settings; not needed to open the library)."""
    found: list[Install] = []
    p = _platform()
    if p == "darwin":
        for base in (Path("/Applications"), Path.home() / "Applications"):
            if not base.is_dir():
                continue
            for d in base.iterdir():
                if not d.name.lower().startswith("rekordbox"):
                    continue
                apps = [d] if d.suffix == ".app" else list(d.glob("rekordbox*.app"))
                for a in apps:
                    ver = ""
                    try:
                        with (a / "Contents" / "Info.plist").open("rb") as f:
                            ver = str(plistlib.load(f).get("CFBundleShortVersionString", ""))
                    except (OSError, ValueError, plistlib.InvalidFileException):
                        ver = _version(d.name)
                    found.append(Install(ver or _version(d.name), a))
    elif p == "win32":
        roots = {os.environ.get("ProgramFiles", r"C:\Program Files"), os.environ.get("ProgramW6432", "")}
        for root in filter(None, roots):
            for vendor in ("rekordbox", "Pioneer"):        # rekordbox 7 / rekordbox 6
                base = Path(root) / vendor
                if base.is_dir():
                    for d in base.iterdir():
                        if d.is_dir() and d.name.lower().startswith("rekordbox"):
                            found.append(Install(_version(d.name), d))

    def key(i: Install):
        return tuple(int(x) for x in i.version.split(".") if x.isdigit()) or (0,)
    return sorted(found, key=key, reverse=True)


def report() -> dict:
    """For Settings and support requests: what we looked at and what we found."""
    return {
        "platform": _platform(),
        "found": str(find_master_db() or ""),
        "candidates": [{"path": str(c.path), "how": c.how, "exists": c.exists} for c in candidates()],
        "installs": [{"version": i.version, "path": str(i.path)} for i in installs()],
    }
