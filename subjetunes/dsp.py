"""Small numpy DSP toolkit (no librosa/scipy needed).

Everything here is deterministic and unit-tested on synthetic signals.
Frame i of an STFT is centred on sample i * hop.
"""
from __future__ import annotations

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view


def stft_mag(y: np.ndarray, n_fft: int = 2048, hop: int = 512) -> np.ndarray:
    """Magnitude spectrogram, shape (n_fft//2 + 1, n_frames)."""
    y = np.asarray(y, dtype=np.float32)
    pad = n_fft // 2
    y = np.pad(y, (pad, pad), mode="constant")
    if y.size < n_fft:
        y = np.pad(y, (0, n_fft - y.size))
    frames = sliding_window_view(y, n_fft)[::hop]
    win = np.hanning(n_fft).astype(np.float32)
    return np.abs(np.fft.rfft(frames * win, axis=1)).T.astype(np.float32)


def fft_freqs(sr: int, n_fft: int) -> np.ndarray:
    return np.fft.rfftfreq(n_fft, 1.0 / sr)


def onset_envelope(S: np.ndarray, rows: slice | np.ndarray | None = None, compression: float = 100.0) -> np.ndarray:
    """Spectral flux (half-wave rectified, log-compressed), averaged over the selected rows."""
    X = S if rows is None else S[rows]
    L = np.log1p(compression * X)
    flux = np.maximum(0.0, np.diff(L, axis=1))
    env = flux.mean(axis=0) if flux.shape[0] else np.zeros(flux.shape[1])
    return np.concatenate([[0.0], env]).astype(np.float32)


def band_rows(sr: int, n_fft: int, lo_hz: float, hi_hz: float) -> np.ndarray:
    f = fft_freqs(sr, n_fft)
    return np.where((f >= lo_hz) & (f < hi_hz))[0]


def _moving(x: np.ndarray, w: int, fn) -> np.ndarray:
    w = max(1, int(w))
    xp = np.pad(x, (w, w), mode="edge")
    return fn(sliding_window_view(xp, 2 * w + 1), axis=1)


def peak_pick(env: np.ndarray, fps: float, min_gap_s: float = 0.03, mean_win_s: float = 0.1,
              delta: float = 0.0, floor: float = 0.0) -> np.ndarray:
    """Indices of local maxima that rise `delta` above the local mean and exceed `floor`."""
    if env.size < 3:
        return np.array([], dtype=int)
    w_max = max(1, int(round(min_gap_s * fps)))
    local_max = _moving(env, w_max, np.max)
    local_mean = _moving(env, max(1, int(round(mean_win_s * fps))), np.mean)
    cand = np.where((env >= local_max) & (env > local_mean + delta) & (env > floor) & (env > 0))[0]
    # enforce minimum gap (keep the first of plateaus)
    out: list[int] = []
    for i in cand:
        if not out or i - out[-1] >= w_max:
            out.append(int(i))
    return np.array(out, dtype=int)


def refine_peaks(env: np.ndarray, idx: np.ndarray) -> np.ndarray:
    """Sub-frame peak positions by parabolic interpolation."""
    out = idx.astype(float).copy()
    for n, i in enumerate(idx):
        if 0 < i < env.size - 1:
            a, b, c = env[i - 1], env[i], env[i + 1]
            den = a - 2 * b + c
            if den != 0:
                out[n] = i + 0.5 * (a - c) / den
    return out


def estimate_tempo(env: np.ndarray, fps: float, lo_bpm: float = 70, hi_bpm: float = 180, prior_bpm: float = 122) -> float:
    """Autocorrelation tempo with a log-normal prior (reduces octave errors)."""
    x = env - env.mean()
    if not np.any(x):
        return prior_bpm
    ac = np.correlate(x, x, mode="full")[x.size - 1:]
    lags = np.arange(ac.size)
    bpm = np.where(lags > 0, 60.0 * fps / np.maximum(lags, 1), 0)
    mask = (bpm >= lo_bpm) & (bpm <= hi_bpm)
    if not mask.any():
        return prior_bpm
    weight = np.exp(-0.5 * (np.log2(np.maximum(bpm, 1e-6) / prior_bpm) / 0.9) ** 2)
    score = np.where(mask, ac * weight, -np.inf)
    lag = int(np.argmax(score))
    # parabolic refinement of the lag
    if 1 <= lag < ac.size - 1:
        a, b, c = ac[lag - 1], ac[lag], ac[lag + 1]
        den = a - 2 * b + c
        lagf = lag + (0.5 * (a - c) / den if den != 0 else 0.0)
    else:
        lagf = float(lag)
    return float(60.0 * fps / lagf)


def beat_phase(env: np.ndarray, period_frames: float, step: float = 0.25) -> float:
    """Phase (in frames, 0..period) of a fixed beat grid that best matches `env`."""
    if period_frames <= 1 or env.size < period_frames * 2:
        return 0.0
    xs = np.arange(env.size, dtype=float)
    best_phi, best = 0.0, -np.inf
    for phi in np.arange(0.0, period_frames, step):
        pos = np.arange(phi, env.size - 1, period_frames)
        s = np.interp(pos, xs, env).sum()
        if s > best:
            best, best_phi = s, float(phi)
    return best_phi


def frame_rms(y: np.ndarray, frame: int = 2048, hop: int = 512) -> np.ndarray:
    if y.size < frame:
        y = np.pad(y, (0, frame - y.size))
    fr = sliding_window_view(y.astype(np.float32), frame)[::hop]
    return np.sqrt(np.mean(fr ** 2, axis=1) + 1e-12)


def spectral_centroid(S: np.ndarray, sr: int, n_fft: int) -> np.ndarray:
    f = fft_freqs(sr, n_fft)[:, None]
    return (f * S).sum(axis=0) / (S.sum(axis=0) + 1e-9)


def spectral_flatness(S: np.ndarray) -> np.ndarray:
    P = S ** 2 + 1e-10
    return np.exp(np.mean(np.log(P), axis=0)) / np.mean(P, axis=0)


def robust_std(x: np.ndarray) -> float:
    x = np.asarray(x, dtype=float)
    if x.size == 0:
        return 0.0
    return float(1.4826 * np.median(np.abs(x - np.median(x))))


def weighted_median(values: np.ndarray, weights: np.ndarray) -> float:
    order = np.argsort(values)
    v, w = values[order], weights[order]
    c = np.cumsum(w)
    return float(v[np.searchsorted(c, 0.5 * c[-1])])
