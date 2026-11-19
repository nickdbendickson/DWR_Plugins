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
