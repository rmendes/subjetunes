# subjetunes/desktop/state.py
"""Everything the UI shows: library (master.db or XML), the current batch, scores,
buckets, tags, clues, staged edits and metadata from other sources."""
from __future__ import annotations

import json
import re
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from ..tagging import masterdb as mdb
from ..tagging.clues import ClueModel, examples_from_library, slug, track_clues
from ..tagging.comments import merge_comment, normalise_path, normalise_tag, user_text
from ..tagging.ledger import Ledger
from ..tagging.suggest import TagConfig, suggest_batch, validate
from . import paths, rbdb
from .buckets import Bucket, assign, load_buckets, rule_error
from .engine import Jobs, analysis_errors, engine_installed, pick_source, save_json
from .library import Library, read_library
from .sources import REGISTRY, TrackQuery, aggregate, lookup_all
from .sources.base import Cache


def _load(p: Path, default: Any) -> Any:
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else default
    except Exception:
        return default


class AppState:
    def __init__(self, opener=None) -> None:
        self.lock = threading.RLock()
        self.opener = opener or mdb.open_db
        self.lib: Library | None = None
        self.db_path: Path | None = None
        self.db_key = ""
        self.batch: dict[str, list[str]] = {"playlists": [], "tracks": []}
        self.demo = False
        self.jobs = Jobs()
        d = paths.data_dir()
        self.ledger = Ledger(d / "tag_ledger.json")
        self.bucket_ov_path = d / "bucket_overrides.json"
        self.bucket_ov: dict[str, dict[str, list[str]]] = _load(self.bucket_ov_path, {})
        self.pending_path = d / "pending.json"
        self.pending: dict[str, dict[str, Any]] = _load(self.pending_path, {})   # track key -> {field: value}
        self.session_path = d / "session.json"
        self.cache = Cache(d / "sources.sqlite")
        self.lookups: dict[str, set[str]] = {}          # track key -> sources still running
        self._rows: list[dict] | None = None
        self._clue_model: ClueModel | None = None
        self.error = ""
        self.loading = ""                                # non-empty while the library loads
        self._rb_running = (0.0, False)
        self.load_config()
        self._restore_session()

    # ---------- config / session
    def load_config(self) -> None:
        import yaml
        cfg_dir = paths.config_dir()
        self.tag_cfg = TagConfig.load(cfg_dir / "tags.yaml", paths.user_config())
        self.buckets: list[Bucket] = load_buckets(cfg_dir / "default.yaml", paths.user_config())
        uc = paths.user_config()
        user = (yaml.safe_load(uc.read_text(encoding="utf-8")) or {}) if uc.exists() else {}
        self.source_settings: dict[str, dict] = user.get("sources", {}) or {}
        self._rows = None

    def _restore_session(self) -> None:
        """Reopen the last library in the background, so the window is usable immediately."""
        s = _load(self.session_path, {})
        self.demo = bool(s.get("demo", False))
        self.batch = s.get("batch") or {"playlists": s.get("selected", []), "tracks": []}
        if s.get("kind") == "xml" and s.get("xml") and Path(s["xml"]).exists():
            self.load_async(lambda: self._set_lib(read_library(Path(s["xml"])), keep_batch=True))
        elif s.get("kind", "masterdb") == "masterdb":
            self.load_async(lambda: self.open_masterdb(Path(s["db"]) if s.get("db") else None, keep_batch=True))

    def load_async(self, fn) -> None:
        if self.loading:
            return
        self.loading = "Opening your rekordbox library"
        self.error = ""

        def run():
            import logging
            import time as _t
            t0 = _t.time()
            try:
                fn()
                logging.info("library loaded in %.1fs (%d tracks)", _t.time() - t0, len(self.lib.tracks) if self.lib else 0)
            except Exception as e:
                logging.exception("library load failed")
                self.error = f"Couldn't open the rekordbox library: {e}"
            finally:
                self.loading = ""
        threading.Thread(target=run, daemon=True, name="library-load").start()

    def _set_lib(self, lib: Library, keep_batch: bool) -> None:
        with self.lock:
            self.lib = lib
            if not keep_batch:
                self.batch = {"playlists": [], "tracks": []}
            self._rows = None
            self._clue_model = None
            self._save_session()

    def _save_session(self) -> None:
        save_json(self.session_path, {
            "kind": self.lib.kind if self.lib else "masterdb",
            "xml": str(self.lib.source) if self.lib and self.lib.kind == "xml" else "",
            "db": str(self.db_path or ""), "batch": self.batch, "demo": self.demo})

    # ---------- library
    @contextmanager
    def db(self):
        """Short-lived read connection. Never kept open, so writes never wait on us."""
        d = self.opener(self.db_path, self.db_key)
        try:
            yield d
        finally:
            d.close()

    def open_masterdb(self, path: Path | None = None, keep_batch: bool = False) -> None:
        self.db_path = path

        def say(msg):
            if self.loading:
                self.loading = msg
        with self.db() as d:                      # read outside the lock: the UI keeps answering
            lib = rbdb.read_masterdb(d, path, progress=say)
            self.db_path = path or Path(getattr(d, "db_directory", ".")) / "master.db"
        self.error = ""
        self._set_lib(lib, keep_batch)

    def reload(self) -> None:
        if self.lib and self.lib.kind == "masterdb":
            self.open_masterdb(self.db_path, keep_batch=True)
        elif self.lib:
            self.lib = read_library(self.lib.source)
            self._rows = None

    def open_xml(self, xml: Path) -> None:
        self._set_lib(read_library(xml), keep_batch=False)

    def set_batch(self, playlists: list[str], tracks: list[str]) -> None:
        with self.lock:
            self.batch = {"playlists": list(dict.fromkeys(playlists)),
                          "tracks": list(dict.fromkeys(normalise_path(t) for t in tracks))}
            self._rows = None
            self._save_session()

    def set_demo(self, on: bool) -> None:
        with self.lock:
            self.demo = on
            self._rows = None
            self._save_session()

    def keys(self) -> list[str]:
        out: dict[str, list[str]] = {}
        if self.lib:
            for p in self.batch["playlists"]:
                pl = self.lib.playlist(p)
                for k in (pl.track_keys if pl else []):
                    out.setdefault(k, []).append(p)
            for k in self.batch["tracks"]:
                if k in self.lib.tracks:
                    out.setdefault(k, [])
        self._membership = out
        return list(out)

    def batch_name(self) -> str:
        b = self.batch
        if b["playlists"] and not b["tracks"]:
            return ", ".join(p.split("/")[-1] for p in b["playlists"][:3]) + ("…" if len(b["playlists"]) > 3 else "")
        if len(b["tracks"]) == 1 and not b["playlists"] and self.lib and b["tracks"][0] in self.lib.tracks:
            return self.lib.tracks[b["tracks"][0]].title
        n = len(self.keys())
        return f"{n} tracks" if n else ""

    def invalidate(self) -> None:
        with self.lock:
            self._rows = None
            self._clue_model = None

    def rekordbox_running(self) -> bool:
        """Cached for a few seconds: scanning processes on every status call adds up."""
        import time as _t
        at, val = self._rb_running
        if _t.time() - at > 4:
            val = mdb.rekordbox_running()
            self._rb_running = (_t.time(), val)
        return val

    # ---------- clues
    def vocab_tags(self) -> list[str]:
        return [t for g in self.vocab() for t in g["tags"]]

    def cached_source_tags(self, key: str) -> list[str]:
        out = []
        for sid, cls in REGISTRY.items():
            if not cls.online:
                continue
            r = self.cache.get(sid, key)
            if r and r.state == "found":
                out += [n for n, w in r.tags if w >= 0.3]
        return out

    def clues_for(self, key: str) -> set[str]:
        t = self.lib.tracks[key]
        return track_clues(t.genre, t.comments, t.mytags, self.cached_source_tags(key))

    # ---------- rows
    def values_for(self, key: str, scores: dict) -> dict:
        t = self.lib.tracks[key]
        v = dict(scores.get(key, {}))
        m = t.meta
        v.setdefault("bpm", t.bpm)
        cam = rbdb.camelot(t.tonality) or t.tonality
        v.update({"camelot": cam, "minor": 1 if cam.endswith("A") else 0 if cam.endswith("B") else None,
                  "rating": t.rating, "year": t.year, "play_count": m.get("play_count", 0),
                  "genre": (t.genre or "").lower(), "clues": frozenset(self.clues_for(key))})
        return v

    def rows(self) -> list[dict]:
        with self.lock:
            if self._rows is not None:
                return self._rows
            keys = self.keys()
            src = pick_source(self.demo)
            self.source_name = src.name if src else None
            ids = {normalise_path(self.lib.tracks[k].path): str(self.lib.tracks[k].content_id or "") for k in keys}
            scores = src.scores([self.lib.tracks[k].path for k in keys], ids=ids) if (src and keys) else {}
            vocab = self.vocab_tags()
            if self._clue_model is None:      # whole-library pass: only redone after tag edits or writes
                self._clue_model = ClueModel().fit(examples_from_library(
                    self.lib.tracks.values(), self.ledger.written, self.ledger.overrides, vocab))
            model = self._clue_model
            base, extra = [], {}
            for k in keys:
                v = self.values_for(k, scores)
                auto, edge = assign(v, self.buckets, self.tag_cfg.margin)
                ov = self.bucket_ov.get(k, {})
                final = [b for b in auto if b not in ov.get("remove", [])] + \
                        [b for b in ov.get("add", []) if b not in auto]
                extra[k] = model.suggest(set(v["clues"]), vocab)
                base.append({"key": k, "buckets": final, "_v": v, "_edge": edge, "_scored": k in scores})
            tagged = {t.key: t for t in suggest_batch(
                [{**b["_v"], "key": b["key"], "buckets": b["buckets"]} for b in base],
                self.tag_cfg, self.ledger.overrides, extra)}
            self._rows = [self._row(b, tagged.get(b["key"])) for b in base]
            return self._rows

    def _row(self, b: dict, tt) -> dict:
        t = self.lib.tracks[b["key"]]
        ov = self.ledger.overrides.get(b["key"])
        v = b["_v"]
        own = user_text(t.comments, self.tag_cfg.block)
        own_tags = {normalise_tag(h) for h in re.findall(r"#([\w\-]+)", own)}   # already in your own text
        errs = analysis_errors()
        return {
            "key": b["key"], "id": t.content_id or t.track_id, "title": t.title, "artist": t.artist,
            "genre": t.genre, "bpm": t.bpm, "tonality": v.get("camelot") or t.tonality, "duration": t.duration_s,
            "year": t.year, "label": t.label, "rating": t.rating, "comment": user_text(t.comments, self.tag_cfg.block),
            "mytags": t.mytags, "clues": sorted(v["clues"]), "playlists": self._membership.get(b["key"], []),
            "scored": b["_scored"],
            "error": "" if b["_scored"] else errs.get(b["key"], ""),
            "scores": {n: x for n, x in v.items() if isinstance(x, (int, float)) and not isinstance(x, bool)
                       and not n.endswith("_conf") and n not in ("bpm", "rating", "year", "play_count", "minor")},
            "labels": {n: x for n, x in v.items() if isinstance(x, str) and n.startswith("crit_")},
            "buckets": b["buckets"], "bucket_edge": b["_edge"], "bucket_edited": b["key"] in self.bucket_ov,
            "native": sorted(track_clues(t.genre, t.comments, t.mytags)),
            "tags": [x for x in tt.tags if x not in own_tags] if tt else [],
            "uncertain": [x for x in tt.uncertain if x not in own_tags] if tt else [],
            "borderline": tt.borderline if tt else [],
            "reasons": {s.tag: s.reason for s in tt.suggestions} if tt else {},
            "tag_edited": bool(ov and (ov.add or ov.remove)), "written": self.ledger.written.get(b["key"]),
            "pending": self.pending.get(b["key"], {}),
        }

    def row(self, key: str) -> dict | None:
        k = normalise_path(key)
        return next((r for r in self.rows() if r["key"] == k), None)

    # ---------- edits: tags, buckets, fields
    def siblings(self, tag: str) -> list[str]:
        for lv in self.tag_cfg.levels:
            if tag in lv.tags:
                return [t for t in lv.tags if t != tag]
        grp = next((r.group for r in self.tag_cfg.rules if r.tag == tag and r.group), None)
        return [r.tag for r in self.tag_cfg.rules if grp and r.group == grp and r.tag != tag]

    def edit_tags(self, key: str, add: list[str], remove: list[str]) -> None:
        with self.lock:
            add = [normalise_tag(t) for t in add]
            remove = [normalise_tag(t) for t in remove] + [s for t in add for s in self.siblings(t)]
            self.ledger.set_override(key, add, [t for t in remove if t not in add])
            self.ledger.save()
            self._rows = None
            self._clue_model = None

    def reset_tags(self, key: str) -> None:
        with self.lock:
            self.ledger.overrides.pop(normalise_path(key), None)
            self.ledger.save()
            self._rows = None
            self._clue_model = None

    def edit_buckets(self, key: str, add: list[str], remove: list[str]) -> None:
        with self.lock:
            ov = self.bucket_ov.setdefault(key, {"add": [], "remove": []})
            for b in add:
                (ov["remove"].remove(b) if b in ov["remove"] else ov["add"].append(b) if b not in ov["add"] else None)
            for b in remove:
                (ov["add"].remove(b) if b in ov["add"] else ov["remove"].append(b) if b not in ov["remove"] else None)
            if not ov["add"] and not ov["remove"]:
                self.bucket_ov.pop(key)
            save_json(self.bucket_ov_path, self.bucket_ov)
            self._rows = None

    def stage(self, key: str, field: str, value: Any) -> None:
        """Remember a field change for rekordbox. Setting it back to the current value unstages it."""
        if field not in rbdb.FIELDS or rbdb.FIELDS[field][1] == "ro":
            raise ValueError(f"{field} can't be edited")
        if field == "key":
            value = rbdb.scale_name(str(value))
            if not value:
                raise ValueError("Use a key like Am, F#, 8A or 'A minor'.")
        if rbdb.FIELDS[field][1] == "int" and str(value).strip() and not str(value).strip().isdigit():
            raise ValueError(f"{rbdb.FIELDS[field][0]} needs a whole number.")
        with self.lock:
            k = normalise_path(key)
            cur = self.lib.tracks[k].meta.get(field)
            p = self.pending.setdefault(k, {})
            if str(cur if cur is not None else "").strip() == str(value if value is not None else "").strip():
                p.pop(field, None)
            else:
                p[field] = value
            if not p:
                self.pending.pop(k)
            save_json(self.pending_path, self.pending)
            self._rows = None

    def unstage(self, key: str | None = None) -> None:
        with self.lock:
            if key is None:
                self.pending = {}
            else:
                self.pending.pop(normalise_path(key), None)
            save_json(self.pending_path, self.pending)
            self._rows = None

    def write_plan(self, tags: bool, playlists: bool, fields: bool = True, skip: set[str] | None = None) -> dict:
        """What 'Write to rekordbox' would do for this batch: {content_id: {field: value}} + playlists.
        skip: change ids ("<content_id>:<field>" or "playlist:<name>") you chose to leave out."""
        skip = skip or set()
        rows = self.rows()
        edits: dict[str, dict[str, Any]] = {}
        if fields:
            for k, f in self.pending.items():
                t = self.lib.tracks.get(k)
                if t and t.content_id:
                    edits[t.content_id] = dict(f)
        if tags:
            for r in rows:
                t = self.lib.tracks[r["key"]]
                if not t.content_id:
                    continue
                cur = edits.get(t.content_id, {}).get("comments", t.comments)
                new = merge_comment(cur, r["tags"], self.tag_cfg.block)
                if new != t.comments:
                    edits.setdefault(t.content_id, {})["comments"] = new
        pls: dict[str, list[str]] = {}
        if playlists:
            for b in self.buckets:
                ids = [self.lib.tracks[r["key"]].content_id for r in rows if b.id in r["buckets"]]
                if ids and f"playlist:{b.name}" not in skip:
                    pls[b.name] = ids
        edits = {cid: {f: v for f, v in fs.items() if f"{cid}:{f}" not in skip} for cid, fs in edits.items()}
        return {"edits": {cid: fs for cid, fs in edits.items() if fs}, "playlists": pls}

    def write(self, tags: bool, playlists: bool, fields: bool = True, skip: set[str] | None = None) -> dict:
        if not self.lib or self.lib.kind != "masterdb":
            raise mdb.WriteError("Writing needs the rekordbox library (master.db), not an XML file.")
        plan = self.write_plan(tags, playlists, fields, skip)
        if not plan["edits"] and not plan["playlists"]:
            return {"written": False, "tracks": 0, "playlists": {}}
        folder = f"{self.batch_name() or 'Batch'} ({datetime.now():%Y-%m-%d %H.%M})"
        rep = rbdb.write_edits(plan["edits"], paths.data_dir() / "backups", self.db_path, self.db_key,
                               opener=self.opener, playlists=plan["playlists"], folder=folder)
        by_id = {t.content_id: k for k, t in self.lib.tracks.items()}
        if tags:
            self.ledger.record({r["key"]: r["tags"] for r in self.rows()
                                if self.lib.tracks[r["key"]].content_id in rep.applied or r["tags"]})
            self.ledger.save()
        for cid, fields in rep.applied.items():          # drop only what was written; skipped edits stay staged
            k = by_id.get(cid)
            if k in self.pending:
                for f in fields:
                    if f in self.pending[k] and str(self.pending[k][f]) == str(fields[f]):
                        self.pending[k].pop(f)
                if not self.pending[k]:
                    self.pending.pop(k)
        save_json(self.pending_path, self.pending)
        self.reload()
        return {"written": True, "tracks": len(rep.applied), "changes": sum(len(v) for v in rep.applied.values()),
                "playlists": rep.playlists, "folder": f"Subjetunes/{folder}", "backup": str(rep.backup_dir),
                "verified": True, "at": datetime.now().isoformat(timespec="seconds")}

    # ---------- track detail + other sources
    def query(self, key: str) -> TrackQuery:
        t = self.lib.tracks[key]
        return TrackQuery(key=key, path=t.path, title=t.title, artist=t.artist, album=t.album,
                          duration_s=t.duration_s, isrc=t.meta.get("isrc", ""), meta=t.meta or {
                              "title": t.title, "artist": t.artist, "genre": t.genre, "comments": t.comments,
                              "bpm": t.bpm, "key": t.tonality, "album": t.album, "label": t.label, "year": t.year},
                          mytags=t.mytags)

    def start_lookup(self, key: str, force: bool = False) -> None:
        k = normalise_path(key)
        if self.lookups.get(k):
            return
        q = self.query(k)
        if force:
            self.cache.drop(k)
        todo = {sid for sid, cls in REGISTRY.items() if cls.online}
        self.lookups[k] = set(todo)

        def done(r):
            self.lookups.get(k, set()).discard(r.source)

        def run():
            try:
                lookup_all(q, self.source_settings, self.cache, on_result=done, force=force)
            finally:
                self.lookups.pop(k, None)
                self._rows = None
        threading.Thread(target=run, daemon=True).start()

    def detail(self, key: str, lookup: bool = True) -> dict:
        """lookup=False: answer from local sources and the cache only, never start online lookups
        (the Tag screen browses fast; it asks for lookups only when 'Elsewhere' is opened)."""
        k = normalise_path(key)
        t = self.lib.tracks[k]
        q = self.query(k)
        local = [cls(self.source_settings.get(sid, {})) for sid, cls in REGISTRY.items() if not cls.online]
        results = []
        for src in local:
            ok, why = src.enabled()
            try:
                results.append(src.lookup(q) if ok else src.result(state="off", note=why))
            except Exception as e:
                results.append(src.result(state="error", note=str(e)[:200]))
        for sid, cls in REGISTRY.items():
            if not cls.online:
                continue
            src = cls(self.source_settings.get(sid, {}))
            ok, why = src.enabled()
            r = self.cache.get(sid, k) if ok else src.result(state="off", note=why)
            results.append(r or src.result(state="waiting"))
        if lookup and any(r.state == "waiting" for r in results) and k not in self.lookups:
            self.start_lookup(k)
        agg = aggregate(results)
        grid = None
        if self.lib.kind == "masterdb" and t.content_id:
            try:
                with self.db() as d:
                    grid = rbdb.beat_grid(d, t.content_id)
            except Exception:
                grid = None
        if grid:
            grid.pop("times", None)
        from .sources.base import _same

        def alts(f):
            cur = self.pending.get(k, {}).get(f, t.meta.get(f, ""))
            out = []
            for a in agg["fields"].get(f, []):
                val = rbdb.scale_name(a["value"]) or a["value"] if f == "key" else a["value"]
                if cur in (None, "") or not _same(f, str(cur), str(val)):
                    out.append({**a, "value": val})
            return out

        fields = [{"id": f, "label": lab, "kind": kind, "value": t.meta.get(f, ""),
                   "pending": self.pending.get(k, {}).get(f), "alts": alts(f)}
                  for f, (lab, kind) in rbdb.FIELDS.items()]
        return {"row": self.row(k), "fields": fields, "cloud": agg["cloud"],
                "sources": [r.to_dict() for r in results], "loading": sorted(self.lookups.get(k, [])),
                "grid": grid, "artwork": bool(t.meta.get("image")), "editable": self.lib.kind == "masterdb",
                "path": t.path, "meta": {x: t.meta.get(x) for x in ("color", "play_count", "length", "file_type",
                                                                    "bitrate", "sample_rate", "added", "analysed")}}

    def artwork(self, key: str) -> Path | None:
        t = self.lib.tracks.get(normalise_path(key))
        if not t or not t.meta.get("image") or self.lib.kind != "masterdb":
            return None
        with self.db() as d:
            return rbdb.artwork_path(d, t.meta["image"])

    # ---------- analysis: hand the engine rekordbox's own BPM, key and beat grid
    def batch_xml(self) -> Path:
        """A throwaway XML of the batch for `subjetunes.cli analyze`, carrying rekordbox's BPM/key
        (AverageBpm/Tonality) so the engine doesn't measure them again, plus a JSON sidecar
        with the beat grids (SUBJETUNES_BATCH env var)."""
        import xml.etree.ElementTree as ET
        keys = self.keys()
        out = paths.data_dir() / "batch"
        out.mkdir(exist_ok=True)
        root = ET.Element("DJ_PLAYLISTS", {"Version": "1.0.0"})
        ET.SubElement(root, "PRODUCT", {"Name": "rekordbox", "Version": "6.0.0", "Company": "AlphaTheta"})
        coll = ET.SubElement(root, "COLLECTION", {"Entries": str(len(keys))})
        side = []
        grids: dict[str, Any] = {}
        if self.lib.kind == "masterdb":
            with self.db() as d:
                grids = {k: rbdb.beat_grid(d, self.lib.tracks[k].content_id) for k in keys}
        tids = []
        for i, k in enumerate(keys, 1):
            t = self.lib.tracks[k]
            tid = str(t.content_id) if str(t.content_id or "").isdigit() else str(i)   # stable: rekordbox's own id
            tids.append(tid)
            loc = t.location or "file://localhost/" + quote(t.path.replace("\\", "/").lstrip("/"), safe="/:")
            ET.SubElement(coll, "TRACK", {"TrackID": tid, "Name": t.title, "Artist": t.artist, "Genre": t.genre,
                                          "AverageBpm": f"{t.bpm or 0:.2f}", "Tonality": t.tonality,
                                          "TotalTime": str(int(t.duration_s or 0)), "Location": loc})
            side.append({"path": t.path, "bpm": t.bpm, "key": t.tonality, "length": t.duration_s,
                         "beat_grid": grids.get(k)})
        pls = ET.SubElement(root, "PLAYLISTS")
        rn = ET.SubElement(pls, "NODE", {"Type": "0", "Name": "ROOT", "Count": "1"})
        pn = ET.SubElement(rn, "NODE", {"Type": "1", "Name": "Subjetunes batch", "KeyType": "0", "Entries": str(len(keys))})
        for tid in tids:
            ET.SubElement(pn, "TRACK", {"Key": tid})
        (out / "batch.xml").write_text(ET.tostring(root, encoding="unicode"), encoding="utf-8")
        save_json(out / "batch.json", {"tracks": side, "source": "rekordbox"})
        return out / "batch.xml"

    # ---------- summary
    def status(self) -> dict[str, Any]:
        if self.loading:
            return {"loading": self.loading, "library": None, "error": "", "batch": self.batch, "batch_name": "",
                    "demo": self.demo, "pending": 0, "counts": {"tracks": 0, "scored": 0, "review": 0, "tagged": 0},
                    "buckets": [], "vocab": self.vocab(), "problems": [], "sources": [], "home": str(paths.home()),
                    "rekordbox_running": False, "engine": False, "source": None}
        rows = self.rows() if self.lib else []
        stats = self.cache.stats()
        return {
            "library": {"kind": self.lib.kind, "path": str(self.lib.source), "tracks": len(self.lib.tracks),
                        "playlists": self.lib.root.to_dict()["children"]} if self.lib else None,
            "error": self.error, "batch": self.batch, "batch_name": self.batch_name(), "demo": self.demo,
            "source": getattr(self, "source_name", None), "engine": engine_installed(),
            "rekordbox_running": self.rekordbox_running(), "loading": "",
            "pending": sum(len(v) for v in self.pending.values()),
            "counts": {"tracks": len(rows), "scored": sum(r["scored"] for r in rows),
                       "review": sum(bool(r["uncertain"] or r["bucket_edge"]) for r in rows),
                       "tagged": sum(bool(r["tags"]) for r in rows)},
            "buckets": [dict(b.to_dict(), error=rule_error(b.rule),
                             count=sum(b.id in r["buckets"] for r in rows)) for b in self.buckets],
            "vocab": self.vocab(), "problems": validate(self.tag_cfg, [b.id for b in self.buckets]),
            "sources": [{"id": sid, "name": cls.name, "online": cls.online, "needs": list(cls.needs),
                         "default_on": cls.default_on,
                         "settings": {k: ("•••" if k in ("api_key", "token", "client_secret") and v else v)
                                      for k, v in self.source_settings.get(sid, {}).items()},
                         "ready": cls(self.source_settings.get(sid, {})).enabled()[0],
                         "why": cls(self.source_settings.get(sid, {})).enabled()[1],
                         "stats": stats.get(sid, {})}
                        for sid, cls in REGISTRY.items()],
            "home": str(paths.home()),
        }

    def search(self, q: str, limit: int = 40) -> list[dict]:
        if not self.lib:
            return []
        words = [w for w in q.lower().split() if w]
        out = []
        for k, t in self.lib.tracks.items():
            hay = f"{t.title} {t.artist} {t.album} {t.genre} {t.label} {' '.join(t.mytags)}".lower()
            if all(w in hay for w in words):
                out.append({"key": k, "title": t.title, "artist": t.artist, "bpm": t.bpm, "tonality": t.tonality,
                            "genre": t.genre})
                if len(out) >= limit:
                    break
        return out

    def vocab(self) -> list[dict]:
        """The defined list, by axis: level tags first, then each group's rule tags and hand-only tags."""
        cfg = self.tag_cfg
        out: list[dict] = [{"group": lv.source, "kind": "level", "tags": lv.tags, "max": 1, "tip": ""} for lv in cfg.levels]
        groups: dict[str, list[str]] = {g: [] for g in cfg.groups}
        manual: set[str] = set()
        for r in cfg.rules:
            groups.setdefault(r.group or "other", []).append(r.tag)
        for g, ts in cfg.manual.items():
            for t in ts:
                if t not in groups.setdefault(g, []):
                    groups[g].append(t)
                    manual.add(t)
        for g, ts in groups.items():
            gd = cfg.groups.get(g)
            out.append({"group": g, "kind": "rule", "tags": ts, "manual": [t for t in ts if t in manual],
                        "max": gd.max if gd else cfg.group_max(g), "tip": gd.tip if gd else ""})
        return out

    # ---------- editing the vocabulary from the UI (written to data/user.yaml)
    def _tag_settings(self) -> dict:
        import yaml
        merged: dict = {}
        for p in (paths.config_dir() / "tags.yaml", paths.user_config()):
            if p.exists():
                data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
                merged.update(data.get("comment_tags", {}) or {})
        merged.setdefault("rules", [])
        merged.setdefault("manual", {})
        return merged

    def _save_tag_settings(self, d: dict) -> None:
        import yaml
        p = paths.user_config()
        data = (yaml.safe_load(p.read_text(encoding="utf-8")) or {}) if p.exists() else {}
        ct = data.setdefault("comment_tags", {})
        ct["rules"] = d["rules"]
        ct["manual"] = {g: ts for g, ts in d["manual"].items() if ts}
        if d.get("conflicts") is not None:
            ct["conflicts"] = d["conflicts"]
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")
        self.load_config()
        self._clue_model = None

    def vocab_add(self, group: str, tag: str) -> str:
        t = normalise_tag(tag)
        if t in self.vocab_tags():
            return t
        d = self._tag_settings()
        d["manual"].setdefault(group, []).append(t)
        self._check_and_save(d)
        return t

    def vocab_rename(self, old: str, new: str) -> str:
        old, new = normalise_tag(old), normalise_tag(new)
        if old == new:
            return new
        if new in self.vocab_tags():
            raise ValueError(f"{new} already exists")
        d = self._tag_settings()
        found = False
        for r in d["rules"]:
            if normalise_tag(r["tag"]) == old:
                r["tag"] = new
                found = True
        for g, ts in d["manual"].items():
            if old in [normalise_tag(t) for t in ts]:
                d["manual"][g] = [new if normalise_tag(t) == old else t for t in ts]
                found = True
        d["conflicts"] = [[new if normalise_tag(x) == old else x for x in pair] for pair in d.get("conflicts", [])]
        if not found:
            raise ValueError(f"{old} isn't in the vocabulary")
        self._check_and_save(d)
        # carry your edits and what was already written over to the new name
        for ov in self.ledger.overrides.values():
            ov.add = [new if t == old else t for t in ov.add]
            ov.remove = [new if t == old else t for t in ov.remove]
        for k, ts in self.ledger.written.items():
            self.ledger.written[k] = [new if t == old else t for t in ts]
        self.ledger.save()
        self._rows = None
        return new

    def vocab_remove(self, tag: str) -> None:
        t = normalise_tag(tag)
        d = self._tag_settings()
        d["rules"] = [r for r in d["rules"] if normalise_tag(r["tag"]) != t]
        d["manual"] = {g: [x for x in ts if normalise_tag(x) != t] for g, ts in d["manual"].items()}
        d["conflicts"] = [p for p in d.get("conflicts", []) if t not in [normalise_tag(x) for x in p]]
        self._check_and_save(d)
        for ov in self.ledger.overrides.values():
            ov.add = [x for x in ov.add if x != t]
        self.ledger.save()
        self._rows = None

    def _check_and_save(self, d: dict) -> None:
        cfg = TagConfig.from_dict(d)
        problems = [p for p in validate(cfg, [b.id for b in self.buckets]) if "prefix" in p or "valid tag" in p or "more than once" in p]
        if problems:
            raise ValueError(problems[0])
        self._save_tag_settings(d)
