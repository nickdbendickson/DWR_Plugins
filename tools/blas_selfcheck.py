#!/usr/bin/env python3
"""Is this machine's BLAS multiplying matrices correctly? numpy only, a few milliseconds.

    python3 tools/blas_selfcheck.py                 # exit 1 when a product or a solve is wrong
    python3 tools/blas_selfcheck.py --report-only   # print the same lines, always exit 0

Why this exists. From 2026-11-09 to 2026-11-13 the plugin-suites-ltr job failed 11 times out
of 174, on eleven commits across nine branches, always the same tests with the same numbers:

    reservoir_carve_terrain_wrapper_keeps_the_volume_at_any_cell
        AssertionError: (40.0, 1426399.0234128712, 1110289.0878099161)
    reservoir_carve_run_hook_writes_a_copy_and_leaves_the_source_dem
    Score.test_v2_clean_positives_trusted_marks_terrain      0.5 not greater than 0.9

The code was right. Ubuntu 22.04's OpenBLAS (libopenblas0-pthread 0.3.20, the BLAS behind the
system numpy in the qgis/qgis:release-3_34 image) picks its kernels by CPU when it loads, and
on an Intel CPU with AVX512_BF16 (Sapphire Rapids and later) it picks "Cooperlake", whose
matrix-matrix routines (dgemm, dtrsm, and with them LAPACK's solve) return wrong numbers once
the matrices pass a modest size. Vector and matrix-vector routines are right, nothing raises,
and the wrong numbers are the same on every run. Upstream removed the cause in 0.3.21;
Ubuntu 22.04 still ships 0.3.20. With the same library, OPENBLAS_CORETYPE=Haswell gives correct
results, and Haswell kernels are what the AMD runners already use. Both were reproduced
outside CI with the library taken from the image: bit for bit the numbers above.

So the job sets OPENBLAS_CORETYPE=Haswell, and runs this check first, twice: once with that
setting removed, to record what the runner would have done by itself, and once as the job
runs, where a wrong answer stops the job before a test can fail for a reason that is not
in the code. See docs/CI.md.

The operands are small whole numbers held as float64, so every true product is exact in any
order of summation and is compared without a tolerance. The reference is np.einsum without
optimize, which is numpy's own loop and calls no BLAS. Every check has a product wider than
192 columns, because on one thread the faulty kernels were only wrong past that width.
"""
import ctypes
import os
import re
import sys

import numpy as np


def checks(dot=None, solve=None):
    """[(what, ok)]. `dot` and `solve` are np.dot / np.linalg.solve unless a test passes others."""
    dot = dot or np.dot
    solve = solve or np.linalg.solve
    r = np.random.RandomState(0)
    a = r.randint(-8, 9, (96, 320)).astype(np.float64)
    b = r.randint(-8, 9, (320, 320)).astype(np.float64)
    ref = np.einsum("ik,kj->ij", a, b)
    bt = np.ascontiguousarray(b.T)
    out = [("matrix product, 96x320 by 320x320", bool(np.array_equal(dot(a, b), ref))),
           ("matrix product by a transposed view", bool(np.array_equal(dot(a, bt.T), ref))),
           ("transposed matrix product (X.T by Y)",
            bool(np.array_equal(dot(bt, np.ascontiguousarray(a.T)), ref.T)
                 and np.array_equal(dot(a.T, a * 2.0), 2.0 * np.einsum("ki,kj->ij", a, a))))]
    h = np.einsum("ki,kj->ij", a[:, :64], a[:, :64]) + 64.0 * np.eye(64)
    g = r.randint(-8, 9, 64).astype(np.float64)
    try:
        x = solve(h, g)
        ok = bool(np.all(np.isfinite(x))
                  and np.abs(np.einsum("ij,j->i", h, x) - g).max() <= 1e-8 * np.abs(g).max())
    except Exception:                                    # a singular-matrix error is a wrong answer too
        ok = False
    out.append(("linear solve, 64 unknowns", ok))
    return out


def cpu_model():
    try:
        with open("/proc/cpuinfo", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return "unknown (no /proc/cpuinfo)"
    m = re.search(r"^model name\s*:\s*(.+)$", text, re.M)
    flags = re.search(r"^flags\s*:\s*(.+)$", text, re.M)
    have = set(flags.group(1).split()) if flags else set()
    simd = [f for f in ("avx2", "avx512f", "avx512_bf16") if f in have]
    return "%s [%s]" % (m.group(1).strip() if m else "unknown", " ".join(simd) or "no avx2")


def blas_libraries():
    """The BLAS / LAPACK shared libraries this process has loaded, from /proc/self/maps."""
    np.dot(np.ones((2, 2)), np.ones((2, 2)))             # make sure the library is in
    try:
        with open("/proc/self/maps", encoding="utf-8", errors="replace") as f:
            paths = {line.split()[-1] for line in f if "/" in line}
    except OSError:
        return []
    return sorted(p for p in paths if re.search(r"(blas|lapack|mkl|blis|atlas)", os.path.basename(p), re.I))


def openblas_facts(paths):
    """'OpenBLAS <config>, kernels <core>, <n> threads' for each OpenBLAS among `paths`."""
    out = []
    for p in paths:
        named = "openblas" in os.path.basename(p).lower()
        try:
            lib = ctypes.CDLL(p)                         # already loaded: this is the same handle
            if not named and not hasattr(lib, "openblas_get_config"):
                continue                                 # a reference BLAS has nothing to say
            facts = []
            for name, kind in (("openblas_get_config", ctypes.c_char_p), ("openblas_get_corename", ctypes.c_char_p),
                               ("openblas_get_num_threads", ctypes.c_int)):
                # numpy's own wheels rename these (scipy_openblas_get_config64_)
                fn = next((getattr(lib, n) for n in (name, name + "64_", "scipy_" + name, "scipy_" + name + "64_")
                           if hasattr(lib, n)), None)
                if fn is None:
                    facts.append("?")
                    continue
                fn.restype = kind
                v = fn()
                facts.append(v.decode("utf-8", "replace") if isinstance(v, bytes) else str(v))
            out.append("%s, kernels: %s, threads: %s" % tuple(facts))
        except (OSError, AttributeError, ValueError) as e:
            if not named:
                continue
            out.append("%s: could not ask it (%s)" % (os.path.basename(p), e))
    return out


def say(text):
    """Every line straight out: on a faulty BLAS the next call may kill the process, and the
    lines that name the CPU and the kernels are the ones worth having."""
    print(text, flush=True)


def main(argv):
    report_only = "--report-only" in argv
    say("blas self-check%s" % (" (report only)" if report_only else ""))
    say("  cpu:      %s, %s core(s)" % (cpu_model(), os.cpu_count()))
    say("  numpy:    %s" % np.__version__)
    libs = blas_libraries()
    say("  blas:     %s" % (", ".join(libs) or "none found among the loaded libraries (numpy's built-in?)"))
    for line in openblas_facts(libs):
        say("  openblas: %s" % line)
    say("  OPENBLAS_CORETYPE=%s OPENBLAS_NUM_THREADS=%s" % (os.environ.get("OPENBLAS_CORETYPE", "(not set)"),
                                                         os.environ.get("OPENBLAS_NUM_THREADS", "(not set)")))
    res = checks()
    for what, ok in res:
        say("  %-5s %s" % ("ok" if ok else "WRONG", what))
    bad = [what for what, ok in res if not ok]
    if not bad:
        say("  result:   every product and the solve are right")
        return 0
    say("  result:   %d of %d WRONG. This BLAS returns wrong numbers on this CPU; any test that multiplies "
          "matrices would fail for a reason that is not in the code." % (len(bad), len(res)))
    if report_only:
        return 0
    say("::error::BLAS self-check failed: %s. See tools/blas_selfcheck.py and docs/CI.md." % "; ".join(bad))
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
