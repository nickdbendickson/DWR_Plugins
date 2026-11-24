"""Run the BoannGrid fill from the command line:

    python -m nd_breach_runner.core.metalterrain verify [-backend metal|cpu]
        fill a synthetic DEM both ways and check each against core/terrain.priority_flood
        (both within 5e-3 m; the priority-flood within 2 float32 steps of the relaxation) plus
        determinism
    python -m nd_breach_runner.core.metalterrain bench [-size small|medium] [-backend ...]
        time the priority-flood and the relaxation, and report sweeps for the relaxation
    python -m nd_breach_runner.core.metalterrain fill in.asc out.asc [-backend ...]
        [--fill-method auto|priority-flood|relax]
        fill an ESRI ASCII-grid DEM (NODATA_value / NaN = outlet)
    python -m nd_breach_runner.core.metalterrain condition in.tif out.tif [options]
        condition a GeoTIFF DEM for road / rail crossings, 4-way from one fill (condition.py):
          --keepout FILE      building footprints / address points / town limits (repeatable)
          --dam-folder DIR    also use the keep-out layers found in a dam folder by name
          --buffer-m M        margin around each keep-out feature (default 25)
          --freeze FILE       raster on the DEM grid, non-zero = frozen embankment / dam crest
          --min-accum N       drainage cells a crossing must carry (default 20)
          --max-cut M         deepest cut allowed, m (default 6); --no-cap for none
          --all-classes       carve every depression class (the pre-3.57.0 sweep; not advised)
          --fill-method M     auto (priority-flood) | priority-flood | relax
          --acknowledge TEXT  export even with an unresolved freeze refusal, recording why
        writes out.tif, out.tif.json (counts), out.tif.deltalog.sqlite and .deltalog.md
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
    orc = _oracle(z)
    ok = True
    ref = None
    for method in ("relax", "priority-flood"):
        f1, i1 = F.fill_dem(z, backend=backend, method=method)
        f2, _ = F.fill_dem(z, backend=backend, method=method)
        det = _hash(f1) == _hash(f2)
        d = np.abs(f1 - orc)
        dmax = float(np.nanmax(d))
        rose = bool(np.all(f1 + 1e-4 >= z))             # fill only raises the true ground
        print("method        : {}  device {} ({})".format(method, i1["device"], i1["backend"]))
        print("sweeps        : {}  converged={}  unreached={}".format(
            i1["sweeps"], i1["converged"], i1.get("unreached")))
        print("determinism   : {}  (SHA256 of two runs {})".format(
            "PASS" if det else "FAIL", "identical" if det else "DIFFER"))
        print("oracle |dh|   : max {:.2e} m vs terrain.priority_flood".format(dmax))
        print("filled >= z   : {}".format("yes" if rose else "NO"))
        this = det and rose and i1["converged"] and dmax <= 5e-3 and i1.get("unreached") == 0
        if ref is None:
            ref = f1
        else:
            valid = np.isfinite(z) & (z != F.NODATA)
            work = (ref[valid] - float(z[valid].min())).astype(np.float32)
            steps = float(np.max(np.abs(f1[valid] - ref[valid]) /
                                 np.spacing(np.abs(work)).astype(np.float64)))
            print("vs relaxation : {:.0f} float32 steps (2 allowed)".format(steps))
            this = this and steps <= 2.0
        print("verdict       : {}".format("PASS" if this else "FAIL"))
        ok = ok and this
    print("VERDICT       : {}".format("PASS" if ok else "FAIL"))
    return 0 if ok else 1


def bench(backend, size, method):
    ny, nx = {"small": (160, 240), "medium": (480, 720)}.get(size, (160, 240))
    z = cases.pitted_plane(ny=ny, nx=nx)
    methods = (method,) if method != "auto" else ("priority-flood", "relax")
    for m in methods:
        t0 = time.time()
        f, info = F.fill_dem(z, backend=backend, method=m)
        dt = time.time() - t0
        print("fill {}x{} method={} ({}, {}): {} sweeps, {:.3f} s, converged={}".format(
            ny, nx, info.get("method"), info["backend"], info["device"], info["sweeps"], dt,
            info["converged"]))
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


def condition_cli(argv):
    import argparse
    from . import burn as B
    from . import condition as C
    ap = argparse.ArgumentParser(prog="metalterrain condition")
    ap.add_argument("dem")
    ap.add_argument("out")
    ap.add_argument("--keepout", action="append", default=[])
    ap.add_argument("--dam-folder")
    ap.add_argument("--buffer-m", type=float, default=C.DEFAULT_BUFFER_M)
    ap.add_argument("--freeze")
    ap.add_argument("--min-accum", type=int, default=20)
    ap.add_argument("--max-cut", type=float, default=B.DEFAULT_MAX_CUT_M)
    ap.add_argument("--no-cap", action="store_true")
    ap.add_argument("--all-classes", action="store_true")
    ap.add_argument("--acknowledge")
    ap.add_argument("--backend", default="auto")
    ap.add_argument("--fill-method", default="auto", choices=("auto", "priority-flood", "relax"))
    a = ap.parse_args(argv)
    t0 = time.time()
    info = C.condition_file(
        a.dem, a.out, keepout_layers=a.keepout, dam_folder=a.dam_folder, buffer_m=a.buffer_m,
        freeze_path=a.freeze, min_accum_cells=a.min_accum,
        classes=None if a.all_classes else B.DEFAULT_CLASSES,
        max_cut_m=None if a.no_cap else a.max_cut, backend=a.backend,
        acknowledge=a.acknowledge, log=print, method=a.fill_method)
    print("done in {:.1f} s: {carved:,} carved, {cells_lowered:,} cells lowered, deepest cut "
          "{max_cut_m:.2f} m, {flow_dead_ends:,} flow dead ends".format(time.time() - t0, **info))
    return 0


def main(argv):
    if len(argv) > 1 and argv[1] == "condition":
        return condition_cli(argv[2:])
    args = [x for x in argv[1:] if not x.startswith("-")]
    backend = "auto"
    size = "small"
    fill_method = "auto"
    for i, x in enumerate(argv):
        if x in ("-backend", "--backend") and i + 1 < len(argv):
            backend = argv[i + 1]
        if x in ("-size", "--size") and i + 1 < len(argv):
            size = argv[i + 1]
        if x in ("-fill-method", "--fill-method") and i + 1 < len(argv):
            fill_method = argv[i + 1]
    args = [a for a in args if a not in (backend, size, fill_method)]
    cmd = args[0] if args else "verify"
    if cmd == "verify":
        return verify(backend)
    if cmd == "bench":
        return bench(backend, size, fill_method)
    if cmd == "fill" and len(args) >= 3:
        z, hdr = _read_asc(args[1])
        t0 = time.time()
        f, info = F.fill_dem(z, backend=backend, log=print, method=fill_method)
        _write_asc(args[2], f, hdr)
        print("filled {} -> {} ({}, {}, {} sweeps, {:.3f} s)".format(
            args[1], args[2], info.get("method"), info["backend"], info["sweeps"],
            time.time() - t0))
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
