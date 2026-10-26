"""Each inventory storage at its own elevation (0.17.47; DWR-2865 / DWR-3034).

DWR-2865 (NickReview): the inventory's normal-storage box held the MAXIMUM (167 ac-ft, DWR at the
1874 ft max pool) and tool 3 put it at the 1870 ft normal pool (the lidar water surface) and
added the DEM fill to the 1875.9 ft spill on top: 237.6 ac-ft. DWR's normal storage is 141 ac-ft
at 1870 ft. DWR-3034 had the same pattern (97.5 vs max_pool_vol 71.6). Now the maximum sits at
the max pool and only the DEM fill above the max pool is added on top of it.

Part 1: core/storage.anchor_inventory_storage on a tiny synthetic curve shaped like 2865 (also:
        a max pool above the crest, the fallback with a normal above the maximum, a DEM fill
        above the maximum and tool 5's sunny day on it, a max pool at the normal pool).
Part 1b: tool 7b's stage-storage area (core/handoff.surface_area_choice) on an anchored curve.
Part 2: core/inventory reads the DWR pool elevations (ft -> m; 0 = not recorded).
Part 3: tool 3 end to end through the mock on the self-test valley with the elevations set, and
        tool 1's output values read by field name.
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
from nd_dam_screening.core import breach_hydro as bh, handoff as ho  # noqa: E402

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
# ... and a normal storage ABOVE the maximum (not the 2865 pattern) stays at the pool as before
# 0.17.47, flagged NORMAL-NOT-BELOW-MAX (a tool-3 warning): pool 200, top of dam as before
N2, M2 = 200 * AF, 150 * AF
cur, _s, info = S.anchor_inventory_storage(CURVE, BELOW, POOL, N2, M2, "maximum", None, None, TOP)
want, _x, _y = S.calibrate_below_pool(CURVE, BELOW, N2)
want, _t = S.ensure_top_of_dam(want, M2, TOP)
assert near(cur[0][1], N2) and [round(v, 3) for _e, v in cur] == [round(v, 3) for _e, v in want], cur[:2]
assert any(f.startswith("NORMAL-NOT-BELOW-MAX") for f in info["flags"]), info["flags"]
assert not any(f.startswith("NORMAL-EQUALS-MAX") for f in info["flags"])
assert not any("not used below the pool" in n for n in info["notes"]), info["notes"]
# max pool below the normal pool: an inconsistent record, the fallback
cur, _s, info = S.anchor_inventory_storage(CURVE, BELOW, POOL, NORM, MAX, "maximum", ME, NE, TOP)
assert any(f.startswith("MAXPOOL-ELEV-BAD") for f in info["flags"]), info
# max pool AT the normal pool (rise 0, or within 0.01 ft) with a maximum above the normal storage:
# inconsistent too (the maximum at the pool plus the whole DEM fill was the double count again):
# MAXPOOL-ELEV-BAD and the fallback of before 0.17.47
want, _x, _y = S.calibrate_below_pool(CURVE, BELOW, NORM)
want, _t = S.ensure_top_of_dam(want, MAX, TOP)
for me in (NE, NE + 0.005 * FT):
    cur, _s, info = S.anchor_inventory_storage(CURVE, BELOW, POOL, NORM, MAX, "maximum", NE, me, TOP)
    assert [round(v, 3) for _e, v in cur] == [round(v, 3) for _e, v in want], (me, cur[:2])
    assert any(f.startswith("MAXPOOL-ELEV-BAD") and "FALLBACK" in f for f in info["flags"]), info["flags"]
    assert info["anchor"] == "maximum storage at the top of dam (fallback)"
# ... but with no normal storage, or normal = maximum, the maximum at the normal pool is consistent
for nrm in (None, MAX):
    cur, _s, info = S.anchor_inventory_storage(CURVE, BELOW, POOL, nrm, MAX, "maximum", NE, NE, TOP)
    assert near(cur[0][1], MAX) and not any("MAXPOOL-ELEV-BAD" in f for f in info["flags"]), info["flags"]
# the DEM fill to the max pool is more than the maximum: the DEM curve is kept, its below-pool
# estimate at the pool (a storage of 0 there failed tool 5's sunny-day scenario), flagged
cur, _s, info = S.anchor_inventory_storage(CURVE, BELOW, POOL, None, F_MAX / 2, "maximum", NE, ME, TOP)
assert near(cur[0][1], BELOW) and [round(v, 3) for _e, v in cur] == [round(v, 3) for _e, v in CURVE]
assert any(f.startswith("FILL-EXCEEDS-MAX") for f in info["flags"]), info
assert info["anchor_m"] is None and _s == 1.0
H_DAM = 15 * FT                                 # the toe 5 ft below the pool: a sunny-day breach
sd = bh.analyze_dam([e for e, _v in cur], [v for _e, v in cur], POOL, TOP, TOP - H_DAM, sunny=True)
assert "skipped" not in sd["sd"] and sd["sd"]["vw_m3"] > 0.0 and near(sd["sd"]["vw_m3"], BELOW), sd["sd"].get("vw_m3")
# the max pool above the DEM crest (MAXPOOL-ABOVE-CREST): the maximum AT the crest, never on a
# point above it, so the storage at the top of dam (tool 5's Vw, the Runner's td_vw_m3) is never
# below the inventory maximum. Here the maximum (400 ac-ft, 14 ft up) is more than the normal plus
# the whole DEM fill to the crest (141 + 140 ac-ft, 10 ft up).
MX2, ME_HI = 400 * AF, NE + 14 * FT
assert NORM + fill(TOP) < MX2 - 1.0
for nrm in (NORM, None, MX2):                   # the record's normal, none, the 2865 pattern
    cur, _s, info = S.anchor_inventory_storage(CURVE, BELOW, POOL, nrm, MX2, "maximum", NE, ME_HI, TOP)
    assert at(cur, TOP) >= MX2 - 1.0 and near(cur[-1][1], MX2), (nrm, cur[-2:])
    assert max(e for e, _v in cur) <= TOP + 1e-9, (nrm, cur[-2:])
    assert any(f.startswith("MAXPOOL-ABOVE-CREST") for f in info["flags"]), info["flags"]
    assert "14.0 ft above the normal pool" in " ".join(info["flags"]) and "crest only 10.0 ft" in " ".join(info["flags"])
    assert near(info["anchor_m"], TOP, 1e-9) and info["anchor"].startswith("maximum storage at the top of dam")
    assert all(b[1] >= a[1] for a, b in zip(cur, cur[1:]))
cur, _s, info = S.anchor_inventory_storage(CURVE, BELOW, POOL, NORM, MX2, "maximum", NE, ME_HI, TOP)
assert near(cur[0][1], NORM) and NORM < at(cur, POOL + 5 * FT) < MX2       # the fill scaled to join them
# a curve short of the given crest, the max pool above the crest: the maximum at the crest
cur, _s, info = S.anchor_inventory_storage(short, BELOW, POOL, NORM, MX2, "maximum", NE, ME_HI, TOP)
assert near(cur[-1][0], TOP, 1e-9) and near(cur[-1][1], MX2) and near(at(cur, TOP), MX2)
assert any(f.startswith("MAXPOOL-ABOVE-CREST") for f in info["flags"]), info["flags"]
# within 1 cm of the crest: the crest itself (no flag); 2 cm above it: flagged
for d, flagged in ((0.005, False), (-0.005, False), (0.02, True)):
    cur, _s, info = S.anchor_inventory_storage(CURVE, BELOW, POOL, NORM, MX2, "maximum",
                                               NE, NE + (TOP - POOL) + d, TOP)
    assert at(cur, TOP) >= MX2 - 1.0 and max(e for e, _v in cur) <= TOP + 1e-9, (d, cur[-2:])
    assert info["anchor_m"] <= TOP + 1e-9, (d, info["anchor_m"])
    assert any(f.startswith("MAXPOOL-ABOVE-CREST") for f in info["flags"]) == flagged, (d, info["flags"])
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

# ---- 1b. tool 7b's stage-storage area (0.17.46, core/handoff) on the anchored curve --------------
# area_at_top_m2 is dV/dz over the top 0.3 m below the crest. Before 0.17.47 ensure_top_of_dam
# raised the LAST point to the maximum when the normal storage plus the DEM fill fell short of it,
# and that jump read as a reservoir several times the real one; the anchored curve's top is DEM fill.
NA, MA_ = 60 * AF, 250 * AF                     # 60 + 140 ac-ft of fill < 250 ac-ft: old raised the end
old, _s, _n = S.calibrate_below_pool(CURVE, BELOW, NA)
old, _t = S.ensure_top_of_dam(old, MA_, TOP)
assert near(old[-1][1], MA_) and "raised" in _t
new, _s, _i = S.anchor_inventory_storage(CURVE, BELOW, POOL, NA, MA_, "maximum", NE, ME, TOP)
dem_area = (fill(TOP) - fill(TOP - 0.3)) / 0.3  # the DEM's own area over the top 0.3 m (~15.8 ac)


def area_of(crv, tag):
    path = os.path.join(tempfile.mkdtemp(prefix="sanchor_area_"), "curve_{}.csv".format(tag))
    with open(path, "w", encoding="utf-8") as fh:  # as tool 3 writes it
        fh.write("elev_m,storage_m3\n")
        for e, v in crv:
            fh.write(f"{e:.3f},{v:.1f}\n")
    a, src, note = ho.surface_area_choice(path, TOP, 12 * FT, {})
    shutil.rmtree(os.path.dirname(path))
    assert src == ho.AREA_SRC_CURVE and not note, (src, note)
    return a


a_new, a_old = area_of(new, "new"), area_of(old, "old")
assert abs(a_new - dem_area) <= 0.02 * dem_area, (a_new / ho.ACRE_M2, dem_area / ho.ACRE_M2)
assert a_old > 3.0 * dem_area, (a_old / ho.ACRE_M2, dem_area / ho.ACRE_M2)     # the old, inflated area
print("7b area at the top of dam: anchored {:.1f} ac, old {:.1f} ac, DEM {:.1f} ac".format(
    a_new / ho.ACRE_M2, a_old / ho.ACRE_M2, dem_area / ho.ACRE_M2))

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
# ... a normal storage above the maximum: kept at the pool as before, warned NORMAL-NOT-BELOW-MAX
crv, row, warn = tool3(1.5 * MX, MX, None, None, "nbm")
assert near(crv[0][1], 1.5 * MX, TOL) and "NORMAL-NOT-BELOW-MAX" in row["stor_notes"]
assert [w for w in warn if "NORMAL-NOT-BELOW-MAX" in w] and not [w for w in warn if "MAXPOOL-ELEV-MISSING" in w], warn
# the max pool above the crest: the maximum at the crest (tool 3's est_crest_m), warned
f_end = crv0[-1][1] - below
MXB = NM + 2.0 * f_end                                  # more than the normal plus the fill to the crest
crv, row, warn = tool3(NM, MXB, 100.0, 100.0 + 2.0 * (crv0[-1][0] - pool), "abovecrest")
crest = row["est_crest_m"]
assert max(e for e, _v in crv) <= crest + 1e-3 and S._interp_curve(crv, crest) >= MXB - 1.0, (crv[-2:], crest)
assert near(crv[0][1], NM, TOL) and row["stor_anchor"].startswith("maximum storage at the top of dam")
assert "MAXPOOL-ABOVE-CREST" in row["stor_notes"] and [w for w in warn if "MAXPOOL-ABOVE-CREST" in w], warn
assert near(row["cal_total_m3"], MXB, 1.0)

# ---- 3b. tool 1 writes inv_maxa_m2 / inv_norm_elev_m / inv_max_elev_m by position: read them back
# BY NAME (with the other numeric fields) from a DWR record that has all three, so a value written
# into the wrong column fails here (ft -> m, ac -> m2, ac-ft -> m3)
kind, flds, feats = st.layer_specs()["dwr"]
(gspec, attrs), = feats
extra = {"max_pool_area": 25.0, "normal_elev": 1870.0, "max_pool_elev": 1874.5}
lay = mock_layers({"dwr_pools": (kind, flds + [(k, "double") for k in extra],
                                 [(gspec, dict(attrs, **extra))])})["dwr_pools"]
r1 = processing.run("nddam:preparedams", {"DWR": lay, "DWR_UNITS": 0, "KEEP": "Intact", "MINH": 0,
                                          "MINV": 0, "DIST_FT": 328.084, "OUT": "TEMPORARY_OUTPUT"}, ctx, fb)
o1 = ctx.layers.get(r1["OUT"], r1["OUT"])
(got,) = [{k.name(): f[k.name()] for k in o1.fields()} for f in o1.getFeatures()]
want = {"height_m": attrs["max_dam_he"] * inv.FT_M, "inv_norm_m3": attrs["normal_vol"] * inv.AF_M3,
        "inv_max_m3": attrs["max_pool_v"] * inv.AF_M3, "inv_area_m2": attrs["normal_are"] * inv.AC_M2,
        "crest_len_m": attrs["dam_length"] * inv.FT_M, "inv_maxa_m2": 25.0 * inv.AC_M2,
        "inv_norm_elev_m": 1870.0 * inv.FT_M, "inv_max_elev_m": 1874.5 * inv.FT_M}
for k, v in want.items():
    assert got[k] is not None and near(got[k], v, 1e-6), (k, got[k], v)
assert len(set(round(v, 3) for v in want.values())) == len(want)   # all distinct: a swap is seen
assert got["dam_id"] and got["source"] == "DWR" and got["struct_cls"] is None, got

shutil.rmtree(work)
print("OK (storage anchored at its own elevation)")
