# tests/test_platforms.py
"""Library lookup and data folders on macOS and Windows, simulated on any OS."""
import json
import os
import sys
from pathlib import Path

import pytest

from subjetunes.desktop import paths, rblocate


def _options(app: Path, db: Path) -> None:
    f = app / "rekordboxAgent" / "storage" / "options.json"
    f.parent.mkdir(parents=True)
    f.write_text(json.dumps({"options": [["db-path", str(db)], ["dp", "x"]]}), encoding="utf-8")


def _settings(app: Path, db_dir: Path) -> None:
    f = app / "rekordbox6" / "rekordbox3.settings"
    f.parent.mkdir(parents=True)
    f.write_text(f'<?xml version="1.0"?><PROPERTIES><VALUE name="masterDbDirectory" val="{db_dir}"/></PROPERTIES>',
                 encoding="utf-8")


@pytest.fixture
def mac(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    return tmp_path


@pytest.fixture
def win(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "PF"))
    return tmp_path


def test_mac_default_location(mac):
    db = mac / "Library" / "Pioneer" / "rekordbox" / "master.db"
    db.parent.mkdir(parents=True)
    db.write_bytes(b"x")
    assert rblocate.find_master_db() == db


def test_mac_library_moved_follows_options_json(mac):
    moved = mac / "Volumes" / "DJ" / "rekordbox" / "master.db"
    moved.parent.mkdir(parents=True)
    moved.write_bytes(b"x")
    app = mac / "Library" / "Application Support" / "Pioneer"
    _options(app, moved)
    _settings(app, mac / "somewhere-stale")          # the two disagree: pyrekordbox would give up here
    assert rblocate.find_master_db() == moved
    hows = [c.how for c in rblocate.candidates()]
    assert hows[0] == "rekordboxAgent options.json" and "default location" in hows


def test_mac_installs_in_both_application_folders(mac, monkeypatch):
    import plistlib
    user_apps = mac / "Applications" / "rekordbox 7" / "rekordbox.app" / "Contents"
    user_apps.mkdir(parents=True)
    with (user_apps / "Info.plist").open("wb") as f:
        plistlib.dump({"CFBundleShortVersionString": "7.2.4"}, f)
    found = rblocate.installs()
    assert [i.version for i in found] == ["7.2.4"]


def test_windows_settings_and_installs(win):
    app = win / "Roaming" / "Pioneer"
    lib = win / "D" / "rb"
    lib.mkdir(parents=True)
    (lib / "master.db").write_bytes(b"x")
    _settings(app, lib)
    assert rblocate.find_master_db() == lib / "master.db"
    (win / "PF" / "rekordbox" / "rekordbox 7.2.4").mkdir(parents=True)
    (win / "PF" / "Pioneer" / "rekordbox 6.8.5").mkdir(parents=True)
    assert [i.version for i in rblocate.installs()] == ["7.2.4", "6.8.5"]


def test_nothing_found_lists_where_we_looked(mac):
    from subjetunes.tagging.masterdb import WriteError, open_db
    with pytest.raises(WriteError) as e:
        open_db(None)
    assert "Library/Pioneer/rekordbox/master.db" in str(e.value).replace("\\\\", "/")


def test_mac_bundle_keeps_data_out_of_the_app(tmp_path, monkeypatch):
    res = tmp_path / "Subjetunes.app" / "Contents" / "Resources"
    (res / "config").mkdir(parents=True)
    (res / "config" / "default.yaml").write_text("a: 1", encoding="utf-8")
    (res / "config" / "user.yaml").write_text("mine: 1", encoding="utf-8")
    monkeypatch.setattr(paths, "shipped_root", lambda: res)
    monkeypatch.delenv("SUBJETUNES_HOME", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "me"))
    assert paths.in_mac_bundle()
    assert paths.home() == tmp_path / "me" / "Library" / "Application Support" / "Subjetunes"
    paths.sync_shipped_config()
    cfg = paths.home() / "config"
    assert (cfg / "default.yaml").read_text() == "a: 1" and not (cfg / "user.yaml").exists()
    (cfg / "user.yaml").write_text("yours", encoding="utf-8")
    paths.sync_shipped_config()
    assert (cfg / "user.yaml").read_text() == "yours"


def test_huggingface_offline_once_model_is_on_disk(tmp_path, monkeypatch):
    for k in ("HF_HOME", "HF_HUB_CACHE", "HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "SUBJETUNES_HF_ONLINE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("SUBJETUNES_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr(paths, "_clap_model_id", lambda: "laion/larger_clap_music")
    # nothing downloaded yet: new downloads go to data/models, and we stay online to fetch it
    assert "not downloaded" in paths.setup_huggingface()
    assert os.environ["HF_HOME"] == str(tmp_path / "home" / "data" / "models")
    assert "HF_HUB_OFFLINE" not in os.environ
    # the model arrives: next start is offline (no update checks, no warning)
    snap = tmp_path / "home" / "data" / "models" / "hub" / "models--laion--larger_clap_music" / "snapshots" / "abc"
    snap.mkdir(parents=True)
    (snap / "config.json").write_text("{}")
    monkeypatch.delenv("HF_HOME")
    assert "offline" in paths.setup_huggingface() and os.environ["HF_HUB_OFFLINE"] == "1"


def test_huggingface_reuses_an_existing_download(tmp_path, monkeypatch):
    for k in ("HF_HOME", "HF_HUB_CACHE", "HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("SUBJETUNES_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr(paths, "_clap_model_id", lambda: "laion/larger_clap_music")
    snap = tmp_path / "cache" / "huggingface" / "hub" / "models--laion--larger_clap_music" / "snapshots" / "x"
    snap.mkdir(parents=True)
    (snap / "model.safetensors").write_text("x")
    summary = paths.setup_huggingface()
    assert "existing Hugging Face cache" in summary and "HF_HOME" not in os.environ   # not downloaded again
    assert os.environ["HF_HUB_OFFLINE"] == "1"
