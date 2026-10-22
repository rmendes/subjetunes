"""Audio-LLM judge: a model that listens (Qwen2.5-Omni etc.) served by llama-server.

Audio input in llama.cpp is marked experimental: run `python -m subjetunes.cli smoke-audio`
after every llama.cpp update.
"""
from __future__ import annotations

import base64
import json
import re
import statistics
import time
from collections import Counter

import requests

from .. import audio
from ..models import Criterion, Judgment, Track
from .base import normalise_value, scale_prompt_anchors, schema_for

JUDGE_SYSTEM = "You are an expert DJ and music critic. You judge only what you hear in the audio. Reply with JSON only."

CAPTION_SCHEMA = {
    "type": "object",
    "properties": {
        "instruments": {"type": "array", "items": {"type": "string"}},
        "vocals": {"type": "object", "properties": {"present": {"type": "boolean"}, "role": {"type": "string"}}},
        "drums": {"type": "object", "properties": {"style": {"type": "string"}, "feel": {"type": "string", "enum": ["tight", "loose", "swung", "none"]}}},
        "bass": {"type": "string"},
        "texture": {"type": "string"},
        "mood_words": {"type": "array", "items": {"type": "string"}},
        "production": {"type": "object", "properties": {"analog_vs_digital": {"type": "string"}, "era_guess": {"type": "string"}}},
        "summary": {"type": "string"},
    },
    "required": ["instruments", "vocals", "drums", "mood_words", "summary"],
}

CAPTION_PROMPT = (
    "Describe this {seconds}-second excerpt of a track from a DJ's library for a music librarian. "
    "List the instruments you hear, whether there are vocals and what role they play, the drum style and feel "
    "(tight, loose, swung or none), the bass, the overall texture, 3-6 mood words, and the production character "
    "(analog vs digital, era guess). End with a summary of at most 40 words. Reply with JSON."
)


def criterion_prompt(c: Criterion, seconds: int, meta: dict | None = None) -> str:
    lines = [f"You are listening to a {seconds}-second excerpt of a track from a DJ's library.",
             f"Criterion: {c.name}. {c.description}".strip()]
    if c.type == "scale":
        lines.append(f"Scale: {scale_prompt_anchors(c)}.")
        lines.append('Reply with JSON: {"value": <integer 0-10>, "confidence": <0-1>, "evidence": "<max 20 words about what you hear>"}')
    elif c.type == "binary":
        lines.append('Reply with JSON: {"value": "yes" or "no", "confidence": <0-1>, "evidence": "<max 20 words>"}')
    else:
        lines.append(f"Choose exactly one of: {', '.join(c.choices)}.")
        lines.append('Reply with JSON: {"value": "<one choice>", "confidence": <0-1>, "evidence": "<max 20 words>"}')
    if meta:
        lines.append(f"Metadata (may be wrong): {json.dumps(meta, ensure_ascii=False)}")
    lines.append("Judge only what you hear.")
    return "\n".join(lines)


def _extract_json(text: str) -> dict:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            raise
        return json.loads(m.group(0))


class LlamaServerClient:
    def __init__(self, base_url: str = "http://127.0.0.1:8080", timeout: float = 600):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def chat_audio(self, prompt: str, wav: bytes, schema: dict | None, system: str = JUDGE_SYSTEM,
                   max_tokens: int = 400) -> dict:
        content = [
            {"type": "text", "text": prompt},
            {"type": "input_audio", "input_audio": {"data": base64.b64encode(wav).decode("ascii"), "format": "wav"}},
        ]
        body = {"messages": [{"role": "system", "content": system}, {"role": "user", "content": content}],
                "temperature": 0, "max_tokens": max_tokens}
        if schema:
            body["response_format"] = {"type": "json_schema", "json_schema": {"name": "answer", "schema": schema}}
        r = requests.post(f"{self.base_url}/v1/chat/completions", json=body, timeout=self.timeout)
        if r.status_code == 400 and schema:                  # build without schema support for multimodal
            body.pop("response_format")
            r = requests.post(f"{self.base_url}/v1/chat/completions", json=body, timeout=self.timeout)
        r.raise_for_status()
        text = r.json()["choices"][0]["message"]["content"]
        return _extract_json(text)


def aggregate(c: Criterion, values: list) -> float | str | None:
    vals = [v for v in values if v is not None]
    if not vals:
        return None
    if c.type == "choice":
        return Counter(vals).most_common(1)[0][0]
    return float(statistics.median(vals))


class AudioLLMJudge:
    kind = "audio_llm"

    def __init__(self, client: LlamaServerClient, entry: dict, audio_cfg: dict, cache=None):
        self.client = client
        self.entry = entry
        self.id = f"audio_llm:{entry['id']}"
        self.version = str(entry.get("version", "1"))
        self.sr = int(entry.get("sample_rate", 16000))
        self.clip_s = float(entry.get("max_clip_s", 25))
        self.positions = audio_cfg.get("judge_positions", [0.3, 0.55, 0.8])
        self.cache = cache

    def supports(self, c: Criterion) -> bool:
        return True

    def clips(self, t: Track) -> list[bytes]:
        segs = audio.load_excerpts(t.path, self.sr, self.positions, self.clip_s, t.duration_s)
        return [audio.wav_bytes(s, self.sr) for s in segs]

    def judge(self, tracks: list[Track], c: Criterion, progress=None) -> list[Judgment]:
        out = []
        schema = schema_for(c)
        for n, t in enumerate(tracks):
            t0 = time.time()
            meta = {"artist": t.artist, "title": t.name, "genre": t.genre} if c.use_metadata else None
            vals, confs, ev = [], [], []
            try:
                for wav in self.clips(t):
                    ans = self.client.chat_audio(criterion_prompt(c, int(self.clip_s), meta), wav, schema)
                    vals.append(normalise_value(c, ans.get("value")))
                    confs.append(float(ans.get("confidence", 0.5) or 0.5))
                    ev.append(str(ans.get("evidence", ""))[:120])
            except Exception as e:
                ev.append(f"error: {e.__class__.__name__}: {str(e)[:120]}")
            v = aggregate(c, vals)
            if v is not None:
                out.append(Judgment(t.track_id, c.id, self.id, self.version, v,
                                    float(statistics.mean(confs)) if confs else 0.5, " | ".join(ev)[:400],
                                    int((time.time() - t0) * 1000)))
            if progress:
                progress(n + 1, len(tracks))
        return out

    def caption(self, t: Track) -> dict:
        """One caption from the middle clip; cached by the caller."""
        clips = self.clips(t)
        wav = clips[len(clips) // 2]
        return self.client.chat_audio(CAPTION_PROMPT.format(seconds=int(self.clip_s)), wav, CAPTION_SCHEMA, max_tokens=600)
