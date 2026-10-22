# subjetunes/desktop/rbdb.py
"""rekordbox master.db as the library: read everything, write staged edits.

Reading works while rekordbox is open. Writing needs rekordbox closed: edits are
staged (data/pending.json) and written in one transaction after a backup, then
verified by reading them back. pyrekordbox bumps rekordbox's change counters (USN)
on commit, so rekordbox picks the changes up like its own.

What we read so nothing is analysed twice: BPM, key, length and rekordbox's own
beat grid (ANLZ PQTZ), plus every tag-like field (genre, comments, My Tags, colour,
rating, label...) to use as clues.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..tagging.comments import normalise_path
from ..tagging import masterdb as mdb
from .library import Library, PlaylistNode, Track

ATTR_PLAYLIST, ATTR_FOLDER, ATTR_SMART = 0, 1, 4

# Camelot wheel for the key names rekordbox stores (ScaleName).
_CAMELOT = {
    "Abm": "1A", "G#m": "1A", "B": "1B", "Ebm": "2A", "D#m": "2A", "F#": "2B", "Gb": "2B",
    "Bbm": "3A", "A#m": "3A", "Db": "3B", "C#": "3B", "Fm": "4A", "Ab": "4B", "G#": "4B",
    "Cm": "5A", "Eb": "5B", "D#": "5B", "Gm": "6A", "Bb": "6B", "A#": "6B", "Dm": "7A", "F": "7B",
    "Am": "8A", "C": "8B", "Em": "9A", "G": "9B", "Bm": "10A", "D": "10B", "F#m": "11A", "Gbm": "11A",
    "A": "11B", "C#m": "12A", "Dbm": "12A", "E": "12B",
}


def camelot(key: str) -> str:
    k = (key or "").strip()
    if k[:-1].isdigit() and k[-1:] in ("A", "B"):
        return k
    return _CAMELOT.get(k, "")


_NOTES = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}
_SHARP = ["C", "Db", "D", "Eb", "E", "F", "F#", "G", "Ab", "A", "Bb", "B"]


def scale_name(key: str) -> str:
    """Any common key spelling -> rekordbox's ScaleName: '8A', 'A minor', 'Amin', 'A#m' -> 'Am', 'Bbm'."""
    k = (key or "").strip()
    if not k:
        return ""
    if k[:-1].isdigit() and k[-1:].upper() in ("A", "B"):
        inv = {v: s for s, v in _CAMELOT.items() if s in _CANON}
        return inv.get(k[:-1] + k[-1].upper(), "")
    m = re.match(r"^([A-Ga-g])\s*(#|b|♯|♭|sharp|flat)?\s*(m|min|minor|maj|major)?$", k, re.I)
    if not m:
        return ""
    pc = _NOTES[m.group(1).upper()]
    acc = (m.group(2) or "").lower()
    pc = (pc + (1 if acc in ("#", "♯", "sharp") else -1 if acc in ("b", "♭", "flat") else 0)) % 12
    minor = (m.group(3) or "").lower() in ("m", "min", "minor")
    name = _SHARP[pc]
    if minor and name in ("Db", "Ab"):
        name = {"Db": "C#", "Ab": "G#"}[name]   # rekordbox's usual minor spellings
    return name + ("m" if minor else "")


_CANON = {"Abm", "G#m", "B", "Ebm", "F#", "Bbm", "Db", "Fm", "Ab", "Cm", "Eb", "Gm", "Bb", "Dm", "F", "Am", "C",
          "Em", "G", "Bm", "D", "F#m", "A", "C#m", "E"}


# Fields shown and editable on the Track screen. kind: text | int | ref (a lookup table) | key | ro
FIELDS: dict[str, tuple[str, str]] = {
    "title": ("Title", "text"), "artist": ("Artist", "ref"), "album": ("Album", "ref"),
    "album_artist": ("Album artist", "ro"), "remixer": ("Remixer", "ref"), "mix_name": ("Mix name", "text"),
    "original_artist": ("Original artist", "ref"), "composer": ("Composer", "ref"), "label": ("Label", "ref"),
    "genre": ("Genre", "ref"), "year": ("Year", "int"), "release_date": ("Release date", "text"),
    "track_no": ("Track", "int"), "disc_no": ("Disc", "int"), "isrc": ("ISRC", "text"),
    "bpm": ("BPM", "ro"), "key": ("Key", "key"), "rating": ("Rating", "int"), "comments": ("Comments", "text"),
}
_TEXT_COL = {"title": "Title", "mix_name": "Subtitle", "release_date": "ReleaseDate", "isrc": "ISRC",
             "comments": "Commnt"}
_INT_COL = {"year": "ReleaseYear", "track_no": "TrackNo", "disc_no": "DiscNo", "rating": "Rating"}
_REF_COL = {"artist": ("ArtistID", "artist"), "remixer": ("RemixerID", "artist"),
            "original_artist": ("OrgArtistID", "artist"), "composer": ("ComposerID", "artist"),
            "album": ("AlbumID", "album"), "label": ("LabelID", "label"), "genre": ("GenreID", "genre")}


def _s(v: Any) -> str:
    return "" if v is None else str(v)


def _name(obj: Any, attr: str) -> str:
    try:
        return _s(getattr(obj, attr, None))
    except Exception:
        return ""


def content_meta(c: Any) -> dict[str, Any]:
    bpm = (c.BPM or 0) / 100 if getattr(c, "BPM", None) else None
    key = _name(c, "KeyName")
    return {
        "title": _s(c.Title), "artist": _name(c, "ArtistName"), "album": _name(c, "AlbumName"),
        "album_artist": _name(c, "AlbumArtistName"), "remixer": _name(c, "RemixerName"),
        "mix_name": _s(getattr(c, "Subtitle", "")), "original_artist": _name(c, "OrgArtistName"),
        "composer": _name(c, "ComposerName"), "label": _name(c, "LabelName"), "genre": _name(c, "GenreName"),
        "year": c.ReleaseYear or None, "release_date": _s(getattr(c, "ReleaseDate", "")),
        "track_no": c.TrackNo or None, "disc_no": c.DiscNo or None, "isrc": _s(getattr(c, "ISRC", "")),
        "bpm": bpm, "key": key, "camelot": camelot(key), "rating": c.Rating or 0,
        "comments": _s(c.Commnt), "color": _name(c, "ColorName"), "play_count": int(c.DJPlayCount or 0)
        if str(c.DJPlayCount or "0").isdigit() else 0, "length": c.Length, "file_type": c.FileType,
        "bitrate": c.BitRate, "sample_rate": c.SampleRate, "image": _s(c.ImagePath),
        "analysed": bool(c.Analysed), "added": _s(getattr(c, "StockDate", "")),
    }


def _table(fetch) -> list:
    try:
        return list(fetch())
    except Exception:
        return []


class _Lookups:
    """Every small table read once, so building the library is ~10 queries instead of
    ~10 per track (lazy relationships made a 20k-track library take minutes)."""

    def __init__(self, db: Any):
        self.artist = {str(a.ID): _s(a.Name) for a in _table(db.get_artist)}
        self.album = {str(a.ID): (_s(a.Name), _s(a.AlbumArtistID)) for a in _table(db.get_album)}
        self.genre = {str(g.ID): _s(g.Name) for g in _table(db.get_genre)}
        self.label = {str(x.ID): _s(x.Name) for x in _table(db.get_label)}
        self.key = {str(k.ID): _s(k.ScaleName) for k in _table(db.get_key)}
        self.color = {str(c.ID): _s(c.Commnt) for c in _table(db.get_color)}


def fast_meta(c: Any, lk: _Lookups) -> dict[str, Any]:
    g = lambda d, i: d.get(str(i), "") if i not in (None, "") else ""   # noqa: E731
    album, album_artist_id = lk.album.get(str(c.AlbumID), ("", "")) if c.AlbumID else ("", "")
    key = g(lk.key, c.KeyID)
    try:
        plays = int(c.DJPlayCount or 0)
    except (TypeError, ValueError):
        plays = 0
    return {
        "title": _s(c.Title), "artist": g(lk.artist, c.ArtistID), "album": album,
        "album_artist": g(lk.artist, album_artist_id), "remixer": g(lk.artist, c.RemixerID),
        "mix_name": _s(c.Subtitle), "original_artist": g(lk.artist, c.OrgArtistID),
        "composer": g(lk.artist, c.ComposerID), "label": g(lk.label, c.LabelID), "genre": g(lk.genre, c.GenreID),
        "year": c.ReleaseYear or None, "release_date": _s(c.ReleaseDate), "track_no": c.TrackNo or None,
        "disc_no": c.DiscNo or None, "isrc": _s(c.ISRC), "bpm": (c.BPM or 0) / 100 if c.BPM else None,
        "key": key, "camelot": camelot(key), "rating": c.Rating or 0, "comments": _s(c.Commnt),
        "color": g(lk.color, c.ColorID), "play_count": plays, "length": c.Length, "file_type": c.FileType,
        "bitrate": c.BitRate, "sample_rate": c.SampleRate, "image": _s(c.ImagePath),
        "analysed": bool(c.Analysed), "added": _s(c.StockDate),
    }


def read_masterdb(db: Any, source: Path | None = None, progress: Callable[[str], None] | None = None) -> Library:
    """Build the same Library the XML reader makes, from an open pyrekordbox database."""
    say = progress or (lambda m: None)
    say("Reading tables")
    lk = _Lookups(db)
    mytags: dict[str, list[str]] = {}
    tags = {str(t.ID): t for t in _table(db.get_my_tag)}
    for link in _table(db.get_my_tag_songs):
        t = tags.get(str(link.MyTagID))
        if t is None:
            continue
        parent = tags.get(str(t.ParentID))
        mytags.setdefault(str(link.ContentID), []).append(f"{parent.Name}/{t.Name}" if parent is not None else _s(t.Name))

    say("Reading tracks")
    tracks: dict[str, Track] = {}
    by_id: dict[str, str] = {}
    for c in db.get_content():
        fp = _s(c.FolderPath)
        if not fp or getattr(c, "rb_local_deleted", 0):
            continue
        m = fast_meta(c, lk)
        k = normalise_path(fp)
        t = Track(track_id=str(c.ID), location="", key=k, title=m["title"], artist=m["artist"], genre=m["genre"],
                  bpm=m["bpm"], tonality=m["camelot"] or m["key"], duration_s=float(c.Length or 0) or None,
                  comments=m["comments"], content_id=str(c.ID), album=m["album"], label=m["label"],
                  year=m["year"], rating=m["rating"], mytags=sorted(mytags.get(str(c.ID), [])), meta=m,
                  file_path=fp)
        tracks[k] = t
        by_id[t.track_id] = k

    say("Reading playlists")
    songs: dict[str, list[tuple[int, str]]] = {}
    for sp in _table(db.get_playlist_songs):
        songs.setdefault(str(sp.PlaylistID), []).append((sp.TrackNo or 0, str(sp.ContentID)))
    rows = _table(db.get_playlist)
    nodes: dict[str, PlaylistNode] = {}
    parent: dict[str, str] = {}
    seq: dict[str, int] = {}
    for r in rows:
        attr = int(r.Attribute or 0)
        pid = str(r.ID)
        pn = PlaylistNode(_s(r.Name), "", attr == ATTR_FOLDER)
        if attr == ATTR_PLAYLIST:
            ids = [cid for _, cid in sorted(songs.get(pid, []))]
            pn.track_keys = [by_id[i] for i in ids if i in by_id]
        elif attr == ATTR_SMART:
            try:
                pn.track_keys = [by_id[str(x.ID)] for x in db.get_playlist_contents(r) if str(x.ID) in by_id]
            except Exception:
                pn.track_keys = []    # smart rules this pyrekordbox version can't evaluate
        nodes[pid] = pn
        parent[pid] = _s(r.ParentID) or "root"
        seq[pid] = int(r.Seq or 0)
    root = PlaylistNode("ROOT", "", True)
    for pid in sorted(nodes, key=lambda p: seq[p]):
        (nodes.get(parent[pid]) or root).children.append(nodes[pid])

    def set_paths(n: PlaylistNode, prefix: str) -> None:
        for ch in n.children:
            ch.path = f"{prefix}/{ch.name}" if prefix else ch.name
            set_paths(ch, ch.path)

    set_paths(root, "")
    src = source or Path(getattr(db, "db_directory", ".")) / "master.db"
    return Library(src, "1.0.0", None, tracks, by_id, root, kind="masterdb")


def beat_grid(db: Any, content_id: str) -> dict | None:
    """rekordbox's beat grid for a track: {'first_beat': s, 'bpm': avg, 'times': [...]} (None if not analysed)."""
    try:
        anlz = db.read_anlz_file(content_id, "DAT")
        if anlz is None:
            return None
        tag = anlz.get_tag("PQTZ") if hasattr(anlz, "get_tag") else anlz["PQTZ"]
        beats, bpms, times = tag.get()
        if not len(times):
            return None
        return {"first_beat": float(times[0]), "bpm": float(bpms.mean()), "downbeats": int((beats == 1).sum()),
                "times": [round(float(x), 4) for x in times]}
    except Exception:
        return None


def artwork_path(db: Any, image: str) -> Path | None:
    if not image:
        return None
    try:
        p = Path(db.share_directory) / image.lstrip("/\\")
        return p if p.is_file() else None
    except Exception:
        return None


# ---------------------------------------------------------------- writing staged edits

@dataclass
class EditReport:
    applied: dict[str, dict[str, Any]] = field(default_factory=dict)   # content_id -> {field: new}
    skipped: list[str] = field(default_factory=list)
    backup_dir: Path | None = None
    playlists: dict[str, int] = field(default_factory=dict)


def _lookup(db: Any, kind: str, name: str) -> Any:
    getter = {"artist": db.get_artist, "album": db.get_album, "label": db.get_label, "genre": db.get_genre}[kind]
    adder = {"artist": db.add_artist, "album": db.add_album, "label": db.add_label, "genre": db.add_genre}[kind]
    found = getter(Name=name)
    found = found.first() if hasattr(found, "first") else found
    return found if found is not None else adder(name)


def apply_edit(db: Any, c: Any, fld: str, value: Any) -> None:
    if fld in _TEXT_COL:
        setattr(c, _TEXT_COL[fld], "" if value is None else str(value))
    elif fld in _INT_COL:
        setattr(c, _INT_COL[fld], int(value) if str(value or "").strip() else 0)
    elif fld in _REF_COL:
        col, kind = _REF_COL[fld]
        name = str(value or "").strip()
        setattr(c, col, None if not name else _lookup(db, kind, name).ID)
    elif fld == "key":
        name = scale_name(str(value))
        k = None
        for cand in {name, {"G#m": "Abm", "Abm": "G#m", "C#m": "Dbm", "Eb": "D#", "F#": "Gb"}.get(name, name)}:
            k = db.get_key(ScaleName=cand)
            k = k.first() if hasattr(k, "first") else k
            if k is not None:
                break
        if k is None:
            raise ValueError(f"key {value!r} isn't one rekordbox has in this library")
        c.KeyID = k.ID
    else:
        raise ValueError(f"{fld} can't be edited")


def _child_folder(db: Any, name: str, parent_id: str) -> Any:
    for p in db.get_playlist():
        if str(p.ParentID) == parent_id and p.Name == name and int(p.Attribute or 0) == ATTR_FOLDER:
            return p
    return None


def write_edits(edits: dict[str, dict[str, Any]], backups_root: Path, db_path: Path | None = None, key: str = "",
                opener: Callable[..., Any] = mdb.open_db,
                playlists: dict[str, list[str]] | None = None, folder: str = "") -> EditReport:
    """edits: {content_id: {field: value}}; comments values are the full new comment.
    playlists: {name: [content_id]} created inside Subjetunes/<folder>. Nothing existing is deleted."""
    if mdb.rekordbox_running():
        raise mdb.WriteError("Close rekordbox first. Your changes stay saved here until then.")
    rep = EditReport()
    db = opener(db_path, key)
    try:
        db_dir = Path(getattr(db, "db_directory", None) or (db_path.parent if db_path else "."))
        rep.backup_dir = mdb.backup(db_dir, backups_root)
        try:
            for cid, fields in edits.items():
                c = db.get_content(ID=cid)
                c = c.first() if hasattr(c, "first") else c
                if c is None:
                    rep.skipped.append(cid)
                    continue
                for f, v in fields.items():
                    apply_edit(db, c, f, v)
                rep.applied[cid] = dict(fields)
            if playlists:
                root = _child_folder(db, "Subjetunes", "root") or db.create_playlist_folder("Subjetunes")
                sub = db.create_playlist_folder(folder or "Batch", parent=root)
                for name, ids in playlists.items():
                    pl = db.create_playlist(name, parent=sub)
                    for cid in ids:
                        c = db.get_content(ID=cid)
                        c = c.first() if hasattr(c, "first") else c
                        if c is not None:
                            db.add_to_playlist(pl, c)
                    rep.playlists[name] = len(ids)
            db.commit()
        except Exception as e:
            db.rollback()
            raise mdb.WriteError(f"Nothing was written (rolled back): {e}") from e
    finally:
        db.close()
    check = opener(db_path, key)
    try:
        bad = []
        for cid, fields in rep.applied.items():
            c = check.get_content(ID=cid)
            c = c.first() if hasattr(c, "first") else c
            m = content_meta(c)
            for f, v in fields.items():
                want = "" if v is None else str(v).strip()
                got = "" if m.get(f) is None else str(m.get(f)).strip()
                if f in _INT_COL:
                    want, got = str(int(want or 0)), str(int(got or 0))
                if f == "key":
                    want, got = camelot(scale_name(want)), camelot(got)
                if want != got:
                    bad.append(f"{c.Title}: {f}")
        if bad:
            raise mdb.WriteError(f"Written, but {len(bad)} values didn't read back ({bad[0]}). Backup: {rep.backup_dir}")
    finally:
        check.close()
    return rep
