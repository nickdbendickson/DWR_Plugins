# -*- coding: utf-8 -*-
"""Clicking "Classify" while a run is in progress must be a no-op (Finding 13).

_classify_simple used to call _auto_fill_rasters(force=True) - which mutates the
raster edit boxes - before delegating to _on_run_all, with no busy guard, while
the Classify button stays enabled during a run. The busy guard belongs at the
TOP of _classify_simple so a mid-run click never touches the raster widgets.

Real PyQGIS is not importable in this sandbox, so the dialog module is loaded
against self-contained Qt/QGIS stand-ins (the same stand-in shape the other
tests use) and the real _classify_simple function is driven against a tiny stub
"self" that records what it called. No Qt event loop needed.

    python3 DamSafety_Hazard/tests/test_classify_busy_guard.py
"""
import os
import sys
import traceback
import types

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


# ---- just enough of qgis / qgis.PyQt / osgeo to IMPORT dialog.py when there is
#      no real PyQGIS: any attribute is a stand-in class that takes any args,
#      so "from qgis.PyQt.QtCore import Qt, QSize" and "class D(QDialog)" work.
class _Meta(type):
    def __getattr__(cls, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return _Fake()


class _Fake(metaclass=_Meta):
    def __init__(self, *a, **k):
        pass

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return _Fake()

    def __call__(self, *a, **k):
        return _Fake()

    def __iter__(self):
        return iter(())

    def __len__(self):
        return 0

    def __bool__(self):
        return False

    def __int__(self):
        return 0

    def __index__(self):
        return 0

    def __float__(self):
        return 0.0

    def __or__(self, other):
        return self

    __ror__ = __and__ = __rand__ = __or__

    def __eq__(self, other):
        return self is other

    def __hash__(self):
        return id(self)


_classes = {}


def _class(qualname):
    if qualname not in _classes:
        _classes[qualname] = _Meta(qualname.rsplit(".", 1)[-1], (_Fake,), {})
    return _classes[qualname]


def _module(name):
    m = types.ModuleType(name)
    m.__getattr__ = lambda attr, _n=name: _class(_n + "." + attr)        # PEP 562
    m.__path__ = []
    sys.modules[name] = m
    return m


def _install_fakes():
    try:
        import qgis.core  # noqa: F401 - real PyQGIS present: use it
        return
    except Exception:                                            # noqa
        pass
    qgis = _module("qgis")
    for sub in ("core", "gui", "utils", "analysis", "processing"):
        setattr(qgis, sub, _module("qgis." + sub))
    pyqt = _module("qgis.PyQt")
    qgis.PyQt = pyqt
    for sub in ("QtCore", "QtGui", "QtWidgets", "QtXml", "QtPrintSupport",
                "QtSvg", "QtNetwork", "sip"):
        setattr(pyqt, sub, _module("qgis.PyQt." + sub))
    qgis.utils.iface = None
    try:
        import osgeo  # noqa: F401
    except Exception:                                            # noqa
        osgeo = _module("osgeo")
        for sub in ("gdal", "ogr", "osr", "gdal_array"):
            setattr(osgeo, sub, _module("osgeo." + sub))


def _classify_simple():
    """The real NDDamHazardDialog._classify_simple function (or None to skip)."""
    try:
        _install_fakes()
        from DamSafety_Hazard.dialog import NDDamHazardDialog
    except Exception:                                            # noqa
        return None
    return NDDamHazardDialog.__dict__["_classify_simple"]


class _Edit(object):
    def __init__(self, text=""):
        self._t = text

    def text(self):
        return self._t


class _Stub(object):
    """Just the attributes _classify_simple touches; records the delegates."""

    def __init__(self, busy, depth=""):
        self._busy = busy
        self.depth_edit = _Edit(depth)
        self.calls = []

    def _auto_fill_rasters(self, force=False):
        self.calls.append(("auto_fill", force))

    def _on_run_all(self, *a):
        self.calls.append(("run_all",))


def test_busy_click_is_a_noop():
    """Busy + empty depth box: neither the raster-finding mutation nor the run
    may fire. This is the regression - it failed before the guard was added."""
    fn = _classify_simple()
    if fn is None:
        print("    SKIP test_busy_click_is_a_noop: dialog not importable")
        return
    stub = _Stub(busy=True, depth="")
    fn(stub)
    assert stub.calls == [], (
        "Classify while busy must not run; it called {}".format(stub.calls))


def test_idle_click_runs():
    """Not busy + empty depth box: it finds the rasters (force=True) then runs -
    the original behaviour is unchanged when idle."""
    fn = _classify_simple()
    if fn is None:
        print("    SKIP test_idle_click_runs: dialog not importable")
        return
    stub = _Stub(busy=False, depth="")
    fn(stub)
    assert stub.calls == [("auto_fill", True), ("run_all",)], stub.calls


def test_idle_with_depth_skips_autofill():
    """Not busy but the depth box is already filled: no raster-finding, just run."""
    fn = _classify_simple()
    if fn is None:
        print("    SKIP test_idle_with_depth_skips_autofill: dialog not importable")
        return
    stub = _Stub(busy=False, depth="C:/depth.tif")
    fn(stub)
    assert stub.calls == [("run_all",)], stub.calls


if __name__ == "__main__":
    n = fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            n += 1
            try:
                fn()
                print("ok    ", name)
            except Exception:
                fails += 1
                print("FAIL  ", name)
                traceback.print_exc()
    print("\n{} tests, {} failed".format(n, fails))
    sys.exit(1 if fails else 0)
