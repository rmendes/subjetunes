"""Text-LLM judge (Ollama): judges a criterion from cached captions + features, without listening.

This is what makes "listen once, judge many times" work: new criteria are
answered in seconds from captions written earlier by the audio-LLM.
"""
from __future__ import annotations

import json
import time

from ..llm import OllamaClient
from ..models import Criterion, Judgment, Track
from .base import normalise_value, scale_prompt_anchors, schema_for

SYSTEM = ("You are a DJ's music librarian. You cannot hear audio; you get measured features (0..1 scales) and "
          "captions written by a model that listened to the track. Judge each track on the criterion. "
          "Reply only with JSON matching the schema.")


class TextLLMJudge:
    kind = "text_llm"

    def __init__(self, client: OllamaClient, model: str, summaries: dict[str, dict], batch_size: int = 5, num_ctx: int = 8192):
        self.client = client
        self.model = model
        self.summaries = summaries            # track_id -> compact dict (scores + caption [+ metadata])
        self.batch_size = batch_size
        self.num_ctx = num_ctx
        self.id = f"text_llm:{model}"
        self.version = "1"

    def supports(self, c: Criterion) -> bool:
        return not c.needs_audio

    def _schema(self, c: Criterion) -> dict:
        item = schema_for(c)
        item["properties"]["track_id"] = {"type": "string"}
        item["required"] = ["track_id", *item["required"]]
        return {"type": "object", "properties": {"results": {"type": "array", "items": item}}, "required": ["results"]}

    def judge(self, tracks: list[Track], c: Criterion, progress=None) -> list[Judgment]:
        todo = [t for t in tracks if t.track_id in self.summaries]
        out: list[Judgment] = []
        scale = f" Scale: {scale_prompt_anchors(c)}." if c.type == "scale" else ""
        choices = f" Choices: {', '.join(c.choices)}." if c.type == "choice" else ""
        for i in range(0, len(todo), self.batch_size):
            batch = todo[i:i + self.batch_size]
            t0 = time.time()
            user = json.dumps({
                "criterion": f"{c.name}. {c.description}{scale}{choices}",
                "answer_format": "value: integer 0-10 for scale, yes/no for binary, one choice for choice",
                "tracks": [self.summaries[t.track_id] for t in batch],
            }, ensure_ascii=False)
            try:
                data = self.client.chat_json(self.model, SYSTEM, user, self._schema(c), num_ctx=self.num_ctx)
            except Exception:
                data = {}
            ms = int((time.time() - t0) * 1000 / max(1, len(batch)))
            ids = {t.track_id for t in batch}
            for r in data.get("results", []) if isinstance(data, dict) else []:
                tid = str(r.get("track_id", ""))
                v = normalise_value(c, r.get("value"))
                if tid in ids and v is not None:
                    out.append(Judgment(tid, c.id, self.id, self.version, v, float(r.get("confidence", 0.5) or 0.5),
                                        str(r.get("evidence", ""))[:200], ms))
            if progress:
                progress(min(i + self.batch_size, len(todo)), len(todo))
        return out
