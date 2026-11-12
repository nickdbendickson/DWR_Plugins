"""20 m NoData-receptor rule on the shared nd_hazard sampler and Runner Receptors.

    python3 -m pytest DamSafety_Runner/tests/test_nodata_sample_rule.py -q
    or: python3 DamSafety_Runner/tests/test_nodata_sample_rule.py

Synthetic grids only (10, 20, 30 m; finite 0; peaks from different cells).
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
# Runner tests usually put the plugin (or nd_breach_runner alias) on the path
for p in (os.path.join(ROOT, "DamSafety_Runner"), ROOT):
    if p not in sys.path:
        sys.path.insert(0, p)

import numpy as np  # noqa: E402

try:
    from core import nd_hazard as nh, impacts as IM
except ImportError:
    from DamSafety_Runner.core import nd_hazard as nh, impacts as IM  # noqa: E402

from DamSafety_Core.damsuite import nodata_sample as NDS  # noqa: E402


def _hole(cell=30.0, dry_centre=False, all_nodata=False):
    """5x5 depth: NoData (or dry 0) at (2,2); wet east 3 / 1 and north 2 / 4."""
    d = np.zeros((5, 5), float)
    v = np.full((5, 5), 0.5, float)
    if all_nodata:
        d[:] = np.nan
    else:
        if not dry_centre:
            d[2, 2] = np.nan
        d[2, 3], v[2, 3] = 3.0, 1.0
        d[1, 2], v[1, 2] = 2.0, 4.0
    x0, y_top = 0.0, 5.0 * cell
    x, y = 2.5 * cell, y_top - 2.5 * cell
    return d, v, x0, y_top, cell, x, y


def test_sample_point_10m_and_20m_score_dry():
    for cell in (10.0, 20.0):
        d, v, x0, yt, cell, x, y = _hole(cell=cell)
        pv = nh.sample_point(d, v, x0, yt, cell, x, y, cell_m=cell)
        assert pv.sampling == NDS.SAMPLING_CENTRE_NODATA_DRY
        assert pv.flag == NDS.FLAG_DRY_REVIEW
        assert pv[0] is None          # historical: NoData depth is None (dry)


def test_sample_point_30m_uses_neighbourhood_option_c():
    d, v, x0, yt, cell, x, y = _hole(cell=30.0)
    pv = nh.sample_point(d, v, x0, yt, cell, x, y, cell_m=cell)
    assert pv.sampling == NDS.SAMPLING_NEIGHBOURHOOD
    assert pv.flag == NDS.FLAG_NEIGHBOURHOOD
    assert pv[0] == 3.0 and pv[1] == 4.0          # peaks from different wet cells


def test_finite_zero_is_not_filled():
    d, v, x0, yt, cell, x, y = _hole(cell=30.0, dry_centre=True)
    pv = nh.sample_point(d, v, x0, yt, cell, x, y, cell_m=cell)
    assert pv.flag is None
    assert pv.sampling == NDS.SAMPLING_CENTRE
    assert pv[0] == 0.0


def test_no_wet_neighbour_unflagged():
    for cell in (10.0, 20.0, 30.0):
        d, v, x0, yt, cell, x, y = _hole(cell=cell, all_nodata=True)
        pv = nh.sample_point(d, v, x0, yt, cell, x, y, cell_m=cell)
        assert pv.flag is None
        assert pv[0] is None          # historical: NoData depth is None (dry)


def test_sample_disc_and_footprint_follow_the_rule():
    d, v, x0, yt, cell, x, y = _hole(cell=30.0)
    # tiny radius: no cell centre in the disc besides the NoData centre (or none)
    dd, vv, how = nh.sample_disc(d, v, x0, yt, cell, x, y, radius=0.1 * cell, cell_m=cell)
    assert dd == 3.0 and vv == 4.0 and "neighbourhood" in how
    d, v, x0, yt, cell, x, y = _hole(cell=10.0)
    dd, vv, how = nh.sample_disc(d, v, x0, yt, cell, x, y, radius=0.1 * cell, cell_m=10.0)
    assert dd == 0.0 and "centre_nodata_dry" in how
    # a small footprint covering only the centre cell
    d, v, x0, yt, cell, x, y = _hole(cell=30.0)
    ring = [(x - 1, y - 1), (x + 1, y - 1), (x + 1, y + 1), (x - 1, y + 1)]
    dd, vv, how = nh.sample_footprint(d, v, x0, yt, cell, ring, cell_m=30.0)
    assert dd == 3.0 and vv == 4.0 and "neighbourhood" in how


def test_receptors_apply_nodata_on_grid():
    from core.grid import Grid
    d, v, x0, yt, cell, x, y = _hole(cell=30.0)
    g = Grid(np.zeros((5, 5)), x0, yt, cell)
    rec = IM.Receptors()
    rec.add_points([(x, y)], ids=["hole"])
    dd, vv, samp, flags = rec.apply_nodata_on_grid(g, d, v, cell_m=30.0)
    assert samp[0] == NDS.SAMPLING_NEIGHBOURHOOD and flags[0] == NDS.FLAG_NEIGHBOURHOOD
    assert dd[0] == 3.0 and vv[0] == 4.0
    d, v, x0, yt, cell, x, y = _hole(cell=10.0)
    g = Grid(np.zeros((5, 5)), x0, yt, cell)
    rec = IM.Receptors()
    rec.add_points([(x, y)], ids=["hole"])
    dd, vv, samp, flags = rec.apply_nodata_on_grid(g, d, v, cell_m=10.0)
    assert samp[0] == NDS.SAMPLING_CENTRE_NODATA_DRY and flags[0] == NDS.FLAG_DRY_REVIEW
    assert dd[0] == 0.0


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print("ok", t.__name__)
    print("{} tests ok".format(len(tests)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
