"""One entry point for loading a library: rekordbox XML export or master.db."""
from __future__ import annotations

from . import rbxml
from .models import Library


def load_library(path: str | None) -> Library:
    p = (path or "").strip().strip('"')
    if p.lower().endswith(".xml"):
        return rbxml.read_library(p)
    from . import rbdb                      # master.db file, its folder, or empty = auto-detect
    return rbdb.read_library(p or None)
