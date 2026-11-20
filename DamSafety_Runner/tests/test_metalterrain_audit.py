"""BoannGrid delta / artifact audit layer (core/metalterrain/audit.py): the fill delta is never
negative and matches the fill; the depression census accounts for every filled cell (per-pit
volumes sum to the total fill volume); and a deep pit reads as a pit. Pure Python, no GPU and no
scipy (the numpy label fallback), so it runs in the cloud.
"""
import numpy as np

from nd_breach_runner.core.metalterrain import fill as F
from nd_breach_runner.core.metalterrain import audit as A
from nd_breach_runner.core.metalterrain import cases
from nd_breach_runner.core.metalflood import backends as mfb
from nd_breach_runner.tests.registry import Skip, test


def _need_cpu_build():
    import os
    if os.name == "nt" or not mfb.find_compiler():
        raise Skip("no C++ compiler for the fill CPU build")


@test
def audit_delta_is_nonnegative_and_matches_fill():
    """The fill delta is filled - dem, never negative (the fill only raises), and NaN at NoData."""
    _need_cpu_build()
    z = cases.pitted_plane(ny=60, nx=90)
    filled, _ = F.fill_dem(z, backend="cpu")
    delta = A.fill_delta(z, filled)
    valid = np.isfinite(z)
    assert np.isnan(delta[~valid]).all() if (~valid).any() else True
    assert (delta[valid] >= 0.0).all(), "a negative fill delta (the fill lowered ground)"
    assert np.allclose(delta[valid], np.maximum(filled - z, 0.0)[valid])


@test
def audit_census_conserves_volume():
    """Every filled cell belongs to exactly one depression, so the per-depression fill volumes
    sum to the total fill volume, and each depression has a sane geometry."""
    _need_cpu_build()
    z = cases.pitted_plane(ny=70, nx=100)
    labels, items = A.depression_census(z, cell_m=10.0, backend="cpu")
    assert len(items) >= 1, "no depressions found on a pitted plane"
    delta = A.fill_delta(z, F.fill_dem(z, backend="cpu")[0])
    total = float(np.nansum(np.where(delta > A.MIN_DELTA_M, delta, 0.0))) * 100.0
    summed = sum(it["volume_m3"] for it in items)
    assert abs(summed - total) < 1e-3 * max(1.0, total), (summed, total)
    for it in items:
        assert it["max_depth_m"] > 0 and it["volume_m3"] > 0 and it["n_cells"] >= 1
        assert it["spill_elev_m"] >= it["floor_elev_m"]
        assert it["klass"] in ("noise", "fill", "pit", "crossing")


@test
def audit_deep_pit_reads_as_pit():
    """The deep interior pit of the NoData-ringed case is censused and classified a real pit."""
    _need_cpu_build()
    z = cases.nodata_ringed_pit(n=64, depth=8.0)
    labels, items = A.depression_census(z, cell_m=10.0, backend="cpu")
    assert items, "the ringed pit was not found"
    deepest = max(items, key=lambda it: it["max_depth_m"])
    assert deepest["max_depth_m"] > 2.0, deepest["max_depth_m"]
    assert deepest["klass"] == "pit", deepest


@test
def audit_label4_fallback_components():
    """The scipy-free 4-connected labeller separates disjoint blobs and joins a 4-connected one
    (a diagonal touch is NOT connected under 4-connectivity)."""
    m = np.zeros((7, 7), bool)
    m[1:3, 1:3] = True            # block A
    m[4:6, 4:6] = True            # block B (disjoint)
    lab, n = A._label4(m)
    assert n == 2, n
    assert lab[1, 1] != lab[4, 4] and lab[1, 1] > 0 and lab[4, 4] > 0
    # a diagonal-only touch stays two components under 4-connectivity
    d = np.zeros((4, 4), bool)
    d[1, 1] = d[2, 2] = True
    _, nd = A._label4(d)
    assert nd == 2, nd


@test
def audit_classify_tags():
    """The first-pass triage tags: a long thin not-shallow depression = crossing, a deep one =
    pit, a tiny shallow one = noise."""
    assert A.classify(area_cells=40, max_depth_m=0.6, aspect=6.0, cell_m=10.0) == "crossing"
    assert A.classify(area_cells=50, max_depth_m=3.0, aspect=1.2, cell_m=10.0) == "pit"
    assert A.classify(area_cells=2, max_depth_m=0.1, aspect=1.0, cell_m=10.0) == "noise"
    assert A.classify(area_cells=10, max_depth_m=0.5, aspect=1.5, cell_m=10.0) == "fill"


if __name__ == "__main__":
    import sys
    fails = 0
    for fn in (audit_delta_is_nonnegative_and_matches_fill, audit_census_conserves_volume,
               audit_deep_pit_reads_as_pit, audit_label4_fallback_components,
               audit_classify_tags):
        try:
            fn()
            print("ok    ", fn.__name__)
        except Skip as e:
            print("skip  ", fn.__name__, "({})".format(e))
        except Exception:
            import traceback
            fails += 1
            print("FAIL  ", fn.__name__)
            traceback.print_exc()
    sys.exit(1 if fails else 0)
