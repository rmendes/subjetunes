# subjetunes/desktop/export.py
"""Write bucket playlists (and optionally comment tags) as a rekordbox XML.

Every TRACK is a verbatim copy of the one in the source collection export, child
elements included. That keeps a forced "Import To Collection" from wiping beat grids
and cues, and keeps Location byte-identical so rekordbox matches existing tracks.
"""
from __future__ import annotations

import copy
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Mapping

from ..tagging.comments import BlockFormat, merge_comment
from .library import Library


@dataclass
class ExportReport:
    path: Path
    playlists: dict[str, int] = field(default_factory=dict)
    tracks: int = 0
    tagged: int = 0


def write_bucket_xml(lib: Library, playlists: Mapping[str, list[str]], out: Path, folder: str,
                     tags: Mapping[str, list[str]] | None = None, fmt: BlockFormat = BlockFormat()) -> ExportReport:
    """playlists: {playlist name: [track keys]} in order. tags: {key: [tags]} to merge into Comments."""
    rep = ExportReport(out)
    root = ET.Element("DJ_PLAYLISTS", {"Version": lib.version or "1.0.0"})
    root.append(copy.deepcopy(lib.product) if lib.product is not None else
                ET.Element("PRODUCT", {"Name": "rekordbox", "Version": "6.0.0", "Company": "AlphaTheta"}))
    coll = ET.SubElement(root, "COLLECTION")
    used: list[str] = []
    seen: set[str] = set()
    for keys in playlists.values():
        for k in keys:
            if k not in seen and k in lib.tracks:
                seen.add(k)
                used.append(k)
    for k in used:
        t = lib.tracks[k]
        el = copy.deepcopy(t.element) if t.element is not None else ET.Element("TRACK", {"TrackID": t.track_id,
                                                                                          "Location": t.location})
        if tags is not None and k in tags:
            new = merge_comment(el.get("Comments", ""), tags[k], fmt)
            if new != el.get("Comments", ""):
                rep.tagged += 1
            el.set("Comments", new)
        coll.append(el)
    coll.set("Entries", str(len(used)))
    rep.tracks = len(used)

    pls = ET.SubElement(root, "PLAYLISTS")
    rn = ET.SubElement(pls, "NODE", {"Type": "0", "Name": "ROOT", "Count": "1"})
    fn = ET.SubElement(rn, "NODE", {"Type": "0", "Name": folder, "Count": str(len(playlists))})
    for name, keys in playlists.items():
        ks = [k for k in keys if k in lib.tracks]
        pn = ET.SubElement(fn, "NODE", {"Name": name, "Type": "1", "KeyType": "0", "Entries": str(len(ks))})
        for k in ks:
            ET.SubElement(pn, "TRACK", {"Key": lib.tracks[k].track_id})
        rep.playlists[name] = len(ks)

    out.parent.mkdir(parents=True, exist_ok=True)
    ET.indent(root, space="  ")
    out.write_text('<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode") + "\n",
                   encoding="utf-8")
    return rep


def default_folder_name() -> str:
    return f"Subjetunes {datetime.now():%Y-%m-%d %H.%M}"
