# subjetunes/desktop/__main__.py
"""Start Subjetunes: API server + a native window (WebView2). Falls back to the browser.

    python -m subjetunes.desktop [--browser] [--port 0]
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
import webbrowser

from . import paths
from .server import start


class JsApi:
    """Exposed to the page as window.pywebview.api (native dialogs).

    Every public attribute here is walked recursively by pywebview to build the JS API.
    The window reference MUST stay private (underscore): a public `window` attribute made
    pywebview crawl the native WinForms form on the UI thread and froze the app
    ("Not Responding" with an empty page).
    """

    def __init__(self) -> None:
        self._window = None

    def pick_file(self, kind: str = "xml") -> str:
        import webview
        types = ("rekordbox XML (*.xml)",) if kind == "xml" else ("rekordbox database (master.db)",)
        res = self._window.create_file_dialog(webview.OPEN_DIALOG, file_types=types)
        return res[0] if res else ""


def _mac_dock_icon() -> None:
    """The Mac launcher hands over to Python, which has no icon of its own: set the Dock icon and the
    app name from the bundle so testers see Subjetunes, not a generic Python rocket."""
    try:
        import AppKit  # pyobjc, installed with pywebview on macOS
        icns = paths.shipped_root() / "Subjetunes.icns"
        if not icns.exists():
            icns = paths.shipped_root().parent / "Resources" / "Subjetunes.icns"
        if icns.exists():
            img = AppKit.NSImage.alloc().initWithContentsOfFile_(str(icns))
            AppKit.NSApplication.sharedApplication().setApplicationIconImage_(img)
        info = AppKit.NSBundle.mainBundle().infoDictionary()
        if info is not None:
            info["CFBundleName"] = "Subjetunes"
    except Exception as e:  # cosmetic only
        logging.info("dock icon not set: %s", e)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="subjetunes")
    ap.add_argument("--browser", action="store_true", help="open in the default browser instead of a window")
    ap.add_argument("--port", type=int, default=0)
    a = ap.parse_args(argv)

    log = paths.data_dir() / "logs"
    log.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=log / "subjetunes.log", level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    if sys.stdout is None:  # pythonw: no console, keep tracebacks in the log
        sys.stdout = sys.stderr = open(log / "subjetunes.log", "a", encoding="utf-8", buffering=1)

    import faulthandler
    faulthandler.enable(open(log / "crash.log", "a", encoding="utf-8"))  # hard crashes leave a trace
    logging.info(paths.HF_SUMMARY)
    srv, url = start(port=a.port)
    logging.info("serving %s", url.split("?")[0])
    if not a.browser:
        try:
            import webview
            api = JsApi()
            api._window = webview.create_window("Subjetunes", url, width=1440, height=900, min_size=(1024, 640),
                                               background_color="#16141b", js_api=api)
            webview.start(_mac_dock_icon if sys.platform == "darwin" else None,
                          gui="edgechromium" if sys.platform.startswith("win") else None, private_mode=False,
                          storage_path=str(paths.data_dir() / "webview"))
            srv.shutdown()
            return 0
        except Exception as e:  # no pywebview / no WebView2 runtime
            logging.warning("window failed (%s), using the browser", e)
    webbrowser.open(url)
    print(f"Subjetunes is running at {url}\nClose this window to quit.")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        srv.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
