# tests/test_startup.py : the "Not Responding" window and slow first load.
import time
from datetime import datetime
from pathlib import Path

import pytest

pytest.importorskip("pyrekordbox")
from rbfixture import make_master_db, opener_for


def test_jsapi_exposes_only_methods():
    """pywebview walks every public attribute of js_api recursively on the UI thread.
    A public reference to the window made it crawl the native form and freeze the app."""
    from subjetunes.desktop.__main__ import JsApi
    api = JsApi()
    api._window = object()
    public = [n for n in dir(api) if not n.startswith("_")]
    assert public == ["pick_file"]
    assert all(callable(getattr(api, n)) for n in public)


def _grow(db: Path, n: int) -> None:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from pyrekordbox.masterdb import models as m
    now = datetime(2026, 1, 1)
    eng = create_engine(f"sqlite:///{db}")
    with Session(eng) as s:
        s.add_all([m.DjmdArtist(ID=f"a{i}", Name=f"Artist {i}", created_at=now, updated_at=now) for i in range(50)])
        s.add_all([m.DjmdContent(ID=f"c{i}", FolderPath=f"C:/Music/{i}.mp3", Title=f"T{i}", ArtistID=f"a{i % 50}",
                                 GenreID="1", KeyID="2", BPM=12800, created_at=now, updated_at=now) for i in range(n)])
        s.add_all([m.DjmdSongPlaylist(ID=f"s{i}", PlaylistID="11", ContentID=f"c{i}", TrackNo=10 + i,
                                      created_at=now, updated_at=now) for i in range(n)])
        s.commit()


def test_reader_uses_a_fixed_number_of_queries(tmp_path):
    from sqlalchemy import event
    from subjetunes.desktop.rbdb import read_masterdb
    db = make_master_db(tmp_path / "rb")
    _grow(db, 400)
    d = opener_for(db)()
    count = {"n": 0}
    event.listen(d.engine, "before_cursor_execute", lambda *a: count.__setitem__("n", count["n"] + 1))
    t0 = time.time()
    lib = read_masterdb(d)
    d.close()
    assert len(lib.tracks) == 403 and len(lib.playlist("Gigs/Friday").track_keys) == 402
    assert lib.tracks["c:/music/7.mp3"].artist == "Artist 7"
    assert count["n"] < 25, f"{count['n']} queries for 403 tracks: something is lazy-loading per track"
    assert time.time() - t0 < 5


def test_library_loads_in_background(tmp_path, monkeypatch):
    monkeypatch.setenv("SUBJETUNES_HOME", str(tmp_path / "home"))
    db = make_master_db(tmp_path / "rb")
    from subjetunes.desktop.state import AppState
    slow = opener_for(db)

    def opener(p=None, k=""):
        time.sleep(0.5)
        return slow(p, k)
    s = AppState(opener=opener)          # starts loading, returns at once
    st = s.status()
    assert st["loading"] and st["library"] is None
    for _ in range(50):
        if not s.loading:
            break
        time.sleep(0.1)
    assert s.status()["library"]["tracks"] == 3
