"""Open the Crest desk in a desktop window.

Serves the bundled web desk (crest/web/, built by `npm run build:desktop`) on 127.0.0.1 and shows it in a pywebview
window. Exports go through a native Save dialog (`window.pywebview.api.save_file`), because blob downloads do nothing
inside a webview. If pywebview cannot start, the desk opens in the system browser, where downloads work as usual.
"""

from __future__ import annotations

import errno
import json
import os
import sys
import threading
import webbrowser
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from crest import WEB_DIR_NAME

#: Fixed local port, so the desk's saved records (browser localStorage is per origin) survive a restart.
#: If it is taken, a random port is used for that session.
PREFERRED_PORT = 47231

TITLE = "Crest — Dam Breach Desk"


def web_dir() -> Path:
    """The bundled web desk: next to this file, or inside the PyInstaller bundle."""
    base = Path(getattr(sys, "_MEIPASS", "")) / "crest" if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
    return base / WEB_DIR_NAME


def check_bundle(root: Path | None = None) -> Path:
    root = root or web_dir()
    index = root / "index.html"
    if not index.is_file():
        raise SystemExit(
            f"Crest desktop: the web desk bundle is missing ({index}).\n"
            "Build it from the repo root with `npm ci && npm run build:desktop`."
        )
    return root


class _Handler(SimpleHTTPRequestHandler):
    extensions_map = {
        **SimpleHTTPRequestHandler.extensions_map,
        ".js": "text/javascript",
        ".mjs": "text/javascript",
        ".css": "text/css",
        ".svg": "image/svg+xml",
        ".json": "application/json",
        ".html": "text/html; charset=utf-8",
    }

    def log_message(self, fmt, *args):  # quiet
        return

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()


def serve(root: Path | None = None, port: int = PREFERRED_PORT) -> ThreadingHTTPServer:
    """Serve the bundle on 127.0.0.1 in a daemon thread. Port 0 means any free port."""
    root = check_bundle(root)
    handler = partial(_Handler, directory=str(root))
    try:
        server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    except OSError as e:
        if port == 0 or e.errno not in (errno.EADDRINUSE, getattr(errno, "WSAEADDRINUSE", -1), 10048):
            raise
        print(f"Crest desktop: port {port} is busy; using a random port (saved records will not carry over).")
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def storage_dir() -> Path:
    """Where the webview keeps localStorage (the desk's records) between runs."""
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
        return base / "Crest"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Crest"
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "crest"


def _save_dialog_type(webview_mod):
    file_dialog = getattr(webview_mod, "FileDialog", None)
    return file_dialog.SAVE if file_dialog is not None else webview_mod.SAVE_DIALOG


def _file_types(name: str) -> tuple[str, ...]:
    ext = Path(name).suffix.lower().lstrip(".")
    labels = {"csv": "CSV", "json": "JSON", "par": "LISFLOOD parameters", "bci": "LISFLOOD boundary", "bdy": "LISFLOOD hydrograph"}
    if ext in labels:
        return (f"{labels[ext]} (*.{ext})", "All files (*.*)")
    return ("All files (*.*)",)


class Api:
    """Exposed to the page as window.pywebview.api (see src/lib/crest/save.ts)."""

    def __init__(self, settings_file: Path | None = None) -> None:
        # Underscored attributes are not exposed to JS by pywebview.
        self._window = None
        self._settings_file = settings_file  # remembers the last Save folder across sessions

    def _start_dir(self) -> str:
        """The folder of the last export if it still exists, else the home folder."""
        try:
            last = json.loads(self._settings_file.read_text(encoding="utf-8")).get("last_save_dir") if self._settings_file else None
        except (OSError, ValueError, AttributeError):
            last = None
        return last if isinstance(last, str) and Path(last).is_dir() else str(Path.home())

    def _remember_dir(self, folder: Path) -> None:
        if not self._settings_file:
            return
        try:
            data = json.loads(self._settings_file.read_text(encoding="utf-8")) if self._settings_file.is_file() else {}
            if not isinstance(data, dict):
                data = {}
        except (OSError, ValueError):
            data = {}
        data["last_save_dir"] = str(folder)
        try:
            self._settings_file.parent.mkdir(parents=True, exist_ok=True)
            self._settings_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except OSError:
            pass  # not worth failing an export over

    def save_file(self, name: str, text: str) -> dict:
        import webview

        name = Path(str(name or "crest_export.txt")).name or "crest_export.txt"
        window = self._window or (webview.windows[0] if webview.windows else None)
        if window is None:
            return {"error": "no window"}
        start = self._start_dir()
        try:
            picked = window.create_file_dialog(
                _save_dialog_type(webview), directory=start, save_filename=name, file_types=_file_types(name)
            )
        except Exception:  # some backends reject the file_types filter
            picked = window.create_file_dialog(_save_dialog_type(webview), directory=start, save_filename=name)
        if not picked:
            return {"cancelled": True}
        path = picked if isinstance(picked, str) else picked[0]
        try:
            write_text(Path(path), text)
        except OSError as e:
            return {"error": str(e)}
        self._remember_dir(Path(path).resolve().parent)
        return {"saved": str(path)}


def write_text(path: Path, text: str) -> None:
    """Write an export exactly as the desk produced it (UTF-8, no newline translation)."""
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(str(text))


def main() -> None:
    server = serve()
    url = f"http://127.0.0.1:{server.server_address[1]}/index.html"
    try:
        import webview

        try:
            webview.settings["ALLOW_DOWNLOADS"] = True  # belt and braces; exports use the Save dialog bridge
        except Exception:
            pass
        store = storage_dir()
        store.mkdir(parents=True, exist_ok=True)
        api = Api(settings_file=store / "crest-settings.json")
        api._window = webview.create_window(TITLE, url, js_api=api, width=1280, height=860, min_size=(900, 600))
        webview.start(private_mode=False, storage_path=str(store))
    except Exception as e:  # no GUI backend: use the system browser (downloads work there)
        print(f"Crest desktop: no webview ({e}). Opening {url} in the browser; press Ctrl+C to stop.")
        webbrowser.open(url)
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            sys.exit(0)


if __name__ == "__main__":
    main()
