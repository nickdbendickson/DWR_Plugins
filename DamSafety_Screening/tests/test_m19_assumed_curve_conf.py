"""M19 (regression): tool 3's assumed quadratic inventory curve (the dry-dam / shallow-basin
last resort of estimate_storage.py) is a GUESS from the inventory volume and the dam height, not
a DEM measurement. It must be reported as LOW confidence with the assumed source label
(stor_method = 'inventory curve'), never as a high-confidence 'inventory' match - even when the
record carries an inventory NORMAL storage.

Ported-from: /home/user/nd_hazard/nd_dam_screening/algorithms/estimate_storage.py
  (the `if assumed is not None: used, method, conf = assumed[1], "inventory curve", "low"` block,
   exercised by nd_ tests/test_v0260.py).

Nothing here needs real QGIS/GDAL/scipy: the dry-dam path goes straight to _inventory_curve, so
it runs against tests/qgis_mock. Run:
  PYTHONPATH=tests/qgis_mock:<parent of the plugin> python3 tests/test_m19_assumed_curve_conf.py
"""
import os
import sys
import tempfile

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(HERE, "qgis_mock"))
sys.path.insert(0, os.path.dirname(PLUGIN))
sys.path.insert(0, HERE)

from osgeo import gdal  # noqa: E402
from qgis.core import (QgsGeometry, QgsPointXY, QgsProcessingContext,  # noqa: E402
                       QgsProcessingFeedback, make_layer)
from nd_dam_screening.algorithms.estimate_storage import EstimateStorage  # noqa: E402

AF, FT = 1233.48183754752, 0.3048
D, S = "double", "string"
N, CELL = 60, 10.0


class _Feedback(QgsProcessingFeedback):
    """The suite's mock feedback has no pushWarning (real QGIS does, and tool 3 calls it on the
    dry-dam fallback); capture the warnings so the test can also check one was raised."""
    def __init__(self):
        super().__init__()
        self.warnings = []

    def pushWarning(self, m):
        self.warnings.append(m)


def _dem(path):
    plane = 500.0 + 0.01 * np.arange(N)[None, :] * CELL + 0.005 * np.arange(N)[:, None] * CELL
    ds = gdal.GetDriverByName("GTiff").Create(path, N, N, 1, gdal.GDT_Float32)
    ds.SetGeoTransform((0.0, CELL, 0.0, N * CELL, 0.0, -CELL))
    ds.GetRasterBand(1).WriteArray(plane)
    ds.FlushCache()


def _run(rows):
    work = tempfile.mkdtemp(prefix="m19_")
    demdir = os.path.join(work, "dem")
    os.makedirs(demdir)
    for did, _attrs in rows:
        _dem(os.path.join(demdir, "dam_{}.tif".format(did)))
    F = [("dam_id", S), ("height_m", D), ("inv_max_m3", D), ("inv_norm_m3", D)]
    centre = QgsGeometry.fromPointXY(QgsPointXY(N * CELL / 2.0, N * CELL / 2.0))
    dams = make_layer(F, [(centre, dict(dam_id=did, **a)) for did, a in rows], "point", "dams")
    wbs = make_layer([("gnis_name", S)], [], "polygon", "wb")        # no water body: dry-dam path
    ctx, fb = QgsProcessingContext(), _Feedback()
    res = EstimateStorage().run_mock(
        dict(DAMS=ctx.add(dams), WB=ctx.add(wbs), DEMDIR=demdir, SEARCH_FT=500.0, SHAPE=1.0,
             CURVES=os.path.join(work, "curves"), OUT="memory:"), ctx, fb)
    out = {str(f["dam_id"]): f for f in ctx.layers[res["OUT"]].getFeatures()}
    return out, fb


def test_assumed_curve_with_normal_storage_is_low_not_high_inventory():
    # inv_norm present -> pre-fix this took the (inv, "inventory", "high") branch and reported the
    # guessed curve as a high-confidence inventory match. It is an assumption: low + "inventory curve".
    out, fb = _run([("DWR-1", dict(height_m=20 * FT, inv_max_m3=5240 * AF, inv_norm_m3=3000 * AF))])
    f = out["DWR-1"]
    assert f["stor_conf"] == "low", ("expected low confidence for the assumed curve", f["stor_conf"])
    assert f["stor_method"] == "inventory curve", ("assumed source label", f["stor_method"])
    # the volume carried is the assumed curve's own total (the inventory maximum here), not inv_norm
    assert abs(float(f["stor_used_m3"]) / AF - 5240) / 5240 < 0.01, f["stor_used_m3"]
    assert "assumption" in (f["stor_notes"] or "").lower(), f["stor_notes"]
    assert any("DWR-1" in w for w in fb.warnings), fb.warnings
    print("ok   assumed curve + inv_norm -> stor_conf=low, stor_method='inventory curve'")


def test_assumed_curve_without_normal_storage_is_also_low_inventory_curve():
    # inv_max only: pre-fix the else branch gave method='estimated' and conf='inventory' (the
    # confidence-field sentinel). The fix labels it consistently as the assumed kind, low.
    out, _fb = _run([("DWR-3", dict(height_m=12 * FT, inv_max_m3=800 * AF, inv_norm_m3=0.0))])
    f = out["DWR-3"]
    assert f["stor_conf"] == "low", f["stor_conf"]
    assert f["stor_method"] == "inventory curve", f["stor_method"]
    assert abs(float(f["stor_used_m3"]) / AF - 800) / 800 < 0.01, f["stor_used_m3"]
    print("ok   assumed curve, inv_max only -> stor_conf=low, stor_method='inventory curve'")


if __name__ == "__main__":
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            n += 1
    print("OK ({} M19 assumed-curve tests)".format(n))
