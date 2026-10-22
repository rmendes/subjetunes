# tests/test_engine_bridge.py
"""The app reads the engine's own cache: analysed tracks get scores, failed ones show the error.
Runs only where the engine package (subjetunes.scoring/cache/config) is installed."""
import pytest

scoring = pytest.importorskip("subjetunes.scoring")


def test_analysed_tracks_get_scores(tmp_path, monkeypatch):
    from subjetunes import config
    from subjetunes.cache import Cache
    from subjetunes.models import Features

    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "engine")
    (tmp_path / "engine").mkdir()
    from rbfixture import make_master_db, opener_for
    from subjetunes.desktop.state import AppState
    import time

    db = make_master_db(tmp_path)
    s = AppState(opener=opener_for(db))
    for _ in range(100):
        if not s.loading:
            break
        time.sleep(0.05)
    s.open_masterdb(db)
    s.set_demo(False)
    s.set_batch(["Gigs/Friday"], [])
    keys = s.keys()
    c = Cache(config.DATA_DIR / "cache.sqlite")
    for i, k in enumerate(keys[:-1]):
        path = s.lib.tracks[k].path.replace("\\", "/")            # the engine stores the XML's decoded path
        c.put_features(Features(path, 1, 1.0, "now", "1", bpm=120 + i, rms_db=-9 - i, onset_rate=3 + i,
                                spectral_flux_mean=1, band_onset_density=[3, 2, 1, 0], spectral_centroid_mean=1500 + i,
                                swing_ratio=0.55, microtiming_std=0.01))
    c.put_features(Features(s.lib.tracks[keys[-1]].path, 1, 1.0, "now", "1", error="RuntimeError: could not decode"))
    c.close()
    s.invalidate()
    rows = {r["key"]: r for r in s.rows()}
    assert s.source_name == "Subjetunes analysis"
    assert sum(r["scored"] for r in rows.values()) == len(keys) - 1
    assert rows[keys[-1]]["error"].startswith("RuntimeError")
    assert any(t.startswith("#e") for r in rows.values() if r["scored"] for t in r["tags"])
