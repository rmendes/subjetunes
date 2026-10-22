"""Judge interface.

A judge turns tracks into Judgments for one user-defined Criterion.
Value conventions:
  scale  -> float 0..1
  binary -> float 0..1 (probability of "yes"); labels are 0 or 1
  choice -> str, one of criterion.choices
"""
from __future__ import annotations

from typing import Protocol

from ..models import Criterion, Judgment, Track


class Judge(Protocol):
    id: str
    version: str
    kind: str            # "zeroshot" | "probe" | "audio_llm" | "text_llm"

    def supports(self, criterion: Criterion) -> bool: ...

    def judge(self, tracks: list[Track], criterion: Criterion, progress=None) -> list[Judgment]: ...


def scale_prompt_anchors(c: Criterion) -> str:
    """'0 = calm; 5 = ...; 10 = ...' from anchors keyed '0.0', '0.5', '1.0' (0..1 scale)."""
    parts = []
    for k in sorted(c.anchors, key=lambda x: float(x)):
        parts.append(f"{round(float(k) * 10)} = {c.anchors[k]}")
    return "; ".join(parts) if parts else "0 = not at all; 10 = extremely"


def schema_for(c: Criterion) -> dict:
    if c.type == "choice":
        value = {"type": "string", "enum": list(c.choices)}
    elif c.type == "binary":
        value = {"type": "string", "enum": ["yes", "no"]}
    else:
        value = {"type": "integer", "minimum": 0, "maximum": 10}
    return {
        "type": "object",
        "properties": {"value": value, "confidence": {"type": "number"}, "evidence": {"type": "string"}},
        "required": ["value", "confidence", "evidence"],
    }


def normalise_value(c: Criterion, raw) -> float | str | None:
    """Model answer -> Subjetunes value convention. None if unusable."""
    if c.type == "choice":
        return raw if isinstance(raw, str) and raw in c.choices else None
    if c.type == "binary":
        if isinstance(raw, str):
            return 1.0 if raw.strip().lower() in ("yes", "true", "1") else 0.0 if raw.strip().lower() in ("no", "false", "0") else None
        try:
            return float(min(1.0, max(0.0, float(raw))))
        except (TypeError, ValueError):
            return None
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    return float(min(1.0, max(0.0, v / 10.0 if v > 1.0 else v)))
