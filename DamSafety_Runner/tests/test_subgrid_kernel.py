"""CPU==reference gate for the sub-grid HLLC kernel (RESEARCH).

kernels_subfv.metal (built as C++ via cpu_kernels_subfv.cpp) must reproduce the validated float64
oracle subfv_hllc_ref.py: it is the SAME scheme, so the only difference is float32 vs float64
round-off. This is the gate the Metal GPU build then has to pass against the CPU build byte-for-byte
(the SGC playbook: cpu==ref in the cloud here, then GPU==cpu on an Apple machine).

Both are driven step-for-step on a FIXED dt (dt0 chosen well under the CFL limit so the kernel's
wave-speed clamp never binds), over a sub-grid channel dam-break with closed walls - a transient with
wet/dry fronts and shocks, which exercises the HLLC flux, the well-balanced bed source, the V>=0 clip
and the semi-implicit friction together. Agreement to ~1e-3 m over a few hundred steps is the float32
signature of the identical scheme.

Run standalone:  PYTHONPATH=. python3 DamSafety_Runner/tests/test_subgrid_kernel.py
"""
import numpy as np

from nd_breach_runner.tests.registry import test
from nd_breach_runner.core.metalflood import subgrid as sg
from nd_breach_runner.core.metalflood import subfv_hllc_ref as hv
from nd_breach_runner.core.metalflood import backends as be


def _channel_dem(ny_c, nx_c, R, dxf, S0, D, chan_rows):
    """Fine DEM (ny_c*R, nx_c*R) of a prismatic rectangular channel D below a floodplain sloping in x."""
    ny_f, nx_f = ny_c * R, nx_c * R
    lo = (ny_f - chan_rows) // 2
    dem = np.zeros((ny_f, nx_f))
    for jf in range(nx_f):
        base = 100.0 - S0 * dxf * jf
        dem[:, jf] = base
        dem[lo:lo + chan_rows, jf] = base - D
    return dem


def _all_faces(tables):
    """Assemble the kernel's all-x-faces (ny, nx+1, r) and all-y-faces (ny+1, nx, r) sub-bed and
    Manning-n^2 profiles from the interior Tables.zfx/zfy and the FREE-edge profiles Tables.edges."""
    t = tables
    ny, nx, r = t.ny, t.nx, t.r
    zfx = np.zeros((ny, nx + 1, r))
    nf2x = np.zeros((ny, nx + 1, r))
    zfx[:, 1:nx, :] = t.zfx
    nf2x[:, 1:nx, :] = t.nf2x
    zfx[:, 0, :], nf2x[:, 0, :] = t.edges["W"]
    zfx[:, nx, :], nf2x[:, nx, :] = t.edges["E"]
    zfy = np.zeros((ny + 1, nx, r))
    nf2y = np.zeros((ny + 1, nx, r))
    zfy[1:ny, :, :] = t.zfy
    nf2y[1:ny, :, :] = t.nf2y
    zfy[0, :, :], nf2y[0, :, :] = t.edges["N"]
    zfy[ny, :, :], nf2y[ny, :, :] = t.edges["S"]
    return zfx, nf2x, zfy, nf2y


def _run_kernel(tables, V0, free_mask, src_cell, Q, dt0, nsteps):
    """Drive the CPU build of kernels_subfv.metal: returns (eta, V, t_elapsed)."""
    t = tables
    ny, nx, r, m = t.ny, t.nx, t.r, t.r * t.r
    back = be.CpuBackend(be.SUBFV_SPEC)
    lay = be.subfv_buffer_layout(ny, nx, 1, 1, 1, r)
    a = back.allocate(lay)
    a["s"][:] = t.s.reshape(-1).astype(np.float32)
    a["nc2"][:] = (t.ncell ** 2).reshape(-1).astype(np.float32)
    a["V"][:] = V0.reshape(-1).astype(np.float32)
    a["ok"][:] = 1
    a["arr"][:] = -9999.0
    zfx, nf2x, zfy, nf2y = _all_faces(t)
    a["zfx"][:] = zfx.reshape(-1).astype(np.float32)
    a["nf2x"][:] = nf2x.reshape(-1).astype(np.float32)
    a["zfy"][:] = zfy.reshape(-1).astype(np.float32)
    a["nf2y"][:] = nf2y.reshape(-1).astype(np.float32)
    # constant point source Q at src_cell via a 1-point series
    a["s_cell"][:] = src_cell[0] * nx + src_cell[1]
    a["s_series"][:] = 0
    a["ser_off"][:] = np.array([0, 1], np.uint32)
    a["ser_t"][:] = 0.0
    a["ser_q"][:] = Q
    p = be.SubFVParams(nx=nx, ny=ny, r0=0, c0=0, wr=ny, wc=nx, free_mask=free_mask, n_src=1,
                       n_series=1, r=r, m=m, clip_end=0, dx=t.dx, dt0=dt0, alpha=hv.CFL_CAP,
                       g=sg.G, dry=1e-3, arr_depth=0.05, t0=0.0, t_end=1.0e9, wsub=t.wsub, pad=0.0)
    back.run_batch(p, nsteps)
    st = a["st"]
    t_elapsed = float(st.view(np.float32)[be_S_T])
    return a["eta"].copy().reshape(ny, nx), a["V"].copy().reshape(ny, nx), t_elapsed


be_S_T = 0   # st word 0 holds the elapsed time as a float


@test
def subgrid_hllc_kernel_matches_reference():
    """The CPU build of kernels_subfv.metal reproduces subfv_hllc_ref to float32 round-off."""
    R, ny_c, nx_c = 4, 4, 12
    dxf = 2.0
    dxc = dxf * R
    S0, n, D, chan_rows = 0.003, 0.035, 2.0, 2
    dem = _channel_dem(ny_c, nx_c, R, dxf, S0, D, chan_rows)
    nn = np.full(dem.shape, n)
    t = sg.build_from_fine(dem, nn, dxc, R)

    # dam-break initial state: a raised water surface over the left third, dry elsewhere, closed walls
    eta0 = t.zbed.copy()
    eta0[:, :nx_c // 3] = t.zbank[:, :nx_c // 3].max() + 1.0
    V0 = sg.cell_volume(eta0, t.s, t.asub)

    Q = 4.0
    src_cell = (ny_c // 2, 0)
    dt0, nsteps = 0.02, 400

    ref = hv.SubFVHLLCRef(t, free_mask=0)
    ref.V = V0.copy()
    src = np.zeros((t.ny, t.nx)); src[src_cell] = Q
    for _ in range(nsteps):
        ref.step(dt0, src=src)
    eta_ref, V_ref = ref.eta, ref.V

    eta_k, V_k, t_elapsed = _run_kernel(t, V0, 0, src_cell, Q, dt0, nsteps)

    # the fixed dt must have bound every step (else the two are not in lock-step)
    assert abs(t_elapsed - nsteps * dt0) < 1e-3, \
        "kernel dt not locked: t={:.4f} vs {:.4f}".format(t_elapsed, nsteps * dt0)

    wet = V_ref > hv.HVEL * t.dx * t.dx
    deta = np.abs(eta_k - eta_ref)[wet]
    dvol = abs(float(V_k.sum()) - float(V_ref.sum())) / max(float(V_ref.sum()), 1e-9)
    max_deta = float(deta.max()) if deta.size else 0.0
    assert max_deta < 2e-3, "kernel eta vs reference max {:.3e} m over {} wet cells".format(
        max_deta, int(wet.sum()))
    assert dvol < 1e-4, "kernel vs reference total volume off {:.3e}".format(dvol)


def _main():
    import traceback
    try:
        subgrid_hllc_kernel_matches_reference()
        print("ok    subgrid_hllc_kernel_matches_reference")
        print("\n1 tests, 0 failed")
        return 0
    except Exception:
        traceback.print_exc()
        print("\n1 tests, 1 failed")
        return 1


if __name__ == "__main__":
    import sys
    sys.exit(_main())
