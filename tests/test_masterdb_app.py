# tests/test_masterdb_app.py : master.db library, staged edits, clues and other sources.
import json
import urllib.request
from pathlib import Path

import pytest

pytest.importorskip("pyrekordbox")

from rbfixture import make_master_db, opener_for
from subjetunes.desktop import rbdb
from subjetunes.tagging import masterdb as mdb
from subjetunes.tagging.clues import ClueModel, track_clues
from subjetunes.tagging.expr import compile_rule


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(mdb, "rekordbox_running", lambda: False)
    return make_master_db(tmp_path / "rb")


@pytest.fixture
def state(db, tmp_path, monkeypatch):
    monkeypatch.setenv("SUBJETUNES_HOME", str(tmp_path / "home"))
    (tmp_path / "home" / "config").mkdir(parents=True)
    (tmp_path / "home" / "config" / "tags.yaml").write_text(
        (Path(__file__).resolve().parents[1] / "config" / "tags.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    from subjetunes.desktop.state import AppState
    s = AppState(opener=opener_for(db))
    import time
    for _ in range(100):                 # the session restore loads in the background first
        if not s.loading:
            break
        time.sleep(0.05)
    s.open_masterdb(db)
    return s


def test_read_masterdb(db):
    with_db = opener_for(db)()
    lib = rbdb.read_masterdb(with_db)
    with_db.close()
    t = lib.tracks["c:/music/selecta.mp3"]
    assert (t.title, t.artist, t.bpm, t.tonality, t.genre, t.label) == ("Selecta", "Bianca Oblivion", 140.0, "7A", "UK Bass", "Sub Zero Rec")
    assert t.mytags == ["Genre/Dubstep"] and t.content_id == "101"
    assert lib.playlist("Gigs/Friday").track_keys == ["c:/music/selecta.mp3", "c:/music/acid fountain.mp3"]


def test_write_edits_and_playlists_verified(db, tmp_path):
    op = opener_for(db)
    rep = rbdb.write_edits({"101": {"label": "New Label", "year": 2023, "key": "D minor", "artist": "Ohm Seven",
                                    "comments": "uk bass {st #e4}"}},
                           tmp_path / "bk", db, opener=op, playlists={"Peak time": ["101", "102"]}, folder="Friday")
    assert rep.applied["101"]["label"] == "New Label" and (rep.backup_dir / "master.db").exists()
    d = op()
    lib = rbdb.read_masterdb(d)
    d.close()
    t = lib.tracks["c:/music/selecta.mp3"]
    assert (t.label, t.year, t.artist, t.tonality, t.comments) == ("New Label", 2023, "Ohm Seven", "7A", "uk bass {st #e4}")
    assert lib.playlist("Subjetunes/Friday/Peak time").track_keys == ["c:/music/selecta.mp3", "c:/music/acid fountain.mp3"]


def test_write_refuses_while_rekordbox_runs(db, tmp_path, monkeypatch):
    monkeypatch.setattr(mdb, "rekordbox_running", lambda: True)
    with pytest.raises(mdb.WriteError):
        rbdb.write_edits({"101": {"title": "x"}}, tmp_path / "bk", db, opener=opener_for(db))


@pytest.mark.parametrize("raw,want", [("8A", "Am"), ("A minor", "Am"), ("D Minor", "Dm"), ("Gb major", "F#"), ("x", "")])
def test_scale_name(raw, want):
    assert rbdb.scale_name(raw) == want


def test_clues_and_in_operator():
    c = track_clues("UK Bass; Dubstep", "8A - Energy 6 | dark, #halftime {st #e4}", ["Genre/Peak Time"])
    assert {"uk-bass", "dubstep", "dark", "halftime", "peak-time"} <= c and "e4" not in c
    assert not any(x.startswith("8a") for x in c)
    assert compile_rule("'dubstep' in clues and energy > 0.5")({"clues": frozenset(c), "energy": 0.8})
    assert compile_rule("'acid' not in clues")({"clues": frozenset(c)})
    assert not compile_rule("'acid' in clues")({})


def test_clue_model_learns_and_matches():
    m = ClueModel().fit([({"uk-bass"}, {"#dark"})] * 4 + [({"uk-bass"}, {"#bright"})])
    s = {x.tag: x for x in m.suggest({"uk-bass", "acid"}, ["#dark", "#bright", "#acid"])}
    assert s["#acid"].stable and "rekordbox" in s["#acid"].reason          # direct match
    assert not s["#dark"].stable and "4 of 5" in s["#dark"].reason         # learned, needs review
    assert "#bright" not in s


def test_state_batch_rows_stage_and_write(state):
    state.set_demo(True)
    state.set_batch(["Gigs/Friday"], ["c:/music/third.mp3"])
    rows = {r["title"]: r for r in state.rows()}
    assert set(rows) == {"Selecta", "Acid Fountain", "Third"}
    assert rows["Selecta"]["mytags"] == ["Genre/Dubstep"] and "uk-bass" in rows["Selecta"]["native"]
    assert "#warmup" not in rows["Third"]["tags"]                # already in your own comment
    state.stage("c:/music/selecta.mp3", "key", "8A")
    assert state.pending["c:/music/selecta.mp3"] == {"key": "Am"}
    with pytest.raises(ValueError):
        state.stage("c:/music/selecta.mp3", "year", "soon")
    plan = state.write_plan(tags=True, playlists=True)
    assert plan["edits"]["101"]["key"] == "Am" and "{st" in plan["edits"]["101"]["comments"]
    res = state.write(tags=True, playlists=True)
    assert res["written"] and not state.pending
    t = state.lib.tracks["c:/music/selecta.mp3"]
    assert t.tonality == "8A" and t.comments.startswith("uk bass {st ")


def test_track_detail_and_sources(state, monkeypatch):
    from subjetunes.desktop.sources import providers
    calls = []

    def fake_get(self, url, **kw):
        calls.append(url)
        if url.endswith("/recording"):
            return {"recordings": [{"id": "r1", "title": "Selecta", "artist-credit": [{"name": "Bianca Oblivion"}]}]}
        if "/recording/" in url:
            return {"title": "Selecta", "artist-credit": [{"name": "Bianca Oblivion", "joinphrase": ""}],
                    "genres": [{"name": "uk bass", "count": 3}], "tags": [{"name": "dubstep", "count": 1}],
                    "isrcs": ["GB1"], "releases": [{"id": "rel", "title": "Sub Zero EP", "date": "2022-03-18"}]}
        return {"label-info": [{"label": {"name": "Sub Zero Records"}, "catalog-number": "SZR002"}]}

    monkeypatch.setattr(providers.MusicBrainz, "get_json", fake_get)
    from subjetunes.desktop.sources import TrackQuery, lookup_all
    q = state.query("c:/music/selecta.mp3")
    res = {r.source: r for r in lookup_all(q, {}, state.cache, only=["musicbrainz", "rekordbox"])}
    mb = res["musicbrainz"]
    assert mb.state == "found" and mb.fields["label"] == "Sub Zero Records" and mb.match > 0.9
    d = state.detail("c:/music/selecta.mp3")
    label = next(f for f in d["fields"] if f["id"] == "label")
    assert any(a["value"] == "Sub Zero Records" for a in label["alts"])
    assert any(c["tag"] == "uk-bass" and len(c["sources"]) >= 2 for c in d["cloud"])


def test_file_tags_source(tmp_path):
    mutagen = pytest.importorskip("mutagen")
    sf = pytest.importorskip("soundfile")
    import numpy as np
    from mutagen.flac import FLAC
    p = tmp_path / "t.flac"
    sf.write(p, np.zeros(4410, dtype="float32"), 44100)
    f = FLAC(p)
    f.update({"title": "Selecta", "artist": "Bianca Oblivion", "genre": "UK Bass; Dubstep", "label": "Sub Zero",
              "grouping": "peak", "comment": "#halftime"})
    f.save()
    from subjetunes.desktop.sources import TrackQuery
    from subjetunes.desktop.sources.providers import FileTags
    r = FileTags({}).lookup(TrackQuery(key="k", path=str(p), title="Selecta", artist="Bianca Oblivion"))
    assert r.state == "found" and r.fields["label"] == "Sub Zero"
    assert {t[0] for t in r.tags} >= {"UK Bass", "Dubstep", "peak", "halftime"}


def test_server_with_masterdb(state):
    from subjetunes.desktop.server import start
    srv, url = start(state)
    base, token = url.split("/?t=")

    def call(path, body=None):
        req = urllib.request.Request(f"{base}/api/{path}", data=None if body is None else json.dumps(body).encode(),
                                     headers={"X-Token": token, "Content-Type": "application/json"})
        return json.loads(urllib.request.urlopen(req).read())

    try:
        st = call("batch", {"playlists": [], "tracks": ["C:/Music/Selecta.mp3"]})
        assert st["batch_name"] == "Selecta" and st["counts"]["tracks"] == 1
        assert call("search?q=acid")["results"][0]["title"] == "Acid Fountain"
        d = call("stage", {"key": "c:/music/selecta.mp3", "fields": {"label": "X Rec"}})
        assert next(f for f in d["fields"] if f["id"] == "label")["pending"] == "X Rec"
        call("stage", {"key": "c:/music/selecta.mp3", "fields": {"year": "2024"}})
        plan = call("write-plan", {"tags": False, "playlists": False})
        hunks = {h["field"]: h for h in plan["hunks"]}
        assert hunks["label"]["old"] == "Sub Zero Rec" and hunks["label"]["new"] == "X Rec" and plan["kept"] == 2
        plan = call("write-plan", {"tags": False, "playlists": False, "skip": [hunks["year"]["id"]]})
        assert plan["kept"] == 1 and any(h["skipped"] for h in plan["hunks"])
        res = call("write", {"tags": False, "playlists": False, "skip": [hunks["year"]["id"]]})
        assert res["written"] and res["changes"] == 1 and res["verified"]
        assert state.lib.tracks["c:/music/selecta.mp3"].label == "X Rec"
        assert state.lib.tracks["c:/music/selecta.mp3"].year == 2022       # skipped, so unchanged
        assert call("status")["pending"] == 1                                # the skipped edit stays staged
    finally:
        srv.shutdown()


def test_vocab_edit_from_the_tag_screen(state):
    state.set_demo(True)
    state.set_batch(["Gigs/Friday"], [])
    state.edit_tags("c:/music/selecta.mp3", ["#acid"], [])
    t = state.vocab_rename("#acid", "#acid-line")
    assert t == "#acid-line" and "#acid-line" in state.vocab_tags() and "#acid" not in state.vocab_tags()
    assert "#acid-line" in state.ledger.overrides["c:/music/selecta.mp3"].add     # your edit moved along
    assert state.vocab_add("sound", "Cowbell") == "#cowbell"
    sound = next(g for g in state.vocab() if g["group"] == "sound")
    assert "#cowbell" in sound["tags"] and "#cowbell" in sound["manual"]
    with pytest.raises(ValueError):
        state.vocab_add("mood", "#dark-ish")        # #dark would be a prefix of it
    state.vocab_remove("#cowbell")
    assert "#cowbell" not in state.vocab_tags()
