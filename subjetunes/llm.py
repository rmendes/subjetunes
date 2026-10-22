"""Ollama client and the bucket/tag proposer (text LLM; it cannot hear audio)."""
from __future__ import annotations

import json
import math
from typing import Any

import requests

from .models import Assignment, Bucket

SYSTEM_PROMPT = """You are a DJ's music librarian. You cannot hear audio. You receive measured
features on 0..1 scales (legend below), optional audio captions written by a listening model,
and track metadata. Assign each track to zero or more of the given buckets and propose up to 5
short lowercase tags. Base decisions on the numbers and captions first, metadata second. Do not
rely on knowledge of the artist unless the metadata clearly supports it. Give a one-sentence
rationale citing the numbers. Only use bucket ids from the list. Reply only with JSON matching the schema."""

FEATURE_LEGEND = {
    "energy": "0 = very low energy, 1 = highest in the library (BPM, onset density, loudness, spectral busyness)",
    "swing": "0 = straight, 1 = heavy triplet swing",
    "looseness": "0 = tightly quantised drums, 1 = loose / human timing",
    "brightness": "0 = dark/dull spectrum, 1 = bright",
    "bpm": "beats per minute (from rekordbox)",
    "mood_* / comp_*": "probability that the audio matches the named description (CLAP zero-shot)",
    "crit_*": "user-defined criteria scores, 0..1",
}

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "track_id": {"type": "string"},
                    "buckets": {"type": "array", "items": {"type": "string"}},
                    "tags": {"type": "array", "items": {"type": "string"}},
                    "confidence": {"type": "number"},
                    "rationale": {"type": "string"},
                },
                "required": ["track_id", "buckets", "tags", "confidence", "rationale"],
            },
        }
    },
    "required": ["results"],
}


class LLMError(RuntimeError):
    pass


class OllamaClient:
    def __init__(self, base_url: str = "http://localhost:11434", timeout: float = 300):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def health(self) -> tuple[bool, str]:
        try:
            r = requests.get(f"{self.base_url}/api/tags", timeout=3)
            r.raise_for_status()
            return True, f"{len(r.json().get('models', []))} models installed"
        except Exception as e:
            return False, f"Ollama not reachable at {self.base_url} ({e.__class__.__name__})"

    def list_models(self) -> list[str]:
        r = requests.get(f"{self.base_url}/api/tags", timeout=5)
        r.raise_for_status()
        return [m["name"] for m in r.json().get("models", [])]

    def loaded_models(self) -> list[dict[str, Any]]:
        r = requests.get(f"{self.base_url}/api/ps", timeout=5)
        r.raise_for_status()
        return r.json().get("models", [])

    def unload(self, model: str) -> None:
        requests.post(f"{self.base_url}/api/generate", json={"model": model, "keep_alive": 0}, timeout=30)

    def unload_all(self) -> list[str]:
        try:
            names = [m.get("name") or m.get("model") for m in self.loaded_models()]
        except Exception:
            return []
        for n in names:
            if n:
                self.unload(n)
        return [n for n in names if n]

    def chat_json(self, model: str, system: str, user: str, schema: dict, num_ctx: int = 8192,
                  temperature: float = 0.0, retries: int = 1) -> dict:
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        last_err = ""
        for _ in range(retries + 1):
            r = requests.post(
                f"{self.base_url}/api/chat",
                json={"model": model, "messages": messages, "format": schema, "stream": False,
                      "options": {"temperature": temperature, "num_ctx": num_ctx}},
                timeout=self.timeout,
            )
            r.raise_for_status()
            content = r.json().get("message", {}).get("content", "")
            try:
                return json.loads(content)
            except json.JSONDecodeError as e:
                last_err = str(e)
                messages += [{"role": "assistant", "content": content},
                             {"role": "user", "content": f"That was not valid JSON ({last_err}). Reply with JSON only."}]
        raise LLMError(f"Invalid JSON from model after retries: {last_err}")


# ---------------------------------------------------------------------------- prompt building

def _round(v: Any) -> Any:
    if isinstance(v, float):
        return None if math.isnan(v) else round(v, 2)
    return v


def track_summary(track_id: str, meta: dict, scores: dict, caption: dict | None = None, include_meta: bool = True) -> dict:
    s = {"track_id": track_id}
    if include_meta:
        s.update({k: meta.get(k, "") for k in ("artist", "name", "genre", "comments") if meta.get(k)})
    s.update({k: _round(v) for k, v in scores.items() if _round(v) is not None})
    if caption:
        s["caption"] = caption.get("summary") or caption
    return s


def build_user_message(buckets: list[Bucket], tracks: list[dict], fewshots: list[dict], candidates: dict[str, list[str]] | None) -> str:
    parts = {
        "buckets": [{"id": b.id, "name": b.name, "description": b.description} for b in buckets],
        "feature_legend": FEATURE_LEGEND,
        "examples_from_human_corrections": fewshots,
        "tracks": tracks,
    }
    if candidates:
        parts["allowed_buckets_per_track"] = candidates
    return json.dumps(parts, ensure_ascii=False)


def parse_results(data: dict, batch_ids: set[str], allowed: dict[str, set[str]]) -> dict[str, Assignment]:
    out: dict[str, Assignment] = {}
    for r in data.get("results", []) if isinstance(data, dict) else []:
        tid = str(r.get("track_id", ""))
        if tid not in batch_ids:
            continue
        ok = allowed.get(tid, set())
        buckets = [b for b in r.get("buckets", []) if isinstance(b, str) and b in ok]
        tags = [str(t).strip().lower()[:32] for t in r.get("tags", []) if str(t).strip()][:5]
        try:
            conf = float(r.get("confidence", 0.5))
        except (TypeError, ValueError):
            conf = 0.5
        conf = min(1.0, max(0.0, conf if conf <= 1 else conf / 100))   # some models answer 0..100
        out[tid] = Assignment(tid, buckets, tags, conf, str(r.get("rationale", ""))[:300], "llm")
    return out


def propose(
    client: OllamaClient,
    model: str,
    buckets: list[Bucket],
    summaries: list[dict],
    candidates: dict[str, list[str]],
    fewshots: list[dict],
    batch_size: int = 5,
    num_ctx: int = 8192,
    progress=None,
) -> tuple[dict[str, Assignment], list[str]]:
    """Ask the LLM about the LLM-enabled buckets. Returns (assignments, track_ids needing review)."""
    llm_buckets = [b for b in buckets if b.use_llm]
    todo = [s for s in summaries if s["track_id"] in candidates]
    out: dict[str, Assignment] = {}
    failed: list[str] = []
    for i in range(0, len(todo), batch_size):
        batch = todo[i:i + batch_size]
        ids = {s["track_id"] for s in batch}
        cand = {t: candidates[t] for t in ids}
        msg = build_user_message(llm_buckets, batch, fewshots, cand)
        try:
            data = client.chat_json(model, SYSTEM_PROMPT, msg, RESPONSE_SCHEMA, num_ctx=num_ctx)
            res = parse_results(data, ids, {t: set(c) for t, c in cand.items()})
        except Exception:
            res = {}
        out.update(res)
        failed += [t for t in ids if t not in res]
        if progress:
            progress(min(i + batch_size, len(todo)), len(todo))
    return out, failed


def fewshots_from_overrides(overrides: dict[str, dict], limit: int = 8) -> list[dict]:
    shots = []
    for tid, o in list(overrides.items())[:limit]:
        shots.append({"track": o.get("summary", {}), "human_buckets": o.get("buckets", []), "human_tags": o.get("tags", [])})
    return shots
