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

    # 2026-11-21 (install test): the suite launcher sets QGIS_CUSTOM_CONFIG_PATH itself, so Check QGIS must not say
    # it is "not set" (same fix as damit-desk #5).
    def test_check_reports_the_launchers_profile_not_unset(self):
        os.environ["KNOWN_ALGS"] = ""
        root = Path(self.tmp.name) / "qgis profile"
        real = Path(self.tmp.name) / "damsafety"
        (root / "profiles").mkdir(parents=True)
        real.mkdir()
        (root / "profiles" / "default").symlink_to(real)
        launcher = Path(self.tmp.name) / "qgis-process-dwr.sh"
        launcher.write_text("#!/bin/bash\n# QGIS_CUSTOM_CONFIG_PATH=/not/this\nexport QGIS_CUSTOM_CONFIG_PATH='%s'\n"
                            'exec "%s" "$@"\n' % (root, self.exe), encoding="utf-8")
        launcher.chmod(0o755)
        os.environ["QGIS_PROCESS"] = str(launcher)
        qgis_run.run_check(self.lines.append)
        text = "\n".join(self.lines)
        self.assertIn(f"default profile under {root} (QGIS_CUSTOM_CONFIG_PATH, set by the launcher qgis-process-dwr.sh)",
                      text)
        self.assertIn(str(real), text)
        self.assertNotIn("not set", text)

    def test_neither_sets_it(self):
        os.environ["KNOWN_ALGS"] = ""
        qgis_run.run_check(self.lines.append)
        self.assertIn("QGIS's default profile (neither Crest nor its qgis_process launcher sets QGIS_CUSTOM_CONFIG_PATH)",
                      "\n".join(self.lines))

    def test_no_qgis_process(self):
        with mock.patch.object(qgis_run, "find_qgis_process", lambda: None):
            r = qgis_run.run_check(self.lines.append)
        self.assertFalse(r["ok"])
        self.assertIn("qgis_process was not found", self.lines[0])


class LauncherConfigPathTests(unittest.TestCase):
    """Reading QGIS_CUSTOM_CONFIG_PATH out of a launcher script: read only, on every OS."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def script(self, name: str, text: str) -> str:
        path = Path(self.tmp.name) / name
        path.write_bytes(text.encode("utf-8"))
        return str(path)

    def test_sh_export_with_spaces_and_comments(self):
        exe = self.script("qgis-process-dwr.sh", "#!/bin/bash\n# QGIS_CUSTOM_CONFIG_PATH=/not/this\n"
                          "export QGIS_CUSTOM_CONFIG_PATH=/sb/my\\ sandbox/qgis-process/dwr\n")
        self.assertEqual(qgis_run.launcher_config_path(exe), "/sb/my sandbox/qgis-process/dwr")

    def test_cmd_set(self):
        exe = self.script("qgis-process-dwr.cmd", '@echo off\r\nrem QGIS_CUSTOM_CONFIG_PATH=C:\\no\r\n'
                          'set "QGIS_CUSTOM_CONFIG_PATH=C:\\Users\\nick\\dam suite\\qgis-process\\dwr"\r\n')
        self.assertEqual(qgis_run.launcher_config_path(exe), "C:\\Users\\nick\\dam suite\\qgis-process\\dwr")

    def test_a_binary_is_not_read(self):
        binary = Path(self.tmp.name) / "qgis_process"
        binary.write_bytes(b"\x7fELF\x00\x00QGIS_CUSTOM_CONFIG_PATH=/x\n")
        self.assertIsNone(qgis_run.launcher_config_path(str(binary)))
        self.assertIsNone(qgis_run.launcher_config_path(str(Path(self.tmp.name) / "missing")))

    def test_the_launcher_wins_over_the_environment(self):
        exe = self.script("q.sh", "export QGIS_CUSTOM_CONFIG_PATH=/from/launcher\n")
        with mock.patch.dict(os.environ, {"QGIS_CUSTOM_CONFIG_PATH": "/from/env"}):
            self.assertEqual(qgis_run.qgis_config_path(exe), ("/from/launcher", "set by the launcher q.sh"))
            plain = self.script("plain.sh", "exec qgis_process \"$@\"\n")
            self.assertEqual(qgis_run.qgis_config_path(plain), ("/from/env", "from Crest's environment"))
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("QGIS_CUSTOM_CONFIG_PATH", None)
            self.assertEqual(qgis_run.qgis_config_path(str(Path(self.tmp.name) / "none")), (None, ""))


if __name__ == "__main__":
    unittest.main()
