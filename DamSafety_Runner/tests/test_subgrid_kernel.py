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

from nd_breach_runner.tests.registry import test, Skip
from nd_breach_runner.core.metalflood import subgrid as sg
from nd_breach_runner.core.metalflood import subfv_hllc_ref as hv
from nd_breach_runner.core.metalflood import backends as be
from nd_breach_runner.core.metalflood import subfv_backend as sb


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


def _run_backend(back, tables, V0, free_mask, src_cell, Q, dt0, nsteps):
    """Drive a backend (CPU or Metal) on kernels_subfv.metal: returns (eta, V, t_elapsed)."""
    t = tables
    ny, nx, r, m = t.ny, t.nx, t.r, t.r * t.r
    lay = sb.subfv_buffer_layout(ny, nx, 1, 1, 1, r)
    a = back.allocate(lay)
    a["s"][:] = t.s.reshape(-1).astype(np.float32)
    a["nc2"][:] = (t.ncell ** 2).reshape(-1).astype(np.float32)
    a["V"][:] = V0.reshape(-1).astype(np.float32)
    a["ok"][:] = 1
    a["arr"][:] = -9999.0
    zfx, nf2x, zfy, nf2y = sg.all_faces(t)
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
    p = sb.SubFVParams(nx=nx, ny=ny, r0=0, c0=0, wr=ny, wc=nx, free_mask=free_mask, n_src=1,
                       n_series=1, r=r, m=m, clip_end=0, dx=t.dx, dt0=dt0, alpha=hv.CFL_CAP,
                       g=sg.G, dry=1e-3, arr_depth=0.05, t0=0.0, t_end=1.0e9, wsub=t.wsub, pad=0.0)
    back.run_batch(p, nsteps)
    st = a["st"]
    t_elapsed = float(st.view(np.float32)[be_S_T])
    return a["eta"].copy().reshape(ny, nx), a["V"].copy().reshape(ny, nx), t_elapsed


be_S_T = 0   # st word 0 holds the elapsed time as a float


def _dam_break_case():
    """A sub-grid-channel dam-break: tables, initial volume V0, the source cell and (Q, dt0, nsteps).
    Closed walls; a raised water column over the left third drives wet/dry fronts and shocks."""
    R, ny_c, nx_c = 4, 4, 12
    dxf = 2.0
    dxc = dxf * R
    S0, n, D, chan_rows = 0.003, 0.035, 2.0, 2
    dem = _channel_dem(ny_c, nx_c, R, dxf, S0, D, chan_rows)
    t = sg.build_from_fine(dem, np.full(dem.shape, n), dxc, R)
    eta0 = t.zbed.copy()
    eta0[:, :nx_c // 3] = t.zbank[:, :nx_c // 3].max() + 1.0
    V0 = sg.cell_volume(eta0, t.s, t.asub)
    return t, V0, (ny_c // 2, 0), 4.0, 0.02, 400


def _drain_case():
    """A sub-grid channel pre-filled with water that slopes down to a FREE east edge: it drains
    eastward off the boundary. Unlike the closed-wall dam-break this exercises the FREE-edge path
    (free_mask != 0) and its Manning normal-depth outflow floor. Returns tables, V0, free_mask and
    (dt0, nsteps)."""
    R, ny_c, nx_c = 4, 4, 10
    dxf = 2.0
    dxc = dxf * R
    S0, n, D, chan_rows = 0.004, 0.035, 2.0, 2
    dem = _channel_dem(ny_c, nx_c, R, dxf, S0, D, chan_rows)
    t = sg.build_from_fine(dem, np.full(dem.shape, n), dxc, R)
    ramp = np.linspace(1.0, 0.0, nx_c)                       # high west -> low east
    top = t.zbank[:, 0].max() + 0.5
    bot = t.zbed[:, -1].max() + 0.3
    surf = bot + (top - bot) * ramp                          # water surface sloping to the east edge
    eta0 = np.maximum(t.zbed, surf[None, :])
    V0 = sg.cell_volume(eta0, t.s, t.asub)
    return t, V0, hv.FREE_E, 0.01, 6000


@test
def subgrid_hllc_free_edge_drains_and_matches_reference():
    """The FREE-edge outflow (transmissive flux with the Manning normal-depth floor that lets a coarse
    sub-grid outlet drain): the CPU build matches the float64 reference to float32, and the pre-filled
    channel actually drains off the boundary."""
    t, V0, fm, dt0, nsteps = _drain_case()

    ref = hv.SubFVHLLCRef(t, free_mask=fm)
    ref.V = V0.copy()
    for _ in range(nsteps):
        ref.step(dt0)                                        # no source: it drains under its own slope
    eta_ref, V_ref = ref.eta, ref.V

    eta_k, V_k, t_elapsed = _run_backend(be.CpuBackend(sb.SUBFV_SPEC), t, V0, fm, (0, 0), 0.0,
                                         dt0, nsteps)

    # fixed dt (lock-step to within 0.1% of the elapsed time, so the two advance together)
    assert abs(t_elapsed - nsteps * dt0) < 1e-3 * (nsteps * dt0), \
        "kernel dt not locked: t={:.4f} vs {:.4f}".format(t_elapsed, nsteps * dt0)
    # it must have drained a real share of the pre-filled water off the FREE edge
    drained = (float(V0.sum()) - float(V_ref.sum())) / max(float(V0.sum()), 1e-9)
    assert drained > 0.3, "little drained off the FREE edge: {:.3f}".format(drained)

    wet = V_ref > hv.HVEL * t.dx * t.dx
    deta = np.abs(eta_k - eta_ref)[wet]
    max_deta = float(deta.max()) if deta.size else 0.0
    dvol = abs(float(V_k.sum()) - float(V_ref.sum())) / max(float(V0.sum()), 1e-9)
    assert max_deta < 2e-3, "kernel eta vs reference (FREE edge) max {:.3e} m over {} wet cells".format(
        max_deta, int(wet.sum()))
    assert dvol < 1e-4, "kernel vs reference total volume off {:.3e}".format(dvol)


@test
def subgrid_subfv_layout_matches_kernel():
    """subfv_buffer_layout is in the exact order of the kernel's buffer indices B(0)..B(28), and the
    CPU build's Params struct is the same size as SubFVParams (the load-time check in CpuBackend)."""
    import ctypes
    names = [n for n, _, _ in sb.subfv_buffer_layout(3, 4, 1, 1, 2, 2)]
    with open(sb.SUBFV_KERNEL_SOURCE, encoding="utf-8") as f:
        src = f.read()
    for k, n in enumerate(names):
        assert "{} B({})".format(n, k) in src, "buffer {} ({}) not at B({}) in the kernel".format(n, k, k)
    assert "constant Params& p B({})".format(len(names)) in src, "Params not after the buffers"
    lib = ctypes.CDLL(be.build_cpu_library(sb.SUBFV_SPEC))
    assert lib.mf_params_size() == ctypes.sizeof(sb.SubFVParams), "Params size mismatch"


@test
def subgrid_hllc_kernel_matches_reference():
    """The CPU build of kernels_subfv.metal reproduces subfv_hllc_ref to float32 round-off."""
    t, V0, src_cell, Q, dt0, nsteps = _dam_break_case()

    ref = hv.SubFVHLLCRef(t, free_mask=0)
    ref.V = V0.copy()
    src = np.zeros((t.ny, t.nx)); src[src_cell] = Q
    for _ in range(nsteps):
        ref.step(dt0, src=src)
    eta_ref, V_ref = ref.eta, ref.V

    eta_k, V_k, t_elapsed = _run_backend(be.CpuBackend(sb.SUBFV_SPEC), t, V0, 0, src_cell, Q, dt0, nsteps)

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


@test
def subgrid_hllc_gpu_matches_cpu():
    """The Apple-GPU (Metal) build matches the CPU build of the same kernel source, cell for cell.
    Runs only on a Mac with a Metal GPU (Nick's M2 Pro, the macOS CI job); skipped in the cloud."""
    ok, why = be.metal_available()
    if not ok:
        raise Skip("no Metal GPU here ({})".format(why))
    # closed-wall dam-break (interior HLLC) and a FREE-edge drain (the boundary outflow path)
    t, V0, src_cell, Q, dt0, nsteps = _dam_break_case()
    eta_c, V_c, _ = _run_backend(be.CpuBackend(sb.SUBFV_SPEC), t, V0, 0, src_cell, Q, dt0, nsteps)
    eta_g, V_g, _ = _run_backend(be.MetalBackend(sb.SUBFV_SPEC), t, V0, 0, src_cell, Q, dt0, nsteps)
    assert np.abs(eta_g - eta_c).max() < 1e-4, "GPU vs CPU eta max {:.3e} m".format(
        float(np.abs(eta_g - eta_c).max()))
    dvol = abs(float(V_g.sum()) - float(V_c.sum())) / max(float(V_c.sum()), 1e-9)
    assert dvol < 1e-5, "GPU vs CPU total volume off {:.3e}".format(dvol)

    td, V0d, fm, dt0d, nsd = _drain_case()
    eta_cd, V_cd, _ = _run_backend(be.CpuBackend(sb.SUBFV_SPEC), td, V0d, fm, (0, 0), 0.0, dt0d, nsd)
    eta_gd, V_gd, _ = _run_backend(be.MetalBackend(sb.SUBFV_SPEC), td, V0d, fm, (0, 0), 0.0, dt0d, nsd)
    assert np.abs(eta_gd - eta_cd).max() < 1e-4, "GPU vs CPU eta (FREE edge) max {:.3e} m".format(
        float(np.abs(eta_gd - eta_cd).max()))
    dvold = abs(float(V_gd.sum()) - float(V_cd.sum())) / max(float(V0d.sum()), 1e-9)
    assert dvold < 1e-5, "GPU vs CPU total volume (FREE edge) off {:.3e}".format(dvold)


def _main():
    import traceback
    fns = [subgrid_subfv_layout_matches_kernel, subgrid_hllc_kernel_matches_reference,
           subgrid_hllc_free_edge_drains_and_matches_reference, subgrid_hllc_gpu_matches_cpu]
    f = 0
    for fn in fns:
        try:
            fn()
            print("ok   ", fn.__name__)
        except Skip as s:
            print("skip ", fn.__name__, "({})".format(s))
        except Exception:
            f += 1
            print("FAIL ", fn.__name__)
            traceback.print_exc()
    print("\n{} tests, {} failed".format(len(fns), f))
    return 1 if f else 0


if __name__ == "__main__":
    import sys
    sys.exit(_main())
