"""Rule-based bucket assignment and merging with LLM proposals and human overrides."""
from __future__ import annotations

import math

import pandas as pd

from .models import Assignment, Bucket
from .rules import RuleError, compile_rule, evaluate


def validate_rules(buckets: list[Bucket], columns: list[str]) -> dict[str, str]:
    """bucket id -> error message, for rules that don't compile or use unknown names."""
    from .rules import names_in

    errors = {}
    for b in buckets:
        if not b.rule.strip():
            continue
        try:
            unknown = names_in(b.rule) - set(columns)
            if unknown:
                errors[b.id] = f"Unknown name(s): {', '.join(sorted(unknown))}"
        except RuleError as e:
            errors[b.id] = str(e)
    return errors


def _row_env(row: pd.Series) -> dict:
    env = {}
    for k, v in row.items():
        if isinstance(v, float) and math.isnan(v):
            env[k] = float("nan")
        else:
            env[k] = v
    return env


def rule_matches(table: pd.DataFrame, buckets: list[Bucket]) -> tuple[dict[str, list[str]], dict[str, str]]:
    """Returns ({track_id: [bucket ids matched by rule]}, {bucket id: error}).
    Buckets with an empty rule match nothing here (LLM-only buckets)."""
    errors = validate_rules(buckets, list(table.columns))
    compiled = {b.id: compile_rule(b.rule) for b in buckets if b.rule.strip() and b.id not in errors}
    matches: dict[str, list[str]] = {tid: [] for tid in table.index}
    for tid, row in table.iterrows():
        env = _row_env(row)
        for bid, tree in compiled.items():
            try:
                if bool(evaluate(tree, env)):
                    matches[tid].append(bid)
            except RuleError as e:
                errors.setdefault(bid, str(e))
    return matches, errors


def rule_assignments(table: pd.DataFrame, buckets: list[Bucket]) -> tuple[dict[str, Assignment], dict[str, str]]:
    """Assignments from rule-only buckets (use_llm=False)."""
    matches, errors = rule_matches(table, buckets)
    rule_only = {b.id for b in buckets if not b.use_llm}
    out = {}
    for tid, bids in matches.items():
        chosen = [b for b in bids if b in rule_only]
        out[tid] = Assignment(track_id=tid, buckets=chosen, confidence=1.0, rationale="rule", source="rule")
    return out, errors


def llm_candidates(matches: dict[str, list[str]], buckets: list[Bucket]) -> dict[str, list[str]]:
    """Which LLM buckets the LLM should consider per track:
    - use_llm with empty rule -> every track is a candidate
    - use_llm with a rule      -> only tracks the rule pre-filtered"""
    out: dict[str, list[str]] = {}
    for tid, bids in matches.items():
        cands = [b.id for b in buckets if b.use_llm and (not b.rule.strip() or b.id in bids)]
        if cands:
            out[tid] = cands
    return out


def merge(
    rule: dict[str, Assignment],
    llm: dict[str, Assignment] | None,
    overrides: dict[str, dict] | None,
) -> dict[str, Assignment]:
    """Human overrides win and are locked; otherwise rule buckets + LLM buckets are combined."""
    out: dict[str, Assignment] = {}
    llm = llm or {}
    overrides = overrides or {}
    for tid in set(rule) | set(llm):
        if tid in overrides:
            o = overrides[tid]
            out[tid] = Assignment(tid, list(o["buckets"]), list(o["tags"]), 1.0, o.get("note") or "human", "human", True)
            continue
        r = rule.get(tid) or Assignment(tid)
        l = llm.get(tid)
        if l is None:
            out[tid] = r
            continue
        buckets = list(dict.fromkeys(r.buckets + l.buckets))
        out[tid] = Assignment(
            track_id=tid, buckets=buckets, tags=l.tags, confidence=l.confidence if l.buckets else r.confidence,
            rationale=l.rationale, source="llm" if l.buckets else r.source,
        )
    for tid, o in overrides.items():
        if tid not in out:
            out[tid] = Assignment(tid, list(o["buckets"]), list(o["tags"]), 1.0, o.get("note") or "human", "human", True)
    return out


def playlists_from(assignments: dict[str, Assignment], buckets: list[Bucket], order: list[str]) -> dict[str, list[str]]:
    """bucket display name -> TrackIDs, in the original playlist order."""
    names = {b.id: b.name for b in buckets}
    out: dict[str, list[str]] = {b.name: [] for b in buckets}
    for tid in order:
        a = assignments.get(tid)
        if not a:
            continue
        for bid in a.buckets:
            if bid in names:
                out[names[bid]].append(tid)
    return out
