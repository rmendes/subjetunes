"""Orchestration: analyse (cached, parallel) -> table of scores -> bucket proposals."""
from __future__ import annotations

import os
import traceback
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import asdict
from datetime import datetime
from typing import Any, Callable

import numpy as np
import pandas as pd

from . import audio, buckets as bk, llm, scoring
from .config import buckets_from, criteria_from
from .features import energy, swing
from .models import Assignment, Features, Library, Track

ANALYZER_VERSION = "1"
Progress = Callable[[int, int, str], None] | None


# ----------------------------------------------------------------------------- analysis

def analyze_file(path: str, bpm_hint: float | None, duration_s: float | None, audio_cfg: dict) -> dict:
    """Runs in a worker process. Never raises: errors are returned in Features.error."""
    now = datetime.now().isoformat(timespec="seconds")
    try:
        size, mtime = audio.file_stat(path)
    except OSError as e:
        return asdict(Features(path, 0, 0.0, now, ANALYZER_VERSION, error=f"stat: {e}"))
    try:
        sr = int(audio_cfg.get("sr", 22050))
        segs = audio.load_excerpts(path, sr, audio_cfg.get("positions", [0.25, 0.5, 0.75]),
                                   float(audio_cfg.get("segment_s", 30)), duration_s)
        e = energy.compute(segs, sr, bpm_hint, audio_cfg.get("bands_hz"))
        s = swing.compute(segs, sr, e["bpm"])
        return asdict(Features(path, size, mtime, now, ANALYZER_VERSION, **e, **s))
    except Exception as ex:
        return asdict(Features(path, size, mtime, now, ANALYZER_VERSION, error=f"{ex.__class__.__name__}: {ex}"[:300]))


def needs_analysis(tracks: list[Track], cache) -> list[Track]:
    todo = []
    for t in tracks:
        try:
            size, mtime = audio.file_stat(t.path)
        except OSError:
            todo.append(t)
            continue
        if cache.get_features(t.path, size, mtime, ANALYZER_VERSION) is None:
            todo.append(t)
    return todo


def analyze_tracks(tracks: list[Track], cfg: dict, cache, progress: Progress = None, workers: int | None = None) -> dict:
    todo = needs_analysis(tracks, cache)
    total, done, errors = len(todo), 0, 0
    if progress:
        progress(0, total, f"{len(tracks) - total} cached, {total} to analyse")
    if not todo:
        return {"analysed": 0, "cached": len(tracks), "errors": 0}
    workers = workers or int(cfg.get("analysis", {}).get("workers") or max(1, (os.cpu_count() or 4) - 4))
    audio_cfg = cfg.get("audio", {})
    if workers <= 1:
        results = (analyze_file(t.path, t.bpm, t.duration_s, audio_cfg) for t in todo)
        for f in results:
            done += 1
            errors += bool(f.get("error"))
            if f.get("file_size"):
                cache.put_features(Features(**f))
            if progress:
                progress(done, total, os.path.basename(f["path"]))
    else:
        with ProcessPoolExecutor(max_workers=workers) as ex:
            futs = [ex.submit(analyze_file, t.path, t.bpm, t.duration_s, audio_cfg) for t in todo]
            for fut in as_completed(futs):
                f = fut.result()
                done += 1
                errors += bool(f.get("error"))
                if f.get("file_size"):
                    cache.put_features(Features(**f))
                if progress:
                    progress(done, total, os.path.basename(f["path"]))
    return {"analysed": total, "cached": len(tracks) - total, "errors": errors}


def embed_tracks(tracks: list[Track], cfg: dict, cache, progress: Progress = None) -> dict:
    """CLAP audio embeddings (main process, CPU). Skips if torch/transformers are missing."""
    from .features import clap

    ok, msg = clap.available()
    if not ok:
        return {"embedded": 0, "skipped": len(tracks), "message": msg}
    ccfg = cfg.get("clap", {})
    model_id = ccfg.get("model_id", "laion/larger_clap_music")
    emb = clap.ClapEmbedder.get(model_id, ccfg.get("threads"))
    acfg = cfg.get("audio", {})
    todo = []
    for t in tracks:
        try:
            size, mtime = audio.file_stat(t.path)
        except OSError:
            continue
        if cache.get_embedding(t.path, model_id, size, mtime) is None:
            todo.append((t, size, mtime))
    errors = 0
    for n, (t, size, mtime) in enumerate(todo):
        try:
            segs = audio.load_excerpts(t.path, clap.CLAP_SR, acfg.get("positions", [0.25, 0.5, 0.75]),
                                       float(ccfg.get("segment_s", 10)), t.duration_s)
            cache.put_embedding(t.path, model_id, size, mtime, emb.audio_embedding(segs))
        except Exception:
            errors += 1
            traceback.print_exc()
        if progress:
            progress(n + 1, len(todo), f"CLAP {os.path.basename(t.path)}")
    return {"embedded": len(todo) - errors, "errors": errors, "model_id": model_id}


# ----------------------------------------------------------------------------- table

def track_ids_for(library: Library, playlist_paths: list[str]) -> list[str]:
    ids: list[str] = []
    seen: set[str] = set()
    for p in playlist_paths:
        pl = library.playlist(p)
        for tid in (pl.track_ids if pl else []):
            if tid not in seen:
                seen.add(tid)
                ids.append(tid)
    return ids


def features_by_track(library: Library, track_ids: list[str], cache) -> dict[str, dict]:
    paths = {library.tracks[t].path: t for t in track_ids if t in library.tracks}
    feats = cache.all_features(list(paths))
    return {paths[p]: asdict(f) for p, f in feats.items() if not f.error}


def embeddings_by_track(library: Library, track_ids: list[str], cache, model_id: str) -> dict[str, np.ndarray]:
    paths = {library.tracks[t].path: t for t in track_ids if t in library.tracks}
    return {paths[p]: v for p, v in cache.embeddings_for(list(paths), model_id).items()}


def criteria_columns(track_ids: list[str], cfg: dict, cache) -> pd.DataFrame:
    """crit_<id> columns from stored judgments, using the judge chosen per criterion (else the latest)."""
    chosen = cfg.get("criteria_judge", {}) or {}
    cols: dict[str, dict[str, Any]] = {}
    wanted = set(track_ids)
    for c in criteria_from(cfg):
        js = cache.get_judgments(c.id, chosen.get(c.id))
        vals = {j.track_id: j.value for j in js if j.track_id in wanted}
        for tid, lab in cache.get_labels(c.id).items():          # your own labels always win
            if tid in wanted:
                vals[tid] = lab["value"]
        if vals:
            cols[f"crit_{c.id}"] = vals
    return pd.DataFrame(cols, index=track_ids) if cols else pd.DataFrame(index=track_ids)


def build_table(
    library: Library,
    track_ids: list[str],
    cache,
    cfg: dict,
    text_embed: Callable[[list[str]], np.ndarray] | None = None,
) -> pd.DataFrame:
    meta = pd.DataFrame(
        [{"track_id": t, "artist": library.tracks[t].artist, "name": library.tracks[t].name,
          "genre": library.tracks[t].genre, "key": library.tracks[t].key, "path": library.tracks[t].path}
         for t in track_ids if t in library.tracks]
    ).set_index("track_id") if track_ids else pd.DataFrame()
    feats = scoring.features_frame(features_by_track(library, track_ids, cache))
    ref = None
    if cfg.get("scoring", {}).get("normalisation", "job") == "global":
        ref = scoring.features_frame({f.path: asdict(f) for f in cache.all_features().values() if not f.error})
    scores = scoring.compute_scores(feats, cfg, ref)
    table = meta.join(scores, how="left")
    for raw in ("swing_ratio", "microtiming_std", "onset_rate", "rms_db"):
        if raw in feats:
            table[raw] = feats[raw]

    ccfg = cfg.get("clap", {})
    if text_embed is not None:
        emb = embeddings_by_track(library, track_ids, cache, ccfg.get("model_id", "laion/larger_clap_music"))
        if emb:
            mood = scoring.clap_axis_scores(emb, ccfg.get("mood_axes", {}), text_embed).add_prefix("mood_")
            comp = scoring.clap_axis_scores(emb, ccfg.get("components", {}), text_embed).add_prefix("comp_")
            table = table.join(mood, how="left").join(comp, how="left")
    # make every configured column exist (NaN) so rules referencing them validate
    for name in ccfg.get("mood_axes", {}):
        if f"mood_{name}" not in table:
            table[f"mood_{name}"] = np.nan
    for name in ccfg.get("components", {}):
        if f"comp_{name}" not in table:
            table[f"comp_{name}"] = np.nan
    crit = criteria_columns(list(table.index), cfg, cache)
    if not crit.empty:
        table = table.join(crit, how="left")
    for c in criteria_from(cfg):
        if f"crit_{c.id}" not in table:
            table[f"crit_{c.id}"] = np.nan
    table["analysed"] = table.index.isin(feats.index)
    return table


RULE_EXCLUDE = {"artist", "name", "genre", "key", "path", "analysed"}


def rule_columns(table: pd.DataFrame) -> list[str]:
    return [c for c in table.columns if c not in RULE_EXCLUDE]


# ----------------------------------------------------------------------------- proposals

def summaries(library: Library, table: pd.DataFrame, cache, include_meta: bool = True) -> dict[str, dict]:
    caps = cache.get_captions([library.tracks[t].path for t in table.index if t in library.tracks])
    out = {}
    for tid, row in table.iterrows():
        t = library.tracks[tid]
        scores = {k: row[k] for k in rule_columns(table) if k in row and not isinstance(row[k], str)}
        scores.update({k: row[k] for k in rule_columns(table) if isinstance(row.get(k), str)})
        out[tid] = llm.track_summary(tid, {"artist": t.artist, "name": t.name, "genre": t.genre, "comments": t.comments},
                                     scores, caps.get(t.path), include_meta)
    return out


def propose(
    library: Library,
    table: pd.DataFrame,
    cfg: dict,
    cache,
    use_llm: bool = False,
    client: llm.OllamaClient | None = None,
    progress=None,
) -> dict[str, Any]:
    buckets = buckets_from(cfg)
    analysed = table[table["analysed"]] if "analysed" in table else table
    env = analysed[rule_columns(analysed)]
    rule_asg, errors = bk.rule_assignments(env, buckets)
    matches, _ = bk.rule_matches(env, buckets)
    llm_asg: dict[str, Assignment] = {}
    needs_review: list[str] = []
    overrides = cache.get_overrides()
    if use_llm and client is not None:
        cands = {t: c for t, c in bk.llm_candidates(matches, buckets).items() if t not in overrides}
        lcfg = cfg.get("llm", {})
        llm_asg, needs_review = llm.propose(
            client, lcfg.get("model", "qwen2.5-coder:14b-instruct-q5_K_M"), buckets,
            list(summaries(library, analysed.loc[list(cands)], cache).values()) if cands else [],
            cands, llm.fewshots_from_overrides(overrides, int(lcfg.get("fewshots", 8))),
            int(lcfg.get("batch_size", 5)), int(lcfg.get("num_ctx", 8192)), progress,
        )
    merged = bk.merge(rule_asg, llm_asg, overrides)
    return {"assignments": merged, "rule_errors": errors, "needs_review": needs_review}
