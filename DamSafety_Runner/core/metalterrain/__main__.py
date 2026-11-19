"""Run the BoannGrid fill from the command line:

    python -m nd_breach_runner.core.metalterrain verify [-backend metal|cpu]
        fill a synthetic DEM and check it against core/terrain.priority_flood + determinism
    python -m nd_breach_runner.core.metalterrain bench [-size small|medium] [-backend ...]
        time the fill and report sweeps to convergence
    python -m nd_breach_runner.core.metalterrain fill in.asc out.asc [-backend ...]
        fill an ESRI ASCII-grid DEM (NODATA_value / NaN = outlet)
"""
import hashlib
import sys
import time

import numpy as np

from . import fill as F
from . import cases


def _hash(a):
    return hashlib.sha256(np.ascontiguousarray(a, dtype="<f8").tobytes()).hexdigest()[:16]


def _oracle(dem):
    from ..terrain import priority_flood
    return priority_flood(np.where(np.isfinite(dem) & (dem != F.NODATA), dem, np.nan))


def verify(backend):
    z = cases.pitted_plane()
    f1, i1 = F.fill_dem(z, backend=backend)
    f2, _ = F.fill_dem(z, backend=backend)
    det = _hash(f1) == _hash(f2)
    orc = _oracle(z)
    d = np.abs(f1 - orc)
    dmax = float(np.nanmax(d))
    z32 = z.astype(np.float32).astype(np.float64)    # the ground the float32 kernel sees
    rose = bool(np.all(f1 + 1e-6 >= z32))
    print("device       : {} ({})".format(i1["device"], i1["backend"]))
    print("sweeps        : {}  converged={}".format(i1["sweeps"], i1["converged"]))
    print("determinism   : {}  (SHA256 of two runs {})".format(
        "PASS" if det else "FAIL", "identical" if det else "DIFFER"))
    print("oracle |dh|   : max {:.2e} m vs terrain.priority_flood".format(dmax))
    print("filled >= z   : {}".format("yes" if rose else "NO"))
    ok = det and rose and i1["converged"] and dmax <= 5e-3
    print("VERDICT       : {}".format("PASS" if ok else "FAIL"))
    return 0 if ok else 1


def bench(backend, size):
    ny, nx = {"small": (160, 240), "medium": (480, 720)}.get(size, (160, 240))
    z = cases.pitted_plane(ny=ny, nx=nx)
    t0 = time.time()
    f, info = F.fill_dem(z, backend=backend)
    dt = time.time() - t0
    print("fill {}x{} ({}, {}): {} sweeps, {:.3f} s, converged={}".format(
        ny, nx, info["backend"], info["device"], info["sweeps"], dt, info["converged"]))
    print("depressions removed: {:.0f} m3 of fill".format(
        float(np.nansum(np.maximum(f - z, 0.0))) * 1.0))
    return 0


def _read_asc(path):
    with open(path) as fh:
        hdr = {}
        for _ in range(6):
            k, v = fh.readline().split()
            hdr[k.lower()] = float(v)
        a = np.loadtxt(fh, ndmin=2)
    nod = hdr.get("nodata_value", -9999.0)
    return np.where(a == nod, np.nan, a), hdr


def _write_asc(path, a, hdr):
    a = np.where(np.isfinite(a), a, hdr.get("nodata_value", -9999.0))
    head = ("ncols {:d}\nnrows {:d}\nxllcorner {:.6f}\nyllcorner {:.6f}\ncellsize {:.6f}\n"
            "NODATA_value {:g}".format(int(hdr["ncols"]), int(hdr["nrows"]),
                                       hdr.get("xllcorner", 0.0), hdr.get("yllcorner", 0.0),
                                       hdr.get("cellsize", 1.0), hdr.get("nodata_value", -9999.0)))
    np.savetxt(path, a, fmt="%.4f", header=head, comments="")


def main(argv):
    args = [x for x in argv[1:] if not x.startswith("-")]
    backend = "auto"
    size = "small"
    for i, x in enumerate(argv):
        if x in ("-backend", "--backend") and i + 1 < len(argv):
            backend = argv[i + 1]
        if x in ("-size", "--size") and i + 1 < len(argv):
            size = argv[i + 1]
    args = [a for a in args if a not in (backend, size)]
    cmd = args[0] if args else "verify"
    if cmd == "verify":
        return verify(backend)
    if cmd == "bench":
        return bench(backend, size)
    if cmd == "fill" and len(args) >= 3:
        z, hdr = _read_asc(args[1])
        f, info = F.fill_dem(z, backend=backend, log=print)
        _write_asc(args[2], f, hdr)
        print("filled {} -> {} ({}, {} sweeps)".format(args[1], args[2], info["backend"],
                                                        info["sweeps"]))
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
