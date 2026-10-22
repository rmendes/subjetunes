# subjetunes/tagging/xml_update.py
"""Put comment tags into rekordbox XML.

Why this file is careful: rekordbox does NOT update tracks that are already in your
collection when you import a playlist from the "rekordbox xml" tree. To push changed
Comments in, you must select the *tracks* (Ctrl+A inside the XML playlist) and choose
"Import To Collection", which overwrites those tracks from the XML. So every TRACK
we write is a verbatim copy of the one in your fresh collection export, child
elements (TEMPO beat grid, POSITION_MARK cues) included, with only Comments changed.
Never feed rekordbox a stripped TRACK in that mode.

If you can close rekordbox, ``masterdb.py`` is the more direct route.
"""

from __future__ import annotations

import copy
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

from .comments import BlockFormat, merge_comment, normalise_path


@dataclass
class XmlReport:
    out_path: Path | None = None
    changed: int = 0
    unchanged: int = 0
    not_found: list[str] = field(default_factory=list)
    too_long: list[str] = field(default_factory=list)


def _index(collection: ET.Element) -> dict[str, ET.Element]:
    return {normalise_path(t.get("Location", "")): t for t in collection.findall("TRACK")}


def _product(root: ET.Element) -> ET.Element:
    p = root.find("PRODUCT")
    return copy.deepcopy(p) if p is not None else ET.Element(
        "PRODUCT", {"Name": "rekordbox", "Version": "6.0.0", "Company": "AlphaTheta"})


def _write(root: ET.Element, out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    ET.indent(root, space="  ")
    body = ET.tostring(root, encoding="unicode")
    out.write_text('<?xml version="1.0" encoding="UTF-8"?>\n' + body + "\n", encoding="utf-8")


def write_comment_update_xml(source_xml: Path, new_tags: Mapping[str, list[str]], out_path: Path,
                             fmt: BlockFormat = BlockFormat(), playlist_name: str = "Subjetunes tag update",
                             only_changed: bool = True) -> XmlReport:
    """XML with one playlist of the tracks whose Comments change.

    source_xml: a *fresh* File > Export Collection in xml format.
    new_tags: {path or Location: [tags]} (keys are normalised).
    """
    src = ET.parse(source_xml).getroot()
    coll = src.find("COLLECTION")
    if coll is None:
        raise ValueError(f"{source_xml} has no COLLECTION element")
    idx = _index(coll)
    rep = XmlReport(out_path=out_path)

    out_root = ET.Element("DJ_PLAYLISTS", {"Version": src.get("Version", "1.0.0")})
    out_root.append(_product(src))
    out_coll = ET.SubElement(out_root, "COLLECTION")
    keys: list[str] = []
    for raw_key, tags in new_tags.items():
        k = normalise_path(raw_key)
        el = idx.get(k)
        if el is None:
            rep.not_found.append(raw_key)
            continue
        old = el.get("Comments", "")
        new = merge_comment(old, tags, fmt)
        if new == old and only_changed:
            rep.unchanged += 1
            continue
        t = copy.deepcopy(el)  # children (TEMPO, POSITION_MARK) kept verbatim
        t.set("Comments", new)
        out_coll.append(t)
        keys.append(t.get("TrackID", ""))
        rep.changed += 1
        if len(new) > fmt.max_len:
            rep.too_long.append(raw_key)
    out_coll.set("Entries", str(len(keys)))

    pls = ET.SubElement(out_root, "PLAYLISTS")
    root_node = ET.SubElement(pls, "NODE", {"Type": "0", "Name": "ROOT", "Count": "1"})
    pl = ET.SubElement(root_node, "NODE", {"Name": playlist_name, "Type": "1", "KeyType": "0",
                                           "Entries": str(len(keys))})
    for k in keys:
        ET.SubElement(pl, "TRACK", {"Key": k})
    _write(out_root, out_path)
    return rep


def apply_to_export(export_xml: Path, source_xml: Path, new_tags: Mapping[str, list[str]], out_path: Path,
                    fmt: BlockFormat = BlockFormat()) -> XmlReport:
    """Add tags to an existing Subjetunes bucket export, in one file.

    Each TRACK in the export's COLLECTION is replaced by the verbatim TRACK from
    ``source_xml`` (so cues and grids survive a forced "Import To Collection"), then
    its Comments are merged. Playlists in the export are left as they are.
    """
    exp = ET.parse(export_xml).getroot()
    coll = exp.find("COLLECTION")
    src_coll = ET.parse(source_xml).getroot().find("COLLECTION")
    if coll is None or src_coll is None:
        raise ValueError("both files need a COLLECTION element")
    idx = _index(src_coll)
    tags_by = {normalise_path(k): v for k, v in new_tags.items()}
    rep = XmlReport(out_path=out_path)
    for i, el in enumerate(list(coll)):
        if el.tag != "TRACK":
            continue
        k = normalise_path(el.get("Location", ""))
        full = idx.get(k)
        if full is None:
            rep.not_found.append(el.get("Location", ""))
            full = el
        t = copy.deepcopy(full)
        t.set("TrackID", el.get("TrackID", t.get("TrackID", "")))  # playlists in the export key on this
        if k in tags_by:
            old = t.get("Comments", "")
            new = merge_comment(old, tags_by[k], fmt)
            t.set("Comments", new)
            rep.changed += new != old
            rep.unchanged += new == old
            if len(new) > fmt.max_len:
                rep.too_long.append(k)
        coll[i] = t
    _write(exp, out_path)
    return rep
