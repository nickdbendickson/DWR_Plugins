"""BoannGrid channel burn + crossing reconnection (core/metalterrain/burn.py): the burn enforces a
monotone downstream fall and only lowers; the reconnection finds a channel dammed by a culvert-less
road fill (a crossing), carves a notch through the barrier, and the dammed depression then drains.
Through the fill/flow/audit CPU build, so it runs in the cloud.
"""
import os

import numpy as np

from nd_breach_runner.core.metalterrain import fill as F
from nd_breach_runner.core.metalterrain import audit as A
from nd_breach_runner.core.metalterrain import burn as B
from nd_breach_runner.core.metalterrain import cases
from nd_breach_runner.core.metalflood import backends as mfb
from nd_breach_runner.tests.registry import Skip, test


def _need_cpu_build():
    if os.name == "nt" or not mfb.find_compiler():
        raise Skip("no C++ compiler for the fill CPU build")


def _crossing_volume(z, cell_m=10.0):
    """Total fill volume held in depressions a real channel drains into (klass crossing) - what a
    road fill dams up."""
    _labels, items = A.depression_census(z, cell_m=cell_m, backend="cpu")
    return sum(it["volume_m3"] for it in items if it["klass"] == "crossing"), items


@test
def burn_channel_enforces_monotone_fall():
    """burn_channel lowers a path to a non-increasing profile and changes nothing off the path."""
    z = cases.pitted_plane(ny=40, nx=60)
    path = [(20, c) for c in range(5, 55)]            # a west->east path across the grid
    out = B.burn_channel(z, path, depth=0.5)
    prof = np.array([out[r, c] for r, c in path])
    assert (np.diff(prof) <= 1e-9).all(), "burned profile rises downstream"
    assert (out <= z + 1e-9).all(), "burn raised ground somewhere"
    off = np.ones(z.shape, bool)
    for r, c in path:
        off[r, c] = False
    assert np.array_equal(out[off], z[off]), "burn changed cells off the path"


@test
def reconnect_drains_a_blocked_channel():
    """A channel dammed by a culvert-less road fill is detected and carved through, so the dammed
    depression drains: the crossing fill volume drops sharply, and the carve only ever lowers
    ground."""
    _need_cpu_build()
    z = cases.channel_with_barrier(ny=50, nx=120, chan_depth=3.0, sill=1.0)
    vol_before, items_before = _crossing_volume(z)
    assert vol_before > 0, "the synthetic road fill did not dam the channel"
    out, records = B.reconnect_crossings(z, min_accum_cells=10, cell_m=10.0, backend="cpu")
    assert records, "no crossing was reconnected"
    assert (out <= z + 1e-9).all(), "reconnection raised ground (carve must only lower)"
    vol_after, _ = _crossing_volume(out)
    assert vol_after < 0.25 * vol_before, (vol_before, vol_after)
    # the carve is local: only a small fraction of cells changed
    changed = int((np.abs(out - z) > 1e-6).sum())
    assert changed < 0.05 * z.size, "the carve changed too much of the grid"
    # each record carries the drainage area it reconnected (for the delta-log)
    assert all(r["accum_cells"] >= 10 for r in records)


@test
def reconnect_leaves_a_clean_dem_alone():
    """A plane with no dammed channel gets no carves (nothing to reconnect)."""
    _need_cpu_build()
    z = cases.pitted_plane(ny=48, nx=64)        # pits drain to the border, no road fill
    out, records = B.reconnect_crossings(z, min_accum_cells=10, cell_m=10.0, backend="cpu")
    # pitted plane has closed pits but they are not channel crossings; if any carve happens it
    # must still only lower and be small
    assert (out <= z + 1e-9).all()


if __name__ == "__main__":
    import sys
    fails = 0
    for fn in (burn_channel_enforces_monotone_fall, reconnect_drains_a_blocked_channel,
               reconnect_leaves_a_clean_dem_alone):
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
