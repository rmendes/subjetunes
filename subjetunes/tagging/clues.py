# subjetunes/tagging/clues.py
"""Existing tags as clues for new ones.

A *clue* is anything already attached to a track: rekordbox genre, My Tags, words and
#hashtags in your comment, file-tag genre/grouping/mood, and tags from Last.fm,
Discogs styles, MusicBrainz genres... Each becomes a slug like "uk-bass".

Clues are used three ways:
  1. Rules can test them:  "dubstep" in clues and energy > 0.6
  2. Direct match: a clue that *is* one of your tags (My Tag "Acid" -> #acid) is
     suggested for that track.
  3. Learned: across the whole library, which of your Subjetunes tags tend to come with
     which clues. If 8 of 10 tracks with genre "uk bass" carry #dark, a new "uk bass"
     track gets #dark as an *uncertain* suggestion (shown in Review, with that reason).
     The more you tag, the better this gets.
"""
from __future__ import annotations

import re
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Iterable, Mapping

from .comments import user_text
from .suggest import Suggestion


def slug(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower().strip().lstrip("#")
    s = re.sub(r"[\s_/]+", "-", s)
    s = re.sub(r"[^a-z0-9&+\-]", "", s)
    return re.sub(r"-{2,}", "-", s).strip("-")


def track_clues(genre: str = "", comment: str = "", mytags: Iterable[str] = (), extra: Iterable[str] = ()) -> set[str]:
    """Clues from rekordbox fields. `extra` takes tags from file tags / online sources."""
    out: set[str] = set()
    for part in re.split(r"[;,/|]+", genre or ""):
        if slug(part):
            out.add(slug(part))
    mine = user_text(comment or "")
    out.update(slug(h) for h in re.findall(r"#([\w\-]+)", mine))
    for part in re.split(r"[;,|\n]+", re.sub(r"#[\w\-]+", "", mine)):
        s = slug(part)
        if s and len(s) <= 30 and not re.fullmatch(r"\d+[ab]?(-energy-\d+)?", s):   # skip "8A - Energy 6" (MIK)
            out.add(s)
    for t in mytags:
        out.add(slug(t.split("/")[-1]))
    out.update(slug(x) for x in extra)
    return {c for c in out if c}


@dataclass
class ClueModel:
    """P(tag | clue) from tracks you've tagged. Tiny and retrained per batch (milliseconds)."""
    min_support: int = 3
    min_prob: float = 0.6
    pair: dict[str, Counter] = field(default_factory=lambda: defaultdict(Counter))
    clue_n: Counter = field(default_factory=Counter)

    def fit(self, examples: Iterable[tuple[set[str], Iterable[str]]]) -> "ClueModel":
        """examples: (clues of a track, tags it carries)."""
        for clues, tags in examples:
            tags = set(tags)
            if not tags:
                continue
            for c in clues:
                self.clue_n[c] += 1
                for t in tags:
                    self.pair[c][t] += 1
        return self

    def suggest(self, clues: set[str], vocab: Iterable[str], have: Iterable[str] = ()) -> list[Suggestion]:
        have = set(have)
        out: list[Suggestion] = []
        vocab = list(vocab)
        by_slug = {slug(t): t for t in vocab}
        # direct: a clue that is literally one of your tags
        for c in clues:
            t = by_slug.get(c)
            if t and t not in have:
                out.append(Suggestion(t, "clue", True, f"already tagged '{c}' in rekordbox"))
                have.add(t)
        # learned
        best: dict[str, tuple[float, str, int, int]] = {}
        for c in clues:
            n = self.clue_n.get(c, 0)
            if n < self.min_support:
                continue
            for t, k in self.pair[c].items():
                if t in have or t not in vocab:
                    continue
                p = (k + 0.5) / (n + 1.0)            # light smoothing
                if p >= self.min_prob and p > best.get(t, (0,))[0]:
                    best[t] = (p, c, k, n)
        for t, (p, c, k, n) in sorted(best.items(), key=lambda x: -x[1][0]):
            out.append(Suggestion(t, "clue", False, f"{k} of {n} tracks with '{c}' have {t}"))
        return out


def examples_from_library(tracks: Iterable, written: Mapping[str, list[str]], overrides: Mapping,
                          vocab: Iterable[str]) -> list[tuple[set[str], set[str]]]:
    """Training pairs from every track that already carries some of your tags."""
    vocab = set(vocab)
    by_slug = {slug(t): t for t in vocab}
    out = []
    for t in tracks:
        clues = track_clues(t.genre, t.comments, t.mytags)
        tags = set(written.get(t.key, []))
        ov = overrides.get(t.key)
        if ov:
            tags = (tags | set(ov.add)) - set(ov.remove)
        tags |= {by_slug[c] for c in clues if c in by_slug}          # My Tag "Acid" counts as #acid
        tags &= vocab
        if tags:
            out.append((clues - {slug(x) for x in tags}, tags))
    return out
