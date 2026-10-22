"""CLAP audio/text embeddings (torch + transformers, CPU).

Audio embeddings are expensive -> cached per (path, model_id) in SQLite.
Text embeddings are cheap -> computed on demand and memoised, so editing
prompts in the UI never requires re-analysing audio.

Uses the processor's feature_extractor/tokenizer directly to avoid the
`audios=` vs `audio=` kwarg change between transformers versions.
"""
from __future__ import annotations

import os
import threading

import numpy as np

CLAP_SR = 48000
_lock = threading.Lock()
_instances: dict[str, "ClapEmbedder"] = {}


def available() -> tuple[bool, str]:
    try:
        import torch  # noqa: F401
        import transformers  # noqa: F401
        return True, "ok"
    except Exception as e:  # pragma: no cover - depends on install
        return False, f"torch/transformers not installed ({e.__class__.__name__}). See requirements-ml.txt"


def _as_tensor(out):
    """get_*_features returns a tensor in transformers 4.x; newer versions may return an output object."""
    if hasattr(out, "detach"):
        return out
    for attr in ("pooler_output", "audio_embeds", "text_embeds", "last_hidden_state"):
        if getattr(out, attr, None) is not None:
            return getattr(out, attr)
    return out[0]


class ClapEmbedder:
    def __init__(self, model_id: str = "laion/larger_clap_music", threads: int | None = None):
        import torch
        from transformers import ClapModel, ClapProcessor

        if threads:
            torch.set_num_threads(int(threads))
        self.torch = torch
        self.model_id = model_id
        self.model = ClapModel.from_pretrained(model_id).eval()
        self.processor = ClapProcessor.from_pretrained(model_id)
        self._text_cache: dict[str, np.ndarray] = {}

    @classmethod
    def get(cls, model_id: str, threads: int | None = None) -> "ClapEmbedder":
        with _lock:
            if model_id not in _instances:
                _instances[model_id] = cls(model_id, threads or max(1, (os.cpu_count() or 4) - 4))
            return _instances[model_id]

    def audio_embedding(self, segments_48k: list[np.ndarray]) -> np.ndarray:
        """Mean of per-segment embeddings, L2-normalised."""
        fe = self.processor.feature_extractor
        inputs = fe([s.astype(np.float32) for s in segments_48k], sampling_rate=CLAP_SR, return_tensors="pt")
        with self.torch.no_grad():
            emb = _as_tensor(self.model.get_audio_features(**inputs)).cpu().numpy()
        emb /= np.linalg.norm(emb, axis=1, keepdims=True) + 1e-9
        v = emb.mean(axis=0)
        return (v / (np.linalg.norm(v) + 1e-9)).astype(np.float32)

    def text_embeddings(self, prompts: list[str]) -> np.ndarray:
        missing = [p for p in dict.fromkeys(prompts) if p not in self._text_cache]
        if missing:
            tok = self.processor.tokenizer(missing, padding=True, return_tensors="pt")
            with self.torch.no_grad():
                emb = _as_tensor(self.model.get_text_features(**tok)).cpu().numpy()
            emb /= np.linalg.norm(emb, axis=1, keepdims=True) + 1e-9
            for p, e in zip(missing, emb):
                self._text_cache[p] = e.astype(np.float32)
        return np.stack([self._text_cache[p] for p in prompts])
