"""Plain data models used across Subjetunes.

Stdlib dataclasses (not pydantic) on purpose: fewer dependencies, easier to
freeze into a portable .exe later. Validation of untrusted input (LLM JSON,
YAML) happens in the module that receives it.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from typing import Any, Literal


def from_dict(cls, data: dict[str, Any]):
    """Build a dataclass from a dict, ignoring unknown keys."""
    names = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in data.items() if k in names})


def to_dict(obj) -> dict[str, Any]:
    return asdict(obj)


# --------------------------------------------------------------------------- library

@dataclass
class Track:
    track_id: str                    # rekordbox TrackID, kept as a string
    location: str                    # raw Location attribute. NEVER re-encode it
    path: str                        # decoded local filesystem path
    name: str = ""
    artist: str = ""
    album: str = ""
    genre: str = ""
    comments: str = ""
    bpm: float | None = None         # AverageBpm
    key: str = ""                    # Tonality
    duration_s: float | None = None  # TotalTime
    raw_attrs: dict[str, str] = field(default_factory=dict)


@dataclass
class Playlist:
    path: str                        # "Folder/Sub/Name"
    name: str
    track_ids: list[str] = field(default_factory=list)


@dataclass
class Library:
    source_path: str
    tracks: dict[str, Track]
    playlists: list[Playlist]
    product: dict[str, str] = field(default_factory=dict)

    def playlist(self, path: str) -> Playlist | None:
        return next((p for p in self.playlists if p.path == path), None)


# --------------------------------------------------------------------------- analysis

@dataclass
class Features:
    path: str
    file_size: int
    file_mtime: float
    analyzed_at: str
    analyzer_version: str
    # energy
    bpm: float = 0.0
    rms_db: float = 0.0
    onset_rate: float = 0.0
    spectral_flux_mean: float = 0.0
    band_onset_density: list[float] = field(default_factory=list)
    spectral_flatness_mean: float = 0.0
    spectral_centroid_mean: float = 0.0
    # swing
    swing_ratio: float = 0.5
    microtiming_std: float = 0.0
    beat_regularity: float = 0.0
    error: str | None = None


# --------------------------------------------------------------------------- decisions

Source = Literal["rule", "llm", "human"]


@dataclass
class Bucket:
    id: str
    name: str
    description: str = ""
    rule: str = ""
    use_llm: bool = False


@dataclass
class Assignment:
    track_id: str
    buckets: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    confidence: float = 1.0
    rationale: str = ""
    source: Source = "rule"
    locked: bool = False


@dataclass
class Criterion:
    id: str
    name: str
    type: Literal["scale", "binary", "choice"] = "scale"
    description: str = ""
    anchors: dict[str, str] = field(default_factory=dict)   # "0.0" -> text
    choices: list[str] = field(default_factory=list)
    clap_prompts: list[str] = field(default_factory=list)   # [positive, negative] for zero-shot
    needs_audio: bool = True
    use_metadata: bool = False
    judges: str | list[str] = "auto"


@dataclass
class Judgment:
    track_id: str
    criterion_id: str
    judge_id: str
    judge_version: str
    value: float | str
    confidence: float = 0.5
    evidence: str = ""
    ms: int = 0
