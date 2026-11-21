"""matches-SGC: the sub-grid finite-volume reference reproduces the sub-grid-channel engine (RESEARCH).

The sub-grid FV tables (subgrid.py) and solvers (subfv_ref.py local-inertial, subfv_hllc_ref.py
HLLC) generalise the sub-grid-channel (SGC) engine: SGC parameterises ONE rectangular channel per
cell by (width w, bankfull depth D); the sub-grid FV instead resolves whatever channel the fine DEM
holds. On a rectangular channel the two must agree. The oracle is sgc_ref.py (the validated float64
SGC reference, itself a port of LISFLOOD-FP 8.3 SGC2). Two checks:

  - STORAGE is the SGC closed form exactly, below AND above bank: a rectangular-channel sub-grid cell
    has the same stage->volume curve as sgc_ref.storage (below bank V = w*dx*depth; above bank
    V = w*dx*D + dx^2*(stage above bank)), to machine precision;
  - a wide prismatic channel reaches the SAME steady in-channel depth as an sgc_ref run with the same
    width, depth, roughness, slope and inflow - to ~2% for the local-inertial solver (the same scheme
    as SGC) and ~4% for the HLLC solver. The small residual is the one modelled difference: SGC's
    compound-section hydraulic radius R = A/(w + 2 min(h,D)) vs the sub-grid's per-sub-column R = h,
    which converge as the channel widens.

Run standalone:  PYTHONPATH=. python3 DamSafety_Runner/tests/test_subgrid_sgc.py
"""
import numpy as np

from nd_breach_runner.tests.registry import test
from nd_breach_runner.core.metalflood import subgrid as sg
from nd_breach_runner.core.metalflood import subfv_ref as sv
from nd_breach_runner.core.metalflood import subfv_hllc_ref as hv
from nd_breach_runner.core.metalflood import sgc_ref as sgc


def _rect_channel_cell(dx, r, chan_rows, D, zf, n):
    """One coarse cell whose fine DEM is a rectangular channel: chan_rows sub-rows D below the
    floodplain zf. Returns (tables, w)."""
    dem = np.full((r, r), float(zf))
    lo = (r - chan_rows) // 2
    dem[lo:lo + chan_rows, :] = zf - D
    t = sg.build_from_fine(dem, np.full((r, r), float(n)), dx, r)
    return t, chan_rows * dx / r


@test
def subgrid_storage_matches_sgc_closed_form():
    """A rectangular-channel sub-grid cell has the SGC stage->volume curve exactly, below and above bank."""
    dx, r, chan_rows, D, zf, n = 20.0, 8, 4, 3.0, 100.0, 0.03
    t, w = _rect_channel_cell(dx, r, chan_rows, D, zf, n)
    worst = 0.0
    for level in [zf - D + 0.05, zf - D + 1.0, zf - 2.0, zf - 0.02, zf, zf + 0.5, zf + 2.0]:
        v_sub = sg.cell_volume(np.array([[level]]), t.s, t.asub)[0, 0]
        h_rel = level - zf                      # SGC state = surface relative to the DEM (bank)
        v_sgc = sgc.storage(np.array([[h_rel]]), np.array([[w]]), np.array([[D]]), dx)[0, 0]
        # closed form, checked independently of sgc_ref
        bed = zf - D
        closed = (w * dx * max(level - bed, 0.0) if level <= zf
                  else w * dx * D + dx * dx * (level - zf))
        worst = max(worst, abs(v_sub - v_sgc), abs(v_sub - closed))
    assert worst < 1e-9, "sub-grid storage vs SGC closed form off by {:.3e} m3".format(worst)


def _subfv_channel_depth(cls, nx, dx, r, chan_rows, D, n, S0, Q, steps):
    """Steady mid-reach in-channel depth of a prismatic rectangular channel run with a sub-grid solver."""
    lo = (r - chan_rows) // 2
    dem = np.zeros((r, nx * r))
    for j in range(nx):
        base = 100.0 - S0 * dx * j
        dem[:, j * r:(j + 1) * r] = base
        dem[lo:lo + chan_rows, j * r:(j + 1) * r] = base - D
    t = sg.build_from_fine(dem, np.full((r, nx * r), n), dx, r)
    free = hv.FREE_E if cls is hv.SubFVHLLCRef else sv.FREE_E
    fv = cls(t, free_mask=free)
    src = np.zeros((t.ny, t.nx))
    src[0, 0] = Q
    for _ in range(steps):
        if cls is hv.SubFVHLLCRef:
            dt = 0.3 * dx / fv.max_wave_speed()
        else:
            dt = 0.4 * dx / np.sqrt(sg.G * fv.max_depth())
        fv.step(dt, src=src)
    jmid = nx // 2
    return float(fv.eta[0, jmid] - t.zbed[0, jmid])


def _sgc_channel_depth(nx, dx, D, n, S0, Q, w, steps):
    """Steady mid-reach in-channel depth of the same channel run with the SGC oracle."""
    z = (100.0 - S0 * dx * np.arange(nx))[None, :]
    ref = sgc.SGCRef(z, np.full((1, nx), w), np.full((1, nx), D), n, n, dx, free_mask=sgc.FREE_E)
    src = np.zeros((1, nx))
    src[0, 0] = Q
    for _ in range(steps):
        dt = min(0.7 * dx / np.sqrt(sg.G * ref.hmax_for_dt()), 5.0)
        ref.step(dt, src)
    return float(ref.h[0, nx // 2] + D)        # depth above the channel bed


@test
def subgrid_channel_matches_sgc_steady_depth():
    """A wide rectangular channel reaches the SGC oracle's steady in-channel depth (within the
    conveyance-model residual)."""
    nx, dx, r, chan_rows, D, n, S0, Q = 50, 20.0, 8, 6, 3.0, 0.035, 0.002, 5.0
    w = chan_rows * dx / r
    d_sgc = _sgc_channel_depth(nx, dx, D, n, S0, Q, w, 20000)
    d_acc = _subfv_channel_depth(sv.SubFVRef, nx, dx, r, chan_rows, D, n, S0, Q, 7000)
    d_fv = _subfv_channel_depth(hv.SubFVHLLCRef, nx, dx, r, chan_rows, D, n, S0, Q, 9000)
    rel_acc = abs(d_acc - d_sgc) / d_sgc
    rel_fv = abs(d_fv - d_sgc) / d_sgc
    assert rel_acc < 0.03, "local-inertial {:.4f} m vs SGC {:.4f} m ({:.1%})".format(d_acc, d_sgc, rel_acc)
    assert rel_fv < 0.05, "HLLC {:.4f} m vs SGC {:.4f} m ({:.1%})".format(d_fv, d_sgc, rel_fv)


def _main():
    fns = [subgrid_storage_matches_sgc_closed_form, subgrid_channel_matches_sgc_steady_depth]
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
