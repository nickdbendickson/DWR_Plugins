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


def _sloping_open_deck(folder, r, extra_rows, extra_cols, sim=600.0):
    """A plane sloping down to the east under 1 m of start water, FREE east and south edges, on a
    fine grid of (4 r + extra_rows) x (12 r + extra_cols) cells. Returns (par_path, start volume)."""
    os.makedirs(folder, exist_ok=True)
    ny, nx = 4 * r + extra_rows, 12 * r + extra_cols
    dxf = 3.0
    x = (np.arange(nx) + 0.5) * dxf
    z = np.tile(10.0 - 0.01 * x, (ny, 1))
    h0 = np.full((ny, nx), 1.0)
    _write_asc(os.path.join(folder, "run.dem.asc"), z, 0.0, 0.0, dxf)
    _write_asc(os.path.join(folder, "run.start"), h0, 0.0, 0.0, dxf)
    with open(os.path.join(folder, "run.bci"), "w") as f:
        f.write("E 0.000 {:.3f} FREE\n".format(ny * dxf))
        f.write("S 0.000 {:.3f} FREE\n".format(nx * dxf))
    lines = ["DEMfile run.dem.asc", "resroot run", "dirroot results", "startfile run.start",
             "bcifile run.bci", "sim_time {:.1f}".format(sim), "initial_tstep 1.0",
             "massint {:.1f}".format(sim), "saveint {:.1f}".format(sim + 10.0), "fpfric 0.03",
             "subgrid {}".format(r)]
    with open(os.path.join(folder, "run.par"), "w") as f:
        f.write("\n".join(lines) + "\n")
    return os.path.join(folder, "run.par"), float(h0.sum() * dxf * dxf)


@test
def subfv_engine_open_edge_drains_when_grid_is_not_a_multiple_of_r():
    """A deck whose rows / columns are NOT a whole multiple of r is padded up to one. The padding
    must not wall the open (FREE) east / south edges: the water has to leave the model exactly as it
    does on a grid that needs no padding. (It once did not: the edge profile was read from the
    padding, so nothing left and a real valley - Lake Delhi 2010, 1982 x 2314 cells at r = 4 - filled
    like a bathtub, 17 to 25 ft over the surveyed high-water marks.)"""
    for r, extra_rows, extra_cols in ((4, 0, 0), (4, 2, 2), (4, 0, 3), (3, 2, 1)):
        d = tempfile.mkdtemp()
        try:
            par, v0 = _sloping_open_deck(d, r, extra_rows, extra_cols)
            stats = es.run_par(par, backend="cpu")
            drained = stats["outflow_m3"] / v0
            assert drained > 0.9, "r={} with {} extra rows, {} extra columns drained only {:.1%}".format(
                r, extra_rows, extra_cols, drained)
            rel = abs(stats["mass_error_m3"]) / v0
            assert rel < 1e-3, "mass error {:.3e} of {:.1f} m3".format(stats["mass_error_m3"], v0)
        finally:
            shutil.rmtree(d, True)


@test
def subfv_engine_open_edge_does_not_strand_a_pool():
    """Water that runs down a short plane into an open (FREE) edge must leave, not come to rest
    against it. With a purely transmissive edge the arriving water piled up at the boundary, the edge
    cell's outward velocity fell to zero, and a level pool sat against the open edge for good (30 % of
    this start water drained, then nothing, however long the run). The Manning normal-depth floor on
    the FREE edge keeps the outflow going while the surface still slopes outward, as the ACC engine's
    LISFLOOD-FP edge does."""
    d = tempfile.mkdtemp()
    try:
        par, v0 = _sloping_open_deck(d, 1, 2, 2)             # 6 x 14 cells at r = 1
        stats = es.run_par(par, backend="cpu")
        drained = stats["outflow_m3"] / v0
        assert drained > 0.9, "a pool was left against the open edge: only {:.1%} drained".format(drained)
        assert abs(stats["mass_error_m3"]) / v0 < 5e-3, stats["mass_error_m3"]
    finally:
        shutil.rmtree(d, True)


def _main():
    import traceback
    fns = [subfv_engine_runs_deck_and_writes_fine_outputs,
           subfv_engine_open_edge_drains_when_grid_is_not_a_multiple_of_r,
           subfv_engine_open_edge_does_not_strand_a_pool]
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
