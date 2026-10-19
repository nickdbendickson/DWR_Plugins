"""M17 regression: tool 8 must match the PRIMARY bld / roads / rail field names
ignoring case (nd_dam_screening 0.18.0 pick()), so a layer whose AADT / occupancy /
passenger column differs only in case is still read, instead of every overtopped road
being silently classified by the 'unknown AADT' assumption.

Runs the HazardClassification algorithm against the qgis.core test doubles
(tests/qgis_mock). Avoids the ACER-chart path (CRIT = all wet) so it needs no charts.

Run: PYTHONPATH=tests/qgis_mock:<parent of the plugin> python3 tests/test_m17_field_case.py
"""
import os, sys, tempfile, shutil
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(HERE, "qgis_mock"))
sys.path.insert(0, os.path.dirname(PLUGIN))
from qgis.core import (QgsGeometry, QgsPointXY, QgsProcessingContext,  # noqa: E402
                       QgsProcessingFeedback, make_layer)
from nd_dam_screening.algorithms import _common as cm  # noqa: E402
from nd_dam_screening.algorithms.hazard_classification import HazardClassification  # noqa: E402
from nd_dam_screening.core import nd_hazard as nh  # noqa: E402

work = tempfile.mkdtemp(prefix="m17_")
FT = 0.3048
cell = 10.0
gt = (0.0, cell, 0.0, 100.0, 0.0, -cell)
xy = lambda r, c: (gt[0] + (c + 0.5) * cell, gt[3] - (r + 0.5) * cell)

# one wet row for the road, one for the railroad, one wet structure cell
depth = np.zeros((10, 10))
vel = np.zeros((10, 10))
depth[2, 2], vel[2, 2] = 1.0, 0.3        # a structure (wet, > 'any depth')
depth[6, :] = 0.4                         # a road, overtopped
depth[8, :] = 0.4                         # a railroad, overtopped
dtif, vtif = os.path.join(work, "d.tif"), os.path.join(work, "v.tif")
cm.write_tif(dtif, depth, gt)
cm.write_tif(vtif, vel, gt)

Dd, Sx = "double", "string"
pt = lambda r, c: QgsGeometry.fromPointXY(QgsPointXY(*xy(r, c)))
line = lambda pts: QgsGeometry.fromPolylineXY([QgsPointXY(*p) for p in pts])
SIMF = [("dam_id", Sx), ("sim_status", Sx), ("depth_tif", Sx), ("vel_tif", Sx)]
sim = make_layer(SIMF, [(pt(0, 0), {"dam_id": "DWR-1", "sim_status": "ok",
                                    "depth_tif": dtif, "vel_tif": vtif})], "point", "sim")

ctx, fb = QgsProcessingContext(), QgsProcessingFeedback()
SIM = ctx.add(sim)


def tool8(**kw):
    """Run tool 8 (all-wet lethality, no charts) and return the DWR-1 output row."""
    r = HazardClassification().run_mock(dict(dict(SIM=SIM, CRIT=2), **kw), ctx, fb)
    out = ctx.layers[r["OUT"]]
    n = out.fields().names()
    return {f["dam_id"]: {k: f[k] for k in n} for f in out.getFeatures()}["DWR-1"]


# ---- A. ROADS: the AADT column is 'AADT', the requested field is the default 'aadt'
roads = make_layer([("AADT", Dd), ("road_class", Sx)],
                   [(line([xy(6, 0), xy(6, 9)]), {"AADT": 2500.0, "road_class": "County 12"})],
                   "line", "roads")
rowA = tool8(ROADS=ctx.add(roads), ROAD_AADT="aadt", ROAD_CLASS="road_class")
print("A roads AADT (case):", {k: rowA[k] for k in ("hz_class", "n_road_wet", "hz_gaps")})
# AADT 2,500 > 2,000 -> High. Before the fix the column is missed (case), the AADT reads
# as unknown and the road is classified Medium by assumption, with a data gap.
assert rowA["hz_class"] == "High", rowA["hz_class"]
assert rowA["n_road_wet"] == 1, rowA["n_road_wet"]
assert "AADT not given" not in (rowA["hz_gaps"] or ""), rowA["hz_gaps"]

# ---- B. RAIL: the passenger column is 'passngr', the requested field is the default 'PASSNGR'
rail = make_layer([("passngr", Sx)],
                  [(line([xy(8, 0), xy(8, 9)]), {"passngr": "B"})], "line", "rail")  # B in PAX_CODES
rowB = tool8(ROADS=None, RAIL=ctx.add(rail), RAIL_PAX="PASSNGR")
print("B rail passenger (case):", {k: rowB[k] for k in ("hz_class", "n_rail_wet")})
# an active passenger line -> High. Before the fix the column is missed and the railroad is
# classified by the 'unknown trains' assumption (Medium).
assert rowB["hz_class"] == "High", rowB["hz_class"]
assert rowB["n_rail_wet"] == 1, rowB["n_rail_wet"]

# ---- C. STRUCTURES: the occupancy column is 'occ_cls', the requested field is the default 'OCC_CLS'
bld = make_layer([("occ_cls", Sx)],
                 [(pt(2, 2), {"occ_cls": "Barn"})], "point", "bld")  # uninhabited keyword
rowC = tool8(ROADS=None, RAIL=None, BLD=ctx.add(bld), OCC="OCC_CLS")
print("C structure occupancy (case):", {k: rowC[k] for k in ("hz_class", "n_bldg_wet")})
# a barn is an uninhabited building -> Low. Before the fix the occupancy column is missed and
# every structure is assumed habitable, so the barn is classified High (all-wet lethality).
assert rowC["hz_class"] == "Low", rowC["hz_class"]
assert rowC["n_bldg_wet"] == 0, rowC["n_bldg_wet"]       # habitable_wet, not the barn

# ---- sanity: with the column spelled as requested the class is the same (no regression)
roads2 = make_layer([("aadt", Dd), ("road_class", Sx)],
                    [(line([xy(6, 0), xy(6, 9)]), {"aadt": 2500.0, "road_class": "County 12"})],
                    "line", "roads2")
rowD = tool8(ROADS=ctx.add(roads2), ROAD_AADT="aadt", ROAD_CLASS="road_class")
assert rowD["hz_class"] == "High" and rowD["n_road_wet"] == 1

assert nh.MEDIUM == "Medium"          # guards the 'by assumption' default this test contrasts with
shutil.rmtree(work)
print("OK (M17 case-insensitive primary field match)")
