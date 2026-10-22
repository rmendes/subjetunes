"""config/models.yaml: every judge model Subjetunes knows about, filtered by the hardware profile."""
from __future__ import annotations

from typing import Any

from .config import load_models_registry


def fits(entry: dict[str, Any], prof: dict[str, Any]) -> tuple[bool, str]:
    runtime = entry.get("runtime", "cpu")
    if runtime not in prof.get("allowed_runtimes", []):
        return False, f"runtime '{runtime}' not allowed on this PC"
    vram = float(entry.get("vram_gb", 0) or 0)
    ram = float(entry.get("ram_gb", 0) or 0)
    offload = float(entry.get("ram_offload_gb", 0) or 0)     # MoE experts kept in RAM
    if vram > float(prof.get("vram_gb_max") or 0) + 1e-6:
        return False, f"needs {vram} GB VRAM > {prof.get('vram_gb_max')} GB limit"
    if ram + offload > float(prof.get("ram_gb_max_for_models") or 0) + 1e-6:
        return False, f"needs {ram + offload} GB RAM > {prof.get('ram_gb_max_for_models')} GB limit"
    return True, "fits"


def eligible(prof: dict[str, Any], kind: str | None = None) -> list[dict[str, Any]]:
    out = []
    for e in load_models_registry():
        if kind and e.get("kind") != kind:
            continue
        ok, why = fits(e, prof)
        out.append({**e, "fits": ok, "why": why})
    return out


def get(model_id: str) -> dict[str, Any] | None:
    return next((e for e in load_models_registry() if e.get("id") == model_id), None)
