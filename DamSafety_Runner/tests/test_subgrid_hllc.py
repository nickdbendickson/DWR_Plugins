"""Validation of the shock-capturing sub-grid finite-volume reference (RESEARCH).

subfv_hllc_ref.py is the HLLC Godunov increment on the sub-grid stage tables - where subfv_ref.py
(local-inertial) smooths a dam-break front, this resolves it. The decisive checks:

  - a FLAT fine DEM -> the sub-grid HLLC is identical to the plain well-balanced HLLC engine of
    kernels_fv.metal (a faithful float64 port, _PlainHLLC below), step for step, to ~float64
    precision;
  - a wet/wet dam break on a flat frictionless bed -> the middle-state depth and the shock-front
    speed are the analytic Stoker solution (the shock the local-inertial scheme cannot resolve);
  - a lake at rest over rough sub-grid topography stays at rest (well-balanced);
  - a closed basin conserves volume (no spurious clip);
  - a prismatic rectangular channel reaches the analytic Manning normal depth.

Run standalone:  PYTHONPATH=. python3 DamSafety_Runner/tests/test_subgrid_hllc.py
"""
import numpy as np

from nd_breach_runner.tests.registry import test
from nd_breach_runner.core.metalflood import subgrid as sg
from nd_breach_runner.core.metalflood import subfv_hllc_ref as hv

G = sg.G
HVEL = hv.HVEL


# --- a faithful float64 port of the plain well-balanced HLLC engine (kernels_fv.metal) -----------
# Same Audusse hydrostatic reconstruction, same mf_hllc flux and Einfeldt wave speeds, same Audusse
# bed source and semi-implicit friction. Boundaries match subfv_hllc_ref: a FREE edge is a
# transmissive outflow with a Manning normal-depth floor (flat cell -> Rh = h, so it reduces to
# subfv_hllc_ref's _free_und on flat tables), and a wall is ideal reflective (zero mass flux, pure
# pressure).

def _vel(hq, hh):
    return np.where(hh > HVEL, hq / np.where(hh > HVEL, hh, 1.0), 0.0)


def _plain_free_und(etac, etain, hc, n2, dx):
    """Manning normal-depth outflow velocity at a flat-cell FREE edge (Rh = h). Mirrors
    subfv_hllc_ref._free_und at a flat bed, where its Rh = A/W = h, and mf_free_und in
    kernels_fv.metal."""
    slope = np.maximum((etain - etac) / dx, 0.0)
    und = np.sqrt(slope) * np.maximum(hc, 0.0) ** (2.0 / 3.0) / np.sqrt(np.maximum(n2, 1e-12))
    return np.where((hc > 0.0) & (slope > hv.SLOPE_EPS), und, 0.0)


def _mf_hllc(hL, unL, utL, hR, unR, utR):
    dryL = hL <= 0.0
    dryR = hR <= 0.0
    cL = np.sqrt(G * np.maximum(hL, 0.0))
    cR = np.sqrt(G * np.maximum(hR, 0.0))
    ustar = 0.5 * (unL + unR) + cL - cR
    cstar = 0.5 * (cL + cR) + 0.25 * (unL - unR)
    SL = np.minimum(unL - cL, ustar - cstar)
    SR = np.maximum(unR + cR, ustar + cstar)
    SL = np.where(dryL, unR - 2.0 * cR, SL)
    SR = np.where(dryL, unR + cR, SR)
    SL = np.where(dryR, unL - cL, SL)
    SR = np.where(dryR, unL + 2.0 * cL, SR)
    FhL = hL * unL
    FmL = hL * unL * unL + 0.5 * G * hL * hL
    FhR = hR * unR
    FmR = hR * unR * unR + 0.5 * G * hR * hR
    inv = 1.0 / np.where(np.abs(SR - SL) > 1e-12, SR - SL, 1.0)
    Fh_s = (SR * FhL - SL * FhR + SL * SR * (hR - hL)) * inv
    Fm_s = (SR * FmL - SL * FmR + SL * SR * (hR * unR - hL * unL)) * inv
    num = SL * hR * (unR - SR) - SR * hL * (unL - SL)
    den = hR * (unR - SR) - hL * (unL - SL)
    Sstar = np.where(np.abs(den) > 1e-12, num / np.where(np.abs(den) > 1e-12, den, 1.0),
                     0.5 * (SL + SR))
    left = SL >= 0.0
    right = SR <= 0.0
    Fh = np.where(left, FhL, np.where(right, FhR, Fh_s))
    Fm = np.where(left, FmL, np.where(right, FmR, Fm_s))
    utsel = np.where(left, utL, np.where(right, utR, np.where(Sstar >= 0.0, utL, utR)))
    Ft = Fh * utsel
    both = dryL & dryR
    return (np.where(both, 0.0, Fh), np.where(both, 0.0, Fm), np.where(both, 0.0, Ft),
            np.where(both, 0.0, np.maximum(np.abs(SL), np.abs(SR))))


class _PlainHLLC:
    def __init__(self, z, n, dx, free_mask=0, cfl=0.45):
        self.z = np.asarray(z, float)
        self.n2 = np.full(self.z.shape, float(n) ** 2)
        self.dx = float(dx)
        self.ny, self.nx = self.z.shape
        self.free_mask = int(free_mask)
        self.cfl = min(float(cfl), 0.45)
        self.h = np.zeros_like(self.z)
        self.hu = np.zeros_like(self.z)
        self.hv = np.zeros_like(self.z)

    def set_h(self, h):
        self.h = np.asarray(h, float).copy()
        self.hu[:] = 0.0
        self.hv[:] = 0.0

    def max_wave_speed(self):
        u = _vel(self.hu, self.h)
        v = _vel(self.hv, self.h)
        c = np.sqrt(G * np.maximum(self.h, 0.0))
        w = np.maximum(np.abs(u), np.abs(v)) + c
        return max(float(w.max()), np.sqrt(G * 1e-3))

    def step(self, dt, src=None):
        ny, nx, dx = self.ny, self.nx, self.dx
        z, h = self.z, self.h
        hu, hvy = self.hu, self.hv
        src = np.zeros((ny, nx)) if src is None else np.asarray(src, float)
        u = _vel(hu, h)
        v = _vel(hvy, h)
        fxh = np.zeros((ny, nx + 1)); fxn = np.zeros((ny, nx + 1)); fxt = np.zeros((ny, nx + 1))
        fyh = np.zeros((ny + 1, nx)); fyn = np.zeros((ny + 1, nx)); fyt = np.zeros((ny + 1, nx))
        fm = self.free_mask
        # interior x-faces
        if nx > 1:
            zf = np.maximum(z[:, :-1], z[:, 1:])
            hLs = np.maximum(0.0, z[:, :-1] + h[:, :-1] - zf)
            hRs = np.maximum(0.0, z[:, 1:] + h[:, 1:] - zf)
            Fh, Fn, Ft, _ = _mf_hllc(hLs, u[:, :-1], v[:, :-1], hRs, u[:, 1:], v[:, 1:])
            fxh[:, 1:nx] = Fh; fxn[:, 1:nx] = Fn; fxt[:, 1:nx] = Ft
        # interior y-faces (normal +y = south)
        if ny > 1:
            zf = np.maximum(z[:-1, :], z[1:, :])
            hUs = np.maximum(0.0, z[:-1, :] + h[:-1, :] - zf)
            hDs = np.maximum(0.0, z[1:, :] + h[1:, :] - zf)
            Fh, Fn, Ft, _ = _mf_hllc(hUs, v[:-1, :], u[:-1, :], hDs, v[1:, :], u[1:, :])
            fyh[1:ny, :] = Fh; fyn[1:ny, :] = Fn; fyt[1:ny, :] = Ft
        # boundaries (match subfv_hllc_ref: transmissive FREE, ideal reflective wall)
        pr = 0.5 * G * h * h
        n2 = self.n2
        if fm & hv.FREE_W:
            uw = u[:, 0]
            if nx > 1:
                und = _plain_free_und(z[:, 0] + h[:, 0], z[:, 1] + h[:, 1], h[:, 0], n2[:, 0], dx)
                uw = np.where(und > 0.0, np.minimum(uw, -und), uw)   # at least und outward (-x)
            fxh[:, 0] = h[:, 0] * uw; fxn[:, 0] = h[:, 0] * uw ** 2 + pr[:, 0]
            fxt[:, 0] = h[:, 0] * uw * v[:, 0]
        else:
            fxn[:, 0] = pr[:, 0]
        if fm & hv.FREE_E:
            ue = u[:, -1]
            if nx > 1:
                und = _plain_free_und(z[:, -1] + h[:, -1], z[:, -2] + h[:, -2], h[:, -1], n2[:, -1], dx)
                ue = np.where(und > 0.0, np.maximum(ue, und), ue)   # at least und outward (+x)
            fxh[:, nx] = h[:, -1] * ue; fxn[:, nx] = h[:, -1] * ue ** 2 + pr[:, -1]
            fxt[:, nx] = h[:, -1] * ue * v[:, -1]
        else:
            fxn[:, nx] = pr[:, -1]
        if fm & hv.FREE_N:
            vn = v[0, :]
            if ny > 1:
                und = _plain_free_und(z[0, :] + h[0, :], z[1, :] + h[1, :], h[0, :], n2[0, :], dx)
                vn = np.where(und > 0.0, np.minimum(vn, -und), vn)   # at least und outward (-y)
            fyh[0, :] = h[0, :] * vn; fyn[0, :] = h[0, :] * vn ** 2 + pr[0, :]
            fyt[0, :] = h[0, :] * vn * u[0, :]
        else:
            fyn[0, :] = pr[0, :]
        if fm & hv.FREE_S:
            vs = v[-1, :]
            if ny > 1:
                und = _plain_free_und(z[-1, :] + h[-1, :], z[-2, :] + h[-2, :], h[-1, :], n2[-1, :], dx)
                vs = np.where(und > 0.0, np.maximum(vs, und), vs)   # at least und outward (+y)
            fyh[ny, :] = h[-1, :] * vs; fyn[ny, :] = h[-1, :] * vs ** 2 + pr[-1, :]
            fyt[ny, :] = h[-1, :] * vs * u[-1, :]
        else:
            fyn[ny, :] = pr[-1, :]
        # Audusse bed source (reconstructed depths at the cell's own four faces)
        etaI = z + h
        zxR = np.maximum(z, np.pad(z[:, 1:], ((0, 0), (0, 1)), mode="edge"))
        zxL = np.maximum(np.pad(z[:, :-1], ((0, 0), (1, 0)), mode="edge"), z)
        zyD = np.maximum(z, np.pad(z[1:, :], ((0, 1), (0, 0)), mode="edge"))
        zyU = np.maximum(np.pad(z[:-1, :], ((1, 0), (0, 0)), mode="edge"), z)
        hxR = np.maximum(0.0, etaI - zxR); hxL = np.maximum(0.0, etaI - zxL)
        hyD = np.maximum(0.0, etaI - zyD); hyU = np.maximum(0.0, etaI - zyU)
        half_g = 0.5 * G
        SxR = half_g * (h * h - hxR * hxR); SxL = half_g * (h * h - hxL * hxL)
        SyD = half_g * (h * h - hyD * hyD); SyU = half_g * (h * h - hyU * hyU)
        invdx = dt / dx
        dh = -invdx * ((fxh[:, 1:] - fxh[:, :-1]) + (fyh[1:, :] - fyh[:-1, :]))
        dhu = -invdx * (((fxn[:, 1:] + SxR) - (fxn[:, :-1] + SxL)) + (fyt[1:, :] - fyt[:-1, :]))
        dhv = -invdx * ((fxt[:, 1:] - fxt[:, :-1]) + ((fyn[1:, :] + SyD) - (fyn[:-1, :] + SyU)))
        hn = h + dh + src * dt / (dx * dx)
        hun = hu + dhu
        hvn = hvy + dhv
        neg = hn < 0.0
        hn = np.where(neg, 0.0, hn); hun = np.where(neg, 0.0, hun); hvn = np.where(neg, 0.0, hvn)
        un = _vel(hun, hn); vn = _vel(hvn, hn)
        spd = np.sqrt(un * un + vn * vn)
        cf = 1.0 + G * self.n2 * dt * spd / np.maximum(hn, HVEL) ** (4.0 / 3.0)
        cf = np.where((spd > 0.0) & (hn > HVEL), cf, 1.0)
        self.h = hn
        self.hu = hun / cf
        self.hv = hvn / cf


# --- tests ---------------------------------------------------------------------------------------

def _flat_tables(ny, nx, dx, r, z0, n):
    dem = np.full((ny * r, nx * r), float(z0))
    nn = np.full((ny * r, nx * r), float(n))
    return sg.build_from_fine(dem, nn, dx, r)


@test
def subgrid_hllc_reduces_to_plain_hllc():
    """A flat sub-grid HLLC run equals the plain well-balanced HLLC engine, step for step."""
    ny, nx, dx, r, z0, n = 4, 24, 10.0, 4, 20.0, 0.03
    t = _flat_tables(ny, nx, dx, r, z0, n)
    fv = hv.SubFVHLLCRef(t, free_mask=hv.FREE_E)
    ref = _PlainHLLC(np.full((ny, nx), z0), n, dx, free_mask=hv.FREE_E)
    h0 = np.zeros((ny, nx))
    h0[:, :nx // 2] = 3.0
    h0[:, nx // 2:] = 0.4
    fv.set_eta(z0 + h0)
    ref.set_h(h0.copy())
    src = np.zeros((ny, nx))
    src[1:3, 1] = 1.5
    worst = 0.0
    for _ in range(300):
        dt = 0.4 * dx / ref.max_wave_speed()
        fv.step(dt, src=src)
        ref.step(dt, src=src)
        worst = max(worst, float(np.max(np.abs((fv.eta - z0) - ref.h))))
    assert worst < 1e-9, "sub-grid HLLC vs plain HLLC diverged by {:.3e} m".format(worst)


def _stoker_middle(h0, h1):
    """Middle-state (depth, velocity, shock speed) of a wet/wet dam break (Stoker's solution)."""
    def f(hm):
        rare = 2.0 * (np.sqrt(G * h0) - np.sqrt(G * hm))
        shock = (hm - h1) * np.sqrt(0.5 * G * (1.0 / hm + 1.0 / h1))
        return rare - shock
    lo, hi = h1, h0
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        if f(mid) > 0.0:
            lo = mid
        else:
            hi = mid
    hm = 0.5 * (lo + hi)
    um = 2.0 * (np.sqrt(G * h0) - np.sqrt(G * hm))
    s = hm * um / (hm - h1)
    return hm, um, s


@test
def subgrid_hllc_dam_break_matches_stoker():
    """A wet/wet dam break reaches the analytic Stoker middle-state depth and shock-front speed."""
    ny, nx, dx, r, z0, n = 3, 400, 2.0, 4, 0.0, 0.0
    t = _flat_tables(ny, nx, dx, r, z0, n)
    fv = hv.SubFVHLLCRef(t, free_mask=hv.FREE_E | hv.FREE_W)
    h0, h1 = 2.0, 0.5
    x0 = nx // 2
    h = np.where(np.arange(nx)[None, :] < x0, h0, h1).astype(float) * np.ones((ny, 1))
    fv.set_eta(z0 + h)
    hm, um, s = _stoker_middle(h0, h1)
    t_end = 0.35 * (x0 * dx) / max(s, np.sqrt(G * h0))   # keep shock + rarefaction off the edges
    fv.run(t_end)
    depth = fv.eta[ny // 2] - z0
    x = (np.arange(nx) + 0.5) * dx
    xc = x0 * dx
    # middle plateau: sample between the rarefaction tail (x0 + um... ) and the shock front
    xshock = xc + s * t_end
    xrare_tail = xc + (um - np.sqrt(G * hm)) * t_end
    mid = (x > xrare_tail + 8 * dx) & (x < xshock - 8 * dx)
    assert mid.sum() >= 5, "no clean middle plateau to sample"
    dm = float(np.mean(depth[mid]))
    assert abs(dm - hm) / hm < 0.02, "middle depth {:.4f} m vs Stoker {:.4f} m".format(dm, hm)
    # shock front: steepest drop in depth; within ~2 cells of the analytic position; and SHARP
    j = int(np.argmin(np.diff(depth[:nx - 1])))
    xfront = x[j]
    assert abs(xfront - xshock) < 3 * dx, "shock at {:.1f} m vs analytic {:.1f} m".format(xfront, xshock)
    drop = depth[max(j - 1, 0)] - depth[min(j + 2, nx - 1)]
    assert drop > 0.5 * (hm - h1), "shock not sharp (drop {:.3f} of {:.3f})".format(drop, hm - h1)


@test
def subgrid_hllc_lake_at_rest_stays_flat():
    """A flat water surface over rough sub-grid topography does not move (well-balanced)."""
    dx, r = 10.0, 4
    rng = np.random.RandomState(1)
    dem = 10.0 + rng.rand(5 * r, 6 * r) * 3.0
    nn = np.full((5 * r, 6 * r), 0.05)
    t = sg.build_from_fine(dem, nn, dx, r)
    fv = hv.SubFVHLLCRef(t)
    level = float(t.zbank.max()) + 1.0
    fv.set_eta(np.full((t.ny, t.nx), level))
    v0 = fv.volume().sum()
    for _ in range(60):
        dt = 0.4 * dx / fv.max_wave_speed()
        fv.step(dt)
    drift = float(np.max(np.abs(fv.eta - level)))
    assert drift < 1e-9, "lake drifted {:.3e} m".format(drift)
    assert abs(fv.volume().sum() - v0) < 1e-6
    assert fv.clipped < 1e-9, "well-balanced run should not clip"


@test
def subgrid_hllc_closed_basin_conserves_volume():
    """No FREE edge, a source then none: injected volume is retained, no spurious clip."""
    ny, nx, dx, r = 5, 7, 10.0, 4
    t = _flat_tables(ny, nx, dx, r, 0.0, 0.03)
    fv = hv.SubFVHLLCRef(t)
    src = np.zeros((ny, nx))
    src[2, 3] = 4.0
    inj = 0.0
    for _ in range(120):
        dt = 0.35 * dx / fv.max_wave_speed()
        fv.step(dt, src=src)
        inj += 4.0 * dt
    for _ in range(400):
        dt = 0.35 * dx / fv.max_wave_speed()
        fv.step(dt)
    bal = abs(fv.volume().sum() - fv.clipped - inj)
    assert bal < 1e-4, "volume bookkeeping off by {:.3e} m3 of {:.1f} injected".format(bal, inj)
    assert fv.clipped < 1e-3 * inj, "clip added {:.3e} m3 (of {:.1f})".format(fv.clipped, inj)


@test
def subgrid_hllc_channel_matches_manning_normal_depth():
    """A prismatic rectangular channel reaches the analytic Manning normal depth (steady, uniform).

    The slope is steep enough that the free outlet drains at normal depth rather than ponding the
    reach into a backwater, so the interior friction/forcing balance is what sets the depth."""
    r = 6
    nx = 60
    dx = 20.0
    n = 0.035
    S0 = 0.005          # steep enough to avoid a subcritical backwater pool at the outlet
    bank = 4.0
    chan_rows = 2
    w = chan_rows * dx / r
    dem = np.zeros((r, nx * r))
    for j in range(nx):
        base = 100.0 - S0 * dx * j
        dem[:, j * r:(j + 1) * r] = base + bank
        lo = (r - chan_rows) // 2
        dem[lo:lo + chan_rows, j * r:(j + 1) * r] = base
    nn = np.full((r, nx * r), n)
    t = sg.build_from_fine(dem, nn, dx, r)
    fv = hv.SubFVHLLCRef(t, free_mask=hv.FREE_E)
    Q = 6.0
    src = np.zeros((t.ny, t.nx))
    src[0, 0] = Q
    for _ in range(9000):
        dt = 0.3 * dx / fv.max_wave_speed()
        fv.step(dt, src=src)
    h_norm = (Q * n / (w * np.sqrt(S0))) ** 0.6
    jmid = nx // 2
    depth = float(fv.eta[0, jmid] - t.zbed[0, jmid])
    rel = abs(depth - h_norm) / h_norm
    assert rel < 0.03, "normal depth {:.3f} m vs analytic {:.3f} m ({:.1%})".format(depth, h_norm, rel)
    out = fv.step(0.2, src=src) / 0.2
    assert abs(out - Q) / Q < 0.03, "outflow {:.3f} vs inflow {:.3f}".format(out, Q)


def _main():
    fns = [subgrid_hllc_reduces_to_plain_hllc, subgrid_hllc_dam_break_matches_stoker,
           subgrid_hllc_lake_at_rest_stays_flat, subgrid_hllc_closed_basin_conserves_volume,
           subgrid_hllc_channel_matches_manning_normal_depth]
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
