"""Each inventory storage at its own elevation (0.17.47; DWR-2865 / DWR-3034).

DWR-2865 (NickReview): the inventory's normal-storage box held the MAXIMUM (167 ac-ft, DWR at the
1874 ft max pool) and tool 3 put it at the 1870 ft normal pool (the lidar water surface) and
added the DEM fill to the 1875.9 ft spill on top: 237.6 ac-ft. DWR's normal storage is 141 ac-ft
at 1870 ft. DWR-3034 had the same pattern (97.5 vs max_pool_vol 71.6). Now the maximum sits at
the max pool and only the DEM fill above the max pool is added on top of it.

Part 1: core/storage.anchor_inventory_storage on a tiny synthetic curve shaped like 2865.
Part 2: core/inventory reads the DWR pool elevations (ft -> m; 0 = not recorded).
Part 3: tool 3 end to end through the mock on the self-test valley with the elevations set.
"""
import csv
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "qgis_mock"))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
sys.path.insert(0, HERE)
from nd_dam_screening.core import storage as S, inventory as inv  # noqa: E402

FT, AF = 0.3048, 1233.4818375


def near(a, b, tol=1.0):
    return abs(float(a) - float(b)) <= tol


# ---- 1. the pure function on a 2865-shaped curve ---------------------------------------------
POOL = 569.94                                # 1869.88 ft, the lidar water surface
BELOW = 110000.0                             # estimated below-pool volume, m3
# DEM fill above the pool: 12 ac reservoir growing to 16 ac over 10 ft (m3)
def fill(e):
    dz = e - POOL
    return (12 * 4046.86) * dz + 0.5 * (4 * 4046.86 / (10 * FT)) * dz * dz
STAGES = [POOL + i * (10 * FT) / 20 for i in range(21)]
CURVE = [(e, BELOW + fill(e)) for e in STAGES]
MAX, NORM = 167 * AF, 141 * AF
NE, ME = 1870 * FT, 1874 * FT                # DWR normal_elev / max_pool_elev
E_MAX = POOL + (ME - NE)
F_MAX = fill(E_MAX)
TOP = STAGES[-1]

# the old logic (before 0.17.47) with the 2865 inputs (normal box = maximum)
old, _s, _n = S.calibrate_below_pool(CURVE, BELOW, MAX)
old, _t = S.ensure_top_of_dam(old, MAX, TOP)
assert near(old[0][1], MAX) and near(old[-1][1], MAX + fill(TOP)), old[-1]     # the bug: max + fill

# the 2865 pattern: normal = maximum (both from max_pool_vol), elevations 4 ft apart
cur, scale, info = S.anchor_inventory_storage(CURVE, BELOW, POOL, MAX, MAX, "maximum", NE, ME, TOP)
at = lambda c, e: S._interp_curve(c, e)
assert near(at(cur, E_MAX), MAX), (at(cur, E_MAX), MAX)                         # maximum at the max pool
assert near(at(cur, POOL), MAX - F_MAX), at(cur, POOL)                          # max minus the fill below it
assert near(cur[-1][1], MAX + fill(TOP) - F_MAX)                                # only the fill above the max pool
assert near(old[-1][1] - cur[-1][1], F_MAX)                                     # the double count removed
assert info["anchor"] == "maximum storage at the max pool" and near(info["anchor_m"], E_MAX, 1e-6)
assert any(f.startswith("NORMAL-EQUALS-MAX") for f in info["flags"]), info["flags"]
assert any(f.startswith("NORMAL-NOT-BELOW-MAX") for f in info["flags"]), info["flags"]
assert all(b[1] >= a[1] for a, b in zip(cur, cur[1:]))                          # never falls with stage
assert any(near(e, E_MAX, 1e-6) for e, _v in cur)                               # the anchor is a curve point

# the DWR record's own values: normal 141 at the pool, maximum 167 at the max pool
cur, scale, info = S.anchor_inventory_storage(CURVE, BELOW, POOL, NORM, MAX, "maximum", NE, ME, TOP)
assert near(at(cur, POOL), NORM) and near(at(cur, E_MAX), MAX), (at(cur, POOL), at(cur, E_MAX))
assert near(cur[-1][1], MAX + fill(TOP) - F_MAX) and not info["flags"], info
mid = POOL + 0.5 * (E_MAX - POOL)                                               # the fill scaled between
assert NORM < at(cur, mid) < MAX
assert near(scale, NORM / BELOW, 1e-6)
assert all(b[1] >= a[1] for a, b in zip(cur, cur[1:]))

# a local datum (DWR-3034: normal 125.7, max 130.7, streambed 100): only the difference is used
cur, _s, info = S.anchor_inventory_storage(CURVE, BELOW, POOL, 59.1 * AF, 71.6 * AF, "maximum",
                                           125.7 * FT, 130.7 * FT, TOP)
assert near(info["anchor_m"], POOL + 5 * FT, 1e-6) and near(at(cur, POOL + 5 * FT), 71.6 * AF)
assert near(at(cur, POOL), 59.1 * AF)

# max pool above the end of the DEM curve: the maximum added as the last point
short = [p for p in CURVE if p[0] <= POOL + 2 * FT + 1e-9]
cur, _s, info = S.anchor_inventory_storage(short, BELOW, POOL, NORM, MAX, "maximum", NE, ME, TOP)
assert near(cur[-1][0], E_MAX, 1e-6) and near(cur[-1][1], MAX) and near(cur[0][1], NORM)
assert any("above the end of the DEM curve" in n for n in info["notes"]), info["notes"]

# no pool elevations (NID-only records): the documented fallback, flagged
cur, _s, info = S.anchor_inventory_storage(CURVE, BELOW, POOL, NORM, MAX, "maximum", None, None, TOP)
want, _x, _y = S.calibrate_below_pool(CURVE, BELOW, NORM)
want, _t = S.ensure_top_of_dam(want, MAX, TOP)
assert [round(v, 3) for _e, v in cur] == [round(v, 3) for _e, v in want]       # as before 0.17.47
assert info["anchor"] == "maximum storage at the top of dam (fallback)"
assert any(f.startswith("MAXPOOL-ELEV-MISSING") and "FALLBACK" in f for f in info["flags"]), info
# ... and with normal = maximum the maximum is not used below the pool any more
cur, _s, info = S.anchor_inventory_storage(CURVE, BELOW, POOL, MAX, MAX, "maximum", None, ME, TOP)
assert near(cur[0][1], BELOW) and cur[-1][1] >= MAX - 1.0, cur[:2]
assert any("normal-pool" in f for f in info["flags"]) and any(f.startswith("NORMAL-EQUALS-MAX") for f in info["flags"])
assert any("not used below the pool" in n for n in info["notes"])
# max pool below the normal pool: an inconsistent record, the fallback
cur, _s, info = S.anchor_inventory_storage(CURVE, BELOW, POOL, NORM, MAX, "maximum", ME, NE, TOP)
assert any(f.startswith("MAXPOOL-ELEV-BAD") for f in info["flags"]), info
# the DEM fill to the max pool is more than the maximum: the pool starts at 0, flagged
cur, _s, info = S.anchor_inventory_storage(CURVE, BELOW, POOL, None, F_MAX / 2, "maximum", NE, ME, TOP)
assert near(cur[0][1], 0.0) and near(at(cur, E_MAX), F_MAX / 2)
assert any(f.startswith("FILL-EXCEEDS-MAX") for f in info["flags"]), info
# Normal storage (kind normal): unchanged - normal storage at the pool plus the DEM fill
cur, _s, info = S.anchor_inventory_storage(CURVE, BELOW, POOL, NORM, MAX, "normal", NE, ME, TOP)
want, _x, _y = S.calibrate_below_pool(CURVE, BELOW, NORM)
assert cur == want and info["anchor"] == "normal storage at the normal pool" and not info["flags"]
cur, _s, info = S.anchor_inventory_storage(CURVE, BELOW, POOL, MAX, MAX, "normal", NE, ME, TOP)
assert any(f.startswith("NORMAL-EQUALS-MAX") for f in info["flags"])            # still flagged
# max_pool_on_dem
assert S.max_pool_on_dem(POOL, NE, ME)[0] == POOL + (ME - NE)
assert S.max_pool_on_dem(POOL, None, ME)[0] is None
assert S.max_pool_on_dem(POOL, NE, NE)[0] == POOL                               # max pool = normal pool

# ---- 2. tool 1 reads the DWR pool elevations ----------------------------------------------------
fld = dict(inv.DWR_DEFAULTS, NE="normal_ele", ME="max_pool_e")
assert "NE" not in inv.DWR_DEFAULTS                     # filled from the aliases, no default box value
rec = {"structure_": 2865.0, "normal_vol": 141.0, "max_pool_v": 167.0, "normal_ele": 1870.0, "max_pool_e": 1874.0}
s = inv.standardize(rec.get, fld)
assert near(s["ne"], 1870 * FT, 1e-9) and near(s["me"], 1874 * FT, 1e-9)
assert inv.standardize(dict(rec, normal_ele=0.0).get, fld)["ne"] is None        # 0 = not recorded
gp = inv.match_fields(["structure_id", "normal_elev", "max_pool_elev", "normal_vol"], inv.DWR_ALIASES)
assert gp["NE"] == "normal_elev" and gp["ME"] == "max_pool_elev"
assert "NE" not in inv.NID_ALIASES and "ME" not in inv.NID_ALIASES             # the NID has none

# ---- 3. tool 3 end to end on the self-test valley ----------------------------------------------
sys.path.insert(0, HERE)
from _optional import need  # noqa: E402
need("scipy", "storage.estimate_storage uses scipy.ndimage")
from qgis.core import (QgsProcessingContext, QgsProcessingFeedback, QgsGeometry, QgsPointXY,  # noqa: E402
                       make_layer)
from qgis import processing  # noqa: E402
import nd_dam_screening as pkg  # noqa: E402
from nd_dam_screening.core import selftest as st  # noqa: E402
from nd_dam_screening.algorithms.prepare_dams import POOL_FIELDS  # noqa: E402


def mock_layers(specs):                       # as tests/test_storage_choice_tool3.py
    types = {"string": "string", "double": "double", "int": "int"}
    out = {}
    for name, (kind, flds, feats) in specs.items():
        rows = []
        for (gkind, coords), attrs in feats:
            if gkind == "point":
                g = QgsGeometry.fromPointXY(QgsPointXY(*coords))
            elif gkind == "line":
                g = QgsGeometry.fromPolylineXY([QgsPointXY(*p) for p in coords])
            else:
                g = QgsGeometry.fromPolygonXY([[QgsPointXY(*p) for p in coords]])
            rows.append((g, attrs))
        out[name] = make_layer([(f, types[t]) for f, t in flds], rows, kind, name)
    return out


plug = pkg.classFactory(None); plug.initGui()
algs = {a.name(): a for a in plug.provider.algs}
for a in algs.values():
    a.initAlgorithm()
processing.REGISTRY.update(algs)
assert {"DWR_NE", "DWR_ME"} <= set(algs["preparedams"]._params)
work = tempfile.mkdtemp(prefix="sanchor_")
layers = mock_layers(st.layer_specs())
dem_folder, _p = st.write_dem(os.path.join(work, "dem"))
ctx, fb = QgsProcessingContext(), QgsProcessingFeedback()
prev = {}
for step, alg, mk, check in st.steps(work, layers, dem_folder)[:2]:
    prev[step] = processing.run("nddam:" + alg, mk(prev), ctx, fb)
    assert not check(prev[step]), (step, check(prev[step]))
src = ctx.layers.get(prev["1"]["OUT"], prev["1"]["OUT"])
fields = [(f.name(), f._t) for f in src.fields()]
assert [n for n, _t in POOL_FIELDS] == [n for n, _t in fields][-2:], fields       # appended last
base = [(f.geometry(), {k: f[k] for k, _ in fields}) for f in src.getFeatures()]
DAM = base[0][1]["dam_id"]


def tool3(normal, maximum, ne, me, tag):
    rows = [(g, dict(a, inv_norm_m3=normal, inv_max_m3=maximum, inv_norm_elev_m=ne, inv_max_elev_m=me))
            for g, a in base]
    dams = ctx.add(make_layer(fields, rows, "point", "dams_" + tag))
    curves = os.path.join(work, "curves_" + tag)
    f3 = QgsProcessingFeedback()
    res = processing.run("nddam:estimatestorage", {
        "DAMS": dams, "WB": layers["waterbodies"], "DEMDIR": os.path.join(work, "2_terrain"),
        "SEARCH_FT": 492.126, "SHAPE": 1.0, "CURVES": curves, "OUT": "TEMPORARY_OUTPUT",
        "STORAGE_CHOICE": 0}, ctx, f3)
    out = ctx.layers.get(res["OUT"], res["OUT"])
    row = [{k.name(): f[k.name()] for k in out.fields()} for f in out.getFeatures()][0]
    with open(os.path.join(curves, "curve_{}.csv".format(DAM)), encoding="utf-8") as fh:
        crv = [(float(r["elev_m"]), float(r["storage_m3"])) for r in csv.DictReader(fh)]
    return crv, row, f3.warnings


# the DEM-only curve gives the fill above the pool
crv0, row0, _w = tool3(None, None, None, None, "dem")
pool = row0["est_pool_m"]
below = row0["est_below_m3"]
rise = 0.5 * (crv0[-1][0] - pool)                       # a max pool half-way up the curve
f_rise = S._interp_curve([(e, v - below) for e, v in crv0], pool + rise)
MX = 3.0 * f_rise + below
NM = 0.8 * MX
# the 2865 pattern through tool 3
crv, row, warn = tool3(MX, MX, 100.0, 100.0 + rise, "same")
TOL = 1e-5 * MX                                         # the curve CSV rounds elevations to 1 mm
assert near(S._interp_curve(crv, pool + rise), MX, TOL), (S._interp_curve(crv, pool + rise), MX)
assert near(crv[0][1], MX - f_rise, TOL)
assert row["stor_anchor"] == "maximum storage at the max pool" and near(row["stor_anchor_m"], pool + rise, 1e-3)
assert "NORMAL-EQUALS-MAX" in row["stor_notes"] and any("NORMAL-EQUALS-MAX" in w for w in warn)
assert row["stor_notes"].startswith("storage choice Max storage: maximum storage"), row["stor_notes"]
assert near(row["cal_total_m3"], crv[-1][1], 1.0)
# the record's normal and maximum
crv, row, warn = tool3(NM, MX, 100.0, 100.0 + rise, "both")
assert near(crv[0][1], NM, TOL) and near(S._interp_curve(crv, pool + rise), MX, TOL)
assert "NORMAL-EQUALS-MAX" not in row["stor_notes"] and not warn, (row["stor_notes"], warn)
# no elevations: the fallback (maximum at the top of dam), noted, logged not warned
crv, row, warn = tool3(NM, MX * 5, None, None, "fallback")
assert row["stor_anchor"] == "maximum storage at the top of dam (fallback)"
assert "MAXPOOL-ELEV-MISSING" in row["stor_notes"] and "raised" in row["stor_notes"] and not warn
assert near(crv[-1][1], MX * 5, 1.0) and near(crv[0][1], NM, TOL)

shutil.rmtree(work)
print("OK (storage anchored at its own elevation)")
