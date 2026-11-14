#!/usr/bin/env python3
"""tools/lisflood_smoke.py: its exit status is the release gate for the Windows LISFLOOD-FP
build, so the three answers are pinned here with stand-in programs (no LISFLOOD-FP, no QGIS).

    python3 tools/test_lisflood_smoke.py
"""
import contextlib
import io
import os
import stat
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import lisflood_smoke as LS                                    # noqa: E402

GOOD = "import os\nos.makedirs('out', exist_ok=True)\nopen(os.path.join('out', 'run.max'), 'w').write('x')\n"
BAD = "import sys\nsys.stderr.write('cannot start\\n')\nsys.exit(3)\n"
SILENT = "pass\n"                                              # exit 0, no result file


def program(folder, name, body):
    """A stand-in 'lisflood': a script this Python runs (a .bat on Windows, a shell script
    elsewhere), started with the par file as its one argument, like the real one."""
    py = os.path.join(folder, name + ".py")
    with open(py, "w", encoding="utf-8") as fh:
        fh.write(body)
    if os.name == "nt":
        exe = os.path.join(folder, name + ".bat")
        with open(exe, "w", encoding="utf-8") as fh:
            fh.write('@"{}" "{}" %*\r\n'.format(sys.executable, py))
    else:
        exe = os.path.join(folder, name)
        with open(exe, "w", encoding="utf-8") as fh:
            fh.write('#!/bin/sh\nexec "{}" "{}" "$@"\n'.format(sys.executable, py))
        os.chmod(exe, os.stat(exe).st_mode | stat.S_IXUSR)
    return exe


def run(args):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = LS.main(args)
    return rc, out.getvalue()


class Smoke(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = os.environ.pop("ND_DAM_PRE_RUN_SMOKE", None)
        self.addCleanup(lambda: self.env is None or os.environ.__setitem__("ND_DAM_PRE_RUN_SMOKE", self.env))

    def test_a_build_that_runs_and_writes_its_result_passes(self):
        rc, out = run([program(self.tmp.name, "good", GOOD)])
        self.assertEqual(rc, 0, out)
        self.assertIn("ok:", out)

    def test_a_build_that_does_not_start_fails_with_its_last_line(self):
        rc, out = run([program(self.tmp.name, "bad", BAD)])
        self.assertEqual(rc, 1, out)
        self.assertIn("exit 3", out)
        self.assertIn("cannot start", out)

    def test_a_build_that_exits_0_but_writes_nothing_fails(self):
        rc, out = run([program(self.tmp.name, "silent", SILENT)])
        self.assertEqual(rc, 1, out)
        self.assertIn("wrote no", out)

    def test_a_missing_file_fails(self):
        rc, out = run([os.path.join(self.tmp.name, "nothing-here")])
        self.assertEqual(rc, 1, out)
        self.assertIn("not found", out)

    def test_the_skip_switch_cannot_pass_a_bad_build(self):
        os.environ["ND_DAM_PRE_RUN_SMOKE"] = "0"
        try:
            rc, out = run([program(self.tmp.name, "bad2", BAD)])
        finally:
            os.environ.pop("ND_DAM_PRE_RUN_SMOKE", None)
        self.assertEqual(rc, 1, out)

    def test_a_wrong_command_line_is_2(self):
        self.assertEqual(run([])[0], 2)
        self.assertEqual(run(["a", "b"])[0], 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
