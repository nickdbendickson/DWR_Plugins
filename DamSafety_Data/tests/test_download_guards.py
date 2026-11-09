"""audit R11 (2026-11-07): a data download is checked against the free disk space of its folder
before it starts, streamed to <file>.part and renamed only when complete (damsuite.updatecore
download_file / ensure_free_space / remote_size; the Data plugin's urllib client streams in
1 MB chunks, its QGIS client streams through QgsNetworkAccessManager - that path needs QGIS and
is not run here). A local HTTP server plays the publisher; no network.

    python3 DamSafety_Data/tests/test_download_guards.py
"""
import http.server
import os
import shutil
import sys
import tempfile
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from DamSafety_Data import http_urllib                      # noqa: E402
from DamSafety_Data.damsuite import updatecore as UC        # noqa: E402

BODY = bytes(range(256)) * 4096                             # 1 MB, not compressible-looking


class _Handler(http.server.BaseHTTPRequestHandler):
    """/file: the body with Content-Length; /cut: announces the body but sends half of it;
    /nosize: the body without Content-Length (chunked)."""
    requests = []

    def do_HEAD(self):                                       # noqa: N802
        self.requests.append(("HEAD", self.path))
        self.send_response(200)
        self.send_header("Content-Length", str(len(BODY)))
        self.end_headers()

    def do_GET(self):                                        # noqa: N802
        self.requests.append(("GET", self.path))
        if self.path == "/cut":
            self.send_response(200)
            self.send_header("Content-Length", str(len(BODY)))
            self.end_headers()
            self.wfile.write(BODY[: len(BODY) // 2])
            self.wfile.flush()
            self.close_connection = True
            return
        self.send_response(200)
        if self.path != "/nosize":
            self.send_header("Content-Length", str(len(BODY)))
            self.end_headers()
            self.wfile.write(BODY)
        else:
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for i in range(0, len(BODY), 65536):
                chunk = BODY[i:i + 65536]
                self.wfile.write(("%x\r\n" % len(chunk)).encode() + chunk + b"\r\n")
            self.wfile.write(b"0\r\n\r\n")

    def log_message(self, *a):
        pass


def _server():
    srv = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, "http://127.0.0.1:{}".format(srv.server_port)


def _fb(log=None):
    return UC.Feedback(log=log)


def test_download_file_streams_to_part_and_renames():
    srv, base = _server()
    d = tempfile.mkdtemp()
    try:
        dest = os.path.join(d, "sub", "data.bin")
        logs = []
        size = UC.download_file(http_urllib.UrllibHttp(), base + "/file", dest, _fb(logs.append))
        assert size == len(BODY), size
        assert os.path.isfile(dest) and not os.path.exists(dest + ".part")
        with open(dest, "rb") as f:
            assert f.read() == BODY
        assert ("HEAD", "/file") in _Handler.requests and ("GET", "/file") in _Handler.requests
    finally:
        srv.shutdown(); srv.server_close(); shutil.rmtree(d, ignore_errors=True)


def test_interrupted_download_leaves_no_part_file_and_says_so():
    srv, base = _server()
    d = tempfile.mkdtemp()
    try:
        dest = os.path.join(d, "data.bin")
        try:
            UC.download_file(http_urllib.UrllibHttp(), base + "/cut", dest, _fb())
        except UC.NetworkError as e:
            assert "interrupted" in str(e), e
        else:
            raise AssertionError("a cut download must raise NetworkError")
        assert not os.path.exists(dest) and not os.path.exists(dest + ".part"), os.listdir(d)
    finally:
        srv.shutdown(); srv.server_close(); shutil.rmtree(d, ignore_errors=True)


def test_unknown_size_still_downloads():
    srv, base = _server()
    d = tempfile.mkdtemp()
    try:
        http = http_urllib.UrllibHttp()
        # the server's HEAD says 1 MB for every path; a client without head() has no size
        class NoHead(object):
            def download(self, url, dest, feedback):
                return http.download(url, dest, feedback)
        dest = os.path.join(d, "x.bin")
        assert UC.download_file(NoHead(), base + "/nosize", dest, _fb()) is None
        assert os.path.getsize(dest) == len(BODY)
        assert UC.remote_size(http, base + "/file") == len(BODY)
    finally:
        srv.shutdown(); srv.server_close(); shutil.rmtree(d, ignore_errors=True)


def test_free_space_check_refuses_with_a_plain_message():
    d = tempfile.mkdtemp()
    try:
        free = UC.free_bytes(d)
        assert free is not None and free > 0
        assert UC.ensure_free_space(d, 0, "x") == free
        # a nested folder that does not exist yet is judged by its nearest existing parent
        assert UC.free_bytes(os.path.join(d, "a", "b", "c")) is not None
        huge = free + 10 * 1024 * 1024 * 1024
        try:
            UC.ensure_free_space(d, huge, "the OSM extract (1,500 MB)")
        except UC.UpdateError as e:
            msg = str(e)
            assert msg.startswith("Not enough disk space for the OSM extract (1,500 MB) in "), msg
            assert d in msg and "free, about" in msg and "needed" in msg and "MB" in msg, msg
            assert "Free some space or choose another data folder" in msg, msg
        else:
            raise AssertionError("ensure_free_space must refuse")
        # the margin: a download that fits but leaves less than MIN_FREE_BYTES is refused too
        try:
            UC.ensure_free_space(d, max(free - UC.MIN_FREE_BYTES // 2, 0), "y")
        except UC.UpdateError:
            pass
        else:
            raise AssertionError("the headroom must be kept free")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_download_file_refuses_before_the_request_when_the_disk_is_full():
    srv, base = _server()
    d = tempfile.mkdtemp()
    real = UC.free_bytes
    _Handler.requests[:] = []
    try:
        UC.free_bytes = lambda folder: 100 * 1024 * 1024          # 100 MB free
        try:
            UC.download_file(http_urllib.UrllibHttp(), base + "/file", os.path.join(d, "f.zip"),
                             _fb(), extract=True, what="the test zip")
        except UC.UpdateError as e:
            assert "Not enough disk space for the test zip (1 MB, extracted)" in str(e), e
        else:
            raise AssertionError("must refuse")
        # the size was asked for; the body was never requested
        assert ("HEAD", "/file") in _Handler.requests
        assert not any(m == "GET" for m, _p in _Handler.requests), _Handler.requests
        assert os.listdir(d) == [], os.listdir(d)
    finally:
        UC.free_bytes = real
        srv.shutdown(); srv.server_close(); shutil.rmtree(d, ignore_errors=True)


def test_http_file_source_uses_the_guarded_download():
    """HttpFileSource.fetch goes through download_file: the data lands under the staging
    folder, no .part is left, and the SHA-256 sidecar is written as before."""
    srv, base = _server()
    d = tempfile.mkdtemp()
    try:
        src = UC.HttpFileSource("t", "Test file", base + "/file", "data.bin", "test",
                                version_mode="headers", extract=False)
        logs = []
        staging = os.path.join(d, ".staging", "t-1")
        version = src.fetch(http_urllib.UrllibHttp(), staging, _fb(logs.append), None)
        assert version.startswith("sha256:"), version
        assert sorted(os.listdir(staging)) == ["data.bin", "data.bin.sha256"], os.listdir(staging)
        assert UC.file_hash(os.path.join(staging, "data.bin"), "sha256") == version[7:]
    finally:
        srv.shutdown(); srv.server_close(); shutil.rmtree(d, ignore_errors=True)


def test_updater_reports_a_full_disk_as_an_error_not_a_crash():
    d = tempfile.mkdtemp()
    real = UC.free_bytes
    try:
        src = UC.HttpFileSource("t", "Test file", "http://example.invalid/x", "x.bin", "test",
                                version_mode="headers", extract=False)
        up = UC.Updater(d, [src], http=http_urllib.UrllibHttp())
        UC.free_bytes = lambda folder: 1024                      # 1 KB free
        res = up.check_and_update(_fb())
        assert len(res) == 1 and res[0].status == "error", res
        assert "Not enough disk space for Test file" in res[0].message, res[0].message
        man = up.load_manifest()
        assert "Not enough disk space" in man["t"]["last_error"], man
    finally:
        UC.free_bytes = real
        shutil.rmtree(d, ignore_errors=True)


def _run():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print("ok     {}".format(t.__name__))
        except Exception as e:  # noqa: BLE001
            failed += 1
            import traceback
            print("FAIL   {}: {}".format(t.__name__, e))
            traceback.print_exc()
    print("\n{} tests, {} failed".format(len(tests), failed))
    return failed


if __name__ == "__main__":
    sys.exit(1 if _run() else 0)
