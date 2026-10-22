# subjetunes/desktop/library.py
"""Read a rekordbox collection XML: tracks (with their raw elements) and the playlist tree.

Kept independent of subjetunes.rbxml so the desktop app can open a library before
anything else is wired. TRACK elements are kept verbatim for re-export.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

from ..tagging.comments import normalise_path


@dataclass
class Track:
    track_id: str
    location: str
    key: str                      # normalise_path(location): the id used everywhere in the app
    title: str = ""
    artist: str = ""
    genre: str = ""
    bpm: float | None = None
    tonality: str = ""
    duration_s: float | None = None
    comments: str = ""
    element: ET.Element | None = field(default=None, repr=False)
    # Filled from master.db (XML exports carry some of these too)
    content_id: str = ""
    album: str = ""
    label: str = ""
    year: int | None = None
    rating: int = 0
    mytags: list[str] = field(default_factory=list)      # "Category/Name"
    meta: dict = field(default_factory=dict)              # every other rekordbox field, for the Track screen
    file_path: str = ""                                   # master.db FolderPath (original case)

    @property
    def path(self) -> str:
        """Filesystem path (original case kept, for opening the file)."""
        if self.file_path:
            return self.file_path
        from urllib.parse import unquote
        s = self.location
        for p in ("file://localhost/", "file:///", "file://"):
            if s.startswith(p):
                s = s[len(p):]
                break
        s = unquote(s)
        return s if s[1:3] == ":/" or s.startswith("/") else "/" + s


@dataclass
class PlaylistNode:
    name: str
    path: str                     # "Folder/Sub/List"
    is_folder: bool
    track_keys: list[str] = field(default_factory=list)
    children: list["PlaylistNode"] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"name": self.name, "path": self.path, "folder": self.is_folder,
                "count": len(self.track_keys), "children": [c.to_dict() for c in self.children]}

    def walk(self):
        yield self
        for c in self.children:
            yield from c.walk()


@dataclass
class Library:
    source: Path
    version: str
    product: ET.Element | None
    tracks: dict[str, Track]                  # by key
    by_id: dict[str, str]                     # TrackID -> key
    root: PlaylistNode
    kind: str = "xml"                         # "xml" | "masterdb"

    def playlist(self, path: str) -> PlaylistNode | None:
        return next((n for n in self.root.walk() if n.path == path and not n.is_folder), None)


def _f(v: str | None) -> float | None:
    try:
        return float(v) if v not in (None, "") else None
    except ValueError:
        return None


def read_library(xml_path: Path) -> Library:
    root = ET.parse(xml_path).getroot()
    if root.tag != "DJ_PLAYLISTS":
        raise ValueError(f"{xml_path.name} is not a rekordbox collection export (no DJ_PLAYLISTS)")
    coll = root.find("COLLECTION")
    tracks: dict[str, Track] = {}
    by_id: dict[str, str] = {}
    by_loc: dict[str, str] = {}
    for el in (coll.findall("TRACK") if coll is not None else []):
        loc = el.get("Location", "")
        if not loc:
            continue
        k = normalise_path(loc)
        t = Track(track_id=el.get("TrackID", ""), location=loc, key=k, title=el.get("Name", ""),
                  artist=el.get("Artist", ""), genre=el.get("Genre", ""), bpm=_f(el.get("AverageBpm")),
                  tonality=el.get("Tonality", ""), duration_s=_f(el.get("TotalTime")),
                  comments=el.get("Comments", ""), element=el, album=el.get("Album", ""),
                  label=el.get("Label", ""), year=int(_f(el.get("Year")) or 0) or None,
                  rating=int(_f(el.get("Rating")) or 0) // 51)
        tracks[k] = t
        by_id[t.track_id] = k
        by_loc[loc] = k

    def build(node: ET.Element, prefix: str) -> PlaylistNode:
        name = node.get("Name", "")
        path = f"{prefix}/{name}" if prefix else name
        if node.get("Type") == "1":
            key_by_loc = node.get("KeyType") == "1"
            keys = []
            for t in node.findall("TRACK"):
                k = by_loc.get(t.get("Key", "")) if key_by_loc else by_id.get(t.get("Key", ""))
                if k:
                    keys.append(k)
            return PlaylistNode(name, path, False, keys)
        pn = PlaylistNode(name, path, True)
        pn.children = [build(c, path) for c in node.findall("NODE")]
        return pn

    top = root.find("PLAYLISTS/NODE")
    tree = PlaylistNode("ROOT", "", True)
    if top is not None:
        tree.children = [build(c, "") for c in top.findall("NODE")]
    return Library(xml_path, root.get("Version", "1.0.0"), root.find("PRODUCT"), tracks, by_id, tree)
