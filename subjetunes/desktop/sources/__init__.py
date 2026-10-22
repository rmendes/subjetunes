# subjetunes/desktop/sources/__init__.py
"""Metadata from other places, normalised so the Track screen can compare them.

Every source returns a Result: fields (title, artist, label, genre, year, ...), weighted
tags (0..1) and a link. Results are cached in data/sources.sqlite, so a track is only
looked up once per source (errors are retried after an hour).
"""
from .base import REGISTRY, Result, TrackQuery, lookup_all, aggregate  # noqa: F401
from . import providers  # noqa: F401,E402  (registers the sources)
