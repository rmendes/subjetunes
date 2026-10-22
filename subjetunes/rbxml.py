"""Read a rekordbox collection XML and write a Subjetunes playlist export.

Import side (user, in rekordbox):
  Preferences > Advanced > Database > rekordbox xml  -> pick the exported file
  Preferences > View > Layout > tick "rekordbox xml" -> it shows in the sidebar
  Right-click the Subjetunes folder in the xml tree -> "Import Playlist"

Because every exported TRACK keeps the *original* Location string, rekordbox
matches it to the existing collection entry and creates no duplicates.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote

from .models import Library, Playlist, Track

LOCATION_PREFIXES = ("file://localhost/", "file:///", "file://")


def location_to_path(location: str) -> str:
    """'file://localhost/C:/Music/a%20b.mp3' -> 'C:/Music/a b.mp3' (Windows)
    'file://localhost/Users/x/a.mp3' -> '/Users/x/a.mp3' (macOS)."""
    loc = location
    for prefix in LOCATION_PREFIXES:
        if loc.startswith(prefix):
            loc = loc[len(prefix):]
            break
    path = unquote(loc)
    # Windows drive letter ("C:/...") stays as-is; otherwise it's an absolute POSIX path.
    if len(path) >= 2 and path[1] == ":":
        return path
    return path if path.startswith("/") else "/" + path


def _float(v: str | None) -> float | None:
    try:
        return float(v) if v not in (None, "") else None
    except ValueError:
        return None


def read_library(xml_path: str | Path) -> Library:
    tree = ET.parse(str(xml_path))
    root = tree.getroot()
    if root.tag != "DJ_PLAYLISTS":
        raise ValueError(f"Not a rekordbox XML (root is <{root.tag}>)")

    product = dict(root.find("PRODUCT").attrib) if root.find("PRODUCT") is not None else {}

    tracks: dict[str, Track] = {}
    by_location: dict[str, str] = {}
    collection = root.find("COLLECTION")
    for el in (collection if collection is not None else []):
        if el.tag != "TRACK":
            continue
        a = dict(el.attrib)
        tid = a.get("TrackID", "")
        loc = a.get("Location", "")
        if not tid or not loc:
            continue
        tracks[tid] = Track(
            track_id=tid,
            location=loc,
            path=location_to_path(loc),
            name=a.get("Name", ""),
            artist=a.get("Artist", ""),
            album=a.get("Album", ""),
            genre=a.get("Genre", ""),
            comments=a.get("Comments", ""),
            bpm=_float(a.get("AverageBpm")),
            key=a.get("Tonality", ""),
            duration_s=_float(a.get("TotalTime")),
            raw_attrs=a,
        )
        by_location[loc] = tid

    playlists: list[Playlist] = []

    def walk(node: ET.Element, parents: list[str]) -> None:
        for child in node:
            if child.tag != "NODE":
                continue
            name = child.attrib.get("Name", "")
            ntype = child.attrib.get("Type", "0")
            if ntype == "0":                                  # folder
                walk(child, parents + [name])
            elif ntype == "1":                                # playlist
                key_type = child.attrib.get("KeyType", "0")
                ids: list[str] = []
                for t in child.findall("TRACK"):
                    key = t.attrib.get("Key", "")
                    tid = by_location.get(key) if key_type == "1" else key
                    if tid and tid in tracks:
                        ids.append(tid)
                playlists.append(Playlist(path="/".join(parents + [name]), name=name, track_ids=ids))

    pl_root = root.find("PLAYLISTS")
    if pl_root is not None:
        top = pl_root.find("NODE")                            # ROOT node
        if top is not None:
            walk(top, [])

    return Library(source_path=str(xml_path), tracks=tracks, playlists=playlists, product=product)


def default_folder_name() -> str:
    return f"Subjetunes {datetime.now():%Y-%m-%d %H%M}"


def write_export(
    library: Library,
    bucket_playlists: dict[str, list[str]],
    out_path: str | Path,
    folder_name: str | None = None,
) -> Path:
    """bucket_playlists: playlist name -> ordered TrackIDs. Empty playlists are skipped."""
    folder_name = folder_name or default_folder_name()
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    used: list[str] = []
    seen: set[str] = set()
    for ids in bucket_playlists.values():
        for tid in ids:
            if tid in library.tracks and tid not in seen:
                seen.add(tid)
                used.append(tid)

    root = ET.Element("DJ_PLAYLISTS", {"Version": "1.0.0"})
    ET.SubElement(root, "PRODUCT", library.product or {"Name": "rekordbox", "Version": "6", "Company": "AlphaTheta"})

    coll = ET.SubElement(root, "COLLECTION", {"Entries": str(len(used))})
    for tid in used:
        # Original attributes, verbatim. Child elements (cues, tempo) intentionally omitted.
        ET.SubElement(coll, "TRACK", dict(library.tracks[tid].raw_attrs))

    non_empty = {n: [t for t in ids if t in seen] for n, ids in bucket_playlists.items()}
    non_empty = {n: ids for n, ids in non_empty.items() if ids}

    playlists = ET.SubElement(root, "PLAYLISTS")
    top = ET.SubElement(playlists, "NODE", {"Type": "0", "Name": "ROOT", "Count": "1"})
    folder = ET.SubElement(top, "NODE", {"Type": "0", "Name": folder_name, "Count": str(len(non_empty))})
    for name, ids in non_empty.items():
        pl = ET.SubElement(folder, "NODE", {"Name": name, "Type": "1", "KeyType": "0", "Entries": str(len(ids))})
        for tid in ids:
            ET.SubElement(pl, "TRACK", {"Key": tid})

    ET.indent(root, space="  ")
    ET.ElementTree(root).write(str(out_path), encoding="UTF-8", xml_declaration=True)
    return out_path
