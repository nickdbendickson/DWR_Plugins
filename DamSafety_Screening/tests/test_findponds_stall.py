"""0.17.54: tool 16 on a big water-body layer (Nick, Absaraka DWR-2801: 'tool 16 stalled out and didn't
find ponds').

- a local DEM: only the water bodies over its extent are read (a statewide layer gave one error per
  water body of the state, 95 000 in 2 minutes on the box);
- a county: its box first, then the county test on the lon / lat of the water body (a projected layer
  had none in any county) and the count outside the county is always reported;
- the USGS 3DEP service with many water bodies: a warning with the time it may take;
- a progress line every HEARTBEAT_S seconds, and a warning saying why nothing was written.
Simulated GDAL and QGIS: the CRS part is checked on the box in a real QGIS (see the PR).
"""
import math
import os
import shutil
import sys
import tempfile

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "qgis_mock"))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
from qgis.core import (QgsGeometry, QgsPointXY, QgsProcessingContext,  # noqa: E402
                       QgsProcessingFeedback, make_layer)
from osgeo import gdal, osr  # noqa: E402
sys.path.insert(0, HERE)
import _optional  # noqa: E402
_optional.need("scipy", "reservoir fill / storage")

from nd_dam_screening.algorithms import find_ponds as fp  # noqa: E402
from nd_dam_screening.algorithms.find_ponds import FindPonds  # noqa: E402

work = tempfile.mkdtemp(prefix="ponds_stall_")

# ---------------------------------------------------------------- 1. helpers
def tif(path, epsg, x0, y0, nx, ny, cell):
    ds = gdal.GetDriverByName("GTiff").Create(path, nx, ny, 1, gdal.GDT_Float32)
    ds.SetGeoTransform((x0, cell, 0.0, y0 + ny * cell, 0.0, -cell))
    s = osr.SpatialReference(); s.ImportFromEPSG(epsg)
    ds.SetProjection(s.ExportToWkt())
    ds.GetRasterBand(1).WriteArray(np.full((ny, nx), 100.0, "float32"))
    ds.FlushCache(); ds = None


tif(os.path.join(work, "a.tif"), 5070, 500000.0, 2599000.0, 100, 50, 2.0)
b = fp.raster_box(os.path.join(work, "a.tif"))
assert all(abs(u - v) < 1e-6 for u, v in zip(b, (500000.0, 2599000.0, 500200.0, 2599100.0))), b
tif(os.path.join(work, "utm.tif"), 26914, 650000.0, 5200000.0, 100, 100, 10.0)
assert fp.raster_box(os.path.join(work, "utm.tif"), "EPSG:4326") is None    # not simulated -> read all
cb = fp.county_box("cass county")
assert cb and -97.8 < cb[0] < -97.6 and -96.9 < cb[2] < -96.7 and 46.55 < cb[1] < 46.7 and 47.2 < cb[3] < 47.3, cb
assert fp.county_box("Nowhere") is None
assert fp.eta_text(40) == "about 40 s" and fp.eta_text(600) == "about 10 min"
assert fp.eta_text(7200) == "about 2.0 h" and fp.eta_text(359146 * 3.0) == "about 12.5 days"
hb = fp.heartbeat(100, 1559, 98, 6, 2, 290.0)
assert hb.startswith("water body 100 of 1559: 98 analysed, 6 written, 2 failed (2.9 s each)") and hb.endswith("about 71 min left"), hb
print("OK (helpers)")

# ---------------------------------------------------------------- 2. county + 3DEP (simulated)
# The mock transforms are identities, so the polygons are drawn in lon / lat 'metres'-free: the
# terrain fetch is replaced and every water body fails fast - the test is about what is read.
calls = []


def no_3dep(*a, **k):
    calls.append(a[0])
    raise RuntimeError("3DEP request failed: HTTP Error 502: Bad Gateway")


fp.terrain.fetch_3dep = no_3dep


def sq(lon, lat, d=0.002):
    return QgsGeometry.fromPolygonXY([[QgsPointXY(lon + x, lat + y) for x, y in
                                       ((0, 0), (d, 0), (d, d), (0, d), (0, 0))]])


rows = [(sq(-97.25, 46.95), dict(wb_id="cass1")), (sq(-97.10, 47.05), dict(wb_id="cass2")),
        (sq(-97.72, 46.95), dict(wb_id="barnes"))]          # Barnes, just west of Cass (inside the box pad)
rows += [(sq(-100.8, 46.8 + 0.01 * k), dict(wb_id="burleigh%d" % k)) for k in range(5)]   # not in the box
ctx = QgsProcessingContext()
WB = ctx.add(make_layer([("wb_id", "string")], rows, "polygon", "water"))
base = dict(WB=WB, WB_ID="wb_id", WB_TYPE="", DUG_WORDS="", MIN_AC=0.0, MAX_AC=0.0, SRC=0, CELL=2.0,
            RADIUS_FT=984.252, CLIP_CELL=4.0, TERRAIN=os.path.join(work, "t"), CURVES=os.path.join(work, "c"))
fb = QgsProcessingFeedback()
FindPonds().run_mock(dict(base, COUNTY="Cass"), ctx, fb)
log = "\n".join(fb.log)
assert "3 of the 8 water bodies of the layer are in the box of County Cass" in log, log
assert len(calls) == 2, calls                                  # only the two Cass ones fetch terrain
assert "2 analysed, 0 written" in log and "2 failed, 1 outside County Cass" in log, log
assert any(w.startswith("No pond or dugout written: 2 failed") for w in fb.warnings), fb.warnings
assert not any("USGS 3DEP service" in w for w in fb.warnings)    # 3 water bodies: no time warning
print("OK (county box, county test, count outside the county, nothing-written warning)")

# a county the local DEM does not cover stops at once
os.makedirs(os.path.join(work, "dem"))
tif(os.path.join(work, "dem", "a.tif"), 5070, 500000.0, 2599000.0, 100, 50, 2.0)
try:
    FindPonds().run_mock(dict(base, COUNTY="Cass", SRC=1, LOCAL=os.path.join(work, "dem")), ctx,
                         QgsProcessingFeedback())
    raise AssertionError("expected: the local DEM does not cover County Cass")
except Exception as e:
    assert "does not cover County Cass" in str(e), e

# ---------------------------------------------------------------- 3. many water bodies on 3DEP
old = fp.SLOW_3DEP_N, fp.HEARTBEAT_S
fp.SLOW_3DEP_N, fp.HEARTBEAT_S = 5, 0.0                       # a progress line at every water body
del calls[:]
fb = QgsProcessingFeedback()
FindPonds().run_mock(dict(base), ctx, fb)
fp.SLOW_3DEP_N, fp.HEARTBEAT_S = old
slow = [w for w in fb.warnings if "USGS 3DEP service" in w]
assert len(slow) == 1 and slow[0].startswith("8 water bodies") and "choose a County" in slow[0], slow
assert "(24 s to 80 s)" in slow[0], slow[0]
beats = [m for m in fb.log if m.startswith("water body ") and " of 8: " in m]
assert len(beats) == 7 and beats[-1].startswith("water body 7 of 8: 7 analysed, 0 written, 7 failed"), beats
assert len(calls) == 8 and "outside County" not in "\n".join(fb.log)
print("OK (3DEP time warning, progress lines)")

# cancel: says where it stopped
fb = QgsProcessingFeedback(); fb.cancel()
FindPonds().run_mock(dict(base), ctx, fb)
assert "Cancelled at water body 0 of 8" in fb.warnings, fb.warnings

shutil.rmtree(work)
print("OK")
