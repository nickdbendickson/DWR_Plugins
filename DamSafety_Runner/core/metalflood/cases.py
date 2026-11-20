"""Small LISFLOOD-FP input decks for the GPU engine's tests (tests/test_metalflood.py) and its
speed test (bench.py): a sloping valley with a dam-break inflow at the top, a
DEM hole, a Manning's n file, a start-depth pond, FREE edges and a gauge."""
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
             "fpfric 0.06", "bcifile run.bci", "bdyfile run.bdy", "acceleration"] + list(extra)
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


def read_asc(path):
    with open(path) as f:
        hdr = [f.readline() for _ in range(6)]
        return np.loadtxt(f, ndmin=2)
