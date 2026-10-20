"""Version-independent Qt and QGIS names for QGIS 3.28 - 3.44 (Qt5 / PyQt5) and QGIS 4.x
(Qt6 / PyQt6).

PyQt6 only accepts fully qualified enums (QNetworkRequest.CacheLoadControl.AlwaysNetwork,
Qgis.MessageLevel.Warning, QgsTask.Flag.CanCancel, ...) and QAction moved to QtGui. Each name is
resolved here once, new spelling first and legacy spelling second, so the rest of the plugin never
uses a version-specific name. The same getter-based pattern as the sibling nd_data_updater's
qtcompat.py and the Hazard plugin's compat.py.
"""

from qgis.PyQt.QtNetwork import QNetworkRequest
from qgis.core import Qgis, QgsBlockingNetworkRequest, QgsTask

try:                                   # Qt6: QtGui, Qt5: QtWidgets
    from qgis.PyQt.QtGui import QAction  # noqa: F401
except ImportError:                    # pragma: no cover - depends on Qt build
    from qgis.PyQt.QtWidgets import QAction  # noqa: F401

try:
    from qgis.core import QgsMapLayerType
except ImportError:                    # pragma: no cover - very new builds
    QgsMapLayerType = None


def _first(*getters):
    """Value of the first getter that works; AttributeError when none does."""
    for g in getters:
        try:
            v = g()
        except Exception:
            continue
        if v is not None:
            return v
    raise AttributeError("none of the enum spellings exist in this Qt / QGIS build")


# network
CACHE_LOAD_CONTROL = _first(lambda: QNetworkRequest.Attribute.CacheLoadControlAttribute,
                            lambda: QNetworkRequest.CacheLoadControlAttribute)
ALWAYS_NETWORK = _first(lambda: QNetworkRequest.CacheLoadControl.AlwaysNetwork,
                        lambda: QNetworkRequest.AlwaysNetwork)
HTTP_STATUS = _first(lambda: QNetworkRequest.Attribute.HttpStatusCodeAttribute,
                     lambda: QNetworkRequest.HttpStatusCodeAttribute)
BLOCKING_NO_ERROR = _first(lambda: QgsBlockingNetworkRequest.ErrorCode.NoError,
                           lambda: QgsBlockingNetworkRequest.NoError)

# QGIS
MSG_INFO = _first(lambda: Qgis.MessageLevel.Info, lambda: Qgis.Info)
MSG_WARNING = _first(lambda: Qgis.MessageLevel.Warning, lambda: Qgis.Warning)
TASK_CAN_CANCEL = _first(lambda: QgsTask.Flag.CanCancel, lambda: QgsTask.CanCancel)
VECTOR_LAYER = _first(lambda: Qgis.LayerType.Vector,
                      lambda: QgsMapLayerType.VectorLayer)
