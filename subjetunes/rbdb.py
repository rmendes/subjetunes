"""Read rekordbox 6/7 master.db (read-only) into the same Library model as rbxml."""
from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

from .models import Library, Playlist, Track

LOCATION_SAFE = "/:"   # tune with `verify_locations` if rekordbox encodes differently


def path_to_location(path: str) -> str:
    p = path.replace("\\", "/")
    return "file://localhost/" + quote(p.lstrip("/"), safe=LOCATION_SAFE)


def _open(db_path: str | None):
    try:
        from pyrekordbox import MasterDatabase as DB          # newer releases
    except ImportError:
        from pyrekordbox import Rekordbox6Database as DB      # older releases
    if db_path and Path(db_path).is_dir():
        db_path = str(Path(db_path) / "master.db")
    return DB(path=db_path) if db_path else DB()


def _name(obj, attr):
    v = getattr(obj, attr, None)
    return getattr(v, "Name", "") if v is not None else ""


def read_library(db_path: str | None = None) -> Library:
    db = _open(db_path)
    tracks: dict[str, Track] = {}
    for c in db.get_content():
        path = c.FolderPath or ""
        if not path:
            continue
        tid = str(c.ID)
        bpm = (c.BPM / 100.0) if c.BPM else None             # stored as BPM * 100
        loc = path_to_location(path)
        title, artist = c.Title or "", _name(c, "Artist")
        tracks[tid] = Track(
            track_id=tid, location=loc, path=path, name=title, artist=artist,
            album=_name(c, "Album"), genre=_name(c, "Genre"), comments=c.Commnt or "",
            bpm=bpm, key=_name(c, "Key") or getattr(getattr(c, "Key", None), "ScaleName", "") or "",
            duration_s=float(c.Length) if c.Length else None,
            raw_attrs={"TrackID": tid, "Name": title, "Artist": artist,
                       "AverageBpm": f"{bpm:.2f}" if bpm else "", "Location": loc},
        )

    pls = list(db.get_playlist())
    by_id = {str(p.ID): p for p in pls}

    def full_path(p) -> str:
        parts, cur = [], p
        while cur is not None:
            parts.append(cur.Name)
            cur = by_id.get(str(cur.ParentID))
        return "/".join(reversed(parts))

    playlists = []
    for p in pls:
        if p.Attribute != 0:          # 1 = folder, 4 = smart playlist (no stored songs)
            continue
        songs = sorted(p.Songs, key=lambda s: s.TrackNo or 0)
        ids = [str(s.ContentID) for s in songs if str(s.ContentID) in tracks]
        playlists.append(Playlist(path=full_path(p), name=p.Name, track_ids=ids))
    db.close()
    return Library(source_path=str(db_path or "master.db"), tracks=tracks, playlists=playlists,
                   product={"Name": "rekordbox", "Version": "7", "Company": "AlphaTheta"})


def verify_locations(xml_path: str, db_path: str | None = None, show: int = 10) -> int:
    """Compare locations we build from master.db with the ones rekordbox writes in an XML export."""
    from . import rbxml
    xml_locs = {t.path.lower(): t.location for t in rbxml.read_library(xml_path).tracks.values()}
    bad = 0
    for t in read_library(db_path).tracks.values():
        real = xml_locs.get(t.path.replace("\\", "/").lower())
        if real and real != t.location:
            bad += 1
            if bad <= show:
                print(f"MISMATCH\n  rekordbox: {real}\n  ours:      {t.location}")
    print(f"{bad} mismatches")
    return bad
