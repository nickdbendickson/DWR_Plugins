"""Shallow-basin storage guard, ported from nd_ Screening 0.28.3.

  1. tool 3: a basin far shallower than the dam (bed to crest < 0.5 x height) is not the dam's
     reservoir; the full-height inventory curve is used when the record has storage and height.
  2. the hand-off sends no surface area when the curve spans < 0.5 x the dam height, so the
     Runner uses storage ~ depth squared instead of an exponent at its cap of 6.

Absaraka (DWR-2801): a dry flood-control dam 27.5 ft high, DWR maximum 1,615.5 ac-ft, and only
a shallow dish of water near it.
"""
import csv
import os
import shutil
import sys
import tempfile

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(HERE, "qgis_mock"))
sys.path.insert(0, os.path.dirname(PLUGIN))
sys.path.insert(0, HERE)
from _optional import need  # noqa: E402
need("scipy", "storage.estimate_storage uses scipy.ndimage")

from nd_dam_screening.core import handoff as ho, storage as st  # noqa: E402
from nd_dam_screening.algorithms.estimate_storage import EstimateStorage  # noqa: E402

FT, AF = 0.3048, 1233.48183754752
HEIGHT_FT, MAXV_AF = 27.5, 1615.5
H = HEIGHT_FT * FT


def _plane(n=80, cell=10.0):
    return 500.0 + 0.002 * np.arange(n)[None, :] * cell + 0.001 * np.arange(n)[:, None] * cell


def test_absaraka_dish_is_a_shallow_basin():
    n, cell = 80, 10.0
    dem = _plane(n, cell)
    dem[36:44, 36:44] = dem[36:44, 36:44].min() - 1.0 * FT        # a 1 ft-deep flat dish
    mask = np.zeros_like(dem, bool)
    mask[36:44, 36:44] = True
    res = st.estimate_storage(dem, mask, cell, (40, 45), known_height_m=H)
    span = st.curve_span_m(res)
    assert span is not None and span < st.SHALLOW_BASIN * H, (span / FT, res.notes)
    assert st.is_shallow_basin(span, H)


def test_real_reservoir_is_not_shallow():
    # a valley with a deep pool: bed to crest is most of the dam height
    n, cell = 80, 10.0
    yy, xx = np.mgrid[0:n, 0:n]
    dem = 500.0 + 0.5 * np.abs(xx - 40) * cell * 0.1 + 0.02 * yy * cell
    pool = 503.0
    mask = dem < pool
    dem = np.where(mask, pool, dem)                                  # hydro-flattened water
    res = st.estimate_storage(dem, mask, cell, (40, 40), known_height_m=8.0)
    assert not st.is_shallow_basin(st.curve_span_m(res), 8.0), (st.curve_span_m(res), res.notes)
    assert not st.is_shallow_basin(1.0, None) and not st.is_shallow_basin(None, 10.0)
    assert not st.is_shallow_basin(0.2, 0.4)                          # no real height: no guard


class _Feat(dict):
    pass


class _Alg(object):
    """Just enough of the algorithm for _inventory_curve (no STORAGE_CHOICE parameter)."""
    def parameterDefinition(self, name):
        return None


def test_inventory_curve_replaces_the_dish():
    f = _Feat(height_m=H, inv_max_m3=MAXV_AF * AF, inv_norm_m3=0.0)
    names = list(f)
    arr = np.full((10, 10), 1000.0)
    res = EstimateStorage._inventory_curve(_Alg(), f, names, arr, 5, 5, {}, None,
                                           st.SHALLOW_BASIN_WHY.format(2.4, HEIGHT_FT))
    assert res is not None and res.confidence == "inventory"
    assert abs(res.total_m3 / AF - MAXV_AF) < 0.01 and abs(res.crest_elev - res.pool_elev - H) < 1e-9
    assert abs(res.curve[-1][1] / AF - MAXV_AF) < 0.01 and res.curve[0] == (1000.0, 0.0)
    assert "not the reservoir" in res.notes[0] and "maximum" in res.notes[0]
    # the full-height curve is not itself "shallow"
    assert not st.is_shallow_basin(st.curve_span_m(res), H)
    # no inventory storage or no height: nothing to fall back to (the basin stands)
    assert EstimateStorage._inventory_curve(_Alg(), _Feat(height_m=H), ["height_m"], arr, 5, 5,
                                            {}, None, "x") is None
    assert EstimateStorage._inventory_curve(_Alg(), _Feat(inv_max_m3=1e6), ["inv_max_m3"], arr, 5, 5,
                                            {}, None, "x") is None


def _curve(path, pts):
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["elev_m", "storage_m3"])
        for e, v in pts:
            w.writerow([e, v])


def test_handoff_sends_no_area_for_a_shallow_curve():
    d = tempfile.mkdtemp(prefix="shallow_")
    try:
        shallow = os.path.join(d, "curve_DWR-2801.csv")
        z0 = 300.0                                    # the Absaraka curve: a 4 ft-deep basin
        _curve(shallow, [(z0 + i * 0.4 * FT, MAXV_AF * AF * (i / 10.0) ** 6) for i in range(11)])
        area, note = ho.area_for_runner(shallow, z0 + 4.0 * FT, H)
        assert area is None and "no surface area sent" in note and "27.5 ft" in note, note
        assert ho.area_at_top_m2(shallow, z0 + 4.0 * FT) > 100 * 4046.86    # what used to go
        full = os.path.join(d, "curve_full.csv")
        _curve(full, [(z0 + i * H / 10.0, MAXV_AF * AF * (i / 10.0) ** 2) for i in range(11)])
        area, note = ho.area_for_runner(full, z0 + H, H)
        assert area and area > 0 and note == ""
        assert ho.area_for_runner(full, z0 + H, None)[0] == ho.area_at_top_m2(full, z0 + H)
        assert ho.area_for_runner(os.path.join(d, "missing.csv"), None, H) == (None, "")
        # the Runner then gets no AREA_AC and says it assumes depth squared
        p, notes = ho.rundam_params({"dam_id": "DWR-2801", "height_m": H, "td_vw_m3": MAXV_AF * AF},
                                    (0.0, 0.0), "EPSG:5070", None, d, None, None, None)
        assert "AREA_AC" not in p or p["AREA_AC"] in (None, ""), p
    finally:
        shutil.rmtree(d)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok  ", name)
    print("test_shallow_basin: all passed")
