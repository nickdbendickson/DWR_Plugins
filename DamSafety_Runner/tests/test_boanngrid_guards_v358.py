"""Runner 3.58.0 BoannGrid guards (Absaraka DWR-2801 rerun, November 2026): the crossing reconnect
never lowers a protected (building / town) cell, never cuts deeper than max_carve_m, can be held to
road/rail crossings, and logs every skipped carve; flow_accum leaves no dead end inside the grid
(every flow path reaches an outlet) and changes no direction that already drained. Synthetic grids
only, through the fill/flow/audit CPU build.
"""
import os

import numpy as np

from nd_breach_runner.core.metalterrain import burn as B
from nd_breach_runner.core.metalterrain import cases
from nd_breach_runner.core.metalterrain import delta_log as DL
from nd_breach_runner.core.metalterrain import fill as F
from nd_breach_runner.core.metalterrain import flow as FL
from nd_breach_runner.core.metalflood import backends as mfb
from nd_breach_runner.tests.registry import Skip, test


def _need_cpu_build():
    if os.name == "nt" or not mfb.find_compiler():
        raise Skip("no C++ compiler for the fill CPU build")


def _barrier():
    """The culvert-less road fill case: a channel trench dammed by a 2-cell road fill."""
    z = cases.channel_with_barrier(ny=50, nx=120, chan_depth=3.0, sill=1.0)
    return z, 25, 60                       # grid, channel row, road column


def _reconnect(z, **kw):
    kw.setdefault("min_accum_cells", 10)
    return B.reconnect_crossings(z, cell_m=10.0, backend="cpu", **kw)


@test
def guards_default_carves_the_road_crossing():
    """With the 3.58.0 defaults (crossings only, 5 m cap) the road fill is still carved, the cut is
    within the cap, and every carved record says how deep it cut."""
    _need_cpu_build()
    z, _r, _c = _barrier()
    out, recs = _reconnect(z)
    done = B.carved(recs)
    assert done, "the default guards stopped the road-crossing carve: {}".format(recs)
    assert (out <= z + 1e-9).all()
    assert float((z - out).max()) <= B.DEFAULT_MAX_CARVE_M + 1e-9
    assert all("max_cut_m" in r for r in done)


@test
def guards_protect_mask_blocks_the_carve():
    """A protected cell on the cut (a house / town limit) -> no cell lowered anywhere, and the
    record is refused 'protect' naming the protected cells."""
    _need_cpu_build()
    z, r, c = _barrier()
    _o, recs0 = _reconnect(z)
    lowered = np.zeros(z.shape, bool)
    lowered[np.abs(_o - z) > 1e-9] = True
    assert lowered[r, c:c + 2].any(), "the reference carve does not cut the road"
    protect = np.zeros(z.shape, bool)
    protect[r - 1:r + 2, c - 1:c + 3] = True
    out, recs = _reconnect(z, protect_mask=protect)
    assert not (np.abs(out - z) > 1e-9)[protect].any(), "a protected cell was lowered"
    assert B.refusals_by_reason(recs).get("protect"), recs
    hit = [x for x in recs if x.get("refused") == "protect"]
    assert all(protect[rc] for x in hit for rc in x["protected_cells"])
    assert not B.carved(recs), "carved around the protect mask on this grid"
    # a protect mask away from the cut changes nothing
    far = np.zeros(z.shape, bool)
    far[2:5, 2:5] = True
    out2, recs2 = _reconnect(z, protect_mask=far)
    assert np.array_equal(out2, _o) and len(B.carved(recs2)) == len(B.carved(recs0))


@test
def guards_depth_cap_refuses_not_clips():
    """A cap below the cut refuses the whole carve (no half notch); a cap above it carves; the cap
    must be positive."""
    _need_cpu_build()
    z, _r, _c = _barrier()
    ref_out, ref = _reconnect(z, max_carve_m=None)
    cut = max(x["max_cut_m"] for x in B.carved(ref))
    assert cut > 1.0, cut
    out, recs = _reconnect(z, max_carve_m=cut * 0.5)
    assert np.array_equal(out, z), "a carve deeper than the cap lowered ground"
    assert B.refusals_by_reason(recs).get("depth"), recs
    dep = [x for x in recs if x.get("refused") == "depth"]
    assert all(x["max_cut_m"] > x["max_carve_m"] for x in dep), recs
    out2, _ = _reconnect(z, max_carve_m=cut + 0.01)
    assert np.array_equal(out2, ref_out)
    for bad in (0, -1.0):
        try:
            _reconnect(z, max_carve_m=bad)
        except ValueError:
            pass
        else:
            assert False, "max_carve_m={} accepted".format(bad)


@test
def guards_crossing_mask_and_classes():
    """crossing_mask: a carve must lower a road/rail cell; classes filter which depressions count."""
    _need_cpu_build()
    z, r, c = _barrier()
    ref_out, _ = _reconnect(z)
    road = np.zeros(z.shape, bool)
    road[:, c:c + 2] = True                 # the road line, north-south
    out, recs = _reconnect(z, crossing_mask=road)
    assert np.array_equal(out, ref_out) and B.carved(recs)
    elsewhere = np.zeros(z.shape, bool)
    elsewhere[:, 5] = True
    out2, recs2 = _reconnect(z, crossing_mask=elsewhere)
    assert np.array_equal(out2, z), "carved where no road/rail cell is cut"
    assert B.refusals_by_reason(recs2).get("not_road_rail"), recs2
    out3, recs3 = _reconnect(z, classes=("pit",))
    assert np.array_equal(out3, z) and not recs3, "a crossing carved with classes=('pit',)"


@test
def guards_refusals_reach_the_delta_log():
    """Each guard refusal is one AUTO CROSSING_STITCH row with action REFUSE_<REASON>, n_cells 0;
    it does not FLAG (only freeze refusals block an export)."""
    _need_cpu_build()
    z, _r, _c = _barrier()
    _o, recs = _reconnect(z, max_carve_m=0.5)
    log = DL.DeltaLog(run_id="GUARDS")
    try:
        n = DL.log_reconnect(log, recs, cell_m=10.0)
        assert n == 0, "a depth refusal counted as a freeze refusal"
        rows = log._db.execute("SELECT action, n_cells, review_status FROM delta_log").fetchall()
        assert rows and all(a == "REFUSE_DEPTH" and k == 0 and s == "AUTO" for a, k, s in rows), rows
        assert not log.flagged()
    finally:
        log.close()


def _d8_filled(seed=3, ny=60, nx=80):
    """A rough surface, D8-filled: the fill relaxes over 8 neighbours, so the D4 field on it has
    cells whose only lower neighbour is diagonal, plus float32 flats - the Absaraka dead ends."""
    rng = np.random.RandomState(seed)
    z = 300.0 + 0.02 * np.add.outer(np.arange(ny), np.arange(nx)) + rng.standard_normal((ny, nx))
    z[10:14, 20:40] = 299.0                                 # an exact flat
    filled, _ = F.fill_dem(z, backend="cpu")
    return z, filled


@test
def flow_accum_leaves_no_dead_end_and_keeps_drained_paths():
    """3.58.0 default: zero dead ends, every valid cell drains to an outlet (outlet sums = valid
    cells), and every direction that was not a dead end in the pre-3.58.0 field is unchanged."""
    _need_cpu_build()
    for conn in (4, 8):
        z, filled = _d8_filled()
        d0, a0, i0 = FL.flow_accum(z, filled=filled, backend="cpu", conn=conn, resolve_flats=False)
        d1, a1, i1 = FL.flow_accum(z, filled=filled, backend="cpu", conn=conn)
        valid = np.isfinite(z)
        dead0 = FL.dead_ends(d0, valid, conn=conn)
        if conn == 4:
            assert dead0.sum() > 0, "the synthetic grid has no D4 dead ends to resolve"
        assert int(FL.dead_ends(d1, valid, conn=conn).sum()) == 0, (conn, i1)
        assert i1.get("dead_ends_after", 0) == 0 and i1.get("dead_ends") == int(dead0.sum()), i1
        assert np.array_equal(d1[~dead0], d0[~dead0]), "a draining direction was changed"
        outlets = valid & (d1 == FL.none_code(conn))
        assert abs(float(np.nansum(a1[outlets])) - float(valid.sum())) < 1e-6, conn
        if not dead0.any():
            assert np.array_equal(a1, a0, equal_nan=True)


@test
def flow_accum_iterate_path_resolves_too():
    """method='iterate' gets the same resolved directions and drainage area as 'topo'."""
    _need_cpu_build()
    z, filled = _d8_filled(seed=5, ny=40, nx=50)
    dt, at, _ = FL.flow_accum(z, filled=filled, backend="cpu")
    di, ai, _ = FL.flow_accum(z, filled=filled, backend="cpu", method="iterate")
    assert np.array_equal(dt, di)
    assert np.allclose(at, ai, equal_nan=True)


if __name__ == "__main__":
    import sys
    import traceback
    fails = 0
    for fn in (guards_default_carves_the_road_crossing, guards_protect_mask_blocks_the_carve,
               guards_depth_cap_refuses_not_clips, guards_crossing_mask_and_classes,
               guards_refusals_reach_the_delta_log,
               flow_accum_leaves_no_dead_end_and_keeps_drained_paths,
               flow_accum_iterate_path_resolves_too):
        try:
            fn()
            print("ok    ", fn.__name__)
        except Skip as e:
            print("skip  ", fn.__name__, "({})".format(e))
        except Exception:
            fails += 1
            print("FAIL  ", fn.__name__)
            traceback.print_exc()
    sys.exit(1 if fails else 0)
