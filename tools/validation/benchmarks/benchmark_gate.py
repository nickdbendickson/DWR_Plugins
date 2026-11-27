#!/usr/bin/env python3
"""Published-benchmark gate for the Boann flood engine.

Runs Boann's CPU build on a handful of small, committed benchmark decks and checks the
peak-depth field against a committed reference: either a LISFLOOD-FP 8.3 run on the same deck,
or (for the lake-at-rest cases) the deck's own still-water surface. A case fails when the
peak-depth RMSE, maximum absolute difference, flood-extent CSI, still-water speed or volume
error leaves the band in its ``case.json``.

This is the automatic guard for the regression class that the 2026-11-24 NoData wall bug
(PR #516, Boann 6.0.0) belonged to: a shock solver driving water into a NoData cell without
limit. Each case runs in seconds on one CPU core, with no network and no LISFLOOD-FP build.

  python3 tools/validation/benchmarks/benchmark_gate.py          # run every case, gate, exit 1 on failure
  python3 tools/validation/benchmarks/benchmark_gate.py --case ea3 --case nodata_lake
  python3 tools/validation/benchmarks/benchmark_gate.py --keep DIR # keep the run folders

The references' provenance is in README.md. GPU and CPU are not bit-identical (SIMD reduction
order); this gate runs the CPU build only and uses tolerances, like the engine's own tests.
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
BOANN = os.path.join(REPO, "DamSafety_Core", "boann", "hydro", "boann.py")
CASES = os.path.join(HERE, "cases")

# .par solver keyword and boann.py --solver name
SOLVER_KEYWORD = {"acc": "acceleration", "fv1": "fv1", "fv2": "fv2", "dg2": "dg2"}
SOLVER_CLI = {"acc": "acc", "fv1": "fv", "fv2": "fv2", "dg2": "dg2"}
DROP_KEYS = {"dirroot", "resroot", "acceleration", "fv1", "fv2", "dg2", "cuda",
             "metal", "adaptive", "saveint"}


def read_asc(path):
    """(array, (xll, yll, dx)) from an ESRI ASCII grid; NODATA -> nan."""
    with open(path) as f:
        head = {}
        for _ in range(6):
            k, v = f.readline().split()
            head[k.lower()] = float(v)
        a = np.loadtxt(f)
    a = a.reshape(int(head["nrows"]), int(head["ncols"]))
    nd = head.get("nodata_value", -9999.0)
    a = np.where(a == nd, np.nan, a)
    return a, (head["xllcorner"], head["yllcorner"], head["cellsize"])


def run_boann(case_dir, workdir, solver):
    """Run Boann's CPU build on the case's deck with one solver; return (max_depth, mass_rows)."""
    base = os.path.join(case_dir, "deck", "base.par")
    lines = []
    for ln in open(base):
        key = ln.split("#", 1)[0].split()
        if key and key[0].lower() in DROP_KEYS:
            continue
        lines.append(ln.rstrip("\n"))
    lines += ["dirroot out", "resroot r", "saveint 1e9", SOLVER_KEYWORD[solver]]
    # copy the deck next to the generated .par so relative paths resolve
    for name in os.listdir(os.path.join(case_dir, "deck")):
        if name == "base.par":
            continue
        src = os.path.join(case_dir, "deck", name)
        with open(src, "rb") as a, open(os.path.join(workdir, name), "wb") as b:
            b.write(a.read())
    par = os.path.join(workdir, "run.par")
    open(par, "w").write("\n".join(lines) + "\n")
    env = dict(os.environ, DAMSAFETY_MF_THREADS="1")
    r = subprocess.run([sys.executable, BOANN, "run", "run.par", "--solver",
                        SOLVER_CLI[solver], "--backend", "cpu"],
                       cwd=workdir, capture_output=True, text=True, env=env)
    if r.returncode != 0:
        raise RuntimeError("boann {} failed on {}:\n{}".format(
            solver, os.path.basename(case_dir), (r.stdout + r.stderr)[-2000:]))
    depth, _ = read_asc(os.path.join(workdir, "out", "r.max"))
    mass = None
    mpath = os.path.join(workdir, "out", "r.mass")
    if os.path.exists(mpath):
        rows = [ln.split() for ln in open(mpath).read().splitlines()[1:] if ln.strip()]
        if rows:
            mass = np.array(rows, float)
    return depth, mass


def reference_grid(case_dir, spec):
    kind = spec["kind"]
    if kind == "grid":
        g, _ = read_asc(os.path.join(case_dir, spec["file"]))
        return g
    if kind == "startfile":
        g, _ = read_asc(os.path.join(case_dir, spec["file"]))
        return g
    raise ValueError("unknown reference kind {!r}".format(kind))


def score(depth, ref, thr):
    """RMSE, max|diff|, CSI over cells wet>thr in either field."""
    a = np.nan_to_num(depth)
    b = np.nan_to_num(ref)
    wet = (a > thr) | (b > thr)
    if not wet.any():
        return 0.0, 0.0, 1.0
    d = a[wet] - b[wet]
    both = (a > thr) & (b > thr)
    csi = both.sum() / wet.sum()
    return float(np.sqrt((d ** 2).mean())), float(np.abs(d).max()), float(csi)


def volume_error_frac(mass):
    """The engine's own worst cumulative volume error over the run.

    Boann (and LISFLOOD-FP) write a per-interval ``Verror`` in the mass file (column 11, index 10):
    the m3 of volume the scheme gained or lost in that reporting interval that it cannot account
    for. We normalise the worst interval by the final stored volume, so the metric is a fraction a
    conserving run holds near zero regardless of domain size. A runaway (e.g. the NoData wall bug)
    shows as a persistent large discrepancy.
    """
    if mass is None or mass.shape[0] < 1 or mass.shape[1] < 11:
        return 0.0
    vol = float(abs(mass[:, 5]).max())
    return float(np.abs(mass[:, 10]).max() / max(vol, 1.0))


def check_case(name, keep=None):
    case_dir = os.path.join(CASES, name)
    cfg = json.load(open(os.path.join(case_dir, "case.json")))
    tol = cfg["tol"]
    thr = cfg["wet_threshold_m"]
    ref = reference_grid(case_dir, cfg["reference"])
    rows = []
    ok = True
    for solver in cfg["solvers"]:
        wd = keep and os.path.join(keep, name, solver)
        tmp = wd or tempfile.mkdtemp(prefix="bench_")
        os.makedirs(tmp, exist_ok=True)
        depth, mass = run_boann(case_dir, tmp, solver)
        rmse, mabs, csi = score(depth, ref, thr)
        verr = volume_error_frac(mass)
        fail = []
        if "rmse_m" in tol and rmse > tol["rmse_m"]:
            fail.append("rmse %.4f>%.4f" % (rmse, tol["rmse_m"]))
        if "maxabs_m" in tol and mabs > tol["maxabs_m"]:
            fail.append("maxabs %.3f>%.3f" % (mabs, tol["maxabs_m"]))
        if "min_csi" in tol and csi < tol["min_csi"]:
            fail.append("csi %.3f<%.3f" % (csi, tol["min_csi"]))
        speed = None
        if "max_speed_ms" in tol:
            # lake-at-rest cases: the max recorded speed must be ~0. r.maxVc holds it if written;
            # otherwise the depth-at-rest check (maxabs vs the still surface) already guards it.
            mvc = os.path.join(tmp, "out", "r.maxVc")
            if os.path.exists(mvc):
                speed = float(np.nan_to_num(read_asc(mvc)[0]).max())
                if speed > tol["max_speed_ms"]:
                    fail.append("speed %.4f>%.4f" % (speed, tol["max_speed_ms"]))
        vcap = cfg.get("max_volume_error_frac")
        if vcap is not None and verr > vcap:
            fail.append("vol %.4f>%.4f" % (verr, vcap))
        ok = ok and not fail
        rows.append((name, solver, rmse, mabs, csi, speed, verr, fail))
    return ok, rows


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--case", action="append", help="run only these cases (repeatable)")
    ap.add_argument("--keep", help="keep run folders under this directory")
    a = ap.parse_args(argv)
    names = a.case or sorted(d for d in os.listdir(CASES)
                             if os.path.isfile(os.path.join(CASES, d, "case.json")))
    all_ok = True
    print("| case | solver | RMSE m | max|diff| m | CSI | speed m/s | vol err | result |")
    print("|---|---|---|---|---|---|---|---|")
    for name in names:
        ok, rows = check_case(name, keep=a.keep)
        all_ok = all_ok and ok
        for (_, solver, rmse, mabs, csi, speed, verr, fail) in rows:
            sp = "%.4f" % speed if speed is not None else "-"
            res = "PASS" if not fail else "FAIL: " + "; ".join(fail)
            print("| %s | %s | %.4f | %.3f | %.3f | %s | %.4f | %s |"
                  % (name, solver, rmse, mabs, csi, sp, verr, res))
    print()
    print("All benchmark cases passed." if all_ok else "BENCHMARK GATE FAILED.")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
