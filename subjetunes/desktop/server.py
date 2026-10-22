# subjetunes/desktop/server.py
"""Local HTTP API + static UI. Binds to 127.0.0.1 only and requires a per-run token,
so other web pages open in a browser can't drive it."""
from __future__ import annotations

import json
import logging
import mimetypes
import threading
import time
import os
import re
import secrets
import subprocess
import sys
import traceback
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

import yaml

from ..tagging.comments import normalise_path
from . import paths
from .buckets import rule_error
from .engine import analyze_commands
from .export import default_folder_name, write_bucket_xml
from .state import AppState

WEB = Path(__file__).with_name("web")
AUDIO_TYPES = {".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".aac": "audio/aac", ".flac": "audio/flac",
               ".wav": "audio/wav", ".aif": "audio/aiff", ".aiff": "audio/aiff", ".ogg": "audio/ogg"}


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _update_user_yaml(mutate: Callable[[dict], None]) -> None:
    p = paths.user_config()
    data = yaml.safe_load(p.read_text(encoding="utf-8")) if p.exists() else {}
    data = data or {}
    mutate(data)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")


class Api:
    def __init__(self, state: AppState):
        self.s = state

    def _lib(self):
        if self.s.loading:
            raise ApiError("The library is still loading. Try again in a moment.", 409)
        if not self.s.lib:
            raise ApiError("Open your rekordbox library first.")
        return self.s.lib

    # ---------------- GET
    def get_status(self, q):
        return self.s.status()

    def get_library_info(self, q):
        """Where we looked for master.db and which rekordbox versions are installed (Settings, support)."""
        from .rblocate import report
        r = report()
        r["open"] = str(self.s.db_path or "") if self.s.lib and self.s.lib.kind == "masterdb" else ""
        r["home"] = str(paths.home())
        return r

    def get_dev(self, q):
        """Dev mode only (scripts/dev.py): a stamp that changes whenever a file in web/ changes."""
        if os.environ.get("SUBJETUNES_DEV") != "1":
            return {"dev": False}
        return {"dev": True, "stamp": max((f.stat().st_mtime for f in WEB.iterdir() if f.is_file()), default=0)}

    def get_tracks(self, q):
        return {"rows": self.s.rows() if self.s.lib else []}

    def get_job(self, q):
        j = self.s.jobs.jobs.get(q.get("id", [""])[0])
        if not j:
            raise ApiError("No such job.", 404)
        return j.to_dict()

    def get_search(self, q):
        self._lib()
        return {"results": self.s.search(q.get("q", [""])[0])}

    def get_track(self, q):
        lib = self._lib()
        k = normalise_path(q.get("key", [""])[0])
        if k not in lib.tracks:
            raise ApiError("That track isn't in the library.", 404)
        return self.s.detail(k, lookup=q.get("lookup", ["1"])[0] != "0")

    # ---------------- library and batch
    def post_open_db(self, b):
        p = str(b.get("path", "")).strip().strip('"')
        self.s.load_async(lambda: self.s.open_masterdb(Path(p) if p else None))
        return self.s.status()

    def post_open(self, b):
        p = Path(str(b.get("path", "")).strip().strip('"'))
        if not p.is_file():
            raise ApiError(f"File not found: {p}.")
        try:
            if p.name.lower().endswith(".db"):
                self.s.load_async(lambda: self.s.open_masterdb(p))
            else:
                self.s.open_xml(p)
        except Exception as e:
            raise ApiError(f"Couldn't read {p.name}: {e}")
        return self.s.status()

    def post_reload(self, b):
        self.s.load_async(self.s.reload)
        return self.s.status()

    def post_batch(self, b):
        self._lib()
        self.s.set_batch([str(x) for x in b.get("playlists", [])], [str(x) for x in b.get("tracks", [])])
        return self.s.status()

    def post_demo(self, b):
        self.s.set_demo(bool(b.get("on")))
        return self.s.status()

    # ---------------- edits
    def _row(self, key):
        return {"row": self.s.row(key), "status": self.s.status()}

    def post_tags(self, b):
        if b.get("reset"):
            self.s.reset_tags(b["key"])
        else:
            self.s.edit_tags(b["key"], b.get("add", []), b.get("remove", []))
        return self._row(b["key"])

    def post_buckets_edit(self, b):
        self.s.edit_buckets(normalise_path(b["key"]), b.get("add", []), b.get("remove", []))
        return self._row(b["key"])

    def post_stage(self, b):
        try:
            for f, v in (b.get("fields") or {}).items():
                self.s.stage(b["key"], f, v)
        except ValueError as e:
            raise ApiError(str(e))
        return self.s.detail(normalise_path(b["key"]))

    def post_unstage(self, b):
        self.s.unstage(b.get("key"))
        return self.s.status()

    def post_lookup(self, b):
        self.s.start_lookup(b["key"], force=bool(b.get("force")))
        return {"ok": True}

    def post_buckets(self, b):
        rows = b.get("buckets", [])
        for r in rows:
            if not re.match(r"^[a-z0-9_]+$", str(r.get("id", ""))):
                raise ApiError(f"Bucket id {r.get('id')!r}: use lowercase letters, digits and _.")
            err = rule_error(str(r.get("rule", "")))
            if err:
                raise ApiError(f"{r.get('name') or r['id']}: {err}")
        clean = [{"id": r["id"], "name": r.get("name") or r["id"], "description": r.get("description", ""),
                  "rule": r.get("rule", ""), "use_llm": bool(r.get("use_llm", False))} for r in rows]
        _update_user_yaml(lambda d: d.__setitem__("buckets", clean))
        self.s.load_config()
        return self.s.status()

    def post_vocab(self, b):
        """{op: add|rename|remove, group?, tag, new?}: edit the tag vocabulary from any tagging screen."""
        try:
            op = b.get("op")
            if op == "add":
                tag = self.s.vocab_add(str(b.get("group") or "other"), str(b["tag"]))
            elif op == "rename":
                tag = self.s.vocab_rename(str(b["tag"]), str(b["new"]))
            elif op == "remove":
                self.s.vocab_remove(str(b["tag"]))
                tag = None
            else:
                raise ApiError("Unknown vocabulary operation.")
        except ValueError as e:
            raise ApiError(str(e))
        return {"tag": tag, "status": self.s.status()}

    def post_check_rule(self, b):
        return {"error": rule_error(str(b.get("rule", "")))}

    def post_sources(self, b):
        """Save settings for one source. Masked secrets ('•••') keep their stored value."""
        sid = str(b.get("id", ""))
        new = {k: v for k, v in (b.get("settings") or {}).items() if v != "•••"}

        def mut(d):
            src = d.setdefault("sources", {}).setdefault(sid, {})
            src.update(new)
        _update_user_yaml(mut)
        self.s.load_config()
        return self.s.status()

    # ---------------- analysis
    def post_analyze(self, b):
        self._lib()
        if not self.s.keys():
            raise ApiError("The batch is empty. Pick playlists or a track first.")
        xml = self.s.batch_xml()
        cmds = analyze_commands(xml, ["Subjetunes batch"], bool(b.get("clap", True)),
                                int(b.get("workers", max(1, (os.cpu_count() or 4) - 4))))
        os.environ["SUBJETUNES_BATCH"] = str(xml.with_suffix(".json"))
        job = self.s.jobs.run(f"Analyse {self.s.batch_name()}", cmds, on_done=lambda j: self.s.invalidate())
        return job.to_dict()

    def post_cancel(self, b):
        self.s.jobs.cancel(str(b.get("id", "")))
        return {"ok": True}

    # ---------------- writing
    def post_write_plan(self, b):
        """Every change as a reviewable hunk. Skipped hunks are listed too, marked skipped."""
        self._lib()
        opts = (bool(b.get("tags", True)), bool(b.get("playlists", True)), bool(b.get("fields", True)))
        skip = set(b.get("skip", []))
        full = self.s.write_plan(*opts)
        lib = self.s.lib
        by_id = {t.content_id: t for t in lib.tracks.values()}
        hunks = []
        for cid, f in full["edits"].items():
            t = by_id.get(cid)
            for k, v in f.items():
                hid = f"{cid}:{k}"
                hunks.append({"id": hid, "key": t.key if t else "", "title": t.title if t else cid,
                              "artist": t.artist if t else "", "field": k,
                              "old": "" if not t or t.meta.get(k) is None else str(t.meta.get(k)),
                              "new": "" if v is None else str(v), "skipped": hid in skip})
        pls = [{"id": f"playlist:{n}", "name": n, "count": len(ids), "skipped": f"playlist:{n}" in skip}
               for n, ids in full["playlists"].items()]
        kept = [h for h in hunks if not h["skipped"]]
        return {"hunks": hunks[:2000], "total": len(hunks), "kept": len(kept),
                "tracks": len({h["id"].split(":")[0] for h in kept}),
                "playlists": pls, "running": self.s.rekordbox_running(),
                "folder": f"Subjetunes/{self.s.batch_name() or 'Batch'}"}

    def post_write(self, b):
        from ..tagging import masterdb
        try:
            return self.s.write(bool(b.get("tags", True)), bool(b.get("playlists", True)), bool(b.get("fields", True)),
                                set(b.get("skip", [])))
        except masterdb.WriteError as e:
            raise ApiError(str(e))

    def post_export(self, b):
        """XML fallback (for an XML library, or to import by hand)."""
        lib = self._lib()
        rows = self.s.rows()
        what = b.get("what", "both")
        folder = str(b.get("folder") or default_folder_name())
        playlists: dict[str, list[str]] = {}
        if what in ("both", "playlists"):
            for bk in self.s.buckets:
                members = [r["key"] for r in rows if bk.id in r["buckets"]]
                if members:
                    playlists[bk.name] = members
        tags = {r["key"]: r["tags"] for r in rows} if what in ("both", "tags") else None
        if tags:
            tagged = [r["key"] for r in rows if r["tags"]]
            if tagged:
                playlists["Tag update"] = tagged
        out = paths.data_dir() / "exports" / f"subjetunes_{datetime.now():%Y%m%d_%H%M%S}.xml"
        rep = write_bucket_xml(lib, playlists, out, folder, tags, self.s.tag_cfg.block)
        return {"path": str(out), "folder": folder, "playlists": rep.playlists, "tracks": rep.tracks,
                "tagged": rep.tagged}

    def post_readback(self, b):
        from .library import read_library
        p = Path(str(b.get("path", "")).strip().strip('"'))
        lib = read_library(p) if p.is_file() else self._lib()
        found = self.s.ledger.read_back({k: t.comments for k, t in lib.tracks.items()}, self.s.tag_cfg.block)
        self.s.ledger.save()
        self.s.invalidate()
        return {"edited": len(found)}

    def post_reveal(self, b):
        p = Path(str(b.get("path", "")))
        if sys.platform.startswith("win") and p.exists():
            subprocess.Popen(["explorer", "/select,", str(p)])
        return {"ok": True}


class Handler(BaseHTTPRequestHandler):
    api: Api
    token: str
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # quiet console
        pass

    def _authorised(self, q) -> bool:
        return secrets.compare_digest(self.headers.get("X-Token", "") or q.get("t", [""])[0], self.token)

    def _send(self, status: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if "Cache-Control" not in (extra or {}):
            self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, obj: Any) -> None:
        self._send(status, json.dumps(obj, ensure_ascii=False, default=str).encode(), "application/json")

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path.startswith("/api/"):
            if not self._authorised(q):
                return self._json(403, {"error": "Missing token."})
            if u.path == "/api/audio":
                return self._audio(q)
            if u.path == "/api/artwork":
                return self._artwork(q)
            return self._call("get_" + u.path[5:].replace("/", "_"), q)
        name = "index.html" if u.path in ("", "/") else u.path.lstrip("/")
        f = (WEB / name).resolve()
        if WEB.resolve() not in f.parents or not f.is_file():
            return self._send(404, b"not found", "text/plain")
        ctype = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
        if f.suffix == ".js":
            ctype = "text/javascript"
        self._send(200, f.read_bytes(), ctype + ("; charset=utf-8" if ctype.startswith("text") else ""))

    do_HEAD = do_GET

    def do_POST(self):
        u = urlparse(self.path)
        if not self._authorised(parse_qs(u.query)):
            return self._json(403, {"error": "Missing token."})
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            return self._json(400, {"error": "Bad JSON."})
        self._call("post_" + u.path[5:].replace("/", "_").replace("-", "_"), body)

    def _call(self, name: str, arg: Any) -> None:
        fn = getattr(self.api, name, None)
        if fn is None:
            return self._json(404, {"error": f"Unknown endpoint {name}."})
        token = object()
        INFLIGHT[token] = (name, time.time(), threading.get_ident())
        try:
            self._json(200, fn(arg))
        except ApiError as e:
            self._json(e.status, {"error": str(e)})
        except Exception as e:
            logging.exception("%s failed", name)
            self._json(500, {"error": f"{type(e).__name__}: {e}"})
        finally:
            INFLIGHT.pop(token, None)

    def _artwork(self, q):
        try:
            p = self.api.s.artwork(q.get("key", [""])[0])
        except Exception:
            p = None
        if not p:
            return self._send(404, b"", "text/plain")
        ctype = mimetypes.guess_type(p.name)[0] or "image/jpeg"
        self._send(200, p.read_bytes(), ctype, {"Cache-Control": "max-age=3600"})

    def _audio(self, q):
        s = self.api.s
        k = normalise_path(q.get("key", [""])[0])
        t = s.lib.tracks.get(k) if s.lib else None
        if not t:
            return self._json(404, {"error": "Track not in the open library."})
        f = Path(t.path)
        if not f.is_file():
            return self._json(404, {"error": f"File missing: {f}"})
        size = f.stat().st_size
        ctype = AUDIO_TYPES.get(f.suffix.lower(), "application/octet-stream")
        rng = re.match(r"bytes=(\d*)-(\d*)", self.headers.get("Range", ""))
        start, end = 0, size - 1
        if rng:
            if rng.group(1):
                start = int(rng.group(1))
                end = int(rng.group(2)) if rng.group(2) else size - 1
            elif rng.group(2):
                start = max(size - int(rng.group(2)), 0)
        end = min(end, size - 1, start + 4 * 1024 * 1024 - 1)    # 4 MB chunks keep seeking snappy
        if start >= size:
            return self._send(416, b"", ctype, {"Content-Range": f"bytes */{size}"})
        with f.open("rb") as fh:
            fh.seek(start)
            data = fh.read(end - start + 1)
        self._send(206 if rng else 200, data, ctype,
                   {"Accept-Ranges": "bytes", "Content-Range": f"bytes {start}-{end}/{size}"})


INFLIGHT: dict[object, tuple[str, float, int]] = {}


def _watchdog(limit: float = 20.0) -> None:
    """If a request runs longer than `limit`, write every thread's stack to the log once.
    That turns a future 'it hangs' into a traceback pointing at the slow line."""
    reported: set[int] = set()
    while True:
        time.sleep(5)
        now = time.time()
        for key, (name, t0, tid) in list(INFLIGHT.items()):
            if now - t0 > limit and id(key) not in reported:
                reported.add(id(key))
                frames = sys._current_frames()
                dump = [f"Request {name} has been running {now - t0:.0f}s. Threads:"]
                for th in threading.enumerate():
                    f = frames.get(th.ident)
                    if f is not None:
                        dump.append(f"--- {th.name}{' (this request)' if th.ident == tid else ''}")
                        dump.append("".join(traceback.format_stack(f)))
                logging.warning("\n".join(dump))


def start(state: AppState | None = None, port: int = 0) -> tuple[ThreadingHTTPServer, str]:
    token = secrets.token_urlsafe(16)
    handler = type("BoundHandler", (Handler,), {"api": Api(state or AppState()), "token": token})
    srv = ThreadingHTTPServer(("127.0.0.1", port), handler)
    srv.daemon_threads = True
    Thread(target=srv.serve_forever, daemon=True, name="http").start()
    Thread(target=_watchdog, daemon=True, name="watchdog").start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}/?t={token}"
