# subjetunes/desktop/engine.py
"""Where scores come from, and the analysis job runner.

Score sources, first one that has data wins:
  1. CoreScores: the subjetunes engine. Contract: a function
         subjetunes.pipeline.score_rows(paths: list[str]) -> list[dict]
     returning one flat dict per analysed track ({"path", "energy", "comp_vocals", ...}),
     i.e. what the bucket rules already see. Used automatically once it exists.
  2. FileScores: data/scores.csv or data/scores.json (same columns as the tagging CLI).
  3. DemoScores: plausible fake numbers, only when the user turns on demo mode.
"""
from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import re
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..tagging.comments import normalise_path
from . import paths


class CoreScores:
    """Scores from the subjetunes engine's own cache (data/cache.sqlite), scored the same way the
    engine does it: subjetunes.scoring.compute_scores for energy/swing/looseness/brightness, CLAP
    prompt pairs for mood_* and comp_*, stored judgments and your labels for crit_*.

    The engine's `analyze` writes raw features; nothing is re-measured here, so this is cheap.
    Results are memoised until the cache file changes."""
    name = "Subjetunes analysis"

    def __init__(self) -> None:
        self.error = ""
        try:
            from subjetunes import cache as _c, config as _cfg, scoring as _s  # noqa: F401
            self._db = _cfg.DATA_DIR / "cache.sqlite"
            self.ok = True
        except Exception as e:                       # engine not in this build
            self.ok, self._db, self.error = False, None, f"{e.__class__.__name__}: {e}"

    @property
    def available(self) -> bool:
        return self.ok and self._db is not None and self._db.exists()

    def _stamp(self) -> tuple:
        out = []
        for suffix in ("", "-wal"):
            f = Path(str(self._db) + suffix)
            out.append(f.stat().st_mtime if f.exists() else 0)
        return tuple(out)

    def scores(self, track_paths: list[str], ids: dict[str, str] | None = None) -> dict[str, dict]:
        if not self.available or not track_paths:
            return {}
        key = (tuple(sorted(normalise_path(p) for p in track_paths)), self._stamp(),
               tuple(sorted((ids or {}).items())))
        hit = _MEMO.get("core")
        if hit and hit[0] == key:
            return hit[1]
        out = _engine_scores(self._db, track_paths, ids or {})
        _MEMO["core"] = (key, out)
        return out


_MEMO: dict[str, Any] = {}
_TEXT_EMB: dict[tuple, Any] = {}


def _engine_scores(db: Path, track_paths: list[str], ids: dict[str, str]) -> dict[str, dict]:
    """{normalised path: flat score dict} for the tracks the engine has analysed."""
    import math
    from dataclasses import asdict

    import numpy as np

    from subjetunes import config as ecfg, scoring
    from subjetunes.cache import Cache

    cfg = ecfg.load_settings()
    cache = Cache(db)
    try:
        want = {normalise_path(p) for p in track_paths}
        all_feats = cache.all_features()
        raw_of = {normalise_path(p): p for p in all_feats}            # engine stores the XML's decoded path
        feats = {k: asdict(all_feats[raw_of[k]]) for k in want if k in raw_of and not all_feats[raw_of[k]].error}
        errors = {k: all_feats[raw_of[k]].error for k in want if k in raw_of and all_feats[raw_of[k]].error}
        if not feats:
            return {}
        frame = scoring.features_frame(feats)
        ref = None
        if cfg.get("scoring", {}).get("normalisation", "job") == "global":
            ref = scoring.features_frame({p: asdict(f) for p, f in all_feats.items() if not f.error})
        table = scoring.compute_scores(frame, cfg, ref)

        ccfg = cfg.get("clap", {}) or {}
        model_id = ccfg.get("model_id", "laion/larger_clap_music")
        emb_raw = cache.embeddings_for([raw_of[k] for k in feats], model_id)
        emb = {normalise_path(p): v for p, v in emb_raw.items()}
        if emb:
            text_embed = _text_embedder(model_id, ccfg)
            if text_embed is not None:
                for prefix, pairs in (("mood_", ccfg.get("mood_axes", {})), ("comp_", ccfg.get("components", {}))):
                    if pairs:
                        cols = scoring.clap_axis_scores(emb, pairs, text_embed).add_prefix(prefix)
                        table = table.join(cols, how="left")

        crit: dict[str, dict[str, Any]] = {}
        if ids:
            by_id = {str(ids[k]): k for k in feats if ids.get(k)}
            try:
                from subjetunes.config import criteria_from
                chosen = cfg.get("criteria_judge", {}) or {}
                for c in criteria_from(cfg):
                    for j in cache.get_judgments(c.id, chosen.get(c.id)):
                        k = by_id.get(str(j.track_id))
                        if k:
                            crit.setdefault(k, {})[f"crit_{c.id}"] = j.value
                            if getattr(j, "confidence", None) is not None:
                                crit[k][f"crit_{c.id}_conf"] = j.confidence
                    for tid, lab in cache.get_labels(c.id).items():          # your labels always win
                        k = by_id.get(str(tid))
                        if k:
                            crit.setdefault(k, {})[f"crit_{c.id}"] = lab["value"]
                            crit[k].pop(f"crit_{c.id}_conf", None)
            except Exception:
                pass

        out: dict[str, dict] = {}
        for k, row in table.iterrows():
            d: dict[str, Any] = {"path": k}
            for col, v in row.items():
                if isinstance(v, (float, np.floating)):
                    if not math.isnan(float(v)):
                        d[col] = round(float(v), 4)
                elif v is not None:
                    d[col] = v
            d.update(crit.get(k, {}))
            out[k] = d
        for k, err in errors.items():
            _MEMO.setdefault("errors", {})[k] = err
        return out
    finally:
        cache.close()


def _text_embedder(model_id: str, ccfg: dict):
    """CLAP text tower, loaded once per run; prompt embeddings memoised (they never change)."""
    try:
        from subjetunes.features import clap
        ok, _ = clap.available()
        if not ok:
            return None
        emb = clap.ClapEmbedder.get(model_id, ccfg.get("threads"))
    except Exception:
        return None

    def embed(prompts: list[str]):
        k = (model_id, tuple(prompts))
        if k not in _TEXT_EMB:
            _TEXT_EMB[k] = emb.text_embeddings(prompts)
        return _TEXT_EMB[k]
    return embed


def analysis_errors() -> dict[str, str]:
    """Tracks the engine tried but couldn't decode (shown in the batch, not silently 'no scores')."""
    return dict(_MEMO.get("errors", {}))


class FileScores:
    name = "Scores file"

    def __init__(self, path: Path | None = None) -> None:
        self.path = path

    def _file(self) -> Path | None:
        if self.path:
            return self.path if self.path.exists() else None
        for n in ("scores.json", "scores.csv"):
            p = paths.data_dir() / n
            if p.exists():
                return p
        return None

    @property
    def available(self) -> bool:
        return self._file() is not None

    def scores(self, track_paths: list[str], ids: dict[str, str] | None = None) -> dict[str, dict]:
        f = self._file()
        if not f:
            return {}
        from ..tagging.cli import load_scores
        return {normalise_path(r["key"]): r for r in load_scores(f)}


class DemoScores:  # noqa: D101
    """Deterministic fake scores so the whole UI can be tried before analysis runs."""
    name = "Demo scores"
    available = True
    FIELDS = ("energy", "swing", "looseness", "mood_agitated", "mood_angry", "mood_analog", "mood_bright",
              "comp_vocals", "comp_piano", "comp_acid", "comp_breakbeat", "comp_strings",
              "crit_hypnotic", "crit_tension")

    def scores(self, track_paths: list[str], ids: dict | None = None) -> dict[str, dict]:
        out = {}
        for p in track_paths:
            h = hashlib.sha256(p.encode()).digest()
            row: dict[str, Any] = {f: round((h[i] / 255) ** (1.6 if f.startswith("comp_") else 1.0), 3)
                                   for i, f in enumerate(self.FIELDS)}
            row["crit_vocal_role"] = ["none", "chopped_sample", "spoken", "sung_hook", "full_song"][h[20] % 5]
            row["energy_spread"] = round(h[22] / 255 * 0.6, 3)          # max-min of the excerpt energies
            row["energy_slope"] = round((h[23] / 255 - 0.4) * 0.6, 3)   # last excerpt minus first
            row["crit_hypnotic_conf"] = round(0.4 + h[21] / 425, 2)
            out[normalise_path(p)] = row
        return out


def pick_source(demo: bool) -> Any:
    if demo:
        return DemoScores()
    for s in (CoreScores(), FileScores()):
        if s.available:
            return s
    return None


# ---------------------------------------------------------------- jobs

@dataclass
class Job:
    id: str
    title: str
    state: str = "running"          # running | done | failed | cancelled
    progress: float | None = None   # 0..1 when the job reports n/m
    lines: list[str] = field(default_factory=list)
    started: float = field(default_factory=time.time)
    proc: subprocess.Popen | None = field(default=None, repr=False)

    def to_dict(self) -> dict:
        return {"id": self.id, "title": self.title, "state": self.state, "progress": self.progress,
                "log": self.lines[-200:], "elapsed": round(time.time() - self.started)}


class Jobs:
    PROGRESS = re.compile(r"(\d+)\s*/\s*(\d+)")

    def __init__(self) -> None:
        self.jobs: dict[str, Job] = {}

    def run(self, title: str, commands: list[list[str]], on_done: Callable[[Job], None] | None = None) -> Job:
        job = Job(id=uuid.uuid4().hex[:8], title=title)
        self.jobs[job.id] = job
        threading.Thread(target=self._worker, args=(job, commands, on_done), daemon=True).start()
        return job

    def _worker(self, job: Job, commands: list[list[str]], on_done) -> None:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            for cmd in commands:
                job.lines.append("$ " + " ".join(cmd[1:]))
                job.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                            encoding="utf-8", errors="replace", cwd=str(paths.home()),
                                            creationflags=flags)
                assert job.proc.stdout is not None
                for line in job.proc.stdout:
                    line = line.rstrip()
                    if line:
                        job.lines.append(line)
                        m = self.PROGRESS.search(line)
                        if m and int(m.group(2)) > 0:
                            job.progress = min(int(m.group(1)) / int(m.group(2)), 1.0)
                code = job.proc.wait()
                if job.state == "cancelled":
                    return
                if code != 0:
                    job.state = "failed"
                    job.lines.append(f"Stopped: the command exited with code {code}.")
                    return
            job.state = "done"
            job.progress = 1.0
        except FileNotFoundError as e:
            job.state = "failed"
            job.lines.append(f"Could not start the analysis: {e}")
        finally:
            if on_done:
                on_done(job)

    def cancel(self, job_id: str) -> None:
        j = self.jobs.get(job_id)
        if j and j.proc and j.state == "running":
            j.state = "cancelled"
            j.proc.terminate()


def python_exe() -> str:
    """python.exe, not pythonw.exe: the job needs stdout. CREATE_NO_WINDOW hides the console."""
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe" and (exe.parent / "python.exe").exists():
        return str(exe.parent / "python.exe")
    return str(exe)


def analyze_commands(xml: Path, playlists: list[str], clap: bool, workers: int) -> list[list[str]]:
    """One `subjetunes.cli analyze` per playlist (the CLI documented in README.md)."""
    cmds = []
    for pl in playlists:
        c = [python_exe(), "-m", "subjetunes.cli", "analyze", "--xml", str(xml), "--playlist", pl,
             "--workers", str(workers)]
        if not clap:
            c.append("--no-clap")
        cmds.append(c)
    return cmds


def engine_installed() -> bool:
    return importlib.util.find_spec("subjetunes.cli") is not None


def save_json(path: Path, data: Any) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)
