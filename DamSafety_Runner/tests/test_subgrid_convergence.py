"""coarse-vs-fine: a coarse sub-grid run recovers the fine-resolution answer (RESEARCH).

The whole point of the sub-grid finite-volume scheme: a coarse cell that keeps its fine (LiDAR)
sub-cells as stage tables conveys and stores a channel the flat coarse cell would lose, so a coarse
run matches a fully-resolved fine run at a fraction of the cells.

The check uses ONE solver (subfv_hllc_ref) at two resolutions, so any difference is the sub-grid
representation alone, not two codes:
  - FINE truth: the fine DEM run at fine dx with r = 1 (each fine cell is its own flat cell) - this
    is the plain HLLC engine fully resolving the channel;
  - COARSE: the SAME fine DEM collapsed into coarse cells (dx = r * fine dx) as sub-grid tables.
A prismatic channel's steady in-channel depth agrees to well under 2% on a mesh with r*r fewer cells.

Run standalone:  PYTHONPATH=. python3 DamSafety_Runner/tests/test_subgrid_convergence.py
"""
import numpy as np

from nd_breach_runner.tests.registry import test
from nd_breach_runner.core.metalflood import subgrid as sg
from nd_breach_runner.core.metalflood import subfv_hllc_ref as hv


def _prismatic_fine_dem(R, nx_c, dxf, S0, D, chan_rows):
    """Fine DEM (R, nx_c*R) of a prismatic rectangular channel D below a sloping floodplain."""
    ny_f, nx_f = R, nx_c * R
    lo = (R - chan_rows) // 2
    dem = np.zeros((ny_f, nx_f))
    for jf in range(nx_f):
        base = 100.0 - S0 * dxf * jf
        dem[:, jf] = base
        dem[lo:lo + chan_rows, jf] = base - D
    return dem, lo


@test
def subgrid_hllc_coarse_recovers_fine_channel():
    """A coarse sub-grid run matches a fine fully-resolved run of the same channel (r*r fewer cells)."""
    R, nx_c = 6, 24
    dxf = 20.0 / R                 # fine cell; coarse cell is 20 m
    dxc = dxf * R
    S0, n, D = 0.004, 0.035, 3.0
    chan_rows = R // 2
    Q = 5.0
    dem, lo = _prismatic_fine_dem(R, nx_c, dxf, S0, D, chan_rows)
    nn = np.full(dem.shape, n)

    # fine truth: r = 1, each fine cell flat -> plain HLLC fully resolving the channel
    tf = sg.build_from_fine(dem, nn, dxf, 1)
    fvf = hv.SubFVHLLCRef(tf, free_mask=hv.FREE_E)
    srcf = np.zeros((tf.ny, tf.nx))
    srcf[lo:lo + chan_rows, 0] = Q / chan_rows          # inflow spread across the channel inlet
    for _ in range(11000):
        fvf.step(0.3 * dxf / fvf.max_wave_speed(), src=srcf)
    jf = (nx_c * R) // 2
    d_fine = float(np.mean(fvf.eta[lo:lo + chan_rows, jf] - dem[lo:lo + chan_rows, jf]))

    # coarse sub-grid: same DEM, r = R
    tc = sg.build_from_fine(dem, nn, dxc, R)
    fvc = hv.SubFVHLLCRef(tc, free_mask=hv.FREE_E)
    srcc = np.zeros((tc.ny, tc.nx))
    srcc[0, 0] = Q
    for _ in range(6500):
        fvc.step(0.3 * dxc / fvc.max_wave_speed(), src=srcc)
    d_coarse = float(fvc.eta[0, nx_c // 2] - tc.zbed[0, nx_c // 2])

    assert tc.ny * tc.nx * (R * R) == tf.ny * tf.nx, "cell-count ratio should be r*r"
    rel = abs(d_coarse - d_fine) / d_fine
    assert rel < 0.02, "coarse {:.4f} m vs fine {:.4f} m ({:.2%}) on {}x fewer cells".format(
        d_coarse, d_fine, rel, R * R)


def _main():
    import traceback
    try:
        subgrid_hllc_coarse_recovers_fine_channel()
        print("ok    subgrid_hllc_coarse_recovers_fine_channel")
        print("\n1 tests, 0 failed")
        return 0
    except Exception:
        traceback.print_exc()
        print("\n1 tests, 1 failed")
        return 1


if __name__ == "__main__":
    import sys
    sys.exit(_main())
