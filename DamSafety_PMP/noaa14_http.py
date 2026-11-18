"""Network for the NOAA Atlas 14 tools (DWR only; the shared core in noaa14/ takes it as arguments).

The same behaviour as DamIT's damit_core/lib/http.py:

* ``get_bytes(url, timeout=60)`` (the PFDS point query) goes through QGIS networking
  (QgsBlockingNetworkRequest: the proxy, authentication and SSL settings of the QGIS profile)
  when a QGIS application is running, else urllib (which follows HTTPS_PROXY / HTTP_PROXY).
* ``download(url, dest)`` (the Atlas 14 GIS zips and temporal files) streams with urllib to a
  temporary file beside ``dest`` and renames it when complete, so a cut download never leaves a
  half file in the cache.

Every call has a timeout and nothing retries in a loop. Failures raise HttpError (an IOError)
with the HTTP status when there was one; gisgrid.fetch_zip turns a 404 into "NOAA has not
published ...". QGIS replaces any User-Agent with its own, so the tool's identification goes in
the X-Client header there (as qgis_transport.py does for StreamStats).
"""
import configparser
import os
import shutil
import tempfile
import urllib.error
import urllib.request

_HERE = os.path.dirname(os.path.abspath(__file__))


def _plugin_version():
    try:
        cp = configparser.ConfigParser(interpolation=None)
        cp.read(os.path.join(_HERE, "metadata.txt"), encoding="utf-8")
        return cp["general"].get("version", "0").strip()
    except Exception:                                             # noqa: BLE001 - only a label
        return "0"


USER_AGENT = "DamSafety_PMP/{} (QGIS plugin; North Dakota dam safety; NOAA Atlas 14)".format(_plugin_version())
CLIENT_HEADER = b"X-Client"


class HttpError(IOError):
    def __init__(self, url, status=None, message=""):
        self.url, self.status = url, status
        super(HttpError, self).__init__("{} {}: {}".format(status or "error", url, message).strip())


def _enum(owner, scope, name):
    """Scoped enum on Qt6 / QGIS 4, with the unscoped spelling of older bindings as fallback."""
    sc = getattr(owner, scope, None)
    return getattr(sc, name) if sc is not None and hasattr(sc, name) else getattr(owner, name)


def qgis_running():
    try:
        from qgis.core import QgsApplication
    except Exception:                                             # noqa: BLE001 - no QGIS here
        return False
    return QgsApplication.instance() is not None


def _qgis_get(url, timeout):
    from qgis.core import QgsBlockingNetworkRequest
    from qgis.PyQt.QtCore import QUrl
    from qgis.PyQt.QtNetwork import QNetworkRequest
    req = QNetworkRequest(QUrl(url))
    req.setRawHeader(CLIENT_HEADER, USER_AGENT.encode("latin-1"))
    try:
        req.setTransferTimeout(int(timeout * 1000))
    except AttributeError:                                        # pragma: no cover - Qt < 5.15
        pass
    br = QgsBlockingNetworkRequest()
    err = br.get(req, True)
    reply = br.reply()
    status = reply.attribute(_enum(QNetworkRequest, "Attribute", "HttpStatusCodeAttribute"))
    try:
        status = int(status)
    except (TypeError, ValueError):
        status = None
    if status and status >= 400:
        raise HttpError(url, status, "HTTP error")
    if err != _enum(QgsBlockingNetworkRequest, "ErrorCode", "NoError"):
        raise HttpError(url, status, (br.errorMessage() or reply.errorString() or "no answer") +
                        " (check the internet connection and the proxy settings in QGIS: Settings > "
                        "Options > Network)")
    return bytes(reply.content())


def get_bytes(url, timeout=60):
    if qgis_running():
        return _qgis_get(url, timeout)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:   # noqa: S310 - https URLs the core builds
            return r.read()
    except urllib.error.HTTPError as e:
        e.close()                                # its response holds the socket open
        raise HttpError(url, e.code, str(e.reason))
    except (urllib.error.URLError, OSError) as e:
        raise HttpError(url, None, str(e))


def download_file(url, dest, timeout=120, cancelled=None, max_bytes=None):
    """Stream ``url`` to ``dest`` (a temporary file beside it, renamed when complete).
    ``cancelled()`` True stops it; ``max_bytes`` refuses a larger download. Returns bytes written."""
    folder = os.path.dirname(os.path.abspath(dest))
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".part_", dir=folder)
    done = 0
    try:
        # the file is opened before the request, so it is closed before the cleanup below even when the request
        # fails (an open file cannot be removed on Windows, and that error would hide the HTTP status)
        with os.fdopen(fd, "wb") as out:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310
                total = int(r.headers.get("Content-Length") or 0) or None
                if max_bytes and total and total > max_bytes:
                    raise HttpError(url, None, "download is {:.0f} MB, over the {:.0f} MB limit".format(
                        total / 1e6, max_bytes / 1e6))
                while True:
                    if cancelled is not None and cancelled():
                        raise HttpError(url, None, "cancelled")
                    chunk = r.read(1 << 20)
                    if not chunk:
                        break
                    out.write(chunk)
                    done += len(chunk)
                    if max_bytes and done > max_bytes:
                        raise HttpError(url, None, "download passed the {:.0f} MB limit".format(max_bytes / 1e6))
        shutil.move(tmp, dest)
        return done
    except urllib.error.HTTPError as e:
        e.close()                                # its response holds the socket open
        raise HttpError(url, e.code, str(e.reason))
    except (urllib.error.URLError, OSError) as e:
        if isinstance(e, HttpError):
            raise
        raise HttpError(url, None, str(e))
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


class Client(object):
    """The ``http`` object atlas14.fetch takes (tests pass a fake with get_bytes)."""

    def get_bytes(self, url, timeout=60):
        return get_bytes(url, timeout)


def downloader(timeout=120, max_bytes=None, cancelled=None, log=None):
    """A ``download(url, dest)`` callable for gisgrid.fetch_zip / temporal.atlas14_curve."""
    def download(url, dest):
        if log is not None:
            log("Downloading {}".format(url))
        return download_file(url, dest, timeout=timeout, cancelled=cancelled, max_bytes=max_bytes)
    return download
