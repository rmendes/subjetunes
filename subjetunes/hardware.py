"""Hardware profile: what this PC can run.

Detection order: psutil (RAM) + `llama-server --list-devices` (GPUs), then
config/hardware.yaml fills anything that couldn't be detected.
"""
from __future__ import annotations

import os
import re
import subprocess
from typing import Any

from .model_manager import find_llama_server

# "  Vulkan0: AMD Radeon RX 6950 XT (16368 MiB, 15569 MiB free)"
_DEVICE_RE = re.compile(r"^\s*(?P<dev>[A-Za-z]+\d+)\s*:\s*(?P<name>.+?)\s*\((?P<total>\d+)\s*MiB(?:,\s*(?P<free>\d+)\s*MiB free)?\)")


def parse_list_devices(text: str) -> list[dict[str, Any]]:
    gpus = []
    for line in text.splitlines():
        m = _DEVICE_RE.match(line)
        if m:
            gpus.append({
                "device": m["dev"], "name": m["name"].strip(),
                "vram_gb": round(int(m["total"]) / 1024, 1),
                "free_gb": round(int(m["free"]) / 1024, 1) if m["free"] else None,
            })
    return gpus


def detect_gpus() -> list[dict[str, Any]]:
    exe = find_llama_server()
    if not exe:
        return []
    try:
        out = subprocess.run([exe, "--list-devices"], capture_output=True, text=True, timeout=30)
        return parse_list_devices(out.stdout + "\n" + out.stderr)
    except Exception:
        return []


def profile(cfg_hw: dict | None = None, detect: bool = True) -> dict[str, Any]:
    cfg_hw = cfg_hw or {}
    prof: dict[str, Any] = {"cpu_threads": os.cpu_count() or 4, "source": []}
    try:
        import psutil
        prof["ram_gb"] = round(psutil.virtual_memory().total / 1024 ** 3, 1)
        prof["source"].append("psutil")
    except Exception:
        prof["ram_gb"] = cfg_hw.get("ram_gb")
    gpus = detect_gpus() if detect else []
    if gpus:
        prof["source"].append("llama-server")
        # ignore integrated GPUs: keep the largest card
        gpus.sort(key=lambda g: g["vram_gb"], reverse=True)
    else:
        gpus = cfg_hw.get("gpus", [])
        prof["source"].append("hardware.yaml")
    prof["gpus"] = gpus
    prof["gpu"] = gpus[0] if gpus else None
    limits = cfg_hw.get("limits", {})
    prof["vram_gb_max"] = limits.get("vram_gb_max", (gpus[0]["vram_gb"] - 1) if gpus else 0)
    prof["ram_gb_max_for_models"] = limits.get("ram_gb_max_for_models", (prof.get("ram_gb") or 16) * 0.7)
    prof["allowed_runtimes"] = limits.get("allowed_runtimes", ["cpu", "ollama", "llama_server_vulkan"])
    return prof
