"""Open the Crest desk in a desktop window.

Serves the bundled web desk (crest/web/, built by `npm run build:desktop`) on 127.0.0.1 and shows it in a pywebview
window. Exports go through a native Save dialog (`window.pywebview.api.save_file`, and `save_file_b64` for the view
PDF / PNG), because blob downloads do nothing
inside a webview. If pywebview cannot start, the desk opens in the system browser, where downloads work as usual.
"""

from __future__ import annotations

import base64
import binascii
import errno
import json
import os
import sys
import threading
import uuid
import webbrowser
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from crest import WEB_DIR_NAME
from crest.qgis_run import run_folder, run_layers, run_model, run_screening

#: Fixed local port, so the desk's saved records (browser localStorage is per origin) survive a restart.
#: If it is taken, a random port is used for that session.
PREFERRED_PORT = 47231

TITLE = "Crest — Dam Breach Desk"

_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()


def _job_log(job: dict, line: str) -> None:
    with _jobs_lock:
        job["log"] += ("" if job["log"].endswith("\n") or not job["log"] else "\n") + str(line) + "\n"


def start_job(
    kind: str,
    dam: dict,
    analysis: dict,
    work: str | None,
    model: str = "lisflood",
    files=None,
    source: str = "pack",
) -> dict:
    """Write the dam folder, fetch layers, or call the plugins on a background thread. Returns immediately."""
    job_id = uuid.uuid4().hex[:12]
    job = {"id": job_id, "done": False, "ok": False, "log": "", "folder": "", "work": ""}
    with _jobs_lock:
        _jobs[job_id] = job

    def work_job():
        try:
            log = lambda line: _job_log(job, line)
            if kind == "layers":
                result = run_layers(dam or {}, work, source, log)
            elif kind == "folder":
                result = run_folder(dam or {}, analysis or {}, work, log, files)
            elif kind == "model":
                result = run_model(dam or {}, analysis or {}, work, log, model, files, source)
            else:
                result = run_screening(dam or {}, analysis or {}, work, log, files)
            with _jobs_lock:
                job["ok"] = bool(result.get("ok"))
                job["folder"] = result.get("folder") or ""
                job["work"] = result.get("work") or ""
        except Exception as exc:
            _job_log(job, f"Crest stopped: {exc}")
        finally:
            with _jobs_lock:
                job["done"] = True

    threading.Thread(target=work_job, daemon=True).start()
    return {"id": job_id}


def job_snapshot(job_id: str) -> dict | None:
    with _jobs_lock:
        found = _jobs.get(job_id)
        return dict(found) if found else None


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

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/job":
            job_id = (parse_qs(parsed.query).get("id") or [""])[0]
            found = job_snapshot(job_id)
            if not found:
                self._send(404, b'{"error":"no job"}', "application/json")
                return
            self._send(200, json.dumps(found).encode(), "application/json")
            return
        super().do_GET()

    def do_POST(self):
        parsed = urlparse(self.path)
        kinds = {"/api/screen": "screen", "/api/model": "model", "/api/folder": "folder", "/api/layers": "layers"}
        kind = kinds.get(parsed.path)
        if kind is None:
            self._send(404, b"not found", "text/plain")
            return
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length > 20_000_000:
            self._send(413, b'{"error":"too large"}', "application/json")
            return
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode() or "{}")
        except json.JSONDecodeError:
            self._send(400, b'{"error":"bad json"}', "application/json")
            return
        if not isinstance(payload, dict):
            self._send(400, b'{"error":"bad json"}', "application/json")
            return
        dam = payload.get("dam") if isinstance(payload.get("dam"), dict) else {}
        if payload.get("dem"):
            dam = dict(dam)
            dam["demPath"] = payload.get("dem")
        model = str(payload.get("model") or "lisflood")
        source = str(payload.get("source") or "pack").lower()
        if source not in ("pack", "download"):
            source = "pack"
        files = payload.get("files") if isinstance(payload.get("files"), dict) else None
        analysis = payload.get("analysis") if isinstance(payload.get("analysis"), dict) else {}
        body = json.dumps(start_job(kind, dam, analysis, payload.get("work"), model, files, source))
        self._send(200, body.encode(), "application/json")

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

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
    labels = {"csv": "CSV", "json": "JSON", "par": "LISFLOOD parameters", "bci": "LISFLOOD boundary", "bdy": "LISFLOOD hydrograph", "geojson": "GeoJSON", "kml": "KML", "pdf": "PDF", "png": "PNG image"}
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

    def save_file_b64(self, name: str, b64: str) -> dict:
        """Binary exports (the view PDF / PNG): `b64` is the file as base64. Same Save dialog as save_file."""
        import webview

        try:
            data = decode_b64(b64)
        except ValueError as e:
            return {"error": str(e)}
        name = Path(str(name or "crest_export.bin")).name or "crest_export.bin"
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
            write_bytes(Path(path), data)
        except OSError as e:
            return {"error": str(e)}
        self._remember_dir(Path(path).resolve().parent)
        return {"saved": str(path)}


#: Largest binary export accepted from the page (a view PDF is a few MB).
MAX_BINARY_BYTES = 100 * 1024 * 1024


def decode_b64(b64: str) -> bytes:
    """Strict base64 (a `data:...;base64,` prefix is allowed). Raises ValueError on anything else."""
    if not isinstance(b64, str):
        raise ValueError("the export was not sent as base64 text")
    text = b64.split(",", 1)[1] if b64.startswith("data:") and "," in b64 else b64
    text = "".join(text.split())
    if len(text) * 3 // 4 > MAX_BINARY_BYTES:
        raise ValueError("the export is too large")
    if text and not _canonical_b64(text):
        raise ValueError("the export was not valid base64")
    try:
        data = base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("the export was not valid base64") from None
    if not data:
        raise ValueError("the export is empty")
    return data


def _canonical_b64(text: str) -> bool:
    """True for canonical base64. Unpadded input is allowed; a stray '=' is not.

    b64decode(..., validate=True) still accepts 'QUJD=' (a pad on a complete
    group) and returns b'ABC'. That must not open a Save dialog.
    """
    pad = len(text) - len(text.rstrip("="))
    if pad > 2 or "=" in text[: len(text) - pad]:
        return False
    body = text[: len(text) - pad]
    if any(c not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/" for c in body):
        return False
    rem = len(body) % 4
    if pad:
        return (rem == 2 and pad == 2) or (rem == 3 and pad == 1)
    return rem != 1


def write_bytes(path: Path, data: bytes) -> None:
    """Write a binary export as is; a partial file is not left behind if the write fails."""
    tmp = path.with_name(path.name + ".part")
    try:
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


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
