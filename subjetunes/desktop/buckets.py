# subjetunes/desktop/buckets.py
"""Bucket definitions (config/default.yaml + user overrides) evaluated on score rows.

Same format as the engine's config: {id, name, description, rule, use_llm}. Buckets
with an empty rule are LLM-only and stay empty here until the engine fills them.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import yaml

from ..tagging.expr import RuleError, compile_rule, is_stable


@dataclass
class Bucket:
    id: str
    name: str
    description: str = ""
    rule: str = ""
    use_llm: bool = False

    def to_dict(self) -> dict:
        return dict(self.__dict__)


DEFAULT_BUCKETS = [
    Bucket("peak_time", "Peak time", "High energy, driving, tight drums.", "energy >= 0.75 and looseness < 0.5"),
    Bucket("warmup_groovy", "Warm-up groove", "Mid energy, swung or loose drums.",
           "0.3 <= energy < 0.6 and (swing > 0.4 or looseness > 0.5)"),
    Bucket("vocal_moments", "Vocal moments", "The vocal is a feature, not just a sample.", "comp_vocals > 0.7", True),
]


def load_buckets(*files: Path) -> list[Bucket]:
    found: list[dict] | None = None
    for f in files:
        if f and f.exists():
            data = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
            if isinstance(data.get("buckets"), list):
                found = data["buckets"]
    if found is None:
        return list(DEFAULT_BUCKETS)
    return [Bucket(id=str(b["id"]), name=str(b.get("name", b["id"])), description=str(b.get("description", "")),
                   rule=str(b.get("rule", "") or ""), use_llm=bool(b.get("use_llm", False))) for b in found]


def rule_error(rule: str) -> str | None:
    if not rule.strip():
        return None
    try:
        compile_rule(rule)
        return None
    except RuleError as e:
        return str(e)


def assign(values: Mapping[str, Any], buckets: list[Bucket], margin: float = 0.05) -> tuple[list[str], list[str]]:
    """(buckets the track is in, of those the ones that sit right on a rule edge)."""
    inside, edge = [], []
    for b in buckets:
        if not b.rule.strip() or rule_error(b.rule):
            continue
        r = compile_rule(b.rule)
        if r(values):
            inside.append(b.id)
            if not is_stable(r, values, margin):
                edge.append(b.id)
    return inside, edge
