# scripts/dev.py
"""Run Subjetunes from source and keep the portable build in step with it.

    .venv\\Scripts\\python scripts\\dev.py               app + portable sync on start and on every change
    .venv\\Scripts\\python scripts\\dev.py --no-build    app only
    .venv\\Scripts\\python scripts\\dev.py --browser     in the browser instead of a window

While it runs:
  * a change to a .py file restarts the app (the window reopens on the page you were on);
  * a change in subjetunes/desktop/web/ reloads the window in place, no restart;
  * every change, after a short pause, rebuilds the portable app in the background:
    Windows `packaging/build_portable.ps1 -Quick`, macOS `packaging/macos/build_macos.sh --quick`.
    -Quick reuses the bundled runtime unless the Python version or a requirements file changed,
    so a sync takes seconds. Output goes to dist/build.log; one status line is printed here.
    Changes made while a build runs are picked up by one more build right after it.
Close the window or press Ctrl+C to stop.

Plain stdlib (no watchdog), the same on Windows and macOS. To build the other platform, push:
.github/workflows/build.yml builds Windows, Apple silicon and Intel Macs side by side.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "subjetunes" / "desktop" / "web"
DIST = ROOT / "dist"
SKIP_DIRS = {"__pycache__", ".git", ".venv", "dist", "data", "node_modules", ".pytest_cache", "tools"}
WATCH = [(ROOT / "subjetunes", (".py", ".html", ".css", ".js", ".yaml", ".json")),
         (ROOT / "config", (".yaml",)),
         (ROOT / "packaging", (".ps1", ".c", ".rc", ".ico", ".exe"))]
WATCH_FILES = [ROOT / "requirements-app.txt", ROOT / "requirements-ml.txt"]


def say(msg: str) -> None:
    print(f"{datetime.now():%H:%M:%S}  {msg}", flush=True)


def snapshot() -> dict[Path, float]:
    out: dict[Path, float] = {}
    for base, exts in WATCH:
        if not base.exists():
            continue
        stack = [base]
        while stack:
            d = stack.pop()
            try:
                entries = list(os.scandir(d))
            except OSError:
                continue
            for e in entries:
                if e.is_dir(follow_symlinks=False):
                    if e.name not in SKIP_DIRS:
                        stack.append(Path(e.path))
                elif e.name.endswith(exts):
                    try:
                        out[Path(e.path)] = e.stat().st_mtime
                    except OSError:
                        pass
    for f in WATCH_FILES:
        if f.exists():
            out[f] = f.stat().st_mtime
    return out


class App:
    """The app from source, as a child process, with SUBJETUNES_DEV=1 (live reload of web/)."""

    def __init__(self, browser: bool) -> None:
        self.args = [sys.executable, "-m", "subjetunes.desktop"] + (["--browser"] if browser else [])
        self.proc: subprocess.Popen | None = None
        self.restarting = False

    def start(self) -> None:
        env = {**os.environ, "SUBJETUNES_DEV": "1", "PYTHONUNBUFFERED": "1"}
        self.proc = subprocess.Popen(self.args, cwd=ROOT, env=env)

    def stop(self) -> None:
        p = self.proc
        if p and p.poll() is None:
            p.terminate()
            try:
                p.wait(8)
            except subprocess.TimeoutExpired:
                p.kill()

    def restart(self) -> None:
        self.restarting = True
        self.stop()
        self.start()
        self.restarting = False

    def exited(self) -> bool:
        return not self.restarting and self.proc is not None and self.proc.poll() is not None


class Builder:
    """Runs one portable sync at a time; requests during a run become exactly one more run."""

    def __init__(self) -> None:
        self.cmd = self._command()
        self.wanted = threading.Event()
        self.proc: subprocess.Popen | None = None
        self.stopping = False
        threading.Thread(target=self._loop, daemon=True).start()

    @staticmethod
    def _command() -> list[str] | None:
        script = ROOT / "packaging" / "build_portable.ps1"
        if sys.platform.startswith("win"):
            return ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script), "-Quick"]
        if sys.platform == "darwin":
            return ["bash", str(ROOT / "packaging" / "macos" / "build_macos.sh"), "--quick"]
        return None

    def request(self) -> None:
        if self.cmd is None:
            return
        self.wanted.set()

    def _loop(self) -> None:
        while not self.stopping:
            self.wanted.wait()
            if self.stopping:
                return
            self.wanted.clear()
            DIST.mkdir(exist_ok=True)
            log = DIST / "build.log"
            t0 = time.time()
            say("portable: syncing…")
            with log.open("a", encoding="utf-8") as f:
                f.write(f"\n===== {datetime.now():%Y-%m-%d %H:%M:%S}  {' '.join(self.cmd)}\n")
                f.flush()
                flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
                self.proc = subprocess.Popen(self.cmd, cwd=ROOT, stdout=f, stderr=subprocess.STDOUT, creationflags=flags)
                code = self.proc.wait()
            self.proc = None
            if self.stopping:
                return
            dt = time.time() - t0
            if code == 0:
                where = r"dist\Subjetunes\Subjetunes.exe" if sys.platform.startswith("win") else "dist/macos-*/Subjetunes.app"
                say(f"portable: up to date ({dt:.0f} s)  {where}")
            else:
                tail = log.read_text(encoding="utf-8", errors="replace").strip().splitlines()[-6:]
                say(f"portable: FAILED after {dt:.0f} s, see dist\\build.log")
                for line in tail:
                    print("    " + line, flush=True)

    def stop(self) -> None:
        self.stopping = True
        self.wanted.set()
        p = self.proc
        if p and p.poll() is None:
            p.terminate()
            (DIST / ".build.lock").unlink(missing_ok=True)   # the script's own cleanup didn't get to run


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--no-build", action="store_true", help="don't sync the portable build")
    ap.add_argument("--browser", action="store_true")
    a = ap.parse_args()

    app = App(a.browser)
    builder = None if a.no_build else Builder()
    if builder and builder.cmd is None:
        say("portable: no local build on this OS (push to GitHub: CI builds Windows and both Macs)")
    say("app: starting from source")
    app.start()
    if builder:
        builder.request()

    seen = snapshot()
    pending: dict[Path, float] = {}
    last_change = 0.0
    try:
        while True:
            time.sleep(0.6)
            if app.exited():
                say("app: closed, stopping")
                break
            now = snapshot()
            changed = {p for p in now.keys() | seen.keys() if now.get(p) != seen.get(p)}
            seen = now
            if changed:
                pending.update({p: time.time() for p in changed})
                last_change = time.time()
                continue
            if pending and time.time() - last_change > 0.8:      # quiet for a moment: act once
                files = list(pending)
                pending.clear()
                names = ", ".join(sorted({f.name for f in files})[:4]) + (" …" if len(files) > 4 else "")
                code = [f for f in files if f.suffix == ".py"]
                if code:
                    say(f"app: restarting ({names})")
                    app.restart()
                elif any(WEB in f.parents for f in files):
                    say(f"app: reloading the window ({names})")      # the page polls /api/dev itself
                if builder:
                    builder.request()
    except KeyboardInterrupt:
        say("stopping")
    finally:
        app.stop()
        if builder:
            builder.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
