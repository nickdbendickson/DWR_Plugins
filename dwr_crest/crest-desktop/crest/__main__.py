"""Open the Crest desk in a desktop window. Falls back to a local page if pywebview is missing."""

from __future__ import annotations

import json
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from crest.engine import analyze
from crest.export import export_dam_json, export_hydrograph_csv, generate_lisflood_package
from crest.samples import SAMPLE_DAMS, blank_dam

def _ui_path() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS) / "crest" / "ui" / "index.html"
    return Path(__file__).resolve().parent / "ui" / "index.html"


class Api:
    def dams(self):
        return SAMPLE_DAMS

    def blank(self):
        return blank_dam()

    def analyze(self, dam):
        return analyze(dam)

    def bundle(self, dam):
        result = analyze(dam)
        pack = generate_lisflood_package(dam, result["breach"]["series"])
        return {
            "analysis": result,
            "csv": export_hydrograph_csv(result["breach"]["series"]),
            "damJson": export_dam_json(dam, result),
            "par": pack["par"],
            "bci": pack["bci"],
            "bdy": pack["bdy"],
        }


def _serve(port_holder: dict) -> ThreadingHTTPServer:
    api = Api()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            return

        def _send(self, code, body, content_type):
            data = body if isinstance(body, bytes) else body.encode()
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                self._send(200, _ui_path().read_text(encoding="utf-8"), "text/html; charset=utf-8")
            else:
                self._send(404, "not found", "text/plain")

        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length) if length else b"{}"
            try:
                payload = json.loads(raw.decode() or "{}")
            except json.JSONDecodeError:
                self._send(400, '{"error":"bad json"}', "application/json")
                return
            if self.path == "/api/dams":
                body = json.dumps({"dams": api.dams(), "blank": api.blank()})
            elif self.path == "/api/bundle":
                body = json.dumps(api.bundle(payload.get("dam") or {}))
            else:
                self._send(404, "not found", "text/plain")
                return
            self._send(200, body, "application/json")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port_holder["port"] = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def main() -> None:
    port_holder: dict = {}
    _serve(port_holder)
    url = f"http://127.0.0.1:{port_holder['port']}/"
    try:
        import webview

        webview.create_window("Crest — Dam Breach Desk", url, width=1280, height=860)
        webview.start()
    except Exception:
        print(f"Crest desk: {url}")
        webbrowser.open(url)
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            sys.exit(0)


if __name__ == "__main__":
    main()
