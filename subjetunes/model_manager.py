"""GPU coordination: one big model on the 16 GB card at a time.

Before an audio-LLM job: unload every Ollama model, start llama-server, wait
for /health. After: stop llama-server. The UI reads `gpu_owner()` to show a
"GPU busy" banner.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import requests

from .config import DATA_DIR, TOOLS_DIR

STATE_FILE = DATA_DIR / "gpu_owner.txt"


def find_llama_server() -> str | None:
    names = ["llama-server.exe", "llama-server"] if os.name == "nt" else ["llama-server"]
    for d in [TOOLS_DIR / "llama.cpp", *sorted(TOOLS_DIR.glob("llama*")), TOOLS_DIR]:
        for n in names:
            p = Path(d) / n
            if p.exists():
                return str(p)
    return shutil.which("llama-server")


def gpu_owner() -> str:
    try:
        return STATE_FILE.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""


def _set_owner(text: str) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(text, encoding="utf-8")


class LlamaServer:
    """Starts/stops llama-server for a registry entry (kind: audio_llm)."""

    def __init__(self, entry: dict[str, Any], port: int = 8080, device: str | None = None):
        self.entry = entry
        self.port = port
        self.device = device
        self.proc: subprocess.Popen | None = None
        self.log_path = DATA_DIR / "logs" / f"llama-server-{entry['id'].replace(':', '_').replace('/', '_')}.log"

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def command(self) -> list[str]:
        exe = find_llama_server()
        if not exe:
            raise FileNotFoundError("llama-server not found. Run scripts/get_llama_cpp.ps1 or put it in tools/llama.cpp/")
        cmd = [exe]
        if self.entry.get("model_path"):
            cmd += ["-m", self.entry["model_path"]]
            if self.entry.get("mmproj_path"):
                cmd += ["--mmproj", self.entry["mmproj_path"]]
        else:
            cmd += ["-hf", self.entry["hf_repo"]]
        if self.device:
            cmd += ["--device", self.device]
        cmd += [str(a) for a in self.entry.get("launch_args", [])]
        cmd += ["--port", str(self.port), "--host", "127.0.0.1"]
        return cmd

    def healthy(self) -> bool:
        try:
            return requests.get(f"{self.base_url}/health", timeout=2).status_code == 200
        except Exception:
            return False

    def start(self, timeout_s: float = 900) -> None:
        """First start may download several GB from Hugging Face -> long timeout."""
        if self.healthy():
            return
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        log = open(self.log_path, "w", encoding="utf-8", errors="replace")
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        self.proc = subprocess.Popen(self.command(), stdout=log, stderr=subprocess.STDOUT, creationflags=flags)
        _set_owner(f"llama-server:{self.entry['id']}")
        t0 = time.time()
        while time.time() - t0 < timeout_s:
            if self.proc.poll() is not None:
                _set_owner("")
                raise RuntimeError(f"llama-server exited (code {self.proc.returncode}). See {self.log_path}")
            if self.healthy():
                return
            time.sleep(2)
        self.stop()
        raise TimeoutError(f"llama-server not healthy after {timeout_s}s. See {self.log_path}")

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None
        _set_owner("")


def free_gpu_for_audio(ollama) -> list[str]:
    """Unload Ollama models so the audio-LLM gets the whole card. Returns what was unloaded."""
    return ollama.unload_all()
