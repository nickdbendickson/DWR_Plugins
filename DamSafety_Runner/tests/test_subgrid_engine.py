"""The sub-grid FV engine runs an ordinary Runner deck and writes fine-resolution outputs (RESEARCH).

engine_subfv.run_par treats the deck DEM as the fine (LiDAR) grid, runs the HLLC sub-grid solver on a
coarsened grid (dx * r, deck keyword 'subgrid r'), and downscales every output raster back to the
deck's native resolution. This drives the whole path - deck read, coarsen, build tables, solve on the
CPU backend, downscale, write the .max/.mxe/.mass files - and checks it conserves mass in a closed
basin and that the fine inundation map resolves the sub-grid channel (wet channel, drier banks).

Run standalone:  PYTHONPATH=. python3 DamSafety_Runner/tests/test_subgrid_engine.py
"""
import os
import tempfile
import shutil

import numpy as np

from nd_breach_runner.tests.registry import test
from nd_breach_runner.core.metalflood import engine_subfv as es
from nd_breach_runner.core.metalflood import deckio as bl
from nd_breach_runner.core.metalflood.cases import write_asc as _write_asc


def _channel_deck(folder, r, sim=120.0):
    """A fine DEM (ny, nx) of a channel below a flat floodplain, with a start pool over the left end;
    closed walls. Returns (par_path, z_fine, ny, nx, chan_rows_mask)."""
    os.makedirs(folder, exist_ok=True)
    ny_c, nx_c = 4, 10
    ny, nx = ny_c * r, nx_c * r
    dxf = 3.0
    z = np.full((ny, nx), 10.0)
    lo = (ny - r) // 2
    z[lo:lo + r, :] = 8.0                      # a channel r sub-rows deep (one coarse row wide)
    h0 = np.zeros((ny, nx))
    h0[:, :nx // 4] = 4.0                       # start pool over the left quarter (drowns the bank)
    xll, yll = 500000.0, 5200000.0
    _write_asc(os.path.join(folder, "run.dem.asc"), z, xll, yll, dxf)
    _write_asc(os.path.join(folder, "run.start"), h0, xll, yll, dxf)
    lines = ["DEMfile run.dem.asc", "resroot run", "dirroot results", "startfile run.start",
             "sim_time {:.1f}".format(sim), "initial_tstep 2.0", "massint {:.1f}".format(sim),
             "saveint {:.1f}".format(sim + 10.0), "fpfric 0.03", "subgrid {}".format(r)]
    with open(os.path.join(folder, "run.par"), "w") as f:
        f.write("\n".join(lines) + "\n")
    chan = np.zeros((ny, nx), bool)
    chan[lo:lo + r, :] = True
    return os.path.join(folder, "run.par"), z, ny, nx, chan


@test
def subfv_engine_runs_deck_and_writes_fine_outputs():
    """A coarse sub-grid run of a channel dam-break conserves mass and writes a fine-resolution
    inundation map that resolves the channel."""
    d = tempfile.mkdtemp()
    try:
        r = 4
        par, z, ny, nx, chan = _channel_deck(d, r)
        stats = es.run_par(par, backend="cpu")
        # it ran on the coarse grid (r*r fewer cells) but kept the fine extent
        assert stats["subgrid_r"] == r
        assert stats["coarse_cells"] * (r * r) == stats["fine_cells"], stats
        # closed basin: start pool is retained (small clip/round-off only)
        rel = abs(stats["mass_error_m3"]) / max(stats["final_volume_m3"], 1.0)
        assert rel < 1e-3, "mass error {:.3e} of {:.1f} m3".format(
            stats["mass_error_m3"], stats["final_volume_m3"])
        # the outputs are written at the deck's NATIVE (fine) resolution
        root = os.path.join(d, "results", "run")
        mx, _, _, _, nod = bl._load("max", bl._read_asc, root + ".max", (ny, nx))
        assert mx.shape == (ny, nx), mx.shape
        dep = np.where(mx == nod, 0.0, mx)
        # the fine map resolves the channel: once the water has spread, channel sub-cells are wetter
        # than the floodplain beside them (mean depth over the downstream half)
        half = slice(nx // 2, nx)
        chan_d = dep[chan][:, None] if False else dep[:, half][chan[:, half]]
        bank_d = dep[:, half][~chan[:, half]]
        assert chan_d.mean() > bank_d.mean() + 0.1, \
            "channel {:.3f} m vs bank {:.3f} m".format(chan_d.mean(), bank_d.mean())
    finally:
        shutil.rmtree(d, True)


def _main():
    import traceback
    try:
        subfv_engine_runs_deck_and_writes_fine_outputs()
        print("ok    subfv_engine_runs_deck_and_writes_fine_outputs")
        print("\n1 tests, 0 failed")
        return 0
    except Exception:
        traceback.print_exc()
        print("\n1 tests, 1 failed")
        return 1


if __name__ == "__main__":
    import sys
    sys.exit(_main())
