# tests/test_desktop.py
import json
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from subjetunes.desktop.buckets import Bucket, assign, rule_error
from subjetunes.desktop.export import write_bucket_xml
from subjetunes.desktop.library import read_library

XML = """<?xml version="1.0" encoding="UTF-8"?>
<DJ_PLAYLISTS Version="1.0.0"><PRODUCT Name="rekordbox" Version="7.0.0" Company="AlphaTheta"/>
<COLLECTION Entries="3">
 <TRACK TrackID="1" Name="A" Artist="X" AverageBpm="128.00" Location="file://localhost/C:/M/a%20%C3%BC.mp3" Comments="mine"><TEMPO Inizio="0" Bpm="128" Metro="4/4" Battito="1"/></TRACK>
 <TRACK TrackID="2" Name="B" Artist="Y" Location="file://localhost/C:/M/b.mp3"/>
 <TRACK TrackID="3" Name="C" Artist="Z" Location="file://localhost/C:/M/c.mp3"/>
</COLLECTION>
<PLAYLISTS><NODE Type="0" Name="ROOT" Count="2">
 <NODE Type="0" Name="Gigs" Count="1"><NODE Type="1" Name="Fri" KeyType="0" Entries="2"><TRACK Key="1"/><TRACK Key="2"/></NODE></NODE>
 <NODE Type="1" Name="ByLoc" KeyType="1" Entries="1"><TRACK Key="file://localhost/C:/M/c.mp3"/></NODE>
</NODE></PLAYLISTS></DJ_PLAYLISTS>"""


@pytest.fixture
def lib(tmp_path):
    p = tmp_path / "rb.xml"
    p.write_text(XML, encoding="utf-8")
    return read_library(p)


def test_read_library(lib):
    assert len(lib.tracks) == 3
    assert lib.playlist("Gigs/Fri").track_keys == ["c:/m/a ü.mp3", "c:/m/b.mp3"]
    assert lib.playlist("ByLoc").track_keys == ["c:/m/c.mp3"]
    assert lib.tracks["c:/m/a ü.mp3"].path == "C:/M/a ü.mp3"
    assert lib.tracks["c:/m/a ü.mp3"].bpm == 128.0


def test_buckets_assign():
    bs = [Bucket("peak", "Peak", rule="energy >= 0.75"), Bucket("llm", "LLM only", rule="", use_llm=True)]
    assert assign({"energy": 0.9}, bs) == (["peak"], [])
    assert assign({"energy": 0.77}, bs) == (["peak"], ["peak"])
    assert rule_error("energy >=") and rule_error("energy > 1") is None


def test_export_verbatim_tracks_and_tags(lib, tmp_path):
    out = tmp_path / "o.xml"
    rep = write_bucket_xml(lib, {"Peak": ["c:/m/a ü.mp3"], "Tag update": ["c:/m/a ü.mp3", "c:/m/b.mp3"]},
                           out, "Subjetunes test", {"c:/m/a ü.mp3": ["#e5"]})
    root = ET.parse(out).getroot()
    assert rep.tracks == 2 and rep.tagged == 1
    a = root.find("COLLECTION/TRACK[@TrackID='1']")
    assert a.get("Comments") == "mine {st #e5}" and a.find("TEMPO") is not None
    assert a.get("Location") == "file://localhost/C:/M/a%20%C3%BC.mp3"
    folder = root.find("PLAYLISTS/NODE/NODE")
    assert folder.get("Name") == "Subjetunes test"
    assert [n.get("Entries") for n in folder] == ["1", "2"]


def test_server_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setenv("SUBJETUNES_HOME", str(tmp_path))
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "tags.yaml").write_text(
        (Path(__file__).resolve().parents[1] / "config" / "tags.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    xml = tmp_path / "rb.xml"
    xml.write_text(XML, encoding="utf-8")
    from subjetunes.desktop.server import start
    srv, url = start()
    base, token = url.split("/?t=")

    def call(path, body=None):
        req = urllib.request.Request(f"{base}/api/{path}", data=None if body is None else json.dumps(body).encode(),
                                     headers={"X-Token": token, "Content-Type": "application/json"})
        return json.loads(urllib.request.urlopen(req).read())

    try:
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen(f"{base}/api/status")            # no token
        call("open", {"path": str(xml)})
        call("demo", {"on": True})
        st = call("batch", {"playlists": ["Gigs/Fri", "ByLoc"]})
        assert st["counts"]["tracks"] == 3 and st["counts"]["scored"] == 3
        rows = call("tracks")["rows"]
        k = rows[0]["key"]
        r = call("tags", {"key": k, "add": ["#e1"]})["row"]
        assert "#e1" in r["tags"] and not any(t in r["tags"] for t in ("#e2", "#e3", "#e4", "#e5"))
        r = call("buckets/edit", {"key": k, "add": ["peak_time"]})["row"]
        assert "peak_time" in r["buckets"] and r["bucket_edited"]
        ex = call("export", {"what": "both", "folder": "T"})
        assert Path(ex["path"]).exists() and ex["playlists"]["Tag update"] == 3
        assert call("check-rule", {"rule": "energy >"})["error"]
    finally:
        srv.shutdown()
