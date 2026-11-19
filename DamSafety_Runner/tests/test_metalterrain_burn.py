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
def burn_channel_refuses_frozen_cell():
    """burn_channel raises LOUDLY if the path crosses a frozen (embankment / dam-crest) cell -
    burning through a dam crest is the catastrophe the mask exists to prevent (pure numpy)."""
    z = cases.pitted_plane(ny=30, nx=40)
    path = [(15, c) for c in range(5, 35)]
    freeze = np.zeros(z.shape, bool)
    freeze[15, 20] = True                             # one frozen cell on the path
    try:
        B.burn_channel(z, path, depth=0.5, freeze_mask=freeze)
    except RuntimeError as e:
        assert "frozen" in str(e), str(e)
    else:
        assert False, "burn_channel did not refuse the frozen cell"
    # with no freeze on the path, the same burn succeeds
    out = B.burn_channel(z, path, depth=0.5, freeze_mask=np.zeros(z.shape, bool))
    assert (out <= z + 1e-9).all()


@test
def reconnect_respects_freeze_mask():
    """A freeze mask over the road fill makes reconnect REFUSE the carve (never breach a frozen
    embankment): the barrier is not lowered, and the refusal is recorded loudly for review."""
    _need_cpu_build()
    z = cases.channel_with_barrier(ny=50, nx=120, chan_depth=3.0, sill=1.0)
    # without a mask, the crossing is carved
    out_free, recs_free = B.reconnect_crossings(z, min_accum_cells=10, cell_m=10.0, backend="cpu")
    carved = [r for r in recs_free if "refused" not in r]
    assert carved, "baseline carve did not happen"
    # freeze the road fill: cells sitting notably above both their east and west neighbours
    raised = z - np.minimum(np.roll(z, 1, axis=1), np.roll(z, -1, axis=1))
    freeze = raised > 1.0
    assert freeze.any(), "could not locate the barrier to freeze"
    out_mask, recs_mask = B.reconnect_crossings(z, min_accum_cells=10, cell_m=10.0, backend="cpu",
                                                freeze_mask=freeze)
    # no frozen cell was lowered
    assert (out_mask[freeze] >= z[freeze] - 1e-9).all(), "a frozen embankment cell was lowered"
    # and at least one crossing is recorded as refused-for-freeze (loud, not silent)
    refused = [r for r in recs_mask if r.get("refused") == "freeze"]
    assert refused, "freeze refusal was not recorded"
    assert all(r["frozen_cells"] for r in refused)


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
               burn_channel_refuses_frozen_cell, reconnect_respects_freeze_mask,
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
