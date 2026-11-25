#!/usr/bin/env python3
"""build_release.boann_libraries: since DamSafety Core 1.7.0 (Boann 6.6.1) the Core zip carries
every Boann / BoannGrid CPU library under boann/hydro/prebuilt/, and no other plugin zip carries
any. Stand-in library files, no compiler needed.

    python3 tools/test_build_release_libs.py"""
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import build_release as br  # noqa: E402

ALL = ("mf", "fv", "fv2", "dg2", "sgc", "subfv", "subli", "mt", "mf4", "mf8")


class BoannLibraries(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="relibs_")
        plat = os.path.join(self.d, br.BIN_PLATFORMS[0])
        os.makedirs(plat)
        for spec in ALL:
            for suffix in ("_cpu_0123456789abcdef.so", "_cpu_0123456789abcdef_omp.so"):
                with open(os.path.join(plat, spec + suffix), "wb") as fh:
                    fh.write(spec.encode())

    def tearDown(self):
        shutil.rmtree(self.d, True)

    def _specs(self, plugin):
        got = br.boann_libraries(self.d, plugin)
        for k in got:
            self.assertTrue(k.startswith(plugin + "/boann/hydro/prebuilt/"), k)
        return sorted({k.rsplit("/", 1)[1].split("_cpu_")[0] for k in got})

    def test_core_gets_every_library(self):
        self.assertEqual(self._specs("DamSafety_Core"), sorted(ALL))

    def test_no_other_plugin_gets_any(self):
        for plugin in ("DamSafety_Runner", "DamSafety_Screening", "DamSafety_Hazard"):
            self.assertEqual(self._specs(plugin), [])


if __name__ == "__main__":
    unittest.main()
