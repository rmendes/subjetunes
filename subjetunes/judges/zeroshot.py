"""Zero-shot judge: compare cached CLAP audio embeddings with the criterion's text prompts."""
from __future__ import annotations

from typing import Callable

import numpy as np

from ..models import Criterion, Judgment, Track
from ..scoring import clap_axis_scores


class ZeroShotJudge:
    kind = "zeroshot"

    def __init__(self, embeddings: dict[str, np.ndarray], text_embed: Callable[[list[str]], np.ndarray], model_id: str):
        self.embeddings = embeddings          # keyed by track_id
        self.text_embed = text_embed
        self.id = f"zeroshot:{model_id}"
        self.version = "1"

    def supports(self, c: Criterion) -> bool:
        return c.type in ("scale", "binary") and len(c.clap_prompts) >= 2

    def judge(self, tracks: list[Track], c: Criterion, progress=None) -> list[Judgment]:
        emb = {t.track_id: self.embeddings[t.track_id] for t in tracks if t.track_id in self.embeddings}
        if not emb:
            return []
        df = clap_axis_scores(emb, {c.id: c.clap_prompts[:2]}, self.text_embed)
        out = []
        for tid, p in df[c.id].items():
            out.append(Judgment(tid, c.id, self.id, self.version, float(p), float(abs(p - 0.5) * 2)))
        return out
