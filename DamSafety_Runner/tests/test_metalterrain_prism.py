"""BoannGrid fine-DEM channel prism carve (core/metalterrain/prism.py): the regional
geometry matches a hand computation, the trapezoidal carve only ever lowers ground, the carved
bed falls monotonically downstream, the cross-section is deepest on the centreline and rises to the
bank, and a larger drainage area carves a wider, deeper channel. Pure numpy - no GPU, no compiler,
no scipy.
"""
import numpy as np

from nd_breach_runner.core.metalterrain import prism as P
from nd_breach_runner.tests.registry import test


def _sloping_plane(ny=40, nx=80, drop=0.05, base=300.0):
    """A plane tilting down to the east (col 0 highest), elevations near 300 m so the carve is
    exercised at the altitudes where float precision bit the fill - here it is pure float64."""
    cols = np.arange(nx, dtype=np.float64)
    return base - drop * cols[None, :] * np.ones((ny, 1))


def _centre_flowline(ny, nx):
    """A straight west->east flowline down the middle row, upstream (col 0) -> downstream."""
    r = ny // 2
    return [(r, c) for c in range(nx)]


@test
def prism_bieger_matches_hand_calc():
    """W = 2.70*DA^0.352 and depth = 0.30*DA^0.213 (Bieger 2015, DA in km^2), by hand at DA = 50."""
    w, d = P.bieger_geometry(50.0)
    assert abs(w - 2.70 * 50.0 ** 0.352) < 1e-9, w
    assert abs(d - 0.30 * 50.0 ** 0.213) < 1e-9, d
    # monotone in DA: more drainage -> wider and deeper
    w2, d2 = P.bieger_geometry(200.0)
    assert w2 > w and d2 > d, (w, w2, d, d2)
    # zero / negative DA is clamped, not a domain error
    assert P.bieger_geometry(-5.0) == P.bieger_geometry(0.0)


@test
def prism_da_from_accum_converts_units():
    """Flow-accumulation cell count -> km^2: N cells of (cell_m)^2 each, /1e6."""
    da = P.da_from_accum(np.array([1, 100, 1_000_000]), cell_m=10.0)
    # 1 cell of 100 m^2 = 1e-4 km^2; 1e6 cells of 100 m^2 = 100 km^2
    assert abs(da[0] - 1.0e-4) < 1e-12, da[0]
    assert abs(da[2] - 100.0) < 1e-9, da[2]


@test
def prism_carve_only_lowers_ground():
    """The carve never raises a cell (banks stay the DEM, bed is cut below it)."""
    z = _sloping_plane()
    ny, nx = z.shape
    line = _centre_flowline(ny, nx)
    da = np.linspace(1.0, 80.0, nx)                  # DA growing downstream
    out, reaches = P.carve_prism(z, line, da, cell_m=1.0)
    assert (out <= z + 1e-9).all(), "prism carve raised ground somewhere"
    assert reaches, "no reach carved"


@test
def prism_bed_falls_downstream():
    """The carved centreline bed is non-increasing from upstream to downstream, even though the
    regional depth (which grows with DA) would otherwise pull the bed back up at a wider reach."""
    z = _sloping_plane(drop=0.02)
    ny, nx = z.shape
    line = _centre_flowline(ny, nx)
    da = np.linspace(1.0, 120.0, nx)
    out, reaches = P.carve_prism(z, line, da, cell_m=1.0)
    bed = np.array([r["bed_elev_m"] for r in reaches])
    assert (np.diff(bed) <= 1e-9).all(), "carved bed rises downstream"
    # the bed is genuinely below the original ground on the centreline
    carved_centre = np.array([out[r, c] for r, c in line if _in(out, r, c)])
    orig_centre = np.array([z[r, c] for r, c in line if _in(z, r, c)])
    assert (carved_centre < orig_centre - 1e-6).any(), "centreline never carved below ground"


def _in(a, r, c):
    return 0 <= r < a.shape[0] and 0 <= c < a.shape[1]


@test
def prism_cross_section_is_trapezoidal():
    """Across the channel the carved bed is deepest on the centreline and rises monotonically to
    the bank (the trapezoidal profile bed + (dist/hw)*depth)."""
    z = _sloping_plane(ny=60, nx=60, drop=0.0)       # flat, so the cross-section is clean
    ny, nx = z.shape
    rmid = ny // 2
    line = [(rmid, c) for c in range(nx)]
    da = np.full(nx, 300.0)                           # big DA -> wide channel, several cells each side
    out, reaches = P.carve_prism(z, line, da, cell_m=1.0)
    c0 = nx // 2
    column = out[:, c0]
    # centre is the minimum of the carved column
    assert np.argmin(column) == rmid, (np.argmin(column), rmid)
    # moving out from the centre, depth below bank is non-increasing (profile rises to the bank)
    depth_below = z[:, c0] - column
    up = depth_below[rmid::-1]                        # centre outward, one way
    down = depth_below[rmid:]                         # centre outward, the other way
    assert (np.diff(up) <= 1e-9).all(), "profile deepens away from centre (up side)"
    assert (np.diff(down) <= 1e-9).all(), "profile deepens away from centre (down side)"


@test
def prism_bigger_da_carves_more():
    """A reach with a larger drainage area is both wider and deeper than a small-DA reach."""
    z = _sloping_plane(ny=80, nx=40, drop=0.0)
    ny, nx = z.shape
    # two separate straight channels, small DA on row 20, big DA on row 60
    small = P.carve_prism(z, [(20, c) for c in range(nx)], np.full(nx, 2.0), cell_m=1.0)[0]
    big = P.carve_prism(z, [(60, c) for c in range(nx)], np.full(nx, 300.0), cell_m=1.0)[0]
    # width is across the channel: count carved cells down a column cutting each channel
    cm = nx // 2
    n_small = int((small[:, cm] < z[:, cm] - 1e-6).sum())
    n_big = int((big[:, cm] < z[:, cm] - 1e-6).sum())
    assert n_big > n_small, (n_small, n_big)
    # the big channel is deeper at its centre
    assert (z[60, nx // 2] - big[60, nx // 2]) > (z[20, nx // 2] - small[20, nx // 2])


@test
def prism_skips_below_threshold_da():
    """Cells below min_da_km2 carry no channel yet (too little drainage to be a mapped channel)."""
    z = _sloping_plane(ny=30, nx=50, drop=0.0)
    ny, nx = z.shape
    line = [(15, c) for c in range(nx)]
    # DA below threshold on the upstream half, above it on the downstream half
    da = np.where(np.arange(nx) < nx // 2, 0.01, 10.0)
    out, reaches = P.carve_prism(z, line, da, cell_m=1.0, min_da_km2=0.05)
    carved_cols = {r["cell"][1] for r in reaches}
    assert all(c >= nx // 2 for c in carved_cols), carved_cols
    assert all(c < nx // 2 for c in range(nx) if c not in carved_cols and da[c] < 0.05)


def _valley(ny=41, nx=60, slope=0.5, cross=0.3, base=300.0):
    """A V-shaped valley: ground drops east (the along-channel slope) and rises away from the
    middle row (the cross-slope), so flow converges onto the thalweg (row ny//2) and runs east to
    the outlet. The along-channel drop is steeper than the cross-slope, so a channel concentrates
    on the thalweg under flow accumulation. Returns (dem, thalweg_row)."""
    rmid = ny // 2
    rows = np.arange(ny)[:, None].astype(np.float64)
    cols = np.arange(nx)[None, :].astype(np.float64)
    return base - slope * cols + cross * np.abs(rows - rmid), rmid


@test
def prism_trace_flowlines_follows_the_valley():
    """trace_flowlines on a V-valley returns the thalweg as one ordered flowline, upstream (west)
    -> downstream (east), staying on the middle row, and covers every channel cell."""
    from nd_breach_runner.core.metalterrain import flow as FL
    z, rmid = _valley()
    direction, accum, _info = FL.flow_accum(z, conn=8)
    min_accum = 5.0
    lines = P.trace_flowlines(direction, accum, min_accum, conn=8)
    assert lines, "no flowline traced"
    longest = max(lines, key=len)
    cols = [c for _r, c in longest]
    assert all(abs(r - rmid) <= 1 for r, _c in longest), longest   # on the thalweg
    assert cols == sorted(cols), "flowline is not ordered downstream (east)"
    # coverage: the union of all flowline cells is exactly the channel network (accum >= threshold)
    chan = set(zip(*(a.tolist() for a in np.where(np.isfinite(accum) & (accum >= min_accum)))))
    covered = set(cell for ln in lines for cell in ln)
    assert covered == chan, (len(covered), len(chan))


@test
def prism_carve_channels_cuts_the_valley_thalweg():
    """carve_channels runs the whole stage from a DEM: it derives the network, carves the thalweg
    below the original ground, never raises a cell, and the carved bed falls downstream."""
    z, rmid = _valley()
    carved, channels, refusals = P.carve_channels(z, cell_m=10.0, min_da_km2=0.0005, conn=8)
    assert channels, "no channel carved"
    assert not refusals, refusals
    assert (carved <= z + 1e-9).all(), "carve raised ground somewhere"
    assert (carved[rmid, :] < z[rmid, :] - 1e-6).any(), "thalweg never cut below ground"
    # the thalweg row receives the most total carving (summed across all columns)
    dz = (z - carved)
    assert abs(int(np.argmax(dz.sum(axis=1))) - rmid) <= 1, int(np.argmax(dz.sum(axis=1)))


@test
def prism_carve_channels_refuses_a_frozen_centreline():
    """A frozen (embankment / dam-crest) cell on the flowline centreline refuses the whole channel
    - the dam-safety invariant - and the frozen cell is never lowered."""
    z, rmid = _valley()
    cmid = z.shape[1] // 2
    freeze = np.zeros(z.shape, dtype=bool)
    freeze[rmid, cmid] = True
    carved, channels, refusals = P.carve_channels(z, cell_m=10.0, min_da_km2=0.0005, conn=8,
                                                  freeze_mask=freeze)
    assert refusals, "a channel through a frozen cell was not refused"
    assert carved[rmid, cmid] == z[rmid, cmid], "a frozen cell was lowered"
    assert refusals[0]["frozen_cells"], "refusal did not name the frozen cell"


@test
def prism_log_prism_records_carves_and_lifts_a_refusal():
    """log_prism writes one CHANNEL_PRISM edit per carved flowline, and a refusal reaches the
    Bottom-line freeze-refusals (so it blocks export through the existing dam-safety gate)."""
    from nd_breach_runner.core.metalterrain import delta_log as DL
    z, rmid = _valley()
    carved, channels, refusals = P.carve_channels(z, cell_m=10.0, min_da_km2=0.0005, conn=8)
    assert channels and not refusals
    with DL.DeltaLog(run_id="test-prism") as log:
        DL.log_prism(log, channels, refusals)
        assert log.counts_by_class().get("CHANNEL_PRISM", {}).get("edits") == len(channels)
        assert not log.freeze_refusals()
    freeze = np.zeros(z.shape, dtype=bool)
    freeze[rmid, z.shape[1] // 2] = True
    _c2, ch2, ref2 = P.carve_channels(z, cell_m=10.0, min_da_km2=0.0005, conn=8, freeze_mask=freeze)
    with DL.DeltaLog(run_id="test-prism-2") as log:
        n = DL.log_prism(log, ch2, ref2)
        assert n == len(ref2) >= 1, (n, len(ref2))
        assert log.freeze_refusals(), "prism freeze-refusal not lifted to the Bottom line"


if __name__ == "__main__":
    import sys
    fails = 0
    for fn in (prism_bieger_matches_hand_calc, prism_da_from_accum_converts_units,
               prism_carve_only_lowers_ground, prism_bed_falls_downstream,
               prism_cross_section_is_trapezoidal, prism_bigger_da_carves_more,
               prism_skips_below_threshold_da,
               prism_trace_flowlines_follows_the_valley,
               prism_carve_channels_cuts_the_valley_thalweg,
               prism_carve_channels_refuses_a_frozen_centreline,
               prism_log_prism_records_carves_and_lifts_a_refusal):
        try:
            fn()
            print("ok    ", fn.__name__)
        except Exception:
            import traceback
            fails += 1
            print("FAIL  ", fn.__name__)
            traceback.print_exc()
    sys.exit(1 if fails else 0)


@test
def prism_default_curve_is_north_dakota():
    """3.53.0 (Nick, 2026-11-23): carve_prism / carve_channels size the channel on the ND curve
    from 79 USGS gauges by default; Bieger 2015 (US) stays available through `coeffs`."""
    assert P.ND_USGS_2026 == {"a_w": 1.946, "b_w": 0.304, "a_d": 0.221, "b_d": 0.256}
    z = _sloping_plane(ny=80, nx=40, drop=0.0)
    line = [(40, c) for c in range(z.shape[1])]
    da = np.full(z.shape[1], 300.0)
    default, reaches = P.carve_prism(z, line, da, cell_m=1.0)
    nd = P.carve_prism(z, line, da, cell_m=1.0, coeffs=P.ND_USGS_2026)[0]
    us = P.carve_prism(z, line, da, cell_m=1.0, coeffs=P.BIEGER_2015_US)[0]
    assert np.array_equal(default, nd)
    assert not np.array_equal(default, us)
    w_nd, d_nd = P.bieger_geometry(300.0, **P.ND_USGS_2026)
    assert abs(w_nd - 1.946 * 300.0 ** 0.304) < 1e-9 and abs(d_nd - 0.221 * 300.0 ** 0.256) < 1e-9
    # ND channels are narrower than the national curve at the same area
    cm = z.shape[1] // 2
    assert (default[:, cm] < z[:, cm] - 1e-6).sum() < (us[:, cm] < z[:, cm] - 1e-6).sum()
