"""Configuration: config/default.yaml deep-merged with config/user.yaml.

All paths are relative to the project root (or to the .exe folder when frozen),
never %APPDATA%, so the tool stays portable.
"""
from __future__ import annotations

import copy
import os
import sys
from pathlib import Path
from typing import Any

import yaml

from .models import Bucket, Criterion, from_dict


def project_root() -> Path:
    if os.environ.get("TAG_HOME"):
        return Path(os.environ["TAG_HOME"]).resolve()
    if getattr(sys, "frozen", False):            # PyInstaller
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


ROOT = project_root()
CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"
EXPORT_DIR = DATA_DIR / "exports"
JOBS_DIR = DATA_DIR / "jobs"
TOOLS_DIR = ROOT / "tools"
USER_CONFIG = CONFIG_DIR / "user.yaml"


def ensure_dirs() -> None:
    for d in (DATA_DIR, EXPORT_DIR, JOBS_DIR):
        d.mkdir(parents=True, exist_ok=True)


def deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _read_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_settings() -> dict[str, Any]:
    cfg = _read_yaml(CONFIG_DIR / "default.yaml")
    cfg = deep_merge(cfg, _read_yaml(CONFIG_DIR / "criteria.yaml"))
    cfg = deep_merge(cfg, {"hardware": _read_yaml(CONFIG_DIR / "hardware.yaml")})
    return deep_merge(cfg, _read_yaml(USER_CONFIG))


def save_user_settings(partial: dict[str, Any]) -> None:
    """Merge `partial` into config/user.yaml (lists are replaced, not merged)."""
    current = _read_yaml(USER_CONFIG)
    merged = deep_merge(current, partial)
    USER_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    with USER_CONFIG.open("w", encoding="utf-8") as f:
        yaml.safe_dump(merged, f, sort_keys=False, allow_unicode=True)


def load_models_registry() -> list[dict[str, Any]]:
    return _read_yaml(CONFIG_DIR / "models.yaml").get("models", [])


def buckets_from(cfg: dict) -> list[Bucket]:
    return [from_dict(Bucket, b) for b in cfg.get("buckets", [])]


def criteria_from(cfg: dict) -> list[Criterion]:
    out = []
    for c in cfg.get("criteria", []):
        c = dict(c)
        c["anchors"] = {str(k): str(v) for k, v in (c.get("anchors") or {}).items()}
        out.append(from_dict(Criterion, c))
    return out
