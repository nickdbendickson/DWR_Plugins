"""Boann's sub-grid local-inertial solver: the float64 reference (core/metalflood/subli_ref.py).

The scheme is Nithila Devi and Kuiry (2024) with two deliberate departures (exact stage curves,
compound-conveyance friction; see subli_ref.py). These tests pin, on the reference:

  - what it reduces to: the older local-inertial sub-grid reference (subfv_ref.py, and through it
    sgc_ref.py) with the weighting off, and the SHIPPED plain ACC kernel on flat cells under the
    same settings (plain Bates, and LISFLOOD-FP's q-centred form with 2-D friction);
  - the properties a flood engine must keep with the paper's upwind weighting on: a lake at rest
    over sub-grid terrain stays at rest, a closed basin keeps its volume, an open edge drains;
  - the friction choice: a channel narrower than the coarse cell runs at the Manning normal depth
    with the compound conveyance, and (r / k)^0.4 too deep with the paper's one mean face depth;
  - the upwind weight itself (their Eq. 8): 1 on a face that carries no discharge, never below
    1 - cfl, and a smoother front than the unweighted step on a low-friction dam break.

The Metal / CPU kernel is gated against this reference in tests/test_subli_kernel.py.

Run standalone:  PYTHONPATH=. python3 DamSafety_Runner/tests/test_subli.py
"""
import numpy as np

from nd_breach_runner.tests.registry import test
from nd_breach_runner.core.metalflood import subgrid as sg
from nd_breach_runner.core.metalflood import subfv_ref as sv
from nd_breach_runner.core.metalflood import subli_ref as sl
from nd_breach_runner.core.metalflood import backends as be


def valley(ny_c, nx_c, r, dxf, seed=3, n_var=False, relief=0.15):
    """A fine DEM (ny_c*r, nx_c*r) of a rough V-shaped valley falling in +x with a 2-sub-cell
    channel cut 1.5 m into its floor, and its Manning n grid (uniform, or varying cell to cell)."""
    rng = np.random.default_rng(seed)
    ny, nx = ny_c * r, nx_c * r
    x = np.arange(nx) * dxf
    y = np.arange(ny) * dxf
    X, Y = np.meshgrid(x, y)
    dem = 50.0 - 0.004 * X + 0.02 * np.abs(Y - y.mean()) + rng.normal(0.0, relief, (ny, nx))
    lo = ny // 2
    dem[lo - 1:lo + 1, :] -= 1.5
    n = np.full((ny, nx), 0.04)
    if n_var:
        n = 0.03 + 0.05 * rng.random((ny, nx))
    return dem, n


def channel(nx, dx, r, chan_rows, n=0.035, S0=0.001, bank=4.0):
    """One coarse row of a prismatic rectangular channel chan_rows sub-cells wide between tall
    banks (the flow stays in the channel). Returns the Tables and the channel width."""
    dem = np.zeros((r, nx * r))
    lo = (r - chan_rows) // 2
    for j in range(nx):
        base = 100.0 - S0 * dx * j
        dem[:, j * r:(j + 1) * r] = base + bank
        dem[lo:lo + chan_rows, j * r:(j + 1) * r] = base
    return sg.build_from_fine(dem, np.full(dem.shape, n), dx, r), chan_rows * dx / r


def _dam_break_start(t, cols=4, over=0.5):
    eta0 = t.zbed.copy()
    eta0[:, :cols] = t.zbank[:, :cols].max() + over
    return eta0


# -- what it reduces to ----------------------------------------------------------------------------

@test
def subli_reference_without_weighting_is_the_older_sub_grid_reference():
    """Weighting off, 1-D friction, the mass-conserving inflow order, no flow-depth cap and no dry
    threshold: step for step the local-inertial reference of subfv_ref.py (which the older tests
    tie to sgc_ref.py and the plain ACC step), on a rough valley with varying roughness, a source
    and an open edge."""
    r, dxf = 4, 2.5
    dem, n = valley(6, 14, r, dxf, n_var=True)
    t = sg.build_from_fine(dem, n, dxf * r, r)
    old = sv.SubFVRef(t, free_mask=sv.FREE_E)
    new = sl.SubLIRef(t, free_mask=sl.FREE_E, weighting="none", fric2d=False,
                      inflow_lf_order=False, maxhflow=1e9, dry=0.0)
    eta0 = _dam_break_start(t)
    old.set_eta(eta0)
    new.set_eta(eta0)
    src = np.zeros((t.ny, t.nx))
    src[3, 1] = 5.0
    worst = 0.0
    for _ in range(600):
        dt = 0.4 * t.dx / np.sqrt(sg.G * old.max_depth())
        oa = old.step(dt, src=src)
        ob = new.step(dt, src=src)
        worst = max(worst, float(np.abs(old.eta - new.eta).max()), abs(oa - ob))
    assert worst < 1e-9, "sub-grid local-inertial references diverged by {:.3e}".format(worst)


def _acc_kernel(z, n, h0, dx, free_mask, src_cell, Q, dt0, nsteps, theta, fric2d, lf_order):
    """The SHIPPED plain ACC kernel (kernels.metal, CPU build) on a fixed dt: depth, max speed."""
    ny, nx = z.shape
    back = be.CpuBackend(be.FLOOD_SPEC)
    try:
        a = back.allocate(be.buffer_layout(ny, nx, 1, 1, 1))
        a["z"][:] = (z - z.min()).ravel()
        a["n2"][:] = (n ** 2).ravel()
        a["h"][:] = h0.ravel()
        a["ok"][:] = 1
        a["scl"][:] = 1.0
        a["tmax"][:] = -9999.0
        a["arr"][:] = -9999.0
        a["s_cell"][:] = src_cell[0] * nx + src_cell[1]
        a["s_series"][:] = 0
        a["ser_off"][:] = np.array([0, 1], np.uint32)
        a["ser_t"][:] = 0.0
        a["ser_q"][:] = Q
        a["st"][2] = np.float32(h0.max()).view(np.uint32)          # S_HMAX
        p = be.Params()
        p.nx, p.ny, p.r0, p.c0, p.wr, p.wc = nx, ny, 0, 0, ny, nx
        p.free_mask, p.n_src, p.n_series = free_mask, 1, 1
        p.fric2d, p.clip_end, p.inflow_lf_order = int(fric2d), 0, int(lf_order)
        p.dx, p.dt0, p.alpha, p.g = dx, dt0, 0.7, sg.G
        p.dry, p.maxhflow, p.velmin, p.arr_depth = 1e-3, 10.0, 0.0, 0.0
        p.t0, p.t_end, p.theta = 0.0, 1.0e9, theta
        back.run_batch(p, nsteps)
        cells = ny * nx
        return (a["h"].reshape(ny, nx).astype(float), a["vmax"][:cells].reshape(ny, nx).astype(float),
                a["hmax"].reshape(ny, nx).astype(float))
    finally:
        back.close()


def _flat_case():
    """A rough 2-D plane with varying roughness, a deep pool at the west end, a point source and an
    open east edge: every part of the ACC face update is exercised."""
    rng = np.random.default_rng(11)
    ny, nx, dx = 9, 24, 10.0
    X = np.arange(nx)[None, :] * dx
    z = 20.0 - 0.003 * X + rng.normal(0.0, 0.05, (ny, nx))
    n = 0.03 + 0.04 * rng.random((ny, nx))
    h0 = np.zeros((ny, nx))
    h0[:, :6] = 2.0
    h0[:, 6:] = 0.05
    return z, n, h0, dx


def _flat_vs_acc(theta, fric2d, lf_order, weighting):
    z, n, h0, dx = _flat_case()
    ny, nx = z.shape
    src_cell, Q, dt0, nsteps = (4, 2), 30.0, 0.5, 500
    h_acc, v_acc, hmax_acc = _acc_kernel(z, n, h0, dx, 4, src_cell, Q, dt0, nsteps, theta,
                                         fric2d, lf_order)
    t = sg.build_from_fine(z, n, dx, 1)                     # r = 1: every coarse cell is flat
    ref = sl.SubLIRef(t, free_mask=sl.FREE_E, weighting=weighting, theta=theta, fric2d=fric2d,
                      inflow_lf_order=lf_order)
    ref.set_eta(z + h0)
    src = np.zeros((ny, nx))
    src[src_cell] = Q
    vmax = np.zeros((ny, nx))
    for _ in range(nsteps):
        ref.step(dt0, src=src)
        vmax = np.maximum(vmax, ref.speed())
    dh = float(np.abs(ref.depth() - h_acc).max())
    dv = float(np.abs(vmax - v_acc).max())
    return dh, dv, float(h_acc.max()), float(v_acc.max())


@test
def subli_flat_cells_are_the_shipped_acc_engine():
    """On flat cells (r = 1) the sub-grid face update is the plain engine's term for term. Driven
    in lock step with the SHIPPED ACC kernel (float32 CPU build of kernels.metal) on a rough plane
    with varying roughness, a source and an open edge, depth and peak speed agree to float32
    round-off - for plain Bates (theta 1) and for LISFLOOD-FP's q-centred form with the 2-D friction
    vector (theta 0.7, the Runner's damped rerun), in both inflow orders."""
    for theta, fric2d, lf, w in ((1.0, False, True, "none"), (1.0, True, True, "none"),
                                 (0.7, True, True, "centred"), (0.7, True, False, "centred"),
                                 (1.0, True, True, "centred")):
        dh, dv, hm, vm = _flat_vs_acc(theta, fric2d, lf, w)
        assert hm > 0.3 and vm > 0.5, "the case did not flow (h {:.2f}, v {:.2f})".format(hm, vm)
        assert dh < 2e-4, "depth off the ACC kernel by {:.2e} m (theta {}, fric2d {}, lf {})".format(
            dh, theta, fric2d, lf)
        assert dv < 2e-3, "peak speed off the ACC kernel by {:.2e} m/s (theta {})".format(dv, theta)


# -- the properties a flood engine keeps, with the paper's upwind weighting -----------------------

@test
def subli_lake_at_rest_stays_at_rest_over_sub_grid_terrain():
    """A level water surface over rough sub-grid terrain, cells partly and fully wet: no discharge
    starts (the weight is 1 on a face with no discharge, so nothing is blended in)."""
    r, dxf = 4, 2.5
    dem, n = valley(6, 14, r, dxf, n_var=True)
    t = sg.build_from_fine(dem, n, dxf * r, r)
    for lvl in (float(np.median(t.zbed)) + 0.4, float(t.zbank.max()) + 0.3):
        for w, th in (("upwind", 1.0), ("centred", 0.7)):
            ref = sl.SubLIRef(t, weighting=w, theta=th)
            ref.set_eta(np.maximum(t.zbed, lvl))
            e0 = ref.eta.copy()
            for _ in range(300):
                ref.step(0.5)
            q = max(float(np.abs(ref.qx).max()), float(np.abs(ref.qy).max()))
            assert q < 1e-9, "a lake at rest moved: |Q| {:.2e} m3/s ({})".format(q, w)
            assert float(np.abs(ref.eta - e0).max()) < 1e-10


@test
def subli_closed_basin_keeps_its_volume():
    """A dam break in a closed rough valley at the engine's own time step (cfl 0.7): the volume is
    kept to round-off and nothing is clipped, for every weighting."""
    r, dxf = 4, 2.5
    dem, n = valley(6, 14, r, dxf, n_var=True)
    t = sg.build_from_fine(dem, n, dxf * r, r)
    for w, th in (("upwind", 1.0), ("centred", 0.7), ("none", 1.0)):
        ref = sl.SubLIRef(t, weighting=w, theta=th)
        ref.set_eta(_dam_break_start(t))
        v0 = float(ref.V.sum())
        ref.run(600.0, cfl=0.7)
        err = abs(float(ref.V.sum()) - v0) / v0
        assert err < 1e-12, "{}: volume error {:.2e}".format(w, err)
        assert ref.clipped == 0.0, "{}: {:.3g} m3 clipped".format(w, ref.clipped)
        assert np.isfinite(ref.eta).all()


@test
def subli_open_edge_drains_a_padded_grid():
    """An open east edge lets the water out, including when the fine grid was padded up to a
    multiple of r (the padded-edge wall of Runner 3.42.1 must not come back)."""
    r, dxf = 4, 2.5
    dem, n = valley(4, 10, r, dxf)
    rows, cols = dem.shape[0], dem.shape[1] - 2             # two fine columns short of a multiple
    pad = np.full(dem.shape, float(dem.max()) + 1000.0)
    pad[:, :cols] = dem[:, :cols]
    t = sg.build_from_fine(pad, n, dxf * r, r, fine_shape=(rows, cols))
    ref = sl.SubLIRef(t, free_mask=sl.FREE_E)
    ref.set_eta(t.zbed + 1.0)                 # a metre of water on the falling valley floor
    v0 = float(ref.V.sum())
    out = 0.0
    tt = 0.0
    while tt < 3600.0:
        dt = 0.7 * t.dx / np.sqrt(sg.G * ref.max_depth())
        out += ref.step(dt)
        tt += dt
    left = float(ref.V.sum())
    assert abs(v0 - out - left) / v0 < 1e-10, "outflow not booked"
    assert out / v0 > 0.8, "only {:.0%} drained through the open edge in an hour".format(out / v0)


# -- the friction choice ---------------------------------------------------------------------------

def _steady_depth(friction, r, chan_rows, weighting="upwind", Q=6.0, nx=40, dx=20.0, n=0.035,
                  S0=0.001):
    t, w = channel(nx, dx, r, chan_rows, n=n, S0=S0)
    ref = sl.SubLIRef(t, free_mask=sl.FREE_E, weighting=weighting, friction=friction,
                      inflow_lf_order=False)
    src = np.zeros((t.ny, t.nx))
    src[0, 0] = Q
    for _ in range(12000):
        dt = 0.35 * dx / np.sqrt(sg.G * ref.max_depth())
        ref.step(dt, src=src)
    depth = float(ref.eta[0, nx // 2] - t.zbed[0, nx // 2])
    return depth, (Q * n / (w * np.sqrt(S0))) ** 0.6


@test
def subli_narrow_channel_runs_at_manning_normal_depth():
    """Steady flow down a rectangular channel narrower than the coarse cell. With the compound
    conveyance the depth is the Manning normal depth (whatever the weighting: uniform flow has no
    discharge gradient to blend). With the paper's one mean face depth (its Eq. 9) the channel is
    treated as a sheet r / k times wider and runs (r / k)^0.4 too deep - the reason the compound
    form is the default."""
    for r, k in ((6, 2), (4, 1), (6, 1)):
        d, hn = _steady_depth("compound", r, k)
        assert abs(d - hn) / hn < 0.01, "compound: {:.3f} m against Manning {:.3f} m (r {}, {} wide)" \
            .format(d, hn, r, k)
        dm, _ = _steady_depth("mean", r, k)
        want = (r / float(k)) ** 0.4
        assert abs(dm / hn - want) < 0.02, "mean-depth form: {:.2f}x Manning, expected {:.2f}x".format(
            dm / hn, want)
    # a channel as wide as the cell: the two forms are the same thing
    d, hn = _steady_depth("compound", 4, 4)
    dm, _ = _steady_depth("mean", 4, 4)
    assert abs(d - hn) / hn < 0.01 and abs(dm - d) < 1e-9
    d0, _ = _steady_depth("compound", 4, 1, weighting="none")
    d1, _ = _steady_depth("compound", 4, 1, weighting="upwind")
    assert abs(d0 - d1) < 1e-4, "the weighting moved a uniform flow ({:.5f} / {:.5f})".format(d0, d1)


# -- the upwind weight -----------------------------------------------------------------------------

@test
def subli_upwind_weight_is_one_at_rest_and_bounded_by_the_cfl():
    """Eq. 8: theta = 1 - (dt / dx) min(|u|, c). From rest every face has theta = 1, so the first
    step is the plain Bates step exactly; and since u, c <= sqrt(g h_deepest), theta never falls
    below 1 - cfl at the engine's time step."""
    r, dxf = 4, 2.5
    dem, n = valley(6, 14, r, dxf)
    t = sg.build_from_fine(dem, n, dxf * r, r)
    a = sl.SubLIRef(t, weighting="upwind")
    b = sl.SubLIRef(t, weighting="none")
    for m in (a, b):
        m.set_eta(_dam_break_start(t))
    dt = 0.7 * t.dx / np.sqrt(sg.G * a.max_depth())
    a.step(dt)
    b.step(dt)
    assert float(np.abs(a.qx - b.qx).max()) == 0.0 and float(np.abs(a.qy - b.qy).max()) == 0.0
    # the weight of every wet x-face over a run, recomputed from the state as the scheme does
    lo = 1.0
    cfl = 0.7
    for _ in range(200):
        dt = cfl * t.dx / np.sqrt(sg.G * a.max_depth())
        A, W, K, hd = sl.face_hydraulics(np.maximum(a.eta[:, :-1], a.eta[:, 1:]), a.zfx[:, 1:-1],
                                         a.nf2x[:, 1:-1], t.wsub)
        wet = hd > sl.DRY
        u = np.abs(a.qx[:, 1:-1]) / np.maximum(A, 1e-12)
        c = np.sqrt(sg.G * A / np.maximum(W, 1e-12))
        th = 1.0 - dt / t.dx * np.minimum(u, c)
        if wet.any():
            lo = min(lo, float(th[wet].min()))
        a.step(dt)
    assert lo >= 1.0 - cfl - 1e-9, "theta fell to {:.3f}, below 1 - cfl".format(lo)
    assert lo < 0.999, "the weighting never engaged (theta stayed {:.4f})".format(lo)


def _front_wiggle(weighting, theta=1.0):
    """Total up-and-down of the water surface along a low-friction flat channel after a dam break,
    beyond the one monotone fall from the reservoir to the bed (0 for a monotone profile)."""
    ny_c, nx_c, r, dxf = 1, 120, 2, 5.0
    dem = np.full((ny_c * r, nx_c * r), 10.0)
    t = sg.build_from_fine(dem, np.full(dem.shape, 0.01), dxf * r, r)
    ref = sl.SubLIRef(t, weighting=weighting, theta=theta, fric2d=False)
    eta0 = np.full((ny_c, nx_c), 10.2)
    eta0[:, :40] = 13.0
    ref.set_eta(eta0)
    v0 = float(ref.V.sum())
    ref.run(40.0, cfl=0.7)
    assert abs(float(ref.V.sum()) - v0) / v0 < 1e-12
    e = ref.eta[0]
    return float(np.abs(np.diff(e)).sum() - (e.max() - e.min())), ref


@test
def subli_upwind_weighting_smooths_a_low_friction_front():
    """A dam break onto a thin layer of water on smooth ground (n 0.01) - the low-friction case the
    weighting exists for (de Almeida et al. 2012; Sridharan et al. 2020). The plain Bates step
    leaves oscillations behind the front; the paper's adaptive upwind weighting removes most of
    them, without a tuned theta."""
    plain, _ = _front_wiggle("none")
    upwind, ref = _front_wiggle("upwind")
    assert plain > 0.05, "the plain step did not oscillate ({:.3f} m): the case is too mild".format(
        plain)
    assert upwind < 0.5 * plain, "upwind weighting left {:.3f} m of wiggle against {:.3f} m plain" \
        .format(upwind, plain)
    assert np.isfinite(ref.eta).all()


def _main():
    fns = [subli_reference_without_weighting_is_the_older_sub_grid_reference,
           subli_flat_cells_are_the_shipped_acc_engine,
           subli_lake_at_rest_stays_at_rest_over_sub_grid_terrain,
           subli_closed_basin_keeps_its_volume, subli_open_edge_drains_a_padded_grid,
           subli_narrow_channel_runs_at_manning_normal_depth,
           subli_upwind_weight_is_one_at_rest_and_bounded_by_the_cfl,
           subli_upwind_weighting_smooths_a_low_friction_front]
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
