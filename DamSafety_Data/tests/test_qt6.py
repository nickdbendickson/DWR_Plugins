"""QGIS 4 (Qt6) readiness for DamSafety_Data (finding H14).

metadata.txt declares supportsQt6=True, but http_qgis.py and plugin.py used to read Qt/QGIS enums
the Qt5-only way (QNetworkRequest.CacheLoadControlAttribute, Qgis.Info, QgsTask.CanCancel,
QgsMapLayerType.VectorLayer, ...), which raise AttributeError on PyQt6 / QGIS 4 so the plugin
would not load. The fix routes every such name through qtcompat.py (the same getter-based pattern
as the sibling nd_data_updater/qtcompat.py and DamSafety_Hazard/compat.py).

This test runs without QGIS. It (1) source-scans the two files for Qt5-only spellings, (2) loads
qtcompat.py against a fake qgis that exposes ONLY the Qt6 scoped names and checks every helper
resolves to the scoped value, (3) does the same against a Qt5-only fake so behaviour is unchanged
on Qt5, and (4) confirms metadata still declares Qt6.

Run:  python3 DamSafety_Data/tests/test_qt6.py
"""
import configparser
import importlib.util
import os
import re
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(HERE)

# Qt5-only spellings that must not appear in the two fixed files.
UNSCOPED = re.compile(
    r"\bQNetworkRequest\.(?:HttpStatusCodeAttribute|CacheLoadControlAttribute|AlwaysNetwork)\b"
    r"|\bQgsBlockingNetworkRequest\.NoError\b"
    r"|\bQgis\.(?:Info|Warning|Critical|Success)\b"
    r"|\bQgsTask\.CanCancel\b|\bQgsMapLayerType\.VectorLayer\b"
    r"|from qgis\.PyQt\.QtWidgets import[^\n]*\bQAction\b")


class NS:
    """A stand-in enum class / namespace whose attributes are set from kwargs."""

    def __init__(self, **kw):
        self.__dict__.update(kw)


def _install_fake_qgis(style):
    """Put a fake qgis package tree in sys.modules. style is 'qt6' or 'qt5'.

    Each fake exposes ONLY one spelling so the test proves which branch resolved: the Qt6 fake has
    only scoped names and no QgsMapLayerType (a new build), the Qt5 fake only the unscoped names.
    Returns the sentinel values the helpers are expected to resolve to.
    """
    want = {k: object() for k in
            ("cache_attr", "always_net", "http_attr", "no_error", "info", "warning",
             "can_cancel", "vector")}
    action = object()

    if style == "qt6":
        qnetworkrequest = NS(
            Attribute=NS(CacheLoadControlAttribute=want["cache_attr"],
                         HttpStatusCodeAttribute=want["http_attr"]),
            CacheLoadControl=NS(AlwaysNetwork=want["always_net"]))
        qgis = NS(MessageLevel=NS(Info=want["info"], Warning=want["warning"]),
                  LayerType=NS(Vector=want["vector"]))
        qgs_blocking = NS(ErrorCode=NS(NoError=want["no_error"]))
        qgs_task = NS(Flag=NS(CanCancel=want["can_cancel"]))
        qgsmaplayertype = None                       # absent: VECTOR_LAYER must use Qgis.LayerType
        qtgui = types.ModuleType("qgis.PyQt.QtGui")
        qtgui.QAction = action                       # Qt6 home of QAction
        qtwidgets = types.ModuleType("qgis.PyQt.QtWidgets")
    else:                                            # qt5: only the legacy unscoped spellings
        qnetworkrequest = NS(CacheLoadControlAttribute=want["cache_attr"],
                             HttpStatusCodeAttribute=want["http_attr"],
                             AlwaysNetwork=want["always_net"])
        qgis = NS(Info=want["info"], Warning=want["warning"])
        qgs_blocking = NS(NoError=want["no_error"])
        qgs_task = NS(CanCancel=want["can_cancel"])
        qgsmaplayertype = NS(VectorLayer=want["vector"])
        qtgui = types.ModuleType("qgis.PyQt.QtGui")  # no QAction here on Qt5
        qtwidgets = types.ModuleType("qgis.PyQt.QtWidgets")
        qtwidgets.QAction = action                   # Qt5 home of QAction

    core = types.ModuleType("qgis.core")
    core.Qgis = qgis
    core.QgsBlockingNetworkRequest = qgs_blocking
    core.QgsTask = qgs_task
    if qgsmaplayertype is not None:
        core.QgsMapLayerType = qgsmaplayertype

    qtnetwork = types.ModuleType("qgis.PyQt.QtNetwork")
    qtnetwork.QNetworkRequest = qnetworkrequest
    pyqt = types.ModuleType("qgis.PyQt")
    pyqt.QtNetwork = qtnetwork
    pyqt.QtGui = qtgui
    pyqt.QtWidgets = qtwidgets
    root = types.ModuleType("qgis")
    root.core = core
    root.PyQt = pyqt

    for name, mod in {
            "qgis": root, "qgis.core": core, "qgis.PyQt": pyqt,
            "qgis.PyQt.QtNetwork": qtnetwork, "qgis.PyQt.QtGui": qtgui,
            "qgis.PyQt.QtWidgets": qtwidgets}.items():
        sys.modules[name] = mod
    want["action"] = action
    return want


def _load_qtcompat_fresh(modname):
    for name in [n for n in sys.modules if n == modname]:
        del sys.modules[name]
    spec = importlib.util.spec_from_file_location(modname, os.path.join(PLUGIN, "qtcompat.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[modname] = mod
    spec.loader.exec_module(mod)
    return mod


def _purge_fake_qgis():
    for name in [n for n in list(sys.modules) if n == "qgis" or n.startswith("qgis.")]:
        del sys.modules[name]


def check_metadata_declares_qt6():
    cp = configparser.ConfigParser(interpolation=None)
    cp.read(os.path.join(PLUGIN, "metadata.txt"), encoding="utf-8")
    assert cp["general"].get("supportsQt6", "").strip() == "True", "supportsQt6 is not True"


def check_no_qt5_only_spelling():
    bad = []
    for fname in ("http_qgis.py", "plugin.py"):
        path = os.path.join(PLUGIN, fname)
        with open(path, encoding="utf-8") as f:
            for i, line in enumerate(f, 1):
                code = line.split("#", 1)[0]
                if UNSCOPED.search(code):
                    bad.append("{}:{}: {}".format(fname, i, line.strip()))
    assert not bad, "Qt5-only spellings remain:\n" + "\n".join(bad)


def check_resolves_under(style):
    want = _install_fake_qgis(style)
    try:
        qt = _load_qtcompat_fresh("qtcompat_%s" % style)
    finally:
        _purge_fake_qgis()
    pairs = {
        "CACHE_LOAD_CONTROL": want["cache_attr"], "ALWAYS_NETWORK": want["always_net"],
        "HTTP_STATUS": want["http_attr"], "BLOCKING_NO_ERROR": want["no_error"],
        "MSG_INFO": want["info"], "MSG_WARNING": want["warning"],
        "TASK_CAN_CANCEL": want["can_cancel"], "VECTOR_LAYER": want["vector"],
        "QAction": want["action"]}
    for name, expected in pairs.items():
        got = getattr(qt, name)
        assert got is expected, "%s under %s resolved to %r, expected %r" % (
            name, style, got, expected)


TESTS = [
    ("metadata_declares_qt6", check_metadata_declares_qt6),
    ("no_qt5_only_spelling_in_fixed_files", check_no_qt5_only_spelling),
    ("qtcompat_resolves_under_qt6_only_fake", lambda: check_resolves_under("qt6")),
    ("qtcompat_resolves_under_qt5_only_fake", lambda: check_resolves_under("qt5")),
]


def main():
    failed = 0
    for name, fn in TESTS:
        try:
            fn()
            print("ok   -", name)
        except Exception as e:  # noqa: BLE001 - this is the test reporter
            failed += 1
            print("FAIL -", name, "::", e)
    print("\n%d passed, %d failed" % (len(TESTS) - failed, failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
