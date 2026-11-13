#!/usr/bin/env python3
"""tools/blas_selfcheck.py: it passes a BLAS that is right, and catches each kind of wrong
answer the Ubuntu 22.04 OpenBLAS "Cooperlake" kernels gave (see that file's docstring).

    python3 tools/test_blas_selfcheck.py

The faulty library cannot be loaded here, so the faults are put in by hand, shaped like the
ones it produced: a product whose rows are scrambled past a certain size, one that is only
wrong when an operand is a transposed view, and a solve that returns finite nonsense.
"""
import contextlib
import io
import os
import sys
import unittest

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import blas_selfcheck as bs  # noqa: E402


def names(res, ok):
    return [what for what, good in res if good is ok]


class Checks(unittest.TestCase):
    def test_a_correct_blas_passes_every_check(self):
        res = bs.checks()
        self.assertEqual(len(res), 4)
        self.assertEqual(names(res, False), [])

    def test_a_product_wrong_only_above_a_size_is_caught(self):
        def dot(a, b):                      # right for small results, rows swapped for wide ones
            out = np.dot(a, b)
            if out.ndim == 2 and out.shape[1] > 192:
                out = out.copy()
                out[[0, 1]] = out[[1, 0]]
            return out
        self.assertEqual(len(names(bs.checks(dot=dot), False)), 3)

    def test_a_product_wrong_only_for_a_transposed_operand_is_caught(self):
        def dot(a, b):
            out = np.dot(a, b)
            if not b.flags["C_CONTIGUOUS"]:
                out = out + 1.0
            return out
        self.assertEqual(names(bs.checks(dot=dot), False), ["matrix product by a transposed view"])

    def test_a_single_wrong_entry_is_caught_without_a_tolerance(self):
        def dot(a, b):
            out = np.dot(a, b).copy()
            out[-1, -1] += 1.0
            return out
        self.assertEqual(len(names(bs.checks(dot=dot), False)), 3)

    def test_a_solve_that_returns_finite_nonsense_is_caught(self):
        res = bs.checks(solve=lambda h, g: np.full(len(g), 4.3e12))
        self.assertEqual(names(res, False), ["linear solve, 64 unknowns"])

    def test_a_solve_that_raises_or_returns_nan_is_caught(self):
        def boom(h, g):
            raise np.linalg.LinAlgError("Singular matrix")
        self.assertEqual(names(bs.checks(solve=boom), False), ["linear solve, 64 unknowns"])
        self.assertEqual(names(bs.checks(solve=lambda h, g: np.full(len(g), np.nan)), False),
                         ["linear solve, 64 unknowns"])

    def test_the_reference_does_not_go_through_the_function_under_test(self):
        # a dot that is always zero must fail every product: the reference is computed apart
        res = bs.checks(dot=lambda a, b: np.zeros((a.shape[0], b.shape[1])))
        self.assertEqual(len(names(res, False)), 3)

    def test_every_check_has_a_product_wide_enough_for_the_single_thread_fault(self):
        # on one thread the faulty kernels were only wrong for results wider than 192 columns
        seen = []

        def dot(a, b):
            out = np.dot(a, b)
            seen.append(out.shape)
            return out
        bs.checks(dot=dot)
        self.assertEqual(seen, [(96, 320), (96, 320), (320, 96), (320, 320)])
        self.assertTrue(seen[0][1] > 192 and seen[1][1] > 192 and seen[3][1] > 192, seen)


class Main(unittest.TestCase):
    def run_main(self, argv, bad=False):
        real = bs.checks
        if bad:
            bs.checks = lambda: [("matrix product, 96x320 by 320x320", False), ("linear solve, 64 unknowns", True)]
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                rc = bs.main(argv)
        finally:
            bs.checks = real
        return rc, out.getvalue()

    def test_it_reports_the_machine_and_exits_0_when_right(self):
        rc, text = self.run_main([])
        self.assertEqual(rc, 0, text)
        for word in ("cpu:", "numpy:", "blas:", "OPENBLAS_CORETYPE=", "every product and the solve are right"):
            self.assertIn(word, text)
        self.assertNotIn("::error::", text)

    def test_a_wrong_blas_fails_the_job_with_an_annotation(self):
        rc, text = self.run_main([], bad=True)
        self.assertEqual(rc, 1)
        self.assertIn("WRONG matrix product, 96x320 by 320x320", text)
        self.assertIn("::error::BLAS self-check failed: matrix product, 96x320 by 320x320.", text)

    def test_report_only_says_so_and_never_fails(self):
        rc, text = self.run_main(["--report-only"], bad=True)
        self.assertEqual(rc, 0)
        self.assertIn("1 of 2 WRONG", text)
        self.assertNotIn("::error::", text)

    def test_libraries_that_cannot_be_asked_do_not_stop_the_check(self):
        self.assertEqual(bs.openblas_facts(["/nonexistent/libblas.so.3"]), [])
        got = bs.openblas_facts(["/nonexistent/libopenblasp-r0.3.20.so"])
        self.assertEqual(len(got), 1)
        self.assertIn("could not ask it", got[0])


class Workflow(unittest.TestCase):
    """The job must set the kernels before numpy loads, and look before it trusts."""

    def setUp(self):
        with open(os.path.join(os.path.dirname(HERE), ".github", "workflows", "ci.yml"), encoding="utf-8") as f:
            text = f.read()
        start = text.index("\n  plugin-suites-ltr:")
        nxt = [m for m in __import__("re").finditer(r"\n  [a-z][a-z0-9-]*:\n", text) if m.start() > start]
        self.job = text[start:nxt[0].start()] if nxt else text[start:]

    def test_the_ltr_job_forces_haswell_kernels_for_every_step(self):
        head = self.job[:self.job.index("    steps:")]
        self.assertRegex(head, r"\n    env:\n(      .*\n)*      OPENBLAS_CORETYPE: Haswell\n")

    def test_the_ltr_job_checks_blas_before_any_suite_and_records_the_unforced_choice(self):
        forced = self.job.index("python3 tools/blas_selfcheck.py\n")
        unforced = self.job.index("env -u OPENBLAS_CORETYPE python3 tools/blas_selfcheck.py --report-only")
        first_suite = self.job.index("DamSafety_Screening/tests/run_all.py")
        self.assertLess(unforced, forced)
        self.assertLess(forced, first_suite)

    def test_the_unforced_run_can_never_fail_the_step(self):
        i = self.job.index("env -u OPENBLAS_CORETYPE python3 tools/blas_selfcheck.py --report-only")
        line = self.job[i:self.job.index("\n", self.job.index("\n", i) + 1)]
        self.assertRegex(line, r"--report-only \\\n\s+\|\| echo \"::warning::")

    def test_no_step_of_the_ltr_job_overrides_the_kernels(self):
        self.assertEqual(self.job.count("OPENBLAS_CORETYPE:"), 1)


if __name__ == "__main__":
    unittest.main()
