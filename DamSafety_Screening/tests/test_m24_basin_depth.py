"""M24 (Screening 0.19.3): the tool-7b shallow-curve guard measures the reservoir basin depth,
not the freeboard.

A tool-3 stage-storage curve starts at the LiDAR normal pool, so its own elevation span is the
freeboard above the pool, not the bed-to-crest depth. area_for_runner now adds tool-3's
est_maxdep_m (below-pool depth) before the 0.5 x height shallow-curve test, so a wet dam whose
freeboard is small but whose basin is deep sends the curve area instead of falling to the Runner's
storage ~ depth-squared assumption (m = 2). Safety guardrails: a curve kept only by the below-pool
depth defers to a DWR-recorded maximum-pool area; and rundam_params drops a curve area that implies
a storage-area exponent m = SA*Ht/Vt above the Runner's cap of 6 (the over-cap misfire).

Pure Python (handoff only): no scipy, no QGIS.

    python3 DamSafety_Screening/tests/test_m24_basin_depth.py
"""
import csv
import importlib
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(HERE)
sys.path.insert(0, os.path.dirname(PLUGIN))
PKG = os.path.basename(PLUGIN)
ho = importlib.import_module(PKG + ".core.handoff")

FT, AC = ho.FT, ho.ACRE_M2
H = 27 * FT                           # 8.2296 m ; 0.5 x H = 13.5 ft = 4.1148 m
Z0 = 100.0
_D = tempfile.mkdtemp(prefix="m24_")


def _curve(name, freeboard_m, area_m2, base=Z0):
    """A prism curve from the normal pool up `freeboard_m` metres: storage = area x depth."""
    p = os.path.join(_D, name)
    with open(p, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["elev_m", "storage_m3"])
        for i in range(21):
            z = freeboard_m * i / 20.0
            w.writerow([base + z, area_m2 * z])
    return p


DEEP = _curve("curve_deep.csv", H, 50000.0)           # freeboard = full height
SHALLOW = _curve("curve_shallow.csv", 1.5, 20000.0)   # freeboard 1.5 m (4.9 ft) - shallow alone
CREST_DEEP = Z0 + H
CREST_SHALLOW = Z0 + 1.5


def test_guard_adds_below_pool_depth():
    assert abs(ho.area_for_runner(DEEP, CREST_DEEP, H)[0] - 50000.0) < 1.0        # deep: area
    a, note = ho.area_for_runner(SHALLOW, CREST_SHALLOW, H)                       # freeboard alone
    assert a is None and "no surface area sent" in note and "27.0 ft" in note, note
    # the below-pool depth lifts it over the guard (1.5 + 3.0 = 4.5 m >= 4.11 m)
    assert abs(ho.area_for_runner(SHALLOW, CREST_SHALLOW, H, below_pool_m=3.0)[0] - 20000.0) < 1.0
    # not enough below-pool depth: still shallow (1.5 + 1.0 = 2.5 m < 4.11 m)
    assert ho.area_for_runner(SHALLOW, CREST_SHALLOW, H, below_pool_m=1.0)[0] is None
    print("ok m24: guard measures freeboard + below-pool depth, not freeboard alone")


def test_choice_precedence():
    # a deep curve keeps priority 1 over a recorded inventory area (unchanged)
    a, src, _ = ho.surface_area_choice(DEEP, CREST_DEEP, H, {"inv_maxa_m2": 999 * AC})
    assert src == ho.AREA_SRC_CURVE and abs(a - 50000.0) < 1.0, (src, a)
    # a curve rescued only by the below-pool depth defers to a recorded maximum-pool area (c)
    a, src, note = ho.surface_area_choice(
        SHALLOW, CREST_SHALLOW, H, {"est_maxdep_m": 3.0, "inv_maxa_m2": 12.5 * AC})
    assert src == ho.AREA_SRC_INVENTORY and abs(a - 12.5 * AC) < 1e-6, (src, a)
    assert "in preference to the stage-storage curve" in note, note
    # a rescued curve with no recorded area: the curve area is sent (the point of the fix)
    a, src, _ = ho.surface_area_choice(SHALLOW, CREST_SHALLOW, H, {"est_maxdep_m": 3.0})
    assert src == ho.AREA_SRC_CURVE and abs(a - 20000.0) < 1.0, (src, a)
    # still too shallow even with the below-pool depth, no recorded area: none
    a, src, _ = ho.surface_area_choice(SHALLOW, CREST_SHALLOW, H, {"est_maxdep_m": 1.0})
    assert src == ho.AREA_SRC_NONE and a is None, (src, a)
    print("ok m24: recorded max-pool area preferred over a below-pool-rescued curve")


def test_m_sanity_drops_over_cap_curve_area():
    attrs = {"dam_id": "M24", "height_m": H}
    # m = 50000 x 8.2296 / 411480 = 1.0 -> kept
    p, _ = ho.rundam_params(dict(attrs, td_vw_m3=411480.0), (0, 0), "EPSG:5070", None, _D,
                            area_top_m2=50000.0, area_source=ho.AREA_SRC_CURVE)
    assert "AREA_AC" in p, p
    # m = 50000 x 8.2296 / 40000 = 10.3 > 6 -> dropped (the over-cap misfire)
    p, notes = ho.rundam_params(dict(attrs, td_vw_m3=40000.0), (0, 0), "EPSG:5070", None, _D,
                                area_top_m2=50000.0, area_source=ho.AREA_SRC_CURVE)
    assert "AREA_AC" not in p and any("cap of 6" in n for n in notes), (p, notes)
    # a recorded inventory area with the same high exponent is authoritative - not dropped
    p, _ = ho.rundam_params(dict(attrs, td_vw_m3=40000.0), (0, 0), "EPSG:5070", None, _D,
                            area_top_m2=50000.0, area_source=ho.AREA_SRC_INVENTORY)
    assert "AREA_AC" in p, p
    print("ok m24: rundam_params drops a curve area implying an exponent above the Runner's cap of 6")


if __name__ == "__main__":
    test_guard_adds_below_pool_depth()
    test_choice_precedence()
    test_m_sanity_drops_over_cap_curve_area()
    print("test_m24_basin_depth: all passed")
