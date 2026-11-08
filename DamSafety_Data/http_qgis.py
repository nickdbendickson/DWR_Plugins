"""HTTP client that goes through QGIS's own network stack.

Using QgsBlockingNetworkRequest means downloads honour the proxy, SSL and
authentication settings in QGIS > Settings > Options > Network, and use the
macOS certificate store, which avoids the certificate errors plain Python
downloads often hit in the QGIS macOS bundle. It is safe to call from a
QgsTask worker thread.

audit R11 (2026-11-07): download() no longer holds the whole body in RAM (a 1.5 GB NSI or
Geofabrik file was read into a QByteArray, then compared, then written). The body is streamed
to the destination (<file>.part by the caller, damsuite.updatecore.download_file, which also
checks the free disk space first) through QgsNetworkAccessManager's readyRead in a QEventLoop
on this thread - the same mechanism QgsBlockingNetworkRequest uses inside, so the proxy, SSL
and authentication settings still apply. A body of at most SMALL_BYTES still goes through
QgsBlockingNetworkRequest as before. If the streaming path is not available in a QGIS build
(an API difference), the urllib client streams instead, through QGIS's proxy settings
(damsuite.netproxy), and the log says so.
"""

from qgis.core import QgsBlockingNetworkRequest
from qgis.PyQt.QtCore import QUrl
from qgis.PyQt.QtNetwork import QNetworkRequest

from .qtcompat import ALWAYS_NETWORK, BLOCKING_NO_ERROR, CACHE_LOAD_CONTROL, HTTP_STATUS

from .core import Cancelled, NetworkError, UpdateError

USER_AGENT = b"ND-Data-Updater-QGIS/1.0"
# HTTP statuses that mean "the link is wrong", as opposed to "can't connect right now".
PERMANENT_STATUSES = {400, 401, 403, 404, 405, 410}
MAX_BYTES = 2 * 1024 * 1024 * 1024   # refuse a download larger than 2 GB
SMALL_BYTES = 32 * 1024 * 1024       # audit R11: up to this the blocking in-memory get is fine
CANCEL_POLL_MS = 500                 # audit R11: how often a streaming download looks at Cancel


class QgisHttp:
    def __init__(self, qgs_feedback=None):
        # QgsFeedback shared with the task, so Cancel aborts an in-progress download.
        self.qgs_feedback = qgs_feedback

    def _request(self, url, headers=None):
        req = QNetworkRequest(QUrl(url))
        req.setRawHeader(b"User-Agent", USER_AGENT)
        for k, v in (headers or {}).items():
            req.setRawHeader(k.encode(), v.encode())
        req.setAttribute(CACHE_LOAD_CONTROL, ALWAYS_NETWORK)
        return req

    def _check(self, blocking, err, url):
        if self.qgs_feedback is not None and self.qgs_feedback.isCanceled():
            raise Cancelled()
        if err == BLOCKING_NO_ERROR:
            return blocking.reply()
        status = blocking.reply().attribute(HTTP_STATUS)
        try:
            status = int(status)
        except (TypeError, ValueError):
            status = None
        message = blocking.errorMessage() or "network error"
        if status in PERMANENT_STATUSES:
            raise UpdateError(f"HTTP {status} for {url} (the link may have changed)")
        raise NetworkError(f"{message} ({url})")

    @staticmethod
    def _headers(reply):
        return {bytes(name).decode("latin-1").lower(): bytes(reply.rawHeader(name)).decode("latin-1")
                for name in reply.rawHeaderList()}

    def head(self, url):
        blocking = QgsBlockingNetworkRequest()
        try:
            if not hasattr(blocking, "head"):
                raise UpdateError("HTTP 405")
            err = blocking.head(self._request(url), True, self.qgs_feedback)
            return self._headers(self._check(blocking, err, url))
        except UpdateError as e:
            if "HTTP 405" not in str(e) and "HTTP 403" not in str(e):
                raise
        # Server refuses HEAD: ask for a single byte instead.
        blocking = QgsBlockingNetworkRequest()
        err = blocking.get(self._request(url, {"Range": "bytes=0-0"}), True, self.qgs_feedback)
        return self._headers(self._check(blocking, err, url))

    def get_bytes(self, url, headers=None):
        blocking = QgsBlockingNetworkRequest()
        err = blocking.get(self._request(url, headers), True, self.qgs_feedback)
        return bytes(self._check(blocking, err, url).content())

    def get_json(self, url, params=None):
        import json
        import urllib.parse
        if params:
            url = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        raw = self.get_bytes(url)
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        return json.loads(raw)

    def _announced_size(self, url):
        """Content-Length of a HEAD (or one-byte GET) of url, or None (never raises)."""
        try:
            headers = self.head(url)
        except Cancelled:
            raise
        except Exception:  # noqa: BLE001 - the size is a courtesy
            return None
        rng = str(headers.get("content-range") or "")
        if "/" in rng and rng.rsplit("/", 1)[1].strip().isdigit():
            return int(rng.rsplit("/", 1)[1].strip())
        size = str(headers.get("content-length") or "").strip()
        return int(size) if size.isdigit() and int(size) > 1 else None

    def download(self, url, dest, feedback):
        """GET url into the file `dest` (the caller gives a .part path and renames it).
        audit R11: streamed to disk; see the module docstring."""
        size = self._announced_size(url)
        if size is not None and size > MAX_BYTES:
            raise UpdateError(f"download is {size} bytes, over the {MAX_BYTES} byte limit")
        if size is not None and size <= SMALL_BYTES:
            return self._download_blocking(url, dest, feedback)
        try:
            return self._download_streaming(url, dest, feedback, size)
        except (AttributeError, TypeError, ImportError) as e:   # an API difference in this build
            self._log(feedback, f"QGIS streaming download not available here ({e}); "
                                "using the urllib client through QGIS's proxy settings")
        from .http_urllib import UrllibHttp
        return UrllibHttp().download(url, dest, feedback)

    @staticmethod
    def _log(feedback, message):
        log = getattr(feedback, "log", None)
        if log:
            log(message)

    def _download_blocking(self, url, dest, feedback):
        """The pre-R11 path, for a body of at most SMALL_BYTES: one blocking get, the body in
        memory, then written to `dest`."""
        blocking = QgsBlockingNetworkRequest()

        def progress(received, total):
            if total and total > 0:
                feedback.fraction(received / total)

        blocking.downloadProgress.connect(progress)
        err = blocking.get(self._request(url), True, self.qgs_feedback)
        reply = self._check(blocking, err, url)
        content = reply.content()
        if content.size() > MAX_BYTES:
            raise UpdateError(f"download is {content.size()} bytes, over the {MAX_BYTES} byte limit")
        expected = self._headers(reply).get("content-length")
        if expected and expected.isdigit() and int(expected) != content.size():
            raise NetworkError(f"download interrupted ({content.size()} of {expected} bytes)")
        with open(dest, "wb") as out:
            out.write(content.data())

    def _download_streaming(self, url, dest, feedback, size):
        """QgsNetworkAccessManager.get() with the body written to `dest` as it arrives
        (readyRead), a QEventLoop on this thread until finished, cancel checked every
        CANCEL_POLL_MS. Raises NetworkError / UpdateError / Cancelled as the blocking path
        does; AttributeError / TypeError when this QGIS build lacks the API (the caller falls
        back to urllib)."""
        from qgis.core import QgsNetworkAccessManager
        from qgis.PyQt.QtCore import QEventLoop, QTimer
        from qgis.PyQt.QtNetwork import QNetworkReply
        nam = QgsNetworkAccessManager.instance()
        reply = nam.get(self._request(url))
        loop = QEventLoop()
        state = {"written": 0, "cancelled": False, "error": None}
        no_error = getattr(QNetworkReply, "NoError", None)
        if no_error is None:
            no_error = QNetworkReply.NetworkError.NoError
        out = open(dest, "wb")

        def drain():
            try:
                data = reply.readAll()
                if data.size():
                    out.write(data.data())
                    state["written"] += data.size()
                    if state["written"] > MAX_BYTES:
                        state["error"] = UpdateError(
                            f"download passed the {MAX_BYTES} byte limit")
                        reply.abort()
            except Exception as e:  # noqa: BLE001 - reported after the loop
                state["error"] = e
                reply.abort()

        def progress(received, total):
            if total and total > 0:
                feedback.fraction(received / total)
            elif size:
                feedback.fraction(received / size)

        def poll():
            if self.qgs_feedback is not None and self.qgs_feedback.isCanceled():
                state["cancelled"] = True
                reply.abort()

        reply.readyRead.connect(drain)
        reply.downloadProgress.connect(progress)
        reply.finished.connect(loop.quit)
        timer = QTimer()
        timer.setInterval(CANCEL_POLL_MS)
        timer.timeout.connect(poll)
        timer.start()
        try:
            if not reply.isFinished():
                loop.exec() if hasattr(loop, "exec") else loop.exec_()
            drain()
        finally:
            timer.stop()
            out.close()
        try:
            if state["cancelled"]:
                raise Cancelled()
            if state["error"] is not None:
                raise state["error"]
            if reply.error() != no_error:
                status = reply.attribute(HTTP_STATUS)
                try:
                    status = int(status)
                except (TypeError, ValueError):
                    status = None
                if status in PERMANENT_STATUSES:
                    raise UpdateError(f"HTTP {status} for {url} (the link may have changed)")
                raise NetworkError(f"{reply.errorString() or 'network error'} ({url})")
            expected = size
            try:
                cl = reply.header(QNetworkRequest.KnownHeaders.ContentLengthHeader) \
                    if hasattr(QNetworkRequest, "KnownHeaders") else \
                    reply.header(QNetworkRequest.ContentLengthHeader)
                if cl is not None and int(cl) > 0:
                    expected = int(cl)
            except (TypeError, ValueError):
                pass
            if expected and state["written"] != expected:
                raise NetworkError(f"download interrupted ({state['written']} of {expected} bytes)")
        finally:
            reply.deleteLater()
        return state["written"]


def gdal_merger():
    """Return a function that merges GeoJSON files into one GeoPackage layer, or
    None if GDAL's Python bindings are not available."""
    try:
        from osgeo import gdal
    except ImportError:
        return None

    def merge(paths, dest):
        # audit R3 (2026-11-07): no gdal.UseExceptions() here - it is process-wide and was
        # never restored, so one NSI merge left GDAL in exception mode for QGIS and every
        # other plugin. VectorTranslate's None return is checked below in both modes.
        for i, path in enumerate(paths):
            kwargs = dict(format="GPKG", layerName="nsi_structures", dstSRS="EPSG:4326",
                          accessMode=None if i == 0 else "append")
            try:
                options = gdal.VectorTranslateOptions(addFields=i > 0, **kwargs)
            except TypeError:  # very old GDAL without addFields
                options = gdal.VectorTranslateOptions(**kwargs)
            try:
                ds = gdal.VectorTranslate(dest, path, options=options)
            except RuntimeError as e:                 # GDAL already in exception mode
                raise UpdateError(f"Could not add {path} to {dest}: {e}") from None
            if ds is None:
                msg = ""
                try:
                    msg = (gdal.GetLastErrorMsg() or "").strip()
                except Exception:  # noqa
                    pass
                raise UpdateError(f"Could not add {path} to {dest}" + (f": {msg}" if msg else ""))
            ds = None  # close and flush

    return merge
