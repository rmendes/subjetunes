"""Turn raw features into 0..1 scores. Pure functions, no I/O, vectorised.

Changing any slider in the UI re-runs only this module, never the audio analysis.
"""
from __future__ import annotations

from typing import Callable

import numpy as np
import pandas as pd

RAW_COLUMNS = [
    "bpm", "rms_db", "onset_rate", "spectral_flux_mean", "spectral_flatness_mean",
    "spectral_centroid_mean", "swing_ratio", "microtiming_std", "beat_regularity",
]

DEFAULT_ENERGY_WEIGHTS = {
    "bpm": 0.25, "onset_rate": 0.20, "band_breadth": 0.20, "rms_db": 0.15,
    "spectral_flux_mean": 0.10, "spectral_flatness_mean": 0.10,
}


def features_frame(features: dict[str, dict]) -> pd.DataFrame:
    """{track_id: features_dict} -> DataFrame indexed by track_id (band densities expanded)."""
    rows = []
    for tid, f in features.items():
        r = {k: f.get(k, np.nan) for k in RAW_COLUMNS}
        for i, d in enumerate(f.get("band_onset_density") or []):
            r[f"band{i}_density"] = d
        r["track_id"] = tid
        r["error"] = f.get("error")
        rows.append(r)
    if not rows:
        return pd.DataFrame(columns=["track_id", *RAW_COLUMNS]).set_index("track_id")
    return pd.DataFrame(rows).set_index("track_id")


def percentile(series: pd.Series, reference: pd.Series | None = None) -> pd.Series:
    """Percentile rank 0..1. With `reference`, rank against that population instead."""
    s = series.astype(float)
    if reference is None:
        n = int(s.notna().sum())
        if n <= 1:
            return pd.Series(np.where(s.isna(), np.nan, 0.5), index=s.index)
        return (s.rank(method="average") - 1) / (n - 1)          # min -> 0, max -> 1
    ref = np.sort(reference.dropna().astype(float).to_numpy())
    if ref.size == 0:
        return pd.Series(np.nan, index=s.index)
    pos = np.searchsorted(ref, s.to_numpy(), side="right")
    out = pos / ref.size
    return pd.Series(np.where(s.isna(), np.nan, out), index=s.index)


def compute_scores(
    feats: pd.DataFrame,
    settings: dict,
    reference: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """feats: output of features_frame. reference: same shape, used for 'global' normalisation."""
    sc = settings.get("scoring", {})
    weights = {**DEFAULT_ENERGY_WEIGHTS, **(sc.get("energy_weights") or {})}
    band_thr = float(sc.get("band_active_onsets_per_s", 2.0))
    ref = reference if reference is not None else None

    out = pd.DataFrame(index=feats.index)
    band_cols = [c for c in feats.columns if c.startswith("band") and c.endswith("_density")]
    if band_cols:
        breadth = (feats[band_cols] >= band_thr).mean(axis=1)
        breadth_ref = (ref[band_cols] >= band_thr).mean(axis=1) if ref is not None and set(band_cols) <= set(ref.columns) else None
    else:
        breadth, breadth_ref = pd.Series(np.nan, index=feats.index), None

    pct: dict[str, pd.Series] = {}
    for col in ("bpm", "onset_rate", "rms_db", "spectral_flux_mean", "spectral_flatness_mean", "spectral_centroid_mean"):
        if col in feats:
            pct[col] = percentile(feats[col], ref[col] if ref is not None and col in ref else None)
    pct["band_breadth"] = percentile(breadth, breadth_ref)

    total_w = sum(max(0.0, float(w)) for w in weights.values()) or 1.0
    energy = sum(pct[k].fillna(0.5) * max(0.0, float(w)) for k, w in weights.items() if k in pct)
    out["energy"] = (energy / total_w).clip(0, 1)

    sr = feats.get("swing_ratio", pd.Series(np.nan, index=feats.index)).astype(float)
    out["swing"] = ((sr - 0.5) / 0.17).clip(0, 1)
    # looseness is absolute (not a percentile): a library of quantised techno should score low.
    full_scale = float(sc.get("looseness_full_scale", 0.08))      # 16th-note units; 0.08 ≈ 10 ms at 120 BPM
    mt = feats.get("microtiming_std", pd.Series(np.nan, index=feats.index)).astype(float)
    out["looseness"] = (mt / full_scale).clip(0, 1)
    out["brightness"] = pct.get("spectral_centroid_mean", pd.Series(np.nan, index=feats.index))
    out["bpm"] = feats.get("bpm", pd.Series(np.nan, index=feats.index))
    return out


def clap_axis_scores(
    embeddings: dict[str, np.ndarray],
    prompt_pairs: dict[str, list[str]],
    text_embed: Callable[[list[str]], np.ndarray],
    logit_scale: float = 100.0,
) -> pd.DataFrame:
    """For each name -> [positive, negative] prompt, P(positive) per track via softmax of cosine sims."""
    if not embeddings or not prompt_pairs:
        return pd.DataFrame(index=list(embeddings.keys()))
    ids = list(embeddings.keys())
    A = np.stack([embeddings[i] for i in ids]).astype(np.float32)
    A /= np.linalg.norm(A, axis=1, keepdims=True) + 1e-9
    names = list(prompt_pairs.keys())
    prompts = [p for n in names for p in prompt_pairs[n][:2]]
    T = np.asarray(text_embed(prompts), dtype=np.float32)
    T /= np.linalg.norm(T, axis=1, keepdims=True) + 1e-9
    sims = A @ T.T * logit_scale                                   # (n_tracks, 2*n_axes)
    out = {}
    for j, n in enumerate(names):
        pos, neg = sims[:, 2 * j], sims[:, 2 * j + 1]
        out[n] = 1.0 / (1.0 + np.exp(-np.clip(pos - neg, -50, 50)))   # 2-way softmax
    return pd.DataFrame(out, index=ids)
