"""QGIS networking for the StreamStats client (DamSafety_PMP/qgis_transport.py).

- With QGIS running, the plugin's client uses QgsBlockingNetworkRequest (transport 'qgis', in the
  provenance) and not urllib; a request with no answer raises a plain message that points to
  QGIS's proxy settings (mocked QgsBlockingNetworkRequest).
- Real QgsBlockingNetworkRequest against a local HTTP server on 127.0.0.1 (no internet): the
  client User-Agent arrives (in X-Client; QGIS sets its own User-Agent), HTTP errors come back with their status and body, a closed port gives the
  proxy hint, QGIS's proxy setting is followed, and the request works from a QgsTask thread.

Skips without QGIS. Run:  QT_QPA_PLATFORM=offscreen python -m pytest DamSafety_PMP/tests/test_qgis_transport.py
"""
import json
import os
import socket
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.pop("NDSUITE_SETTINGS", None)

pytest.importorskip("qgis.core")
from qgis.core import QgsApplication, QgsNetworkAccessManager, QgsTask  # noqa: E402

_APP = QgsApplication.instance()
if _APP is None:
    os.environ["HOME"] = tempfile.mkdtemp(prefix="pmp_qt_home_")   # never the real profile
    _APP = QgsApplication([], True)
    _APP.initQgis()

from DamSafety_PMP import qgis_transport as qt  # noqa: E402
from DamSafety_PMP import streamstats_dialog as sd  # noqa: E402

ss = qt.ss
UA_START = "DamSafety_PMP/"


# ---------------------------------------------------------------- mocked QgsBlockingNetworkRequest
class _FakeReply:
    def __init__(self, status, body):
        self._status, self._body = status, body

    def attribute(self, _attr):
        return self._status

    def content(self):
        return self._body

    def errorString(self):
        return "Connection refused"


class _FakeFeedback:
    def __init__(self):
        self.c = False

    def cancel(self):
        self.c = True

    def isCanceled(self):
        return self.c


def _fake_req(status, body=b"", error="", calls=None):
    class FakeReq:
        ErrorCode = type("ErrorCode", (), {"NoError": 0, "NetworkError": 1, "TimeoutError": 2,
                                           "ServerExceptionError": 3})

        def get(self, nr, force, fb):
            calls.append(("GET", nr, force))
            return 0 if status else 1

        def post(self, nr, data, force, fb):
            calls.append(("POST", nr, bytes(data)))
            return 0 if status else 1

        def reply(self):
            return _FakeReply(status, body)

        def errorMessage(self):
            return error
    return FakeReq


@pytest.fixture
def fake(monkeypatch):
    real = qt._qgis_classes

    def install(status, body=b"", error=""):
        calls = []
        _Req, _Fb, QUrl, QNetworkRequest = real()
        monkeypatch.setattr(qt, "_qgis_classes",
                            lambda: (_fake_req(status, body, error, calls), _FakeFeedback,
                                     QUrl, QNetworkRequest))
        return calls
    return install


def test_plugin_client_uses_qgis_networking_not_urllib(tmp_path, fake, monkeypatch):
    calls = fake(200, b'{"ok": 1}')
    monkeypatch.setattr(ss, "http_fetch", lambda *a: pytest.fail("urllib used inside QGIS"))
    task = sd.StreamStatsTask(lambda t: None, lambda t: None)
    c = sd.default_client(tmp_path, task)
    assert c.transport == "qgis" and isinstance(c.fetcher, qt.QgisFetcher)
    assert task.fetcher is c.fetcher
    c.min_interval_s = 0
    assert c.request("POST", "https://streamstats.usgs.gov/x", payload={"a": 1}, label="t") == {"ok": 1}
    method, nr, body = calls[0]
    assert method == "POST" and body == b'{"a":1}'
    # QGIS sets its own User-Agent, so the client's travels in X-Client
    assert bytes(nr.rawHeader(b"X-Client")).decode().startswith(UA_START)
    assert bytes(nr.rawHeader(b"Content-Type")) == b"application/json"
    assert c.log[0]["transport"] == "qgis"
    prov = json.loads(next(tmp_path.glob("t_*.prov.json")).read_text())
    assert prov["transport"] == "qgis"


def test_no_answer_gives_plain_message_with_qgis_proxy_hint(tmp_path, fake):
    fake(None, error="Proxy connection refused")
    c = ss.StreamStatsClient(tmp_path, fetcher=qt.QgisFetcher(), transport="qgis",
                             min_interval_s=0)
    with pytest.raises(ss.NetworkError) as ei:
        c.request("GET", "https://streamstats.usgs.gov/ss-delineate/v1/x")
    msg = str(ei.value)
    assert msg.startswith("Could not reach StreamStats (streamstats.usgs.gov)")
    assert "Proxy connection refused" in msg and "Settings > Options > Network" in msg
    assert isinstance(ei.value, ss.StreamStatsError)
    assert not list(tmp_path.glob("*.json"))          # nothing cached


def test_http_error_keeps_status(tmp_path, fake):
    fake(503, b"busy")
    c = ss.StreamStatsClient(tmp_path, fetcher=qt.QgisFetcher(), transport="qgis",
                             min_interval_s=0, retries=0)
    with pytest.raises(ss.StreamStatsError, match="HTTP 503"):
        c.request("GET", "https://streamstats.usgs.gov/x")


def test_network_error_shown_in_dialog(tmp_path, fake):
    fake(None, error="Host streamstats.usgs.gov not found")
    d = sd.StreamStatsDialog(None, None, run_sync=True)
    d.folder.setText(str(tmp_path / "out"))
    d.lat.setValue(46.98337)
    d.lon.setValue(-97.39113)
    d.start()
    assert d.status.text() == "Could not reach StreamStats"
    assert "Settings > Options > Network" in d.log.toPlainText()
    assert d.checks.blocked
    d.close()


def test_make_fetcher_falls_back_to_urllib_without_qgis(monkeypatch):
    monkeypatch.setattr(qt, "qgis_available", lambda: False)
    f, name = qt.make_fetcher()
    assert f is ss.http_fetch and name == "urllib"


def test_cancel_aborts_request_in_flight():
    f = qt.QgisFetcher()
    f.feedback = _FakeFeedback()
    task = sd.StreamStatsTask(lambda t: None, lambda t: None)
    task.fetcher = f
    task.cancel()
    assert f.feedback.isCanceled() and task.isCanceled()


# ---------------------------------------------------------------- real requests, 127.0.0.1 only
class _Echo(BaseHTTPRequestHandler):
    def _answer(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        code = 404 if "missing" in self.path else 200
        out = json.dumps({"path": self.path, "method": self.command, "body": body.decode(),
                          "ua": self.headers.get("User-Agent", ""),
                          "client": self.headers.get("X-Client", "")}).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    do_GET = do_POST = _answer

    def log_message(self, *a):
        pass


@pytest.fixture(scope="module")
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Echo)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    yield "http://127.0.0.1:%d" % srv.server_address[1], srv.server_address[1]
    srv.shutdown()


def test_real_request_sends_user_agent_and_keeps_http_errors(server):
    base, _ = server
    f = qt.QgisFetcher()
    st, raw = f("POST", base + "/ok", b'{"x":1}', {"User-Agent": "DamSafety_PMP/test",
                                                  "Content-Type": "application/json"})
    got = json.loads(raw)
    assert st == 200 and got["method"] == "POST" and got["body"] == '{"x":1}'
    assert got["client"] == "DamSafety_PMP/test" and "QGIS/" in got["ua"]
    st, raw = f("GET", base + "/missing", None, {"User-Agent": "DamSafety_PMP/test"})
    assert st == 404 and json.loads(raw)["path"] == "/missing"


def test_real_closed_port_gives_proxy_hint():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()                                           # nothing listens there now
    with pytest.raises(ss.NetworkError, match="Settings > Options > Network"):
        qt.QgisFetcher()("GET", "http://127.0.0.1:%d/x" % port, None, {})


def test_real_request_follows_qgis_proxy_setting(server):
    """Proxy set as in Settings > Options > Network; the local server plays the proxy."""
    from qgis.core import QgsSettings
    _, port = server
    keys = {"proxy/proxyEnabled": True, "proxy/proxyType": "HttpProxy",          # QGIS 3
            "proxy/proxyHost": "127.0.0.1", "proxy/proxyPort": str(port),
            "proxy/proxy-enabled": True, "proxy/proxy-type": "HttpProxy",        # QGIS 4
            "proxy/proxy-host": "127.0.0.1", "proxy/proxy-port": str(port)}
    st_ = QgsSettings()
    old = {k: st_.value(k) for k in keys}
    for k, v in keys.items():
        st_.setValue(k, v)
    nam = QgsNetworkAccessManager.instance()
    nam.setupDefaultProxyAndCache()
    try:
        st, raw = qt.QgisFetcher()("GET", "http://streamstats.example.invalid/via-proxy", None,
                                   {"User-Agent": "DamSafety_PMP/test"})
    finally:
        for k, v in old.items():
            if v is None:
                st_.remove(k)
            else:
                st_.setValue(k, v)
        nam.setupDefaultProxyAndCache()
    # the local server got the request as a proxy: the absolute URL of a host that doesn't exist
    assert st == 200
    assert json.loads(raw)["path"] == "http://streamstats.example.invalid/via-proxy"


def test_real_request_from_a_qgstask_worker_thread(server):
    base, _ = server
    box = {}

    def job(task):
        return qt.QgisFetcher()("GET", base + "/task", None, {"User-Agent": "DamSafety_PMP/test"})

    task = sd.StreamStatsTask(job, lambda t: box.setdefault("done", True))
    QgsApplication.taskManager().addTask(task)
    t0 = time.monotonic()
    while "done" not in box and time.monotonic() - t0 < 60:
        _APP.processEvents()
        time.sleep(0.02)
    assert box.get("done") and task.error is None, task.error
    st, raw = task.result
    assert st == 200 and json.loads(raw)["path"] == "/task"


if __name__ == "__main__":
    rc = pytest.main(["-q", "-p", "no:cacheprovider", __file__])
    sys.stdout.flush()
    os._exit(int(rc))
