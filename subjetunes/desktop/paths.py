# subjetunes/desktop/paths.py
"""Where things live. Portable: everything sits next to Subjetunes.exe.

    Subjetunes/                  <- home (SUBJETUNES_HOME overrides)
      Subjetunes.exe
      runtime/                   embedded Python + site-packages
      app/subjetunes/...         source (this package)
      config/                    defaults shipped with the app
      data/                      yours: user.yaml, cache.sqlite, ledgers, exports, backups
      tools/                     ffmpeg, llama.cpp (optional)
"""
from __future__ import annotations

import os
from pathlib import Path


def shipped_root() -> Path:
    """Where the app's own files are: the repo, the portable folder, or Contents/Resources in a Mac .app."""
    pkg = Path(__file__).resolve().parents[2]          # .../app or repo root
    return pkg.parent if pkg.name == "app" else pkg


def in_mac_bundle() -> bool:
    return ".app/Contents/" in str(shipped_root()).replace("\\", "/")


def home() -> Path:
    """Where your data lives. Portable Windows: next to Subjetunes.exe. Mac app: a .app is read-only
    (and runs from a random read-only copy if it wasn't moved out of Downloads), so data goes to
    ~/Library/Application Support/Subjetunes. The Mac launcher sets SUBJETUNES_HOME to that."""
    env = os.environ.get("SUBJETUNES_HOME")
    if env:
        return Path(env).expanduser()
    if in_mac_bundle():
        return Path.home() / "Library" / "Application Support" / "Subjetunes"
    return shipped_root()


def sync_shipped_config() -> None:
    """When data lives apart from the app (the Mac bundle), copy the shipped config/ into home/config,
    because the engine reads and writes config/ under its home. Your user.yaml is never touched;
    shipped files are refreshed when the app's copy is newer (an update)."""
    src = shipped_root() / "config"
    dst = home() / "config"
    if not src.is_dir() or src.resolve() == dst.resolve():
        return
    dst.mkdir(parents=True, exist_ok=True)
    import shutil
    for f in src.iterdir():
        if not f.is_file() or f.name == "user.yaml":
            continue
        t = dst / f.name
        if not t.exists() or f.stat().st_mtime > t.stat().st_mtime + 1:
            shutil.copy2(f, t)


# The engine (subjetunes.config) finds config/ and data/ from TAG_HOME, else from where its package
# sits. In the portable layout that would be app/, not the folder with config/default.yaml, so the
# app, the engine and every analysis job (child processes inherit this) share one home.
os.environ.setdefault("TAG_HOME", str(home()))
try:
    sync_shipped_config()
except OSError:
    pass


# ---------------------------------------------------------------- Hugging Face (CLAP model)
def _clap_model_id() -> str:
    """The CLAP model the engine is configured to use (config/default.yaml, then your user.yaml)."""
    mid = "laion/larger_clap_music"
    try:
        import yaml
        for f in (home() / "config" / "default.yaml", shipped_root() / "config" / "default.yaml",
                  home() / "config" / "user.yaml", home() / "data" / "user.yaml"):
            if f.is_file():
                d = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
                mid = ((d.get("clap") or {}).get("model_id")) or mid
    except Exception:
        pass
    return mid


def _hub_has(hub: Path, model_id: str) -> bool:
    snaps = hub / ("models--" + model_id.replace("/", "--")) / "snapshots"
    try:
        return any(any(d.iterdir()) for d in snaps.iterdir() if d.is_dir())
    except OSError:
        return False


def setup_huggingface() -> str:
    """Where the CLAP model lives, and whether to go online for it. Returns a one-line summary.

    * New downloads go to data/models (portable: the model travels with the app's data; Mac: it lives in
      Application Support, not in a hidden ~/.cache). A model you already downloaded to the usual
      Hugging Face cache is used where it is, not downloaded again.
    * Once the configured model is on disk, Hugging Face is set to offline: no update checks on every
      start, so no "unauthenticated requests" warning, faster startup, and analysis works without
      internet. Set SUBJETUNES_HF_ONLINE=1 to check for updates (or after changing clap.model_id, which
      is detected anyway: an absent model always goes online once).
    * HF_TOKEN, if you set one yourself, is left alone and used for downloads.
    """
    if os.environ.get("HF_HOME") or os.environ.get("HF_HUB_CACHE"):
        hub = Path(os.environ.get("HF_HUB_CACHE") or Path(os.environ["HF_HOME"]) / "hub")
        where = "your HF_HOME"
    else:
        ours = home() / "data" / "models"
        default_hub = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "huggingface" / "hub"
        mid = _clap_model_id()
        if not _hub_has(ours / "hub", mid) and _hub_has(default_hub, mid):
            hub, where = default_hub, "the existing Hugging Face cache"
        else:
            os.environ["HF_HOME"] = str(ours)
            hub, where = ours / "hub", "data/models"
    mid = _clap_model_id()
    have = _hub_has(hub, mid)
    if have and os.environ.get("SUBJETUNES_HF_ONLINE") != "1":
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    return f"CLAP model {mid}: {'on disk' if have else 'not downloaded yet'} in {where}" + \
           (", offline" if os.environ.get("HF_HUB_OFFLINE") == "1" else "")


try:
    HF_SUMMARY = setup_huggingface()
except Exception as e:      # never block startup over this
    HF_SUMMARY = f"Hugging Face setup skipped: {e}"


def config_dir() -> Path:
    for p in (home() / "config", shipped_root() / "config", Path(__file__).resolve().parents[2] / "config"):
        if p.exists():
            return p
    return home() / "config"


def data_dir() -> Path:
    d = home() / "data"
    d.mkdir(parents=True, exist_ok=True)
    return d


def user_config() -> Path:
    """User overrides. Portable builds keep it in data/; dev repos used config/user.yaml."""
    legacy = config_dir() / "user.yaml"
    return legacy if legacy.exists() and not (data_dir() / "user.yaml").exists() else data_dir() / "user.yaml"
