"""Audio loading.

Decoder order: librosa (if installed) -> ffmpeg on PATH -> stdlib `wave` (WAV only).
We never analyze whole tracks: we take a few excerpts spread across the track,
which skips intros/outros and is several times faster.
"""
from __future__ import annotations

import io
import os
import shutil
import subprocess
import wave
from pathlib import Path

import numpy as np


class AudioError(RuntimeError):
    pass


def file_stat(path: str) -> tuple[int, float]:
    st = os.stat(path)
    return st.st_size, st.st_mtime


def _has_librosa() -> bool:
    try:
        import librosa  # noqa: F401
        return True
    except Exception:
        return False


def _ffmpeg() -> str | None:
    return shutil.which("ffmpeg")


def _ffprobe() -> str | None:
    return shutil.which("ffprobe")


def get_duration(path: str) -> float:
    if _has_librosa():
        import librosa
        try:
            return float(librosa.get_duration(path=path))
        except Exception:
            pass
    if _ffprobe():
        out = subprocess.run(
            [_ffprobe(), "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", path],
            capture_output=True, text=True,
        )
        try:
            return float(out.stdout.strip())
        except ValueError:
            pass
    if path.lower().endswith(".wav"):
        with wave.open(path, "rb") as w:
            return w.getnframes() / float(w.getframerate())
    raise AudioError(f"Cannot read duration (install ffmpeg or librosa): {path}")


def _resample(y: np.ndarray, sr_in: int, sr_out: int) -> np.ndarray:
    if sr_in == sr_out or y.size == 0:
        return y
    n_out = int(round(y.size * sr_out / sr_in))
    x_old = np.linspace(0.0, 1.0, num=y.size, endpoint=False)
    x_new = np.linspace(0.0, 1.0, num=n_out, endpoint=False)
    return np.interp(x_new, x_old, y).astype(np.float32)   # linear; good enough for analysis


def load_segment(path: str, sr: int, offset_s: float, duration_s: float) -> np.ndarray:
    """Mono float32 in [-1, 1] at `sr`."""
    if not Path(path).exists():
        raise AudioError(f"File not found: {path}")
    if _has_librosa():
        import librosa
        try:
            y, _ = librosa.load(path, sr=sr, mono=True, offset=max(0.0, offset_s), duration=duration_s)
            return y.astype(np.float32)
        except Exception as e:                       # fall through to ffmpeg
            last = e
    else:
        last = None
    if _ffmpeg():
        cmd = [
            _ffmpeg(), "-v", "error", "-ss", f"{max(0.0, offset_s):.3f}", "-t", f"{duration_s:.3f}",
            "-i", path, "-ac", "1", "-ar", str(sr), "-f", "f32le", "-",
        ]
        out = subprocess.run(cmd, capture_output=True)
        if out.returncode == 0:
            return np.frombuffer(out.stdout, dtype=np.float32).copy()
        last = AudioError(out.stderr.decode(errors="replace")[:300])
    if path.lower().endswith(".wav"):
        with wave.open(path, "rb") as w:
            sr_in, ch, width = w.getframerate(), w.getnchannels(), w.getsampwidth()
            w.setpos(min(w.getnframes(), int(offset_s * sr_in)))
            raw = w.readframes(int(duration_s * sr_in))
        if width != 2:
            raise AudioError("Only 16-bit WAV supported without ffmpeg/librosa")
        y = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        if ch > 1:
            y = y.reshape(-1, ch).mean(axis=1)
        return _resample(y, sr_in, sr)
    raise AudioError(f"Cannot decode {path}: {last or 'install ffmpeg or librosa'}")


def excerpt_offsets(duration_s: float, positions: list[float], seg_len_s: float) -> list[float]:
    """Start offsets for excerpts centred on relative `positions` (0..1)."""
    if duration_s <= seg_len_s:
        return [0.0]
    out = []
    for p in positions:
        start = p * duration_s - seg_len_s / 2
        out.append(float(min(max(0.0, start), duration_s - seg_len_s)))
    return out


def load_excerpts(path: str, sr: int, positions: list[float], seg_len_s: float, duration_s: float | None = None) -> list[np.ndarray]:
    dur = duration_s or get_duration(path)
    segs = [load_segment(path, sr, off, seg_len_s) for off in excerpt_offsets(dur, positions, seg_len_s)]
    segs = [s for s in segs if s.size > sr]          # drop empty/too-short reads
    if not segs:
        raise AudioError(f"No audio decoded from {path}")
    return segs


def wav_bytes(y: np.ndarray, sr: int) -> bytes:
    """16-bit PCM mono WAV in memory (for the audio-LLM)."""
    pcm = (np.clip(y, -1.0, 1.0) * 32767.0).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()
