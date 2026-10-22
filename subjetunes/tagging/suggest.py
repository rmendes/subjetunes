# subjetunes/tagging/suggest.py
"""Turn per-track scores into comment tags.

Input per track is the same flat dict the bucket rules see (``energy``, ``swing``,
``looseness``, ``bpm``, ``mood_*``, ``comp_*``, ``crit_*``) plus the track's buckets.
Optional ``<name>_conf`` entries (judge confidence 0..1) gate criterion tags.

Every tag comes out either *stable* (written to Comments) or *uncertain* (shown in
Review, written only with ``write_uncertain: true``). A rule tag is uncertain when
nudging any one input by ``margin`` would flip it, or when a judge's confidence is
below ``min_confidence``. Level tags (``#e1``..``#e5``) are always written, because
they're ordinal and one step off is mild; near a cut they're flagged *borderline*. Your Review edits always win: ``add`` tags are kept even
past ``max_tags``, ``remove`` tags never come back.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

import yaml

from .comments import TAG_RE, BlockFormat, normalise_path, normalise_tag
from .expr import RuleError, compile_rule, is_stable

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "tags.yaml"


# ---------------------------------------------------------------- config

@dataclass
class LevelDef:
    source: str
    tags: list[str]
    cuts: list[float] = field(default_factory=list)
    mode: str = "absolute"           # "absolute": fixed cuts | "quantile": equal groups over this batch


@dataclass
class RuleDef:
    tag: str
    when: str
    group: str = ""                  # tags sharing a group are mutually exclusive (first match wins)


@dataclass
class ChoiceDef:
    source: str                      # e.g. crit_vocal_role -> "sung_hook"
    prefix: str
    skip: list[str] = field(default_factory=list)


@dataclass
class BucketTagDef:
    enabled: bool = True
    prefix: str = "#"
    skip: list[str] = field(default_factory=list)


@dataclass
class GroupDef:
    name: str
    max: int = 1                     # how many tags of this axis a track may carry
    tip: str = ""


@dataclass
class TagConfig:
    block: BlockFormat = field(default_factory=BlockFormat)
    max_tags: int = 10
    margin: float = 0.05
    min_confidence: float = 0.6
    write_uncertain: bool = False
    levels: list[LevelDef] = field(default_factory=list)
    rules: list[RuleDef] = field(default_factory=list)
    choices: list[ChoiceDef] = field(default_factory=list)
    buckets: BucketTagDef = field(default_factory=BucketTagDef)
    groups: dict[str, GroupDef] = field(default_factory=dict)
    conflicts: list[tuple[str, str]] = field(default_factory=list)
    manual: dict[str, list[str]] = field(default_factory=dict)   # hand-only tags per group (never suggested)

    def group_max(self, group: str) -> int:
        if not group:
            return 99
        g = self.groups.get(group)
        return g.max if g else 1

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "TagConfig":
        d = dict(d.get("comment_tags", d))
        b = d.get("block", {}) or {}
        cfg = cls(
            block=BlockFormat(**{k: b[k] for k in ("open", "close", "position", "max_len") if k in b}),
            max_tags=int(d.get("max_tags", 10)),
            margin=float(d.get("margin", 0.05)),
            min_confidence=float(d.get("min_confidence", 0.6)),
            write_uncertain=bool(d.get("write_uncertain", False)),
            levels=[LevelDef(source=x["source"], tags=[normalise_tag(t) for t in x["tags"]],
                             cuts=[float(c) for c in x.get("cuts", [])], mode=x.get("mode", "absolute"))
                    for x in d.get("levels", []) or []],
            rules=[RuleDef(tag=normalise_tag(x["tag"]), when=str(x["when"]), group=str(x.get("group", "")))
                   for x in d.get("rules", []) or []],
            choices=[ChoiceDef(source=x["source"], prefix=x.get("prefix", "#"), skip=list(x.get("skip", [])))
                     for x in d.get("choices", []) or []],
        )
        cfg.groups = {str(k): GroupDef(name=str(k), max=int((v or {}).get("max", 1)), tip=str((v or {}).get("tip", "")))
                      for k, v in (d.get("groups") or {}).items()}
        cfg.conflicts = [(normalise_tag(a), normalise_tag(b)) for a, b in (d.get("conflicts") or [])]
        cfg.manual = {str(g): [normalise_tag(t) for t in ts or []] for g, ts in (d.get("manual") or {}).items()}
        if "buckets" in d and d["buckets"] is not None:
            bt = d["buckets"]
            cfg.buckets = BucketTagDef(enabled=bool(bt.get("enabled", True)), prefix=bt.get("prefix", "#"),
                                       skip=list(bt.get("skip", [])))
        return cfg

    @classmethod
    def load(cls, *paths: Path) -> "TagConfig":
        """Load config/tags.yaml, then let later files (e.g. config/user.yaml) override top-level keys."""
        merged: dict[str, Any] = {}
        for p in paths or (DEFAULT_CONFIG_PATH,):
            if p and Path(p).exists():
                data = yaml.safe_load(Path(p).read_text(encoding="utf-8")) or {}
                merged.update(data.get("comment_tags", {}) if "comment_tags" in data else {})
        return cls.from_dict(merged)


def validate(cfg: TagConfig, bucket_ids: Iterable[str] = ()) -> list[str]:
    """Problems that would make tags misbehave in rekordbox. Empty list = fine.

    The important one: rekordbox's 'Comments contains' is a substring match, so if
    '#vocal' and '#vocals' both exist, a smart playlist for '#vocal' catches both.
    """
    problems: list[str] = []
    for lv in cfg.levels:
        if lv.mode == "absolute" and len(lv.cuts) != len(lv.tags) - 1:
            problems.append(f"level '{lv.source}': {len(lv.tags)} tags need {len(lv.tags) - 1} cuts, got {len(lv.cuts)}")
        if lv.cuts != sorted(lv.cuts):
            problems.append(f"level '{lv.source}': cuts must be ascending")
        if lv.mode not in ("absolute", "quantile"):
            problems.append(f"level '{lv.source}': mode must be absolute or quantile")
    for r in cfg.rules:
        try:
            compile_rule(r.when)
        except RuleError as e:
            problems.append(f"tag {r.tag}: {e}")
    tags = [t for lv in cfg.levels for t in lv.tags] + [r.tag for r in cfg.rules] + [t for ts in cfg.manual.values() for t in ts]
    if cfg.buckets.enabled:
        tags += [_bucket_tag(cfg.buckets, b) for b in bucket_ids if b not in cfg.buckets.skip]
    prefixes = [c.prefix for c in cfg.choices]
    for t in tags:
        if not TAG_RE.match(t):
            problems.append(f"{t!r} is not a valid tag (lowercase letters, digits, - and _ after '#')")
    uniq = sorted(set(tags))
    for a in uniq:
        for b in uniq:
            if a != b and b.startswith(a):
                problems.append(f"'{a}' is a prefix of '{b}': 'Comments contains {a}' would also match {b}")
        for p in prefixes:
            if a.startswith(p) and p != "#":
                problems.append(f"'{a}' starts with choice prefix '{p}' and will collide with its values")
    dup = {t for t in tags if tags.count(t) > 1}
    for t in sorted(dup):
        problems.append(f"{t} is defined more than once")
    return problems


# ---------------------------------------------------------------- suggestion

@dataclass
class Suggestion:
    tag: str
    source: str        # level | rule | choice | bucket | human
    stable: bool
    reason: str


@dataclass
class TrackTags:
    key: str
    suggestions: list[Suggestion]
    tags: list[str]          # what goes into Comments, in order
    uncertain: list[str]     # suggested but not sure enough to write
    borderline: list[str] = field(default_factory=list)  # level tags written, but close to a cut

    @property
    def needs_review(self) -> bool:
        return bool(self.uncertain)


@dataclass
class Override:
    add: list[str] = field(default_factory=list)
    remove: list[str] = field(default_factory=list)


def _num(x: Any) -> float | None:
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) else f


def _bucket_tag(bt: BucketTagDef, bucket_id: str) -> str:
    return normalise_tag(bt.prefix + bucket_id.replace("_", "-"))


def quantile_cuts(values: Iterable[Any], n_levels: int) -> list[float]:
    """Cut points that split the batch into n roughly equal groups."""
    xs = sorted(v for v in (_num(x) for x in values) if v is not None)
    if not xs or n_levels < 2:
        return []
    out = []
    for i in range(1, n_levels):
        pos = i * (len(xs) - 1) / n_levels
        lo = int(math.floor(pos))
        hi = min(lo + 1, len(xs) - 1)
        out.append(xs[lo] + (xs[hi] - xs[lo]) * (pos - lo))
    return out


def _level(lv: LevelDef, cuts: list[float], values: Mapping[str, Any], margin: float) -> Suggestion | None:
    x = _num(values.get(lv.source))
    if x is None or len(cuts) != len(lv.tags) - 1:
        return None
    idx = sum(1 for c in cuts if x >= c)
    near = min((abs(x - c) for c in cuts), default=1.0)
    return Suggestion(lv.tags[idx], "level", near >= margin, f"{lv.source}={x:.2f}")


def derive(values: Mapping[str, Any]) -> dict[str, Any]:
    """Valence (how positive) and arousal (how energetic the emotion is) for the mood rules.
    The engine can supply them; otherwise brightness and energy stand in."""
    v = dict(values)
    if _num(v.get("valence")) is None and _num(v.get("mood_bright")) is not None:
        v["valence"] = _num(v["mood_bright"])
    if _num(v.get("arousal")) is None and _num(v.get("energy")) is not None:
        a = _num(v["energy"])
        ag = _num(v.get("mood_agitated"))
        v["arousal"] = a if ag is None else 0.75 * a + 0.25 * ag
    return v


def strength(rule, values: Mapping[str, Any]) -> float:
    """How far the inputs could move before the rule flips (0 = right on the edge)."""
    for m in (0.3, 0.2, 0.15, 0.1, 0.07, 0.05, 0.03, 0.02, 0.01):
        if is_stable(rule, values, m):
            return m
    return 0.0


def _low_confidence(names: Iterable[str], values: Mapping[str, Any], min_conf: float) -> list[str]:
    low = []
    for n in names:
        c = _num(values.get(f"{n}_conf"))
        if c is not None and c < min_conf:
            low.append(f"{n} confidence {c:.2f}")
    return low


def suggest_one(key: str, values: Mapping[str, Any], buckets: Iterable[str], cfg: TagConfig,
                level_cuts: Mapping[str, list[float]] | None = None,
                override: Override | None = None, extra: list[Suggestion] | None = None) -> TrackTags:
    sugg: list[Suggestion] = []
    for lv in cfg.levels:
        cuts = (level_cuts or {}).get(lv.source, lv.cuts) if lv.mode == "quantile" else lv.cuts
        s = _level(lv, cuts, values, cfg.margin)
        if s:
            sugg.append(s)

    values = derive(values)
    matched: list[tuple[RuleDef, float, Suggestion]] = []
    for r in cfg.rules:
        rule = compile_rule(r.when)
        if not rule(values):
            continue
        low = _low_confidence(rule.names, values, cfg.min_confidence)
        stable = is_stable(rule, values, cfg.margin) and not low
        why = ", ".join(f"{n}={values[n]:.2f}" if isinstance(values.get(n), (int, float)) and not isinstance(values.get(n), bool)
                        else f"{n}={values.get(n)}" for n in sorted(rule.names) if n != "clues")
        matched.append((r, strength(rule, values) + (0 if low else 0.001), Suggestion(r.tag, "rule", stable, why + (f" ({'; '.join(low)})" if low else ""))))
    # strongest evidence first inside each axis, at most group.max per axis
    kept: list[tuple[RuleDef, float, Suggestion]] = []
    per_group: dict[str, int] = {}
    for r, st, sg in sorted(matched, key=lambda x: -x[1]):
        if per_group.get(r.group, 0) >= cfg.group_max(r.group):
            continue
        per_group[r.group] = per_group.get(r.group, 0) + 1
        kept.append((r, st, sg))
    # contradictions (euphoric + dark, ...): the weaker one goes
    score = {sg.tag: st for _, st, sg in kept}
    drop = {min(p, key=lambda t: score[t]) for p in cfg.conflicts if p[0] in score and p[1] in score}
    order = {r.tag: i for i, r in enumerate(cfg.rules)}
    sugg += [sg for r, _, sg in sorted(kept, key=lambda x: (x[0].group, -x[1], order[x[0].tag])) if sg.tag not in drop]

    for ch in cfg.choices:
        v = values.get(ch.source)
        if v is None or (isinstance(v, float) and math.isnan(v)) or str(v) in ch.skip or str(v) == "":
            continue
        try:
            tag = normalise_tag(ch.prefix + str(v).replace("_", "-"))
        except ValueError:
            continue
        low = _low_confidence([ch.source], values, cfg.min_confidence)
        sugg.append(Suggestion(tag, "choice", not low, f"{ch.source}={v}" + (f" ({low[0]})" if low else "")))

    if cfg.buckets.enabled:
        for b in buckets:
            if b and b not in cfg.buckets.skip:
                sugg.append(Suggestion(_bucket_tag(cfg.buckets, b), "bucket", True, f"bucket {b}"))

    if extra:   # e.g. clue-based suggestions; respect each axis's cap and the conflicts list
        taken = {x.tag for x in sugg}
        group_of = {r.tag: r.group for r in cfg.rules if r.group}
        group_of.update({t: g for g, ts in cfg.manual.items() for t in ts})
        count: dict[str, int] = {}
        for t in taken:
            if t in group_of:
                count[group_of[t]] = count.get(group_of[t], 0) + 1
        for x in extra:
            g = group_of.get(x.tag)
            clash = any((x.tag == a and b in taken) or (x.tag == b and a in taken) for a, b in cfg.conflicts)
            if x.tag in taken or clash or (g and count.get(g, 0) >= cfg.group_max(g)):
                continue
            sugg.append(x)
            taken.add(x.tag)
            if g:
                count[g] = count.get(g, 0) + 1

    ov = override or Override()
    removed = {normalise_tag(t) for t in ov.remove}
    added = [normalise_tag(t) for t in ov.add]
    # a tag you added beats an automatic one it contradicts
    clashes = {b for a, b in cfg.conflicts if a in added} | {a for a, b in cfg.conflicts if b in added}
    auto = [s for s in sugg if s.tag not in removed and s.tag not in added and s.tag not in clashes]
    # Levels are ordinal: a track always gets one, and being one step off is mild,
    # so a level near a cut is written anyway and only flagged as borderline.
    written = [s.tag for s in auto if s.stable or s.source == "level" or cfg.write_uncertain]
    room = max(cfg.max_tags - len(added), 0)
    tags = added + written[:room]
    uncertain = [s.tag for s in auto if not s.stable and s.source != "level" and s.tag not in tags]
    borderline = [s.tag for s in auto if not s.stable and s.source == "level" and s.tag in tags]
    sugg = [Suggestion(t, "human", True, "your edit") for t in added] + auto
    return TrackTags(key=key, suggestions=sugg, tags=tags, uncertain=uncertain, borderline=borderline)


def suggest_batch(rows: list[Mapping[str, Any]], cfg: TagConfig,
                  overrides: Mapping[str, Override] | None = None,
                  extra: Mapping[str, list[Suggestion]] | None = None) -> list[TrackTags]:
    """rows: dicts with 'key' (file path or XML Location), 'buckets' (list of ids) and score columns.
    Result keys and override keys are compared after normalise_path()."""
    cuts = {lv.source: quantile_cuts((r.get(lv.source) for r in rows), len(lv.tags))
            for lv in cfg.levels if lv.mode == "quantile"}
    ov = {normalise_path(k): v for k, v in (overrides or {}).items()}
    out = []
    for r in rows:
        key = normalise_path(str(r["key"]))
        out.append(suggest_one(key, r, r.get("buckets") or [], cfg, cuts, ov.get(key), (extra or {}).get(key)))
    return out
