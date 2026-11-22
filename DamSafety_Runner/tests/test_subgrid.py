"""Validation of the sub-grid finite-volume tables + CPU reference (RESEARCH).

Two decisive reductions, plus well-balancing and mass conservation:
  - a FLAT fine DEM -> the sub-grid solver is identical to the plain ACC path (here: sgc_ref with no
    channel), step for step, to ~float64 precision;
  - a rectangular channel -> the steady in-channel depth is the analytic Manning normal depth, and
    the below-bank storage matches the SGC closed form;
  - a lake at rest stays at rest (v = 0, no drift);
  - a closed basin conserves volume.
Run standalone:  PYTHONPATH=. python3 DamSafety_Runner/tests/test_subgrid.py
"""
import numpy as np

from nd_breach_runner.tests.registry import test
from nd_breach_runner.core.metalflood import subgrid as sg
from nd_breach_runner.core.metalflood import subfv_ref as sv
from nd_breach_runner.core.metalflood import sgc_ref as sgc


def _flat_tables(ny, nx, dx, r, z0, n):
    dem = np.full((ny * r, nx * r), float(z0))
    nn = np.full((ny * r, nx * r), float(n))
    return sg.build_from_fine(dem, nn, dx, r)


@test
def subgrid_tables_reduce_to_flat_cell():
    """V, P, A, K of a flat cell are the plain finite-volume forms."""
    dx, r, z0, n = 10.0, 4, 5.0, 0.03
    t = _flat_tables(2, 2, dx, r, z0, n)
    eta = np.full((2, 2), z0 + 1.7)
    V = sg.cell_volume(eta, t.s, t.asub)
    P = sg.cell_area(eta, t.s, t.asub)
    assert abs(V[0, 0] - dx * dx * 1.7) < 1e-9, V[0, 0]
    assert abs(P[0, 0] - dx * dx) < 1e-9, P[0, 0]
    assert abs(sg.invert_eta(V, t.s, t.csum, t.asub)[0, 0] - (z0 + 1.7)) < 1e-12
    A, W, K = sg.face_hydraulics(np.array([z0 + 1.7]), t.zfx[0, 0][None], t.nf2x[0, 0][None], t.wsub)
    assert abs(A[0] - dx * 1.7) < 1e-9 and abs(W[0] - dx) < 1e-9
    assert abs(K[0] - dx * 1.7 ** (5.0 / 3.0) / n) < 1e-6, K[0]


@test
def subgrid_reduces_to_plain_acc():
    """A flat sub-grid run equals sgc_ref with no channel (plain ACC), step for step."""
    ny, nx, dx, r, z0, n = 4, 12, 10.0, 4, 20.0, 0.04
    t = _flat_tables(ny, nx, dx, r, z0, n)
    fv = sv.SubFVRef(t, free_mask=sv.FREE_E)
    ref = sgc.SGCRef(np.full((ny, nx), z0), np.zeros((ny, nx)), np.zeros((ny, nx)), n, n, dx,
                     free_mask=sgc.FREE_E)
    # a water step (dam-break on flat ground), same state in both
    h0 = np.zeros((ny, nx))
    h0[:, :nx // 2] = 3.0
    h0[:, nx // 2:] = 0.5
    fv.set_eta(z0 + h0)
    ref.set_h(h0.copy())
    src = np.zeros((ny, nx))
    src[1:3, 1] = 2.0
    worst = 0.0
    for _ in range(200):
        dt = 0.4 * dx / np.sqrt(sg.G * fv.max_depth())
        fv.step(dt, src=src)
        ref.step(dt, src=src)
        worst = max(worst, float(np.max(np.abs((fv.eta - z0) - ref.h))))
    assert worst < 1e-9, "sub-grid vs plain ACC diverged by {:.3e} m".format(worst)


@test
def subgrid_lake_at_rest_stays_flat():
    """A flat water surface over varied sub-grid topography does not move."""
    dx, r = 10.0, 4
    rng = np.random.RandomState(0)
    dem = 10.0 + rng.rand(5 * r, 6 * r) * 3.0   # rough bed
    nn = np.full((5 * r, 6 * r), 0.05)
    t = sg.build_from_fine(dem, nn, dx, r)
    fv = sv.SubFVRef(t)
    level = float(t.zbank.max()) + 1.0          # drown everything
    fv.set_eta(np.full((t.ny, t.nx), level))
    v0 = fv.volume().sum()
    for _ in range(50):
        fv.step(0.5)
    drift = float(np.max(np.abs(fv.eta - level)))
    assert drift < 1e-9, "lake drifted {:.3e} m".format(drift)
    assert abs(fv.volume().sum() - v0) < 1e-6


@test
def subgrid_closed_basin_conserves_volume():
    """No FREE edge, a source then none: injected volume is retained."""
    ny, nx, dx, r = 5, 7, 10.0, 4
    t = _flat_tables(ny, nx, dx, r, 0.0, 0.04)
    fv = sv.SubFVRef(t)
    src = np.zeros((ny, nx))
    src[2, 3] = 5.0
    inj = 0.0
    for _ in range(100):
        dt = 0.4 * dx / np.sqrt(sg.G * fv.max_depth())
        fv.step(dt, src=src)
        inj += 5.0 * dt
    for _ in range(300):
        dt = 0.4 * dx / np.sqrt(sg.G * fv.max_depth())
        fv.step(dt)
    err = abs(fv.volume().sum() - inj)
    assert err < 1e-4, "volume error {:.3e} m3 of {:.1f} injected".format(err, inj)


@test
def subgrid_channel_matches_manning_normal_depth():
    """A prismatic rectangular channel reaches the analytic Manning normal depth (wide-channel
    conveyance R = h, which is exactly the sub-grid K form)."""
    r = 6
    nx = 40            # cells downstream (x)
    dx = 20.0
    n = 0.035
    S0 = 0.001         # bed slope
    bank = 4.0         # tall banks: keep the flow in the channel
    # one coarse row; channel occupies the middle 2 sub-rows -> width w = 2 * dx/r
    chan_rows = 2
    w = chan_rows * dx / r
    dem = np.zeros((r, nx * r))
    for j in range(nx):
        base = 100.0 - S0 * dx * j            # bed falls downstream
        dem[:, j * r:(j + 1) * r] = base + bank          # floodplain (high)
        lo = (r - chan_rows) // 2
        dem[lo:lo + chan_rows, j * r:(j + 1) * r] = base  # channel invert
    nn = np.full((r, nx * r), n)
    t = sg.build_from_fine(dem, nn, dx, r)
    fv = sv.SubFVRef(t, free_mask=sv.FREE_E)
    Q = 6.0            # m3/s inflow
    src = np.zeros((t.ny, t.nx))
    src[0, 0] = Q      # inject at the upstream cell
    # run to steady state
    for _ in range(6000):
        dt = 0.35 * dx / np.sqrt(sg.G * fv.max_depth())
        fv.step(dt, src=src)
    h_norm = (Q * n / (w * np.sqrt(S0))) ** 0.6     # wide-channel Manning normal depth
    # sample mid-reach in-channel depth (eta - channel invert)
    jmid = nx // 2
    depth = float(fv.eta[0, jmid] - t.zbed[0, jmid])
    rel = abs(depth - h_norm) / h_norm
    assert rel < 0.05, "normal depth {:.3f} m vs analytic {:.3f} m ({:.1%})".format(depth, h_norm, rel)
    # mass: steady outflow ~ inflow
    out = fv.step(0.2, src=src) / 0.2
    assert abs(out - Q) / Q < 0.05, "outflow {:.3f} vs inflow {:.3f}".format(out, Q)


@test
def subgrid_fine_depth_downscales_and_conserves():
    """The coarse eta downscales to a LiDAR-resolution depth map that (a) integrates back to the exact
    coarse cell volume, and (b) resolves wet channel / dry banks inside a single coarse cell."""
    dx, r = 20.0, 6
    chan_rows, D, bank = 2, 3.0, 2.0
    # one coarse cell: a channel D below a floodplain, banks above
    dem = np.full((r, r), 100.0 + bank)
    lo = (r - chan_rows) // 2
    dem[lo:lo + chan_rows, :] = 100.0           # channel invert
    t = sg.build_from_fine(dem, np.full((r, r), 0.03), dx, r)

    # a stage that drowns the channel but not the banks -> channel wet, banks dry
    eta = np.array([[101.0]])
    fd = sg.fine_depth(eta, dem, r)
    assert fd.shape == (r, r)
    wet = fd > 0
    assert wet[lo:lo + chan_rows, :].all(), "channel sub-cells should be wet"
    assert not wet[:lo, :].any() and not wet[lo + chan_rows:, :].any(), "banks should be dry"
    # exact volume consistency: sum(asub * fine depth) == V(eta)
    vol_fine = t.asub * fd.sum()
    vol_cell = sg.cell_volume(eta, t.s, t.asub)[0, 0]
    assert abs(vol_fine - vol_cell) < 1e-9, "fine map volume {:.6f} vs cell {:.6f}".format(
        vol_fine, vol_cell)
    # r = 1 reduces to the plain coarse depth (eta - dem)
    flat = np.full((3, 3), 10.0)
    t1 = sg.build_from_fine(flat, np.full((3, 3), 0.03), dx, 1)
    e1 = np.full((3, 3), 11.25)
    assert np.allclose(sg.fine_depth(e1, flat, 1), 1.25)


@test
def subgrid_padded_grid_keeps_the_true_edge_profiles():
    """A fine grid padded on its south / east sides up to a multiple of r (fine_shape = its true
    shape) takes its south and east domain-edge profiles from the last TRUE row / column, never from
    the padding - which would wall an open edge. The north / west profiles are unchanged."""
    r, dx = 4, 40.0
    rows, cols = 10, 13                               # true fine shape: pads to 12 x 16
    rng = np.random.RandomState(7)
    true = 100.0 + rng.rand(rows, cols)
    pad = np.full((12, 16), 1100.0)
    pad[:rows, :cols] = true
    nn = np.full(pad.shape, 0.03)
    t = sg.build_from_fine(pad, nn, dx, r, fine_shape=(rows, cols))
    east, south = t.edges["E"][0], t.edges["S"][0]
    assert np.array_equal(east.reshape(-1)[:rows], true[:, cols - 1]), "east edge is not the true column"
    assert np.all(east.reshape(-1)[rows:] == 1100.0)   # the pad rows of that column stay a wall
    assert np.array_equal(south.reshape(-1)[:cols], true[rows - 1, :]), "south edge is not the true row"
    assert np.all(south.reshape(-1)[cols:] == 1100.0)
    assert np.array_equal(t.edges["W"][0].reshape(-1), pad[:, 0])
    assert np.array_equal(t.edges["N"][0].reshape(-1), pad[0, :])
    # without fine_shape (an unpadded grid) the edges are the grid's own last row / column, as before
    t0 = sg.build_from_fine(pad, nn, dx, r)
    assert np.array_equal(t0.edges["E"][0].reshape(-1), pad[:, -1])
    assert np.array_equal(t0.edges["S"][0].reshape(-1), pad[-1, :])
    # a fine_shape that is not "the padded shape less under r" is refused
    try:
        sg.build_from_fine(pad, nn, dx, r, fine_shape=(rows - 4, cols))
    except ValueError:
        pass
    else:
        raise AssertionError("an impossible fine_shape was accepted")


def _main():
    fns = [subgrid_tables_reduce_to_flat_cell, subgrid_reduces_to_plain_acc,
           subgrid_lake_at_rest_stays_flat, subgrid_closed_basin_conserves_volume,
           subgrid_channel_matches_manning_normal_depth, subgrid_fine_depth_downscales_and_conserves,
           subgrid_padded_grid_keeps_the_true_edge_profiles]
    import traceback
    f = 0
    for fn in fns:
        try:
            fn()
            print("ok   ", fn.__name__)
        except Exception:
            f += 1
            print("FAIL ", fn.__name__)
            traceback.print_exc()
    print("\n{} tests, {} failed".format(len(fns), f))
    return 1 if f else 0


if __name__ == "__main__":
    import sys
    sys.exit(_main())
