"""audit M16 (Runner 3.17.8, SAFETY-NUM): builtin_lisflood.Model.window(prev=...) scans only
the previous window (guarded, self-checked) instead of the whole grid. The golden baseline
(test_golden_baseline.py) must pass unchanged; this file adds the step-by-step proof that the
prev-window scan gives the full-grid window at every recompute, and the same h / qx / qy bit
for bit, on a grid large enough for the window to stay strictly interior, plus the clamp-aware
guard (flood at the grid edge), the self-check and the touched-side fallback."""
import numpy as np

from nd_breach_runner.core import builtin_lisflood as bl
from nd_breach_runner.tests.registry import test


def _model(ny, nx, src_rc, q=40.0, free=("N", "S", "E", "W")):
    dx = 10.0
    yy, xx = np.mgrid[0:ny, 0:nx]
    r0, c0 = src_rc
    z = 100.0 + 0.002 * np.hypot(yy - r0, xx - c0) * dx + 0.05 * np.sin(xx / 7.0) * np.cos(yy / 5.0)
    z[ny // 3, nx // 4: nx // 2] = bl.NODATA          # a hole in the DEM
    ts = np.array([0.0, 1800.0, 3600.0])
    sources = [(np.array([r0]), np.array([c0]), ts, np.array([q, q, 0.0]) * dx)]
    return bl.Model(z, np.full(z.shape, 0.05), np.zeros(z.shape), dx, set(free), sources,
                    0.0, 0.0)


def _drive(m, steps, use_prev, record):
    """run_par's loop shape: window every WINDOW_EVERY steps, the adaptive dt, sources, step."""
    win = win_prev = None
    t = 0.0
    for k in range(steps):
        if k % bl.WINDOW_EVERY == 0 or win is None:
            win = m.window(bl.WINDOW_MARGIN, prev=win_prev if use_prev else None)
            win_prev = win
            record.append(win)
            if win is None:
                win = (0, 1, 0, 1)
        hm = float(m.h[win[0]:win[1], win[2]:win[3]].max())
        dt = min(5.0, bl.ALPHA * m.dx / np.sqrt(bl.G * hm)) if hm > bl.DRY_M else 5.0
        src = m.source_rate(t + 0.5 * dt, win)
        m.step(dt, win, src)
        m.edge_outflow()
        t += dt


def _same_state(a, b):
    return all(np.array_equal(x.view(np.uint64), y.view(np.uint64))
               for x, y in ((a.h, b.h), (a.qx, b.qx), (a.qy, b.qy)))


def _compare(ny, nx, src, steps):
    a, b = _model(ny, nx, src), _model(ny, nx, src)
    used = {"restricted": 0}
    real_in = bl.Model._window_in

    def spy(self, margin, prev):
        got = real_in(self, margin, prev)
        if got is not None and self is a:
            used["restricted"] += 1
            full = self._window_full(margin)
            assert got == full, (got, full)          # step by step: prev-window == full grid
        return got
    bl.Model._window_in = spy
    try:
        wa, wb = [], []
        _drive(a, steps, True, wa)
        _drive(b, steps, False, wb)
    finally:
        bl.Model._window_in = real_in
    assert wa == wb, "window sequences differ"
    assert _same_state(a, b), "h / qx / qy differ"
    assert a.window_mismatch == 0
    return wa, used["restricted"]


@test
def m16_prev_window_equals_full_scan_strictly_interior():
    wins, used = _compare(400, 400, (200, 200), 1200)
    interior = [w for w in wins if w and w[0] > 0 and w[1] < 400 and w[2] > 0 and w[3] < 400]
    assert len(interior) >= len(wins) - 2, (len(interior), len(wins))   # stayed inside
    assert used >= len(wins) - 3, (used, len(wins))                      # the fast path ran
    assert wins[-1][1] - wins[-1][0] > 2 * bl.WINDOW_MARGIN + 5          # the flood spread


@test
def m16_prev_window_equals_full_scan_at_the_grid_edge():
    """Source by the south edge: the bbox sits on a clamped side - the guard must not fall back
    there (clamp-aware), and the answer stays the full-grid one."""
    wins, used = _compare(160, 220, (156, 110), 1500)
    clamped = [w for w in wins if w and w[1] == 160]
    assert clamped and used >= len(wins) - 3, (len(clamped), used, len(wins))


@test
def m16_touched_side_and_self_check_fall_back_to_the_full_grid():
    m = _model(120, 120, (60, 60))
    full0 = m.window(bl.WINDOW_MARGIN)
    # a prev window whose side the wet bbox touches: full scan
    tight = (60, 61, 60, 61)
    assert m._window_in(bl.WINDOW_MARGIN, tight) is None
    assert m.window(bl.WINDOW_MARGIN, prev=tight) == full0
    # water written outside the window (what a future full-grid term would do): the periodic
    # self-check finds it and returns the full-grid window
    prev = m.window(bl.WINDOW_MARGIN)
    m.h[5, 5] = 0.5
    restricted = m._window_in(bl.WINDOW_MARGIN, prev)
    full = m._window_full(bl.WINDOW_MARGIN)
    assert restricted != full
    m.window_calls = bl.WINDOW_SELF_CHECK - 1                # the next call self-checks
    assert m.window(bl.WINDOW_MARGIN, prev=prev) == full and m.window_mismatch == 1
    # an unusable prev (e.g. the driver's (0, 1, 0, 1) stand-in is never passed; junk is safe)
    assert m.window(bl.WINDOW_MARGIN, prev=(0, 0, 0, 0)) == full
    # nothing wet and no source: None as before
    e = bl.Model(np.zeros((30, 30)), np.full((30, 30), 0.05), np.zeros((30, 30)), 10.0,
                 set(), [], 0.0, 0.0)
    assert e.window(bl.WINDOW_MARGIN) is None
    assert e.window(bl.WINDOW_MARGIN, prev=(0, 30, 0, 30)) is None


@test
def m16_named_constants_and_driver_never_feeds_the_stand_in_back():
    import inspect
    assert bl.WINDOW_MARGIN > bl.WINDOW_EVERY
    src = inspect.getsource(bl.run_par)
    assert "m.window(WINDOW_MARGIN, prev=win_prev)" in src
    assert "win_prev = win\n" in src and "steps % WINDOW_EVERY == 0" in src
    i = src.index("win_prev = win\n")
    assert src.index("win = (0, 1, 0, 1)") > i                # prev is set before the stand-in
