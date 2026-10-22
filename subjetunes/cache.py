"""SQLite store for everything expensive or human-made.

Features and embeddings are keyed by file path and only valid while the file's
size and mtime match. Human data (overrides, labels) is never invalidated.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np

from .models import Assignment, Features, Judgment, from_dict

SCHEMA = """
CREATE TABLE IF NOT EXISTS features(
  path TEXT PRIMARY KEY, file_size INTEGER, file_mtime REAL, analyzer_version TEXT,
  analyzed_at TEXT, json TEXT, error TEXT);
CREATE TABLE IF NOT EXISTS embeddings(
  path TEXT, model_id TEXT, file_size INTEGER, file_mtime REAL, dim INTEGER, vec BLOB,
  PRIMARY KEY(path, model_id));
CREATE TABLE IF NOT EXISTS assignments(
  track_id TEXT, job_id TEXT, json TEXT, source TEXT, locked INTEGER, updated_at TEXT,
  PRIMARY KEY(track_id, job_id));
CREATE TABLE IF NOT EXISTS overrides(
  track_id TEXT PRIMARY KEY, summary_json TEXT, buckets_json TEXT, tags_json TEXT,
  note TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS labels(
  track_id TEXT, criterion_id TEXT, value TEXT, holdout INTEGER, created_at TEXT,
  PRIMARY KEY(track_id, criterion_id));
CREATE TABLE IF NOT EXISTS judgments(
  track_id TEXT, criterion_id TEXT, judge_id TEXT, judge_version TEXT, value TEXT,
  confidence REAL, evidence TEXT, ms INTEGER, created_at TEXT,
  PRIMARY KEY(track_id, criterion_id, judge_id));
CREATE TABLE IF NOT EXISTS captions(
  path TEXT, judge_id TEXT, judge_version TEXT, json TEXT, created_at TEXT,
  PRIMARY KEY(path, judge_id));
CREATE TABLE IF NOT EXISTS benchmarks(
  run_id TEXT, criterion_id TEXT, judge_id TEXT, metric TEXT, value REAL, n INTEGER, created_at TEXT);
CREATE TABLE IF NOT EXISTS jobs(
  job_id TEXT PRIMARY KEY, kind TEXT, created_at TEXT, status TEXT, done INTEGER, total INTEGER,
  message TEXT, spec_json TEXT, updated_at TEXT);
"""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class Cache:
    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=30)
        self.conn.execute("PRAGMA journal_mode=WAL")       # UI reads while the CLI job writes
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def _exec(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self.conn.execute(sql, params)
            self.conn.commit()
            return cur

    def _query(self, sql: str, params: tuple = ()) -> list[tuple]:
        with self._lock:
            return self.conn.execute(sql, params).fetchall()

    # ------------------------------------------------------------------ features
    def get_features(self, path: str, size: int, mtime: float, version: str) -> Features | None:
        rows = self._query(
            "SELECT json FROM features WHERE path=? AND file_size=? AND abs(file_mtime-?)<0.01 AND analyzer_version=?",
            (path, size, mtime, version),
        )
        return from_dict(Features, json.loads(rows[0][0])) if rows else None

    def put_features(self, f: Features) -> None:
        self._exec(
            "INSERT OR REPLACE INTO features VALUES(?,?,?,?,?,?,?)",
            (f.path, f.file_size, f.file_mtime, f.analyzer_version, f.analyzed_at, json.dumps(asdict(f)), f.error),
        )

    def all_features(self, paths: list[str] | None = None) -> dict[str, Features]:
        rows = self._query("SELECT path, json FROM features")
        wanted = set(paths) if paths is not None else None
        return {p: from_dict(Features, json.loads(j)) for p, j in rows if wanted is None or p in wanted}

    # ------------------------------------------------------------------ embeddings
    def get_embedding(self, path: str, model_id: str, size: int, mtime: float) -> np.ndarray | None:
        rows = self._query(
            "SELECT dim, vec FROM embeddings WHERE path=? AND model_id=? AND file_size=? AND abs(file_mtime-?)<0.01",
            (path, model_id, size, mtime),
        )
        if not rows:
            return None
        return np.frombuffer(rows[0][1], dtype=np.float32).copy()

    def put_embedding(self, path: str, model_id: str, size: int, mtime: float, vec: np.ndarray) -> None:
        v = np.asarray(vec, dtype=np.float32)
        self._exec("INSERT OR REPLACE INTO embeddings VALUES(?,?,?,?,?,?)", (path, model_id, size, mtime, v.size, v.tobytes()))

    def embeddings_for(self, paths: list[str], model_id: str) -> dict[str, np.ndarray]:
        rows = self._query("SELECT path, vec FROM embeddings WHERE model_id=?", (model_id,))
        wanted = set(paths)
        return {p: np.frombuffer(b, dtype=np.float32).copy() for p, b in rows if p in wanted}

    # ------------------------------------------------------------------ assignments / overrides
    def put_assignments(self, job_id: str, items: list[Assignment]) -> None:
        with self._lock:
            self.conn.executemany(
                "INSERT OR REPLACE INTO assignments VALUES(?,?,?,?,?,?)",
                [(a.track_id, job_id, json.dumps(asdict(a)), a.source, int(a.locked), _now()) for a in items],
            )
            self.conn.commit()

    def get_assignments(self, job_id: str) -> dict[str, Assignment]:
        rows = self._query("SELECT track_id, json FROM assignments WHERE job_id=?", (job_id,))
        return {t: from_dict(Assignment, json.loads(j)) for t, j in rows}

    def put_override(self, track_id: str, summary: dict, buckets: list[str], tags: list[str], note: str = "") -> None:
        self._exec(
            "INSERT OR REPLACE INTO overrides VALUES(?,?,?,?,?,?)",
            (track_id, json.dumps(summary), json.dumps(buckets), json.dumps(tags), note, _now()),
        )

    def get_overrides(self) -> dict[str, dict[str, Any]]:
        rows = self._query("SELECT track_id, summary_json, buckets_json, tags_json, note, created_at FROM overrides ORDER BY created_at DESC")
        return {
            t: {"summary": json.loads(s), "buckets": json.loads(b), "tags": json.loads(g), "note": n, "created_at": c}
            for t, s, b, g, n, c in rows
        }

    def delete_override(self, track_id: str) -> None:
        self._exec("DELETE FROM overrides WHERE track_id=?", (track_id,))

    # ------------------------------------------------------------------ gold-set labels
    def put_label(self, track_id: str, criterion_id: str, value: float | str, holdout: bool = False) -> None:
        self._exec("INSERT OR REPLACE INTO labels VALUES(?,?,?,?,?)", (track_id, criterion_id, json.dumps(value), int(holdout), _now()))

    def get_labels(self, criterion_id: str) -> dict[str, dict[str, Any]]:
        rows = self._query("SELECT track_id, value, holdout FROM labels WHERE criterion_id=?", (criterion_id,))
        return {t: {"value": json.loads(v), "holdout": bool(h)} for t, v, h in rows}

    def delete_label(self, track_id: str, criterion_id: str) -> None:
        self._exec("DELETE FROM labels WHERE track_id=? AND criterion_id=?", (track_id, criterion_id))

    # ------------------------------------------------------------------ judgments / captions
    def put_judgments(self, items: list[Judgment]) -> None:
        with self._lock:
            self.conn.executemany(
                "INSERT OR REPLACE INTO judgments VALUES(?,?,?,?,?,?,?,?,?)",
                [(j.track_id, j.criterion_id, j.judge_id, j.judge_version, json.dumps(j.value), j.confidence, j.evidence, j.ms, _now()) for j in items],
            )
            self.conn.commit()

    def get_judgments(self, criterion_id: str, judge_id: str | None = None) -> list[Judgment]:
        sql = "SELECT track_id, criterion_id, judge_id, judge_version, value, confidence, evidence, ms FROM judgments WHERE criterion_id=?"
        params: tuple = (criterion_id,)
        if judge_id:
            sql += " AND judge_id=?"
            params += (judge_id,)
        return [Judgment(t, c, j, v, json.loads(val), conf, ev, ms) for t, c, j, v, val, conf, ev, ms in self._query(sql, params)]

    def put_caption(self, path: str, judge_id: str, judge_version: str, caption: dict) -> None:
        self._exec("INSERT OR REPLACE INTO captions VALUES(?,?,?,?,?)", (path, judge_id, judge_version, json.dumps(caption), _now()))

    def get_captions(self, paths: list[str] | None = None) -> dict[str, dict]:
        rows = self._query("SELECT path, json FROM captions ORDER BY created_at")
        wanted = set(paths) if paths is not None else None
        return {p: json.loads(j) for p, j in rows if wanted is None or p in wanted}

    # ------------------------------------------------------------------ benchmarks
    def put_benchmark(self, run_id: str, rows: list[tuple[str, str, str, float, int]]) -> None:
        with self._lock:
            self.conn.executemany(
                "INSERT INTO benchmarks VALUES(?,?,?,?,?,?,?)", [(run_id, c, j, m, v, n, _now()) for c, j, m, v, n in rows]
            )
            self.conn.commit()

    def get_benchmarks(self, criterion_id: str) -> list[dict[str, Any]]:
        rows = self._query(
            "SELECT run_id, judge_id, metric, value, n, created_at FROM benchmarks WHERE criterion_id=? ORDER BY created_at",
            (criterion_id,),
        )
        return [dict(zip(("run_id", "judge_id", "metric", "value", "n", "created_at"), r)) for r in rows]

    # ------------------------------------------------------------------ jobs (progress shared between CLI and UI)
    def create_job(self, job_id: str, kind: str, spec: dict, total: int = 0) -> None:
        self._exec(
            "INSERT OR REPLACE INTO jobs VALUES(?,?,?,?,?,?,?,?,?)",
            (job_id, kind, _now(), "queued", 0, total, "", json.dumps(spec), _now()),
        )

    def update_job(self, job_id: str, **kw: Any) -> None:
        allowed = {"status", "done", "total", "message"}
        sets = ", ".join(f"{k}=?" for k in kw if k in allowed)
        if not sets:
            return
        self._exec(f"UPDATE jobs SET {sets}, updated_at=? WHERE job_id=?", tuple(v for k, v in kw.items() if k in allowed) + (_now(), job_id))

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        rows = self._query("SELECT job_id, kind, created_at, status, done, total, message, spec_json, updated_at FROM jobs WHERE job_id=?", (job_id,))
        if not rows:
            return None
        keys = ("job_id", "kind", "created_at", "status", "done", "total", "message", "spec", "updated_at")
        d = dict(zip(keys, rows[0]))
        d["spec"] = json.loads(d["spec"])
        return d

    def latest_jobs(self, limit: int = 10) -> list[dict[str, Any]]:
        rows = self._query("SELECT job_id FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,))
        return [self.get_job(r[0]) for r in rows]
