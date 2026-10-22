"""Swing and tightness from onset micro-timing against a fitted beat grid.

- Beat grid: period from BPM (rekordbox's if available), phase fitted to the
  LOW band (kick drum) so the grid sits on the downbeats.
- Onsets: full band, high time resolution (hop 128 ≈ 5.8 ms at 22.05 kHz),
  refined with parabolic interpolation.
- beat_regularity: share of grid beats with a kick (low-band) onset within ±6% of a beat.
- swing_ratio: strength-weighted median position of off-beat onsets inside the
  beat (0.50 = straight 8ths, ~0.67 = triplet swing).
- microtiming_std: trimmed RMS spread of onsets around their own per-16th-slot
  median, in 16th-note units. Systematic swing is removed, only "looseness" remains.
Assumes 4/4. TODO: 3/4 detection.
"""
from __future__ import annotations

import numpy as np

from .. import dsp

N_FFT = 1024
HOP = 128
OFFBEAT_WINDOW = (0.42, 0.72)
MIN_OFFBEAT_ONSETS = 6
BEAT_TOLERANCE = 0.06


def _segment(y: np.ndarray, sr: int, bpm: float) -> dict:
    fps = sr / HOP
    S = dsp.stft_mag(y, N_FFT, HOP)
    low = dsp.onset_envelope(S, dsp.band_rows(sr, N_FFT, 20, 200))
    full = dsp.onset_envelope(S)

    period = 60.0 / bpm * fps                                  # frames per beat
    phi = dsp.beat_phase(low, period)

    scale = float(np.percentile(full, 99.5)) or 1.0
    env = full / scale
    idx = dsp.peak_pick(env, fps, min_gap_s=0.04, delta=0.05, floor=0.12)
    if idx.size < 4:
        return {"frac": np.array([]), "w": np.array([]), "ioi_reg": 0.0}
    pos = dsp.refine_peaks(env, idx)
    frac = ((pos - phi) / period) % 1.0
    w = env[idx]

    # beat regularity: share of grid beats with a low-band (kick) onset within ±6% of a beat
    low_n = low / (float(np.percentile(low, 99.5)) or 1.0)
    lidx = dsp.peak_pick(low_n, fps, min_gap_s=0.1, delta=0.05, floor=0.2)
    beats = np.arange(phi, env.size - 1, period)
    if beats.size and lidx.size:
        lpos = dsp.refine_peaks(low_n, lidx)
        dist = np.abs(lpos[None, :] - beats[:, None]).min(axis=1)
        reg = float(np.mean(dist < BEAT_TOLERANCE * period))
    else:
        reg = 0.0
    return {"frac": frac, "w": w, "ioi_reg": reg}


def compute(segments: list[np.ndarray], sr: int, bpm: float) -> dict:
    if not bpm or bpm <= 0:
        raise ValueError("swing.compute needs a BPM")
    per = [_segment(y, sr, bpm) for y in segments]
    frac = np.concatenate([p["frac"] for p in per]) if per else np.array([])
    w = np.concatenate([p["w"] for p in per]) if per else np.array([])

    # swing ratio
    lo, hi = OFFBEAT_WINDOW
    m = (frac >= lo) & (frac <= hi)
    swing_ratio = dsp.weighted_median(frac[m], w[m]) if m.sum() >= MIN_OFFBEAT_ONSETS else 0.5

    # micro-timing: deviation in 16th units around each slot's median offset
    raw = frac * 4.0
    k = np.round(raw)
    dev = raw - k
    slot = (k.astype(int)) % 4
    resid = []
    for s in range(4):
        d = dev[slot == s]
        if d.size >= 3:
            resid.append(d - np.median(d))
    if resid:
        r = np.concatenate(resid)
        r = r[np.abs(r) < 0.3]                     # drop onsets assigned to the wrong slot
        micro = float(np.sqrt(np.mean(r ** 2))) if r.size else 0.0
    else:
        micro = 0.0

    return {
        "swing_ratio": float(swing_ratio),
        "microtiming_std": float(micro),
        "beat_regularity": float(np.mean([p["ioi_reg"] for p in per])) if per else 0.0,
    }
