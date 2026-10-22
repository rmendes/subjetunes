# tests/test_tagging.py
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace

import pytest

from subjetunes.tagging import (BlockFormat, Override, TagConfig, merge_comment, normalise_path,
                                normalise_tag, read_tags, suggest_batch, suggest_one, user_text, validate)
from subjetunes.tagging.expr import RuleError, compile_rule, is_stable
from subjetunes.tagging.ledger import Ledger
from subjetunes.tagging import masterdb
from subjetunes.tagging.xml_update import apply_to_export, write_comment_update_xml

CFG_PATH = Path(__file__).resolve().parents[1] / "config" / "tags.yaml"


# ---------- expressions
def test_rule_basic_and_missing():
    r = compile_rule("comp_acid >= 0.7 and energy > 0.5")
    assert r({"comp_acid": 0.8, "energy": 0.6})
    assert not r({"comp_acid": 0.8})                      # missing -> False
    assert not r({"comp_acid": float("nan"), "energy": 1})
    assert compile_rule("not (energy > 0.5)")({"energy": 0.2})
    assert compile_rule("crit_vocal_role == 'sung_hook'")({"crit_vocal_role": "sung_hook"})


@pytest.mark.parametrize("bad", ["__import__('os')", "a.b > 1", "open('x')", "[1,2]", "x if y else z", ""])
def test_rule_rejects_unsafe(bad):
    with pytest.raises(RuleError):
        compile_rule(bad)


def test_stability():
    r = compile_rule("energy >= 0.75")
    assert is_stable(r, {"energy": 0.9}, 0.05)
    assert not is_stable(r, {"energy": 0.77}, 0.05)


# ---------- comments
def test_merge_is_idempotent_and_keeps_user_text():
    fmt = BlockFormat()
    c = merge_comment("8A - Energy 6 | nice", ["#e4", "#acid"], fmt)
    assert c == "8A - Energy 6 | nice {st #e4 #acid}"
    assert merge_comment(c, ["#e4", "#acid"], fmt) == c
    c2 = merge_comment(c, ["#e5"], fmt)
    assert c2 == "8A - Energy 6 | nice {st #e5}"
    assert read_tags(c2) == ["#e5"]
    assert user_text(c2) == "8A - Energy 6 | nice"
    assert merge_comment(c2, [], fmt) == "8A - Energy 6 | nice"
    assert merge_comment("", ["#a"], fmt) == "{st #a}"
    assert merge_comment("x", ["#a"], BlockFormat(position="start")) == "{st #a} x"


def test_user_braces_are_not_ours():
    assert merge_comment("{stems} ok", ["#a"]) == "{stems} ok {st #a}"


def test_normalise():
    assert normalise_tag("Acid Line") == "#acid-line"
    assert normalise_tag("#Ébène") == "#ebene"
    assert normalise_path("file://localhost/C:/Music/a%20b.mp3") == normalise_path("C:\\MUSIC\\a b.mp3")
    assert normalise_path("file://localhost/Users/x/A.mp3") == "/Users/x/A.mp3"


# ---------- suggestion
def cfg():
    return TagConfig.load(CFG_PATH)


def test_default_config_is_valid():
    assert validate(cfg(), ["peak_time", "warmup_groovy", "late_night_weird", "vocal_moments"]) == []


def test_validate_catches_prefix_collision():
    c = TagConfig.from_dict({"rules": [{"tag": "#vocal", "when": "x>1"}, {"tag": "#vocals", "when": "x>1"}]})
    assert any("prefix" in p for p in validate(c))
    c = TagConfig.from_dict({"rules": [{"tag": "#vocal", "when": "x>1"}], "buckets": {"prefix": "#"}})
    assert any("prefix" in p for p in validate(c, ["vocal_moments"]))


def test_suggest_one_axes_caps_and_uncertainty():
    c = cfg()
    v = {"energy": 0.9, "looseness": 0.1, "comp_vocals": 0.72, "comp_acid": 0.95,
         "mood_bright": 0.1, "crit_hypnotic": 0.9, "crit_hypnotic_conf": 0.3,
         "crit_vocal_role": "sung_hook"}
    t = suggest_one("k", v, ["peak_time"], c, {"energy": [0.2, 0.4, 0.6, 0.8]})
    assert {"#e5", "#dark", "#driving", "#acid", "#peak", "#b-peak-time", "#vox-sung-hook"} <= set(t.tags)
    assert "#vocal" in t.uncertain          # 0.72 is within 0.05 of 0.7
    assert "#hypnotic" in t.uncertain       # judge confidence too low
    assert "#instrumental" not in t.tags


def test_mood_capped_at_two_strongest_first():
    c = cfg()
    v = {"energy": 0.95, "mood_bright": 0.05, "crit_hypnotic": 0.99, "crit_dancefloor_tension": 0.99, "mood_angry": 0.99}
    t = suggest_one("k", v, [], c, {"energy": [0.2, 0.4, 0.6, 0.8]})
    moods = [s.tag for s in t.suggestions if s.tag in ("#dark", "#hypnotic", "#tense", "#aggressive")]
    assert len(moods) == 2


def test_conflicts_drop_the_weaker_and_your_pick_wins():
    c = TagConfig.from_dict({"groups": {"a": {"max": 3}}, "rules": [
        {"tag": "#euphoric", "when": "x >= 0.5", "group": "a"}, {"tag": "#dark", "when": "y >= 0.5", "group": "a"}],
        "conflicts": [["#euphoric", "#dark"]]})
    t = suggest_one("k", {"x": 0.95, "y": 0.56}, [], c)
    assert "#euphoric" in t.tags and "#dark" not in t.tags + t.uncertain
    t = suggest_one("k", {"x": 0.95, "y": 0.56}, [], c, override=Override(add=["#dark"]))
    assert "#dark" in t.tags and "#euphoric" not in t.tags


def test_valence_arousal_derived():
    from subjetunes.tagging.suggest import derive
    v = derive({"mood_bright": 0.8, "energy": 0.6, "mood_agitated": 1.0})
    assert v["valence"] == 0.8 and abs(v["arousal"] - 0.7) < 1e-9
    assert derive({"valence": 0.2, "mood_bright": 0.9})["valence"] == 0.2


def test_overrides_win():
    c = cfg()
    v = {"energy": 0.9, "comp_acid": 0.95}
    t = suggest_one("k", v, [], c, {"energy": [0.2, 0.4, 0.6, 0.8]},
                    Override(add=["#closing"], remove=["#acid"]))
    assert t.tags[0] == "#closing" and "#acid" not in t.tags


def test_quantile_levels_spread_batch():
    c = cfg()
    rows = [{"key": f"C:/m/{i}.mp3", "energy": i / 99} for i in range(100)]
    res = suggest_batch(rows, c)
    counts = {f"#e{n}": sum(f"#e{n}" in r.tags for r in res) for n in range(1, 6)}
    assert all(18 <= x <= 22 for x in counts.values()), counts


def test_max_tags():
    c = cfg(); c.max_tags = 2
    t = suggest_one("k", {"energy": 0.9, "comp_acid": 0.95, "comp_piano": 0.95}, [], c,
                    {"energy": [0.2, 0.4, 0.6, 0.8]}, Override(add=["#keep"]))
    assert t.tags[:2] == ["#keep", "#e5"] and len(t.tags) == 2


# ---------- XML
SRC = """<?xml version="1.0" encoding="UTF-8"?>
<DJ_PLAYLISTS Version="1.0.0">
  <PRODUCT Name="rekordbox" Version="7.0.0" Company="AlphaTheta"/>
  <COLLECTION Entries="2">
    <TRACK TrackID="11" Name="Ünïcode" Location="file://localhost/C:/Music/%C3%9Cn%C3%AF%20a.mp3" Comments="8A - Energy 6">
      <TEMPO Inizio="0.025" Bpm="128.00" Metro="4/4" Battito="1"/>
      <POSITION_MARK Name="" Type="0" Start="32.1" Num="0"/>
    </TRACK>
    <TRACK TrackID="12" Name="B" Location="file://localhost/C:/Music/b.mp3" Comments=""/>
  </COLLECTION>
  <PLAYLISTS><NODE Type="0" Name="ROOT" Count="0"/></PLAYLISTS>
</DJ_PLAYLISTS>"""


def test_comment_update_xml_keeps_children(tmp_path):
    src = tmp_path / "rb.xml"; src.write_text(SRC, encoding="utf-8")
    out = tmp_path / "out.xml"
    rep = write_comment_update_xml(src, {"C:\\Music\\Ünï a.mp3": ["#e4"], "C:/Music/b.mp3": [],
                                         "C:/nope.mp3": ["#x"]}, out)
    assert rep.changed == 1 and rep.unchanged == 1 and rep.not_found == ["C:/nope.mp3"]
    root = ET.parse(out).getroot()
    t = root.find("COLLECTION/TRACK")
    assert t.get("Comments") == "8A - Energy 6 {st #e4}"
    assert t.get("Location") == "file://localhost/C:/Music/%C3%9Cn%C3%AF%20a.mp3"
    assert t.find("TEMPO") is not None and t.find("POSITION_MARK") is not None
    pl = root.find("PLAYLISTS/NODE/NODE")
    assert pl.get("Entries") == "1" and pl.find("TRACK").get("Key") == "11"


def test_apply_to_export_restores_children(tmp_path):
    src = tmp_path / "rb.xml"; src.write_text(SRC, encoding="utf-8")
    exp = tmp_path / "exp.xml"
    exp.write_text("""<?xml version="1.0" encoding="UTF-8"?><DJ_PLAYLISTS Version="1.0.0"><COLLECTION Entries="1">
      <TRACK TrackID="11" Location="file://localhost/C:/Music/%C3%9Cn%C3%AF%20a.mp3"/></COLLECTION>
      <PLAYLISTS><NODE Type="0" Name="ROOT" Count="1"><NODE Type="1" Name="P" KeyType="0" Entries="1"><TRACK Key="11"/></NODE></NODE></PLAYLISTS></DJ_PLAYLISTS>""", encoding="utf-8")
    out = tmp_path / "o.xml"
    rep = apply_to_export(exp, src, {"file://localhost/C:/Music/%C3%9Cn%C3%AF%20a.mp3": ["#acid"]}, out)
    t = ET.parse(out).getroot().find("COLLECTION/TRACK")
    assert rep.changed == 1 and t.find("POSITION_MARK") is not None
    assert t.get("Comments") == "8A - Energy 6 {st #acid}" and t.get("Name") == "Ünïcode"


# ---------- master.db (stub)
class FakeDb:
    def __init__(self, rows, store):
        self.rows, self.store = rows, store
        self.committed = False
    def get_content(self, ID=None):
        if ID is None:
            return list(self.rows.values())
        return self.rows.get(ID)
    def commit(self): self.store["committed"] = True
    def rollback(self): self.store["rolled_back"] = True
    def close(self): pass


def test_masterdb_dry_run_and_write(tmp_path, monkeypatch):
    rows = {"1": SimpleNamespace(ID="1", FolderPath="C:/Music/b.mp3", Commnt="keep me"),
            "2": SimpleNamespace(ID="2", FolderPath="C:/Music/c.mp3", Commnt=None)}
    store = {}
    opener = lambda p, k: FakeDb(rows, store)
    rep = masterdb.write_comments({"c:\\music\\B.mp3": ["#e3"]}, tmp_path, opener=opener)
    assert len(rep.changes) == 1 and rows["1"].Commnt == "keep me"     # dry run
    dbdir = tmp_path / "rb"; dbdir.mkdir(); (dbdir / "master.db").write_bytes(b"x")
    monkeypatch.setattr(masterdb, "rekordbox_running", lambda: False)
    rep = masterdb.write_comments({"C:/Music/b.mp3": ["#e3"]}, tmp_path / "bk", dbdir / "master.db",
                                  opener=opener, dry_run=False)
    assert rep.written and rows["1"].Commnt == "keep me {st #e3}" and store["committed"]
    assert (rep.backup_dir / "master.db").exists()


# ---------- ledger round trip
def test_ledger_read_back(tmp_path):
    lg = Ledger(tmp_path / "l.json")
    lg.record({"C:/Music/b.mp3": ["#e4", "#acid"]})
    found = lg.read_back({"file://localhost/C:/Music/b.mp3": "x {st #e4 #hypnotic}"})
    ov = found["c:/music/b.mp3"]
    assert ov.add == ["#hypnotic"] and ov.remove == ["#acid"]
    lg.save()
    lg2 = Ledger(tmp_path / "l.json")
    t = suggest_one("c:/music/b.mp3", {"energy": 0.9, "comp_acid": 0.95}, [], cfg(),
                    {"energy": [0.2, 0.4, 0.6, 0.8]}, lg2.overrides["c:/music/b.mp3"])
    assert "#hypnotic" in t.tags and "#acid" not in t.tags
