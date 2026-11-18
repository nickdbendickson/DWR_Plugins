"""Speed test of the GPU flood engine: one synthetic dam-break valley run on the Apple GPU,
on the CPU build of the same kernels (one core), and through LISFLOOD-FP when one is given or
found, with the floods compared. Prints a table and writes metalflood_bench.json.

    <QGIS python> -m nd_breach_runner.core.metalflood.bench [--size small|medium|large]
        [--lisflood PATH] [--threads N] [--no-cpu] [--out DIR]

small: 300 x 500 cells (10 m), 2 h.   medium: 800 x 1400 (5 m), 2 h.   large: 1600 x 2800
(2.5 m), 1 h (4.5 M cells; the CPU build is skipped there unless --cpu)."""
import argparse
import glob
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time

import numpy as np

from . import backends as be
from . import engine as eng

SIZES = {"small": dict(ny=300, nx=500, dx=10.0, peak_cms=800.0, sim_s=7200.0),
         "medium": dict(ny=800, nx=1400, dx=5.0, peak_cms=1200.0, sim_s=7200.0),
         "large": dict(ny=1600, nx=2800, dx=2.5, peak_cms=1200.0, sim_s=3600.0)}


def _deck(folder, size):
    from .cases import valley_deck
    s = SIZES[size]
    return valley_deck(folder, ny=s["ny"], nx=s["nx"], dx=s["dx"], peak_cms=s["peak_cms"],
                       sim_s=s["sim_s"], hole=False, extra=["hazard", "inittm_depth 0.003"])


def _find_lisflood():
    pats = [os.path.expanduser("~/Library/Application Support/QGIS/QGIS3/profiles/*/python/"
                               "plugins/*/bin/macos-arm64/lisflood"),
            os.path.expanduser("~/.local/share/QGIS/QGIS3/profiles/*/python/plugins/*/bin/"
                               "linux-x64/lisflood")]
    for p in pats:
        for f in sorted(glob.glob(p)):
            if os.access(f, os.X_OK):
                return f
    return None


def _read(path):
    with open(path) as f:
        for _ in range(6):
            f.readline()
        return np.loadtxt(f, ndmin=2)


def _compare(ref_dir, dir_):
    out = {}
    for ext in ("max", "maxVc", "inittm"):
        a = _read(os.path.join(ref_dir, "results", "run." + ext))
        b = _read(os.path.join(dir_, "results", "run." + ext))
        if ext == "inittm":
            m = (a >= 0) & (b >= 0)
        else:
            m = (a > 0.003) | (b > 0.003)
        d = np.abs(a - b)[m]
        out[ext] = {"mean_abs": round(float(d.mean()), 4) if d.size else 0.0,
                    "p95_abs": round(float(np.percentile(d, 95)), 4) if d.size else 0.0}
    a = _read(os.path.join(ref_dir, "results", "run.max")) > 0.1
    b = _read(os.path.join(dir_, "results", "run.max")) > 0.1
    out["csi_0.1m"] = round(float((a & b).sum() / max((a | b).sum(), 1)), 4)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--size", choices=sorted(SIZES), default="medium")
    ap.add_argument("--lisflood", default=None, help="a LISFLOOD-FP executable to compare with")
    ap.add_argument("--threads", type=int, default=0, help="LISFLOOD-FP threads (0 = all)")
    ap.add_argument("--no-cpu", action="store_true", help="skip the one-core CPU build")
    ap.add_argument("--cpu", action="store_true", help="run the CPU build even for 'large'")
    ap.add_argument("--out", default=".")
    args = ap.parse_args(argv)

    work = tempfile.mkdtemp(prefix="metalflood_bench_")
    s = SIZES[args.size]
    info = {"size": args.size, "cells": s["ny"] * s["nx"], "cell_m": s["dx"],
            "sim_h": s["sim_s"] / 3600.0, "machine": platform.machine(),
            "system": platform.platform(), "python": sys.version.split()[0],
            "engine": eng.VERSION, "runs": {}}
    ok, why = be.metal_available()
    info["metal"] = why
    print("GPU flood engine speed test: {} ({:,} cells of {:g} m, {:g} h of flood)".format(
        args.size, info["cells"], s["dx"], info["sim_h"]))
    print("Metal: " + (why if ok else "not available ({})".format(why)))

    runs = []
    if ok:
        runs.append("metal")
    if not args.no_cpu and (args.size != "large" or args.cpu) and be.find_compiler():
        runs.append("cpu")
    lf = args.lisflood or os.environ.get("NDBREACH_LISFLOOD") or _find_lisflood()
    if lf:
        runs.append("lisflood")
    try:
        for name in runs:
            d = os.path.join(work, name)
            par = _deck(d, args.size)
            print("running {} ...".format(name), flush=True)
            t0 = time.time()
            if name == "lisflood":
                env = dict(os.environ)
                if args.threads:
                    env["OMP_NUM_THREADS"] = str(args.threads)
                r = subprocess.run([lf, "run.par"], cwd=d, env=env, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.STDOUT)
                wall = time.time() - t0
                info["runs"][name] = {"wall_s": round(wall, 2), "exe": lf,
                                      "threads": args.threads or os.cpu_count(),
                                      "exit": r.returncode}
            else:
                st = eng.run_par(par, backend=name)
                wall = time.time() - t0
                info["runs"][name] = {"wall_s": round(wall, 2), "device": st["device"],
                                      "steps": st["steps"],
                                      "mass_error_pct": round(100.0 * st["mass_error_m3"]
                                                              / max(st["injected_m3"], 1.0), 6)}
            print("  {}: {:.1f} s".format(name, wall), flush=True)
        ref = "lisflood" if "lisflood" in runs else ("cpu" if "cpu" in runs else None)
        for name in runs:
            if ref and name != ref:
                info["runs"][name]["vs_" + ref] = _compare(os.path.join(work, ref),
                                                           os.path.join(work, name))
    finally:
        shutil.rmtree(work, True)

    print("\n{:<10} {:>10} {:>9}   {}".format("engine", "wall (s)", "speed-up", "notes"))
    base = info["runs"].get("cpu") or info["runs"].get("lisflood")
    for name, r in info["runs"].items():
        sp = base["wall_s"] / r["wall_s"] if base and r["wall_s"] > 0 else float("nan")
        note = r.get("device") or "LISFLOOD-FP, {} threads".format(r.get("threads"))
        print("{:<10} {:>10.1f} {:>8.1f}x   {}".format(name, r["wall_s"], sp, note))
        for k, v in r.items():
            if k.startswith("vs_"):
                print("{:<10} {} {}".format("", k, json.dumps(v)))
    out = os.path.join(os.path.abspath(args.out), "metalflood_bench.json")
    with open(out, "w") as f:
        json.dump(info, f, indent=1)
    print("\nwritten: " + out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
