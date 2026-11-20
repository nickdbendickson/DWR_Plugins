"""'Check QGIS hand-off' (qgis_run.run_check): qgis_process found, the profile it reads, and each tool the Report
step's buttons call registered there. Fake qgis_process only; nothing is written."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from crest import qgis_run  # noqa: E402

FAKE = """#!/usr/bin/env python3
import os, sys
known = os.environ.get("KNOWN_ALGS", "").split(",")
if len(sys.argv) > 2 and sys.argv[1] == "help":
    if sys.argv[2] in known:
        print("----------------\\nAlgorithm\\n----------------\\n" + sys.argv[2])
        sys.exit(0)
    print("Algorithm " + sys.argv[2] + " not found!")
    sys.exit(1)
sys.exit(2)
"""


@unittest.skipIf(os.name == "nt", "the fake qgis_process is a shebang script (POSIX only)")
class CheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.exe = Path(self.tmp.name) / "qgis_process"
        self.exe.write_text(FAKE, encoding="utf-8")
        self.exe.chmod(0o755)
        self.lines: list[str] = []
        self.env = mock.patch.dict(os.environ, {"QGIS_PROCESS": str(self.exe)})
        self.env.start()
        os.environ.pop("QGIS_CUSTOM_CONFIG_PATH", None)

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_all_tools_found_is_ok(self):
        os.environ["KNOWN_ALGS"] = ",".join(a for a, _ in qgis_run.HANDOFF_ALGORITHMS)
        r = qgis_run.run_check(self.lines.append)
        self.assertTrue(r["ok"], self.lines)
        self.assertEqual([x["algorithm"] for x in r["check"]], [a for a, _ in qgis_run.HANDOFF_ALGORITHMS])
        self.assertTrue(any("hand-off is ready" in ln for ln in self.lines))

    def test_a_missing_plugin_is_named(self):
        os.environ["KNOWN_ALGS"] = "nddam:preflight,nddam:runchain"
        r = qgis_run.run_check(self.lines.append)
        self.assertFalse(r["ok"])
        missing = [x["algorithm"] for x in r["check"] if not x["found"]]
        self.assertEqual(missing, ["ndbreach:rundam"])
        self.assertTrue(any(ln.startswith("MISSING ndbreach:rundam") for ln in self.lines), self.lines)
        self.assertTrue(any("not found" in ln for ln in self.lines))

    def test_the_profile_link_is_reported(self):
        os.environ["KNOWN_ALGS"] = ""
        root = Path(self.tmp.name) / "qp"
        real = Path(self.tmp.name) / "damsafety"
        (root / "profiles").mkdir(parents=True)
        real.mkdir()
        (root / "profiles" / "default").symlink_to(real)
        os.environ["QGIS_CUSTOM_CONFIG_PATH"] = str(root)
        qgis_run.run_check(self.lines.append)
        self.assertTrue(any(str(real) in ln for ln in self.lines), self.lines)

    def test_no_qgis_process(self):
        with mock.patch.object(qgis_run, "find_qgis_process", lambda: None):
            r = qgis_run.run_check(self.lines.append)
        self.assertFalse(r["ok"])
        self.assertIn("qgis_process was not found", self.lines[0])


if __name__ == "__main__":
    unittest.main()
