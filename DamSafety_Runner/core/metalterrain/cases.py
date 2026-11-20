"""Synthetic DEMs for the BoannGrid fill tests (tests/test_metalterrain.py) and its bench
(__main__.py): a tilted plane with gaussian pits (closed depressions to fill), and a variant
ringed by NoData with an interior pit (the free-exit case the Phase-A conditioner got wrong)."""
import numpy as np


def pitted_plane(ny=80, nx=120, dx=10.0, bed=300.0, slope=0.01, pits=((0.30, 0.45, 6.0, 7.0),
                                                                       (0.65, 0.35, 3.5, 5.0),
                                                                       (0.50, 0.75, 9.0, 9.0)),
                 seed=0):
    """A plane tilted to the north-east with gaussian pits and a little rough texture. `pits` is
    (row_frac, col_frac, depth_m, radius_cells). Border cells are outlets (no NoData)."""
    yy, xx = np.mgrid[0:ny, 0:nx].astype(np.float64)
    z = bed + slope * dx * xx + 0.6 * slope * dx * yy
    rng = np.random.RandomState(seed)
    z += 0.05 * rng.standard_normal((ny, nx))
    for rf, cf, depth, rad in pits:
        cy, cx = rf * ny, cf * nx
        z -= depth * np.exp(-((xx - cx) ** 2 + (yy - cy) ** 2) / (2.0 * rad * rad))
    return z


def channel_with_barrier(ny=60, nx=120, dx=10.0, bed=300.0, slope=0.01, chan_row=None,
                         chan_depth=3.0, barrier_col=None, barrier_w=2, sill=1.0, seed=0):
    """A plane draining east with a channel trench along one row, crossed by a road that fills the
    trench at the crossing (a culvert-less road fill): the plane drains east freely, but the
    channel west of the road is dammed into a long depression it drains into - the crossing case
    Stage 4c reconnection must detect and carve through. Border cells are outlets (no NoData)."""
    yy, xx = np.mgrid[0:ny, 0:nx].astype(np.float64)
    z = bed - slope * dx * xx                       # drains east (+x), ground falls eastward
    rng = np.random.RandomState(seed)
    z += 0.02 * rng.standard_normal((ny, nx))
    cr = ny // 2 if chan_row is None else int(chan_row)
    z[cr, :] -= chan_depth                           # the channel trench
    bc = nx // 2 if barrier_col is None else int(barrier_col)
    z[cr, bc:bc + barrier_w] += chan_depth + sill    # the road fills the trench to a sill at the crossing
    return z


def nested_basins(ny=30, nx=60, high=350.0, inner_floor=310.0, inner_rim=325.0,
                  outer_floor=320.0, outer_rim=330.0, exit_low=300.0):
    """A basin inside a basin: a deep inner basin that spills over an inner saddle into a
    shallower outer basin, which spills over a higher outer saddle down a channel to the right
    border (the only outlet). Everything else is a high wall.

    This is the case where epsilon-relaxation Planchon-Darboux and priority-flood classically
    diverge: the inner basin must NOT stop filling at the inner saddle (inner_rim); it must
    chain through the inner spill, across the outer basin, and up to the OUTER spill (outer_rim)
    - the lowest point of the only path to a true outlet. So the whole enclosed region
    (inner basin + inner saddle + outer basin) fills to outer_rim, with the eps drainage ramp
    rising from the outer spill back into the deepest (inner) cells.

    Returns (z, meta); meta has the boolean masks `inner`, `outer`, `enclosed` and the floats
    `inner_rim`, `outer_rim`, `fill_level` (== outer_rim) so the test can assert the fill level
    and the spill sequence, not just the final surface shape. Requires nx >= 40, no NoData;
    border cells are outlets (as pitted_plane)."""
    z = np.full((ny, nx), high, dtype=np.float64)
    band = slice(1, ny - 1)                      # interior rows; rows 0 / ny-1 stay high walls
    c_in0, c_in1 = 6, 16                          # inner basin columns [c_in0, c_in1)
    c_irim = 16                                   # inner saddle (inner -> outer)
    c_out0, c_out1 = 17, 31                       # outer basin columns [c_out0, c_out1)
    c_orim = 31                                   # outer saddle (outer -> exit channel)
    z[band, c_in0:c_in1] = inner_floor
    z[band, c_irim] = inner_rim
    z[band, c_out0:c_out1] = outer_floor
    z[band, c_orim] = outer_rim
    # exit channel: monotone downhill from the outer saddle to the right-border outlet, so it
    # drains freely and is not filled.
    xs = np.arange(c_orim + 1, nx)
    z[band, c_orim + 1:nx] = outer_rim - (outer_rim - exit_low) * (xs - c_orim) / (nx - 1 - c_orim)
    inner = np.zeros((ny, nx), bool)
    inner[band, c_in0:c_in1] = True
    outer = np.zeros((ny, nx), bool)
    outer[band, c_out0:c_out1] = True
    enclosed = np.zeros((ny, nx), bool)
    enclosed[band, c_in0:c_orim] = True          # inner basin + inner saddle + outer basin
    meta = {"inner": inner, "outer": outer, "enclosed": enclosed,
            "inner_rim": inner_rim, "outer_rim": outer_rim, "fill_level": outer_rim}
    return z, meta


def nodata_ringed_pit(n=64, dx=10.0, bed=300.0, collar=4, depth=8.0, rad=6.0):
    """A square valid island inside a NoData collar, with one deep pit. The pit cannot drain to
    the grid border (the collar blocks it); it must drain to the NoData, so the fill is finite.
    Returns a float array with np.nan in the collar."""
    z = np.full((n, n), np.nan)
    yy, xx = np.mgrid[0:n, 0:n].astype(np.float64)
    inner = bed + 0.004 * dx * xx + 0.004 * dx * yy
    inner -= depth * np.exp(-((xx - n * 0.5) ** 2 + (yy - n * 0.5) ** 2) / (2.0 * rad * rad))
    m = np.zeros((n, n), bool)
    m[collar:n - collar, collar:n - collar] = True
    z[m] = inner[m]
    return z
