"""Test helper (0.17.23): the qgis mock (tests/qgis_mock) with the REAL PyQt5 widgets in place of
the widget fakes, so the popup can be built offscreen (QT_QPA_PLATFORM=offscreen) and grabbed.
load() returns the QApplication, or None when PyQt5 is not installed."""
import os
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))


def load():
    try:
        from PyQt5 import QtCore, QtGui, QtWidgets
    except ImportError:
        return None
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    sys.path.insert(0, os.path.join(HERE, "qgis_mock"))
    sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
    sys.path.insert(0, HERE)
    import qgis  # noqa: F401  (the mock package)
    pkg = types.ModuleType("qgis.PyQt")
    pkg.__path__ = []
    core = types.ModuleType("qgis.PyQt.QtCore")
    core.__dict__.update({k: v for k, v in QtCore.__dict__.items() if not k.startswith("__")})
    core.FLAVOR, core.QT6 = "3.28", False
    gui = types.ModuleType("qgis.PyQt.QtGui")
    gui.__dict__.update({k: v for k, v in QtGui.__dict__.items() if not k.startswith("__")})
    wid = types.ModuleType("qgis.PyQt.QtWidgets")
    wid.__dict__.update({k: v for k, v in QtWidgets.__dict__.items() if not k.startswith("__")})
    sip = types.ModuleType("qgis.PyQt.sip")
    for name, mod in (("qgis.PyQt", pkg), ("qgis.PyQt.QtCore", core), ("qgis.PyQt.QtGui", gui),
                      ("qgis.PyQt.QtWidgets", wid), ("qgis.PyQt.sip", sip)):
        sys.modules[name] = mod
    pkg.QtCore, pkg.QtGui, pkg.QtWidgets, pkg.sip = core, gui, wid, sip
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication(["test"])
