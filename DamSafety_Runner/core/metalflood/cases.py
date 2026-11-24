"""Small LISFLOOD-FP input decks for the GPU engine's tests (tests/test_metalflood.py) and its
speed test (bench.py): a sloping valley with a dam-break inflow at the top, a
DEM hole, a Manning's n file, a start-depth pond, FREE edges and a gauge.

Additional diagnostic cases for the reference harness (CPU-build vs serial oracle,
CPU-build vs Metal on Mac):

- level_pool_lake: flat-bed lake with a constant inflow, checks volume conservation
  and still-water behaviour under the donor-cell limiter.
- nested_basin: two nested closed depressions that must fill to the outer spill
  (mirrors metalterrain.cases.nested_basins geometry). Useful for verifying that
  the flood engine does not create artificial barriers after BoannGrid conditioning.
- breach_inflow_cell: focused on the source cell and its immediate neighbours so the
  intentional DryCheck vs donor-cell difference can be isolated and regression-tested.
"""
import os

import numpy as np


def write_asc(path, a, xll, yll, dx, fmt="%.4f", nodata=-9999.0):
    header = ("ncols {}\nnrows {}\nxllcorner {:.6f}\nyllcorner {:.6f}\ncellsize {:.6f}\n"
              "NODATA_value {:g}".format(a.shape[1], a.shape[0], xll, yll, dx, nodata))
    np.savetxt(path, a, fmt=fmt, header=header, comments="")


def valley_deck(folder, ny=60, nx=90, dx=20.0, peak_cms=400.0, sim_s=3 * 3600.0,
                hole=True, pond=True, nfile=True, gauge=True, free="NSEW", saveint=None,
                initial_tstep=10.0, bed=300.0, extra=()):
    """Write run.par and its files into folder; returns the .par path. The valley runs from
    west (high) to east, with a meandering channel and rough banks."""
    os.makedirs(folder, exist_ok=True)
    yy, xx = np.mgrid[0:ny, 0:nx].astype(float)
    centre = ny / 2.0 + 0.18 * ny * np.sin(xx / max(nx, 1) * 2.5 * np.pi)
    across = np.abs(yy - centre) * dx
    z = bed + 0.004 * (nx - xx) * dx + 0.02 * across + 1.5 * np.tanh(across / 60.0)
    z += 0.15 * np.sin(xx * 0.9) * np.cos(yy * 0.7)
    if hole:
        z[int(ny * 0.15):int(ny * 0.15) + 3, int(nx * 0.55):int(nx * 0.62)] = -9999.0
    xll, yll = 500000.0, 5200000.0
    write_asc(os.path.join(folder, "run.dem.asc"), z, xll, yll, dx)
    lines = ["DEMfile run.dem.asc", "resroot run", "dirroot results",
             "sim_time {:.1f}".format(sim_s), "initial_tstep {:.1f}".format(initial_tstep),
             "massint 300.0", "saveint {:.1f}".format(saveint or sim_s + 10.0),
             "fpfric 0.05", "bcifile run.bci", "bdyfile run.bdy", "acceleration"] + list(extra)
    if nfile:
        n = np.where(across < 40.0, 0.035, 0.08)
        write_asc(os.path.join(folder, "run.n.asc"), n, xll, yll, dx)
        lines.append("manningfile run.n.asc")
    if pond:
        h0 = np.zeros_like(z)
        h0[int(ny * 0.7):int(ny * 0.7) + 4, int(nx * 0.3):int(nx * 0.3) + 5] = 0.8
        write_asc(os.path.join(folder, "run.start"), h0, xll, yll, dx)
        lines.append("startfile run.start")
    if gauge:
        gx = xll + (int(nx * 0.7) + 0.5) * dx
        gy = yll + ny * dx - 0.5 * dx
        with open(os.path.join(folder, "run.gauge"), "w") as f:
            f.write("1\n{:.3f} {:.3f} E {:.3f}\n".format(gx, gy, ny * dx))
        lines.append("gaugefile run.gauge")
    with open(os.path.join(folder, "run.par"), "w") as f:
        f.write("\n".join(lines) + "\n")
    r_src = int(round(centre[0, 2]))
    x_src = xll + 2.5 * dx
    y_src = yll + ny * dx - (r_src + 0.5) * dx
    with open(os.path.join(folder, "run.bci"), "w") as f:
        f.write("P {:.3f} {:.3f} QVAR breach\n".format(x_src, y_src))
        for e in free:               # LISFLOOD-FP needs the edge's extent (0 0 = no cells)
            lo, hi = (xll, xll + nx * dx) if e in "NS" else (yll, yll + ny * dx)
            f.write("{} {:.3f} {:.3f} FREE\n".format(e, lo, hi))
    t = np.array([0.0, 0.25, 0.6, 1.0, 1.6, 2.2, 10.0]) * 3600.0
    q = np.array([0.0, 0.3, 1.0, 0.55, 0.2, 0.0, 0.0]) * peak_cms
    with open(os.path.join(folder, "run.bdy"), "w") as f:
        f.write("test inflow\nbreach\n{} seconds\n".format(len(t)))
        for tv, qv in zip(t, q):
            f.write("{:.6f} {:.1f}\n".format(qv / dx, tv))
    return os.path.join(folder, "run.par")


def level_pool_lake(folder, n=40, dx=10.0, bed=100.0, inflow_cms=50.0, sim_s=3600.0,
                    free="NSEW", h0=None, extra=()):
    """Flat-bed lake. Default: constant point-source inflow and FREE edges (a draining pool).

    Pass free="" and h0= a depth for the still-water case: closed walls, a uniform start
    depth, no inflow. That is the serial-oracle check that the donor-cell limiter does not
    invent a slope on water at rest. h0 is the start depth (m); None writes no start file."""
    os.makedirs(folder, exist_ok=True)
    z = np.full((n, n), bed, dtype=float)
    xll, yll = 0.0, 0.0
    write_asc(os.path.join(folder, "run.dem.asc"), z, xll, yll, dx)
    lines = ["DEMfile run.dem.asc", "resroot run", "dirroot results",
             "sim_time {:.1f}".format(sim_s), "initial_tstep 5.0",
             "massint 300.0", "saveint {:.1f}".format(sim_s + 10.0),
             "fpfric 0.04", "acceleration"] + list(extra)
    if h0 is not None:
        write_asc(os.path.join(folder, "run.start"), np.full((n, n), float(h0)), xll, yll, dx)
        lines.append("startfile run.start")
    has_edge = bool(free)
    has_src = float(inflow_cms) > 0.0
    if has_edge or has_src:
        at = lines.index("acceleration")
        lines[at:at] = ["bcifile run.bci", "bdyfile run.bdy"]
    with open(os.path.join(folder, "run.par"), "w") as f:
        f.write("\n".join(lines) + "\n")
    if not (has_edge or has_src):
        return os.path.join(folder, "run.par")
    # source near centre; omitted when the lake is closed and still
    r_src, c_src = n // 2, n // 2
    x_src = xll + (c_src + 0.5) * dx
    y_src = yll + (n - r_src - 0.5) * dx
    with open(os.path.join(folder, "run.bci"), "w") as f:
        if has_src:
            f.write("P {:.3f} {:.3f} QVAR lake\n".format(x_src, y_src))
        for e in free:
            lo, hi = (xll, xll + n * dx) if e in "NS" else (yll, yll + n * dx)
            f.write("{} {:.3f} {:.3f} FREE\n".format(e, lo, hi))
    t = np.array([0.0, max(sim_s, 1.0)])
    q = np.array([inflow_cms, inflow_cms])
    with open(os.path.join(folder, "run.bdy"), "w") as f:
        f.write("constant inflow\nlake\n{} seconds\n".format(len(t)))
        for tv, qv in zip(t, q):
            f.write("{:.6f} {:.1f}\n".format(qv / dx, tv))
    return os.path.join(folder, "run.par")


def nested_basin(folder, ny=30, nx=60, dx=10.0, high=350.0, inner_floor=310.0,
                 inner_rim=325.0, outer_floor=320.0, outer_rim=330.0,
                 exit_low=300.0, inflow_cms=20.0, sim_s=7200.0, extra=()):
    """Nested closed depressions (inner basin spilling into outer). Geometry matches
    metalterrain.cases.nested_basins so the flood engine can be checked against a
    conditioned surface that has already filled to the outer spill."""
    os.makedirs(folder, exist_ok=True)
    z = np.full((ny, nx), high, dtype=float)
    band = slice(1, ny - 1)
    c_in0, c_in1 = 6, 16
    c_irim = 16
    c_out0, c_out1 = 17, 31
    c_orim = 31
    z[band, c_in0:c_in1] = inner_floor
    z[band, c_irim] = inner_rim
    z[band, c_out0:c_out1] = outer_floor
    z[band, c_orim] = outer_rim
    xs = np.arange(c_orim + 1, nx)
    z[band, c_orim + 1:nx] = outer_rim - (outer_rim - exit_low) * (xs - c_orim) / (nx - 1 - c_orim)
    xll, yll = 0.0, 0.0
    write_asc(os.path.join(folder, "run.dem.asc"), z, xll, yll, dx)
    lines = ["DEMfile run.dem.asc", "resroot run", "dirroot results",
             "sim_time {:.1f}".format(sim_s), "initial_tstep 5.0",
             "massint 300.0", "saveint {:.1f}".format(sim_s + 10.0),
             "fpfric 0.05", "bcifile run.bci", "bdyfile run.bdy", "acceleration"] + list(extra)
    with open(os.path.join(folder, "run.par"), "w") as f:
        f.write("\n".join(lines) + "\n")
    # inflow into the inner basin
    r_src = ny // 2
    c_src = (c_in0 + c_in1) // 2
    x_src = xll + (c_src + 0.5) * dx
    y_src = yll + (ny - r_src - 0.5) * dx
    with open(os.path.join(folder, "run.bci"), "w") as f:
        f.write("P {:.3f} {:.3f} QVAR nest\n".format(x_src, y_src))
        for e in "NSEW":
            lo, hi = (xll, xll + nx * dx) if e in "NS" else (yll, yll + ny * dx)
            f.write("{} {:.3f} {:.3f} FREE\n".format(e, lo, hi))
    t = np.array([0.0, sim_s])
    q = np.array([inflow_cms, inflow_cms])
    with open(os.path.join(folder, "run.bdy"), "w") as f:
        f.write("nested inflow\nnest\n{} seconds\n".format(len(t)))
        for tv, qv in zip(t, q):
            f.write("{:.6f} {:.1f}\n".format(qv / dx, tv))
    return os.path.join(folder, "run.par")


def breach_inflow_cell(folder, n=21, dx=10.0, bed=100.0, peak_cms=200.0,
                       sim_s=1800.0, extra=()):
    """Tiny domain focused on the breach/source cell and its immediate neighbours.
    Isolates the intentional difference between the donor-cell limiter and LISFLOOD-FP's
    DryCheck ordering so it can be regression-tested without a large valley."""
    os.makedirs(folder, exist_ok=True)
    z = np.full((n, n), bed, dtype=float)
    # mild slope so water can leave the domain
    z[:, :] += 0.001 * np.arange(n) * dx
    xll, yll = 0.0, 0.0
    write_asc(os.path.join(folder, "run.dem.asc"), z, xll, yll, dx)
    lines = ["DEMfile run.dem.asc", "resroot run", "dirroot results",
             "sim_time {:.1f}".format(sim_s), "initial_tstep 2.0",
             "massint 60.0", "saveint {:.1f}".format(sim_s + 10.0),
             "fpfric 0.05", "bcifile run.bci", "bdyfile run.bdy", "acceleration"] + list(extra)
    with open(os.path.join(folder, "run.par"), "w") as f:
        f.write("\n".join(lines) + "\n")
    r_src, c_src = n // 2, 2          # near the west edge
    x_src = xll + (c_src + 0.5) * dx
    y_src = yll + (n - r_src - 0.5) * dx
    with open(os.path.join(folder, "run.bci"), "w") as f:
        f.write("P {:.3f} {:.3f} QVAR breach\n".format(x_src, y_src))
        for e in "NSEW":
            lo, hi = (xll, xll + n * dx) if e in "NS" else (yll, yll + n * dx)
            f.write("{} {:.3f} {:.3f} FREE\n".format(e, lo, hi))
    t = np.array([0.0, 0.2, 0.5, 1.0, 2.0]) * 3600.0
    q = np.array([0.0, 0.4, 1.0, 0.3, 0.0]) * peak_cms
    with open(os.path.join(folder, "run.bdy"), "w") as f:
        f.write("breach cell\nbreach\n{} seconds\n".format(len(t)))
        for tv, qv in zip(t, q):
            f.write("{:.6f} {:.1f}\n".format(qv / dx, tv))
    return os.path.join(folder, "run.par")


def read_asc(path):
    with open(path) as f:
        hdr = [f.readline() for _ in range(6)]
        return np.loadtxt(f, ndmin=2)
