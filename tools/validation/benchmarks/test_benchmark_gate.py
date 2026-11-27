#!/usr/bin/env python3
"""Self-test for benchmark_gate.py's scoring and gate logic (no engine, runs in fast-checks).

Checks the metrics on hand-made grids and confirms every committed case has a well-formed
case.json with a reference that exists. Run:

    python3 tools/validation/benchmarks/test_benchmark_gate.py
"""
import json
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import benchmark_gate as bg  # noqa: E402

FAILS = []


def ok(cond, msg):
    if not cond:
        FAILS.append(msg)


def test_score():
    a = np.zeros((10, 10))
    a[2:5, 2:5] = 1.0
    # identical fields: rmse 0, csi 1
    rmse, mabs, csi = bg.score(a, a, 0.1)
    ok(rmse == 0.0 and mabs == 0.0 and csi == 1.0, "identical grids should score perfectly")
    # a uniform 0.5 m shift over the wet block
    b = a.copy()
    b[2:5, 2:5] = 1.5
    rmse, mabs, csi = bg.score(a, b, 0.1)
    ok(abs(rmse - 0.5) < 1e-9, "uniform 0.5 m shift -> rmse 0.5 (got %.4f)" % rmse)
    ok(abs(mabs - 0.5) < 1e-9, "uniform 0.5 m shift -> maxabs 0.5")
    ok(csi == 1.0, "same wet footprint -> csi 1.0")
    # disjoint footprints -> csi 0
    c = np.zeros((10, 10))
    c[7:9, 7:9] = 1.0
    _, _, csi = bg.score(a, c, 0.1)
    ok(csi == 0.0, "disjoint wet footprints -> csi 0 (got %.3f)" % csi)


def test_volume():
    # Verror column (index 10) of 0 everywhere -> 0
    m = np.zeros((5, 12))
    m[:, 5] = 1000.0  # Vol
    ok(bg.volume_error_frac(m) == 0.0, "zero Verror -> 0")
    # one interval loses 10 m3 of a 1000 m3 body -> 0.01
    m[2, 10] = -10.0
    ok(abs(bg.volume_error_frac(m) - 0.01) < 1e-9,
       "10 m3 of 1000 -> 0.01 (got %.4f)" % bg.volume_error_frac(m))
    ok(bg.volume_error_frac(None) == 0.0, "no mass file -> 0")


def test_cases_well_formed():
    cases_dir = os.path.join(HERE, "cases")
    names = [d for d in os.listdir(cases_dir)
             if os.path.isfile(os.path.join(cases_dir, d, "case.json"))]
    ok(len(names) >= 5, "at least five committed cases (got %d)" % len(names))
    for name in names:
        d = os.path.join(cases_dir, name)
        cfg = json.load(open(os.path.join(d, "case.json")))
        for key in ("solvers", "reference", "wet_threshold_m", "tol"):
            ok(key in cfg, "%s: case.json missing %r" % (name, key))
        ok(os.path.exists(os.path.join(d, "deck", "base.par")),
           "%s: deck/base.par missing" % name)
        ref = cfg["reference"]
        ok(os.path.exists(os.path.join(d, ref["file"])),
           "%s: reference %s missing" % (name, ref["file"]))
        for s in cfg["solvers"]:
            ok(s in bg.SOLVER_CLI, "%s: unknown solver %r" % (name, s))


def main():
    test_score()
    test_volume()
    test_cases_well_formed()
    if FAILS:
        print("FAIL:")
        for f in FAILS:
            print("  -", f)
        return 1
    print("benchmark gate self-test: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
