"""Hazard 2.22.2 (fulltest 2026-10-30): report / map book legends are set to a manual layer
list without the QGIS 4 DeprecationWarning for QgsLayoutItemLegend.setAutoUpdateModel()
(compat.legend_manual: setSyncMode(Manual) on QGIS 4, setAutoUpdateModel(False) on QGIS 3).

    QT_QPA_PLATFORM=offscreen python3 DamSafety_Hazard/tests/test_legend_sync.py
"""
import os
import sys
import warnings

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qgis.core import (QgsApplication, QgsLayout, QgsLayoutItemLegend,  # noqa: E402
                       QgsProject, QgsVectorLayer)
_app = QgsApplication.instance() or QgsApplication([], True)
_app.setPrefixPath(os.environ.get("QGIS_PREFIX_PATH", "/home/box/miniforge3/envs/qgis334"), True)
_app.initQgis()

from DamSafety_Hazard.compat import legend_manual  # noqa: E402

FAILS = []


def check(what, ok, got=""):
    print("{:4} {}{}".format("ok" if ok else "FAIL", what, "" if ok else "  -> {!r}".format(got)))
    if not ok:
        FAILS.append(what)


def main():
    proj = QgsProject.instance()
    a = QgsVectorLayer("Point?crs=EPSG:4326", "a", "memory")
    b = QgsVectorLayer("Point?crs=EPSG:4326", "b", "memory")
    proj.addMapLayers([a, b])
    layout = QgsLayout(proj)
    leg = QgsLayoutItemLegend(layout)
    with warnings.catch_warnings(record=True) as got:
        warnings.simplefilter("always")
        legend_manual(leg)
    dep = [str(w.message) for w in got if issubclass(w.category, DeprecationWarning)]
    check("no DeprecationWarning", not dep, dep)
    if hasattr(leg, "syncMode"):
        check("QGIS 4: sync mode is Manual", "Manual" in repr(leg.syncMode()), leg.syncMode())
    else:
        check("QGIS 3: auto update off", leg.autoUpdateModel() is False)
    root = leg.model().rootGroup()
    root.removeAllChildren()
    root.addLayer(a)
    proj.addMapLayer(QgsVectorLayer("Point?crs=EPSG:4326", "c", "memory"))
    names = [n.name() for n in root.children()]
    check("the legend keeps its own layer list", names == ["a"], names)
    print("\n{} failure(s)".format(len(FAILS)))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
