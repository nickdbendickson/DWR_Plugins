"""Hazard 2.22.1 (DWR-4031 toe10m): the road-crossing note gives the depth grid's real
cell size. It said "Max_Depth_ft is the 30 m depth cell" on a 10 m grid (hard-coded).
No rasters.

    QT_QPA_PLATFORM=offscreen python3 DamSafety_Hazard/tests/test_depth_cell_label.py
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qgis.core import QgsApplication  # noqa: E402

_app = QgsApplication.instance() or QgsApplication([], True)
_app.setPrefixPath(os.environ.get("QGIS_PREFIX_PATH", "/home/box/miniforge3/envs/qgis334"), True)
_app.initQgis()

from DamSafety_Hazard import pipeline as P  # noqa: E402

FAILS = []


def check(name, ok, detail=""):
    print("{} {}{}".format("ok  " if ok else "FAIL", name, "" if ok else "  " + repr(detail)))
    if not ok:
        FAILS.append(name)


def main():
    for cell, want in [((10.0, 10.0), "10 m"), ((30.0, 30.0), "30 m"),
                       ((9.144, 9.144), "9.1 m"), ((1.0, 1.0), "1 m"),
                       ((9.144, 10.0), "9.1 x 10 m"), (None, "depth-grid"),
                       ((0.0, 0.0), "depth-grid"), ("x", "depth-grid")]:
        got = P.depth_cell_label(cell)
        check("depth_cell_label({!r}) == {!r}".format(cell, want), got == want, got)
    # 10 ft cells reported in metres
    got = P.depth_cell_label((10 * P.FT_TO_M, 10 * P.FT_TO_M))
    check("10 ft cell -> 3 m", got == "3 m", got)
    src = open(os.path.join(ROOT, "DamSafety_Hazard", "pipeline.py"), encoding="utf-8").read()
    check("no hard-coded '30 m depth cell' in pipeline.py", "30 m depth cell" not in src)
    check("note uses depth_cell_label", "depth_cell_label(res.get(\"cell_size_m\"))" in src)
    print("\n{} failure(s)".format(len(FAILS)))
    return 1 if FAILS else 0


if __name__ == "__main__":
    _rc = main()
    sys.stdout.flush()
    os._exit(_rc)
