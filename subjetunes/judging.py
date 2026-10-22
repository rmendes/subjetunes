"""Builds judges from config and runs them (shared by the CLI and the UI)."""
from __future__ import annotations

from typing import Any, Callable

import numpy as np

from . import models_registry, pipeline
from .config import criteria_from
from .judges.audio_llm import AudioLLMJudge, LlamaServerClient
from .judges.probe import ProbeJudge
from .judges.text_llm import TextLLMJudge
from .judges.zeroshot import ZeroShotJudge
from .llm import OllamaClient
from .models import Criterion, Library


def text_embed_fn(cfg: dict) -> Callable[[list[str]], np.ndarray] | None:
    """CLAP text encoder if torch/transformers are installed, else None (mood/comp columns stay empty)."""
    from .features import clap

    ok, _ = clap.available()
    if not ok:
        return None
    ccfg = cfg.get("clap", {})
    emb = clap.ClapEmbedder.get(ccfg.get("model_id", "laion/larger_clap_music"), ccfg.get("threads"))
    return emb.text_embeddings


def criterion(cfg: dict, cid: str) -> Criterion:
    for c in criteria_from(cfg):
        if c.id == cid:
            return c
    raise KeyError(f"Unknown criterion '{cid}'. Defined: {[c.id for c in criteria_from(cfg)]}")


def labels_for(cache, cid: str) -> dict[str, Any]:
    return {t: v["value"] for t, v in cache.get_labels(cid).items()}


def cheap_judges(cfg: dict, library: Library, track_ids: list[str], cache, c: Criterion) -> list:
    """Zero-shot (if prompts and CLAP available) + probe (if enough labels). Both CPU, fast."""
    out: list = []
    model_id = cfg.get("clap", {}).get("model_id", "laion/larger_clap_music")
    emb = pipeline.embeddings_by_track(library, track_ids, cache, model_id)
    if not emb:
        return out
    te = text_embed_fn(cfg)
    if te is not None:
        z = ZeroShotJudge(emb, te, model_id)
        if z.supports(c):
            out.append(z)
    p = ProbeJudge(emb, labels_for(cache, c.id), model_id)
    if p.supports(c):
        out.append(p)
    return out


def text_llm_judge(cfg: dict, library: Library, track_ids: list[str], cache) -> TextLLMJudge:
    lcfg = cfg.get("llm", {})
    table = pipeline.build_table(library, track_ids, cache, cfg, text_embed_fn(cfg))
    summ = pipeline.summaries(library, table, cache, include_meta=False)
    return TextLLMJudge(OllamaClient(lcfg.get("ollama_url", "http://localhost:11434")),
                        lcfg.get("model", "qwen2.5-coder:14b-instruct-q5_K_M"), summ,
                        int(lcfg.get("batch_size", 5)), int(lcfg.get("num_ctx", 8192)))


def audio_llm_judge(cfg: dict, model_id: str, cache, port: int = 8080) -> AudioLLMJudge:
    entry = models_registry.get(model_id)
    if not entry or entry.get("kind") != "audio_llm":
        raise KeyError(f"'{model_id}' is not an audio_llm in config/models.yaml")
    return AudioLLMJudge(LlamaServerClient(f"http://127.0.0.1:{port}"), entry, cfg.get("audio", {}), cache)


def uncertain_tracks(cache, cid: str, track_ids: list[str], band: tuple[float, float] = (0.35, 0.65)) -> list[str]:
    """Cascade: tracks whose cheap-judge result is missing, unsure, or where cheap judges disagree."""
    js = [j for j in cache.get_judgments(cid) if j.judge_id.split(":")[0] in ("probe", "zeroshot")]
    by_track: dict[str, list] = {}
    for j in js:
        by_track.setdefault(j.track_id, []).append(j)
    out = []
    for t in track_ids:
        got = by_track.get(t, [])
        if not got:
            out.append(t)
            continue
        vals = [g.value for g in got]
        if all(isinstance(v, float) for v in vals):
            if any(band[0] <= v <= band[1] for v in vals) or (max(vals) - min(vals) > 0.4):
                out.append(t)
        elif len(set(vals)) > 1 or any(g.confidence < 0.5 for g in got):
            out.append(t)
    return out
