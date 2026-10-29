"""QGIS network transport for core/streamstats.py (QgsBlockingNetworkRequest).

The StreamStats client takes any `fetcher(method, url, body, headers) -> (status, body)`. In
QGIS this one is used, so requests go through QgsNetworkAccessManager and follow QGIS's proxy
(Settings > Options > Network), authentication and SSL settings. It is called from the QgsTask
worker thread: QgsBlockingNetworkRequest is safe to use there (it runs its own event loop on the
thread's network access manager). Outside QGIS (tests, scripts) `make_fetcher` falls back to the
client's urllib transport.

User-Agent: QgsNetworkAccessManager replaces any User-Agent with its own ("Mozilla/5.0
QGIS/<version>/<OS>"), so the client's User-Agent string ("DamSafety_PMP/0.4.0 (QGIS plugin;
...)") is sent in the X-Client header instead. (A request preprocessor could rewrite the
User-Agent, but QgsNetworkAccessManager.setRequestPreprocessor crashes QGIS 3.34's Python
bindings, so it is not used.)
"""
from typing import Dict, Optional, Tuple

from .core import streamstats as ss


def _qgis_classes():
    from qgis.core import QgsBlockingNetworkRequest, QgsFeedback
    from qgis.PyQt.QtCore import QUrl
    from qgis.PyQt.QtNetwork import QNetworkRequest
    return QgsBlockingNetworkRequest, QgsFeedback, QUrl, QNetworkRequest


CLIENT_HEADER = b"X-Client"     # carries the client User-Agent string (QGIS sets its own)


def qgis_available() -> bool:
    try:
        from qgis.core import QgsApplication
        _qgis_classes()
    except Exception:                                                 # noqa: BLE001
        return False
    return QgsApplication.instance() is not None


def _enum(owner, scope, name):
    """Scoped enum on Qt6 / QGIS 4, with the unscoped spelling of older bindings as fallback."""
    sc = getattr(owner, scope, None)
    return getattr(sc, name) if sc is not None and hasattr(sc, name) else getattr(owner, name)


class QgisFetcher:
    """fetcher(method, url, body, headers) -> (status, body) through QgsBlockingNetworkRequest.

    Any HTTP answer is returned with its status and body (the client turns non-200 into an
    error). No HTTP answer (connection, DNS, proxy, SSL, timeout) raises ss.NetworkError with a
    hint to check QGIS's proxy settings. `cancel()` aborts the request in flight. The timeout
    is QGIS's network timeout (Settings > Options > Network)."""

    def __init__(self) -> None:
        self.feedback = None

    def cancel(self) -> None:
        if self.feedback is not None:
            self.feedback.cancel()

    def __call__(self, method: str, url: str, body: Optional[bytes],
                 headers: Dict[str, str]) -> Tuple[int, bytes]:
        Req, Feedback, QUrl, QNetworkRequest = _qgis_classes()
        nr = QNetworkRequest(QUrl(url))
        for k, v in headers.items():
            name = CLIENT_HEADER if k.lower() == "user-agent" else k.encode("latin-1")
            nr.setRawHeader(name, v.encode("latin-1"))
        try:
            from qgis.core import QgsSetRequestInitiatorClass
            QgsSetRequestInitiatorClass(nr, "DamSafety_PMP_StreamStats")
        except Exception:                                             # noqa: BLE001
            pass
        self.feedback = fb = Feedback()
        req = Req()
        if method == "GET":
            err = req.get(nr, True, fb)
        elif method == "POST":
            err = req.post(nr, body or b"", True, fb)
        else:
            raise ValueError("unsupported method %s" % method)
        self.feedback = None
        if fb.isCanceled():
            raise ss.Cancelled("cancelled")
        reply = req.reply()
        status = reply.attribute(_enum(QNetworkRequest, "Attribute", "HttpStatusCodeAttribute"))
        try:
            status = int(status)
        except (TypeError, ValueError):
            status = 0
        if status > 0:                   # an HTTP answer, also 4xx / 5xx
            return status, bytes(reply.content())
        if err == _enum(Req, "ErrorCode", "TimeoutError"):
            reason = "no answer within QGIS's network timeout"
        else:
            reason = req.errorMessage() or reply.errorString() or "no answer"
        raise ss.NetworkError(url, reason, ss.QGIS_PROXY_HINT)


def make_fetcher():
    """-> (fetcher, transport name): QGIS networking when QGIS is running, else urllib."""
    if qgis_available():
        return QgisFetcher(), "qgis"
    return ss.http_fetch, "urllib"
