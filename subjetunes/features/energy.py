"""Energy features: loudness, onset density, spectral busyness across bands."""
from __future__ import annotations

import numpy as np

from .. import dsp

N_FFT = 2048
HOP = 512
DEFAULT_BANDS_HZ = [(20, 150), (150, 1000), (1000, 5000), (5000, 16000)]


def _segment(y: np.ndarray, sr: int, bands_hz) -> dict:
    fps = sr / HOP
    S = dsp.stft_mag(y, N_FFT, HOP)
    dur = max(1e-6, y.size / sr)

    full = dsp.onset_envelope(S)
    scale = float(np.percentile(full, 99)) or 1.0
    full_n = full / scale
    onsets = dsp.peak_pick(full_n, fps, min_gap_s=0.05, delta=0.05, floor=0.1)

    densities = []
    for lo, hi in bands_hz:
        rows = dsp.band_rows(sr, N_FFT, lo, min(hi, sr / 2))
        if rows.size == 0:
            densities.append(0.0)
            continue
        env = dsp.onset_envelope(S, rows) / scale       # same scale for every band -> comparable
        pk = dsp.peak_pick(env, fps, min_gap_s=0.05, delta=0.05, floor=0.1)
        densities.append(pk.size / dur)

    rms = dsp.frame_rms(y, N_FFT, HOP)
    return {
        "rms_db": float(20 * np.log10(rms.mean() + 1e-9)),
        "onset_rate": onsets.size / dur,
        "spectral_flux_mean": float(full.mean()),
        "band_onset_density": densities,
        "spectral_flatness_mean": float(dsp.spectral_flatness(S).mean()),
        "spectral_centroid_mean": float(dsp.spectral_centroid(S, sr, N_FFT).mean()),
        "_tempo": dsp.estimate_tempo(full, fps),
    }


def compute(segments: list[np.ndarray], sr: int, bpm_hint: float | None = None, bands_hz=None) -> dict:
    bands_hz = bands_hz or DEFAULT_BANDS_HZ
    per = [_segment(y, sr, bands_hz) for y in segments]
    out = {
        k: float(np.mean([p[k] for p in per]))
        for k in ("rms_db", "onset_rate", "spectral_flux_mean", "spectral_flatness_mean", "spectral_centroid_mean")
    }
    out["band_onset_density"] = [float(x) for x in np.mean([p["band_onset_density"] for p in per], axis=0)]
    out["bpm"] = float(bpm_hint) if bpm_hint else float(np.median([p["_tempo"] for p in per]))
    return out
