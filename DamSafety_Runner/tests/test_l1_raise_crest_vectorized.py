"""L1: lisflood.raise_crest was vectorized with numpy (it used to iterate every
model cell in a pure-Python double loop, re-run on each LISFLOOD-FP pass). The
vectorized version must produce BYTE-IDENTICAL output -- the same raised DEM and
the same returned count -- as the original loop.

These tests pin that: an independent, hand-written copy of the original per-cell
loop (`_raise_crest_loop`) is the oracle, and the live lf.raise_crest must match
it byte-for-byte (data array via tobytes(), which is NaN-safe) and in its count,
for both the dam-line branch and the point+length fallback branch.
"""
import numpy as np

from nd_breach_runner.core import lisflood as lf
from nd_breach_runner.core.grid import Grid
from nd_breach_runner.tests.registry import test as register


def _raise_crest_loop(dem, crest_xy, inflow_xy, top_m, length_m=200.0, width_m=12.0,
                      line_xy=None):
    """The original pure-Python double loop raise_crest replaced. Independent
    oracle: same geometry test and same z-condition, one cell at a time."""
    if dem is None or top_m is None or (crest_xy is None and not line_xy):
        return 0
    use_line = line_xy is not None and len(list(line_xy)) >= 2
    vx = vy = px = py = 0.0
    half = 0.0
    if not use_line:
        vx = (inflow_xy[0] - crest_xy[0]) if inflow_xy else 0.0
        vy = (inflow_xy[1] - crest_xy[1]) if inflow_xy else 1.0
        n = (vx * vx + vy * vy) ** 0.5 or 1.0
        px, py = -vy / n, vx / n
        vx, vy = vx / n, vy / n
        half = max(length_m, dem.dx) / 2.0

    def near_line(x, y, w):
        h = w / 2.0
        pts = list(line_xy)
        for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
            sx, sy = x1 - x0, y1 - y0
            L2 = sx * sx + sy * sy
            t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((x - x0) * sx + (y - y0) * sy) / L2))
            d = ((x - (x0 + t * sx)) ** 2 + (y - (y0 + t * sy)) ** 2) ** 0.5
            if d <= h:
                return True
        return False

    data = dem.data
    nraise = 0
    for r in range(data.shape[0]):
        for c in range(data.shape[1]):
            x, y = dem.xy(r, c)
            if use_line:
                hit = near_line(x, y, max(width_m, dem.dx))
            else:
                dx, dy = x - crest_xy[0], y - crest_xy[1]
                along = dx * px + dy * py
                across = abs(dx * vx + dy * vy)
                hit = abs(along) <= half and across <= width_m / 2.0
            if not hit:
                continue
            z = data[r, c]
            if z == z and z < top_m:
                data[r, c] = top_m
                nraise += 1
    return nraise


def _base_dem(nr=8, nc=10, dx=10.0, x0=0.0, y0=80.0):
    """A sloped surface that straddles top_m, with a NaN cell, a nodata-sentinel
    cell (-9999, which the original loop DOES raise -- it only checks z==z and
    z<top_m, not validity), and a cell already above top_m."""
    xs = x0 + (np.arange(nc) + 0.5) * dx
    ys = y0 - (np.arange(nr) + 0.5) * dx
    X, Y = np.meshgrid(xs, ys)
    z = 90.0 + 0.2 * X - 0.1 * Y
    z[2, 3] = np.nan        # must stay NaN (never raised)
    z[4, 7] = -9999.0       # nodata sentinel: original loop raises it -> must still raise
    z[1, 1] = 150.0         # above top_m: must not be raised even when hit
    return Grid(z, x0, y0, dx)


def _assert_byte_identical(kwargs):
    """raise_crest and the oracle loop must agree exactly on a fresh copy each."""
    base = _base_dem()
    g_new = base.like(base.data.copy())
    g_ref = base.like(base.data.copy())
    n_new = lf.raise_crest(g_new, **kwargs)
    n_ref = _raise_crest_loop(g_ref, **kwargs)
    assert n_new == n_ref, ("count differs: vectorized={} loop={}".format(n_new, n_ref))
    assert g_new.data.shape == g_ref.data.shape
    assert g_new.data.dtype == g_ref.data.dtype == np.float64
    # tobytes() is a strict byte-for-byte compare and keeps NaN bit patterns.
    assert g_new.data.tobytes() == g_ref.data.tobytes(), "raised DEM is not byte-identical"
    return n_new


@register
def l1_raise_crest_point_length_branch_is_byte_identical():
    n = _assert_byte_identical(dict(crest_xy=(50.0, 40.0), inflow_xy=(50.0, 70.0),
                                    top_m=100.0, length_m=60.0, width_m=25.0))
    assert n > 0, "non-line branch raised no cells; test geometry is not exercising a hit"


@register
def l1_raise_crest_point_length_default_orientation_is_byte_identical():
    # inflow_xy=None exercises the vx=0, vy=1 default orientation of the fallback.
    _assert_byte_identical(dict(crest_xy=(45.0, 35.0), inflow_xy=None,
                                top_m=100.0, length_m=50.0, width_m=22.0))


@register
def l1_raise_crest_dam_line_branch_is_byte_identical():
    n = _assert_byte_identical(dict(crest_xy=(50.0, 40.0), inflow_xy=(50.0, 70.0),
                                    top_m=100.0, width_m=18.0,
                                    line_xy=[(10.0, 10.0), (50.0, 55.0), (90.0, 20.0)]))
    assert n > 0, "line branch raised no cells; test geometry is not exercising a hit"


@register
def l1_raise_crest_degenerate_line_segment_is_byte_identical():
    # A repeated point makes a zero-length segment (L2 == 0 -> t = 0.0 path).
    _assert_byte_identical(dict(crest_xy=(50.0, 40.0), inflow_xy=(50.0, 70.0),
                                top_m=100.0, width_m=30.0,
                                line_xy=[(30.0, 40.0), (30.0, 40.0), (70.0, 40.0)]))


@register
def l1_raise_crest_matches_loop_on_random_grids():
    # Both branches over several random surfaces and geometries.
    rng = np.random.default_rng(12345)
    for _ in range(12):
        nr, nc = int(rng.integers(5, 14)), int(rng.integers(5, 16))
        dx = float(rng.choice([5.0, 10.0, 20.0]))
        x0 = float(rng.integers(-200, 200))
        y0 = float(rng.integers(0, 400))
        xs = x0 + (np.arange(nc) + 0.5) * dx
        ys = y0 - (np.arange(nr) + 0.5) * dx
        X, Y = np.meshgrid(xs, ys)
        z = 95.0 + 0.05 * (X - x0) - 0.03 * (y0 - Y) + rng.normal(0, 1.5, size=X.shape)
        # scatter a few NaN / above-top cells
        z[rng.integers(nr), rng.integers(nc)] = np.nan
        z[rng.integers(nr), rng.integers(nc)] = 200.0
        base = Grid(z, x0, y0, dx)
        top_m = 100.0
        cx = x0 + rng.random() * nc * dx
        cy = y0 - rng.random() * nr * dx
        if rng.random() < 0.5:
            kwargs = dict(crest_xy=(cx, cy), inflow_xy=(cx + rng.normal() * 30, cy + rng.normal() * 30),
                          top_m=top_m, length_m=float(rng.uniform(20, 120)),
                          width_m=float(rng.uniform(8, 40)))
        else:
            pts = [(x0 + rng.random() * nc * dx, y0 - rng.random() * nr * dx) for _ in range(3)]
            kwargs = dict(crest_xy=(cx, cy), inflow_xy=(cx, cy + 10), top_m=top_m,
                          width_m=float(rng.uniform(8, 40)), line_xy=pts)
        g_new = base.like(base.data.copy())
        g_ref = base.like(base.data.copy())
        n_new = lf.raise_crest(g_new, **kwargs)
        n_ref = _raise_crest_loop(g_ref, **kwargs)
        assert n_new == n_ref, (n_new, n_ref, kwargs)
        assert g_new.data.tobytes() == g_ref.data.tobytes(), ("mismatch", kwargs)


@register
def l1_raise_crest_guard_returns_zero_and_leaves_dem():
    base = _base_dem()
    g = base.like(base.data.copy())
    before = g.data.tobytes()
    assert lf.raise_crest(g, crest_xy=(50.0, 40.0), inflow_xy=(50.0, 70.0), top_m=None) == 0
    assert g.data.tobytes() == before, "guard path must not touch the DEM"


if __name__ == "__main__":
    # Runnable standalone: the suite's run_tests.main() hard-exits when scipy is
    # missing, and scipy is not importable in this sandbox.
    import traceback
    from nd_breach_runner.tests.registry import TESTS
    mine = [fn for fn in TESTS if fn.__name__.startswith("l1_raise_crest")]
    failed = 0
    for fn in mine:
        try:
            fn()
            print("ok    ", fn.__name__)
        except Exception:
            failed += 1
            print("FAIL  ", fn.__name__)
            traceback.print_exc()
    print("\n{} tests, {} failed".format(len(mine), failed))
    raise SystemExit(1 if failed else 0)
