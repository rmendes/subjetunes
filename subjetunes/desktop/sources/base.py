# subjetunes/desktop/sources/base.py
from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
import unicodedata
from dataclasses import asdict, dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Callable

FIELD_KEYS = ("title", "artist", "album", "album_artist", "remixer", "mix_name", "original_artist", "composer",
              "label", "genre", "year", "release_date", "track_no", "disc_no", "isrc", "bpm", "key", "catalog",
              "comments")
UA = "Subjetunes/0.3 (personal DJ library tool; https://github.com/)"


@dataclass
class TrackQuery:
    key: str
    path: str
    title: str
    artist: str
    album: str = ""
    duration_s: float | None = None
    isrc: str = ""
    meta: dict = field(default_factory=dict)       # rekordbox fields
    mytags: list[str] = field(default_factory=list)


@dataclass
class Result:
    source: str
    name: str
    state: str = "none"                  # found | none | error | off
    url: str = ""
    fields: dict[str, Any] = field(default_factory=dict)
    tags: list[list] = field(default_factory=list)   # [[name, weight 0..1], ...]
    extra: dict[str, Any] = field(default_factory=dict)
    note: str = ""
    match: float | None = None           # how sure we are it's the same track, 0..1

    def to_dict(self) -> dict:
        return asdict(self)


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    s = re.sub(r"\b(feat|ft|featuring)\b.*", "", s)
    s = re.sub(r"\((original|extended)( mix)?\)", "", s)
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def base_title(title: str) -> str:
    """'Selecta (Extended Mix)' -> 'Selecta' for searching."""
    return re.sub(r"\s*[\(\[].*?[\)\]]\s*", " ", title or "").strip() or title


def similarity(q: TrackQuery, artist: str, title: str) -> float:
    a = SequenceMatcher(None, norm(q.artist), norm(artist)).ratio() if q.artist else 0.6
    t = max(SequenceMatcher(None, norm(q.title), norm(title)).ratio(),
            SequenceMatcher(None, norm(base_title(q.title)), norm(base_title(title))).ratio())
    return round(0.45 * a + 0.55 * t, 3)


def split_tags(text: str, weight: float = 1.0) -> list[list]:
    out = []
    for part in re.split(r"[;,/|\n]+", text or ""):
        p = part.strip().strip("#").strip()
        if p and len(p) <= 40:
            out.append([p, weight])
    return out


class Source:
    id = ""
    name = ""
    needs: tuple[str, ...] = ()          # settings keys required, e.g. ("api_key",)
    online = True
    min_interval = 0.0                   # seconds between requests (rate limits)
    trust = 1.0                          # weight in the word cloud
    default_on = True

    def __init__(self, settings: dict):
        self.settings = settings or {}
        self._lock = threading.Lock()
        self._last = 0.0

    def enabled(self) -> tuple[bool, str]:
        if not self.settings.get("enabled", self.default_on):
            return False, "Turned off in Settings"
        missing = [n for n in self.needs if not self.settings.get(n)]
        if missing:
            return False, f"Needs {', '.join(missing)} in Settings"
        return True, ""

    def http(self, method: str, url: str, **kw) -> Any:
        import requests
        with self._lock:
            wait = self.min_interval - (time.time() - self._last)
            if wait > 0:
                time.sleep(wait)
            headers = {"User-Agent": UA, **kw.pop("headers", {})}
            r = requests.request(method, url, headers=headers, timeout=kw.pop("timeout", 12), **kw)
            self._last = time.time()
        r.raise_for_status()
        return r

    def get_json(self, url: str, **kw) -> Any:
        return self.http("GET", url, **kw).json()

    def lookup(self, q: TrackQuery) -> Result:  # pragma: no cover - implemented by subclasses
        raise NotImplementedError

    def result(self, **kw) -> Result:
        return Result(source=self.id, name=self.name, **kw)


REGISTRY: dict[str, type[Source]] = {}


def register(cls: type[Source]) -> type[Source]:
    REGISTRY[cls.id] = cls
    return cls


class Cache:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        with self._conn() as c:
            c.execute("create table if not exists results (source text, key text, json text, state text,"
                      " fetched real, primary key (source, key))")

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=10)

    def get(self, source: str, key: str, max_age: float = 30 * 86400) -> Result | None:
        with self._lock, self._conn() as c:
            row = c.execute("select json, state, fetched from results where source=? and key=?", (source, key)).fetchone()
        if not row:
            return None
        age = time.time() - row[2]
        if age > (3600 if row[1] == "error" else max_age):
            return None
        return Result(**json.loads(row[0]))

    def put(self, r: Result, key: str) -> None:
        with self._lock, self._conn() as c:
            c.execute("insert or replace into results values (?,?,?,?,?)",
                      (r.source, key, json.dumps(r.to_dict()), r.state, time.time()))

    def stats(self) -> dict[str, dict]:
        """Per source: how many tracks answered found / none / error, and when it last answered."""
        out: dict[str, dict] = {}
        with self._lock, self._conn() as c:
            for src, state, n, last in c.execute(
                    "select source, state, count(*), max(fetched) from results group by source, state"):
                e = out.setdefault(src, {"found": 0, "none": 0, "error": 0, "off": 0, "last": 0})
                e[state] = n
                e["last"] = max(e["last"], last or 0)
        return out

    def drop(self, key: str) -> None:
        with self._lock, self._conn() as c:
            c.execute("delete from results where key=?", (key,))


def lookup_all(q: TrackQuery, settings: dict[str, dict], cache: Cache, only: list[str] | None = None,
               on_result: Callable[[Result], None] | None = None, force: bool = False) -> list[Result]:
    """Run every source (cached results first). Online sources run in parallel threads."""
    results: dict[str, Result] = {}
    threads = []
    for sid, cls in REGISTRY.items():
        if only and sid not in only:
            continue
        src = cls(settings.get(sid, {}))
        ok, why = src.enabled()
        if not ok:
            results[sid] = src.result(state="off", note=why)
            continue
        cached = None if (force or not src.online) else cache.get(sid, q.key)
        if cached:
            results[sid] = cached
            continue

        def work(src=src, sid=sid):
            try:
                r = src.lookup(q)
            except Exception as e:  # network errors, API changes: show, don't crash
                msg = str(e)
                r = src.result(state="error", note=msg[:200])
            if src.online:
                cache.put(r, q.key)
            results[sid] = r
            if on_result:
                on_result(r)

        if src.online:
            th = threading.Thread(target=work, daemon=True)
            th.start()
            threads.append(th)
        else:
            work()
    for th in threads:
        th.join(timeout=30)
    order = list(REGISTRY)
    return sorted(results.values(), key=lambda r: order.index(r.source))


def slug(tag: str) -> str:
    s = unicodedata.normalize("NFKD", tag).encode("ascii", "ignore").decode().lower().strip().lstrip("#")
    s = re.sub(r"[\s_/]+", "-", s)
    return re.sub(r"[^a-z0-9&+\-]", "", s).strip("-")


def _same(f: str, a: str, b: str) -> bool:
    if f == "bpm":
        try:
            return abs(float(a) - float(b)) < 0.05
        except ValueError:
            pass
    return norm(a) == norm(b)


def aggregate(results: list[Result]) -> dict[str, Any]:
    """Word cloud + per-field alternatives across sources."""
    cloud: dict[str, dict] = {}
    for r in results:
        if r.state != "found":
            continue
        trust = REGISTRY[r.source].trust if r.source in REGISTRY else 1.0
        for name, w in r.tags:
            k = slug(name)
            if not k or len(k) < 2:
                continue
            e = cloud.setdefault(k, {"tag": k, "label": name.strip(), "weight": 0.0, "sources": []})
            e["weight"] += float(w) * trust
            if r.source not in e["sources"]:
                e["sources"].append(r.source)
    tags = sorted(cloud.values(), key=lambda e: -e["weight"])[:60]
    top = max((e["weight"] for e in tags), default=1.0) or 1.0
    for e in tags:
        e["weight"] = round(e["weight"] / top, 3)

    fields: dict[str, list[dict]] = {}
    for r in results:
        if r.state != "found":
            continue
        for f, v in r.fields.items():
            if v in (None, "", 0) or f not in FIELD_KEYS:
                continue
            vs = str(v).strip()
            alts = fields.setdefault(f, [])
            hit = next((a for a in alts if _same(f, a["value"], vs)), None)
            if hit:
                hit["sources"].append(r.source)
            else:
                alts.append({"value": vs, "sources": [r.source]})
    return {"cloud": tags, "fields": fields}
