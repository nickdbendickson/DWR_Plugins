"""How Crest finds the suite data folder (install test 2026-11-21): NDSUITE_DATA, then the QGIS setting
ndsuite/data_folder that DamSafety Setup's "Start here" saves (in the profile Crest's qgis_process reads), then
~/.ndsuite/config.json. Read only: nothing here writes a profile or a config file."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from crest import layers  # noqa: E402


class SuiteFolderLookupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        env = {k: v for k, v in os.environ.items()
               if k not in ("NDSUITE_DATA", "QGIS_PROCESS", "QGIS_CUSTOM_CONFIG_PATH", "XDG_DATA_HOME", "APPDATA")}
        env.update({"HOME": str(self.home), "USERPROFILE": str(self.home), "PATH": str(self.root / "nobin")})
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.from_qgis = self.root / "from_qgis"
        self.from_json = self.root / "from_json"
        self.from_env = self.root / "from_env"
        for p in (self.from_qgis, self.from_json, self.from_env):
            p.mkdir()

    def profile(self, value: str) -> Path:
        """A QGIS_CUSTOM_CONFIG_PATH folder whose default profile has [ndsuite] data_folder=value."""
        cfg = self.root / "qgis profile"
        ini = cfg / "profiles" / "default" / "QGIS" / "QGIS3.ini"
        ini.parent.mkdir(parents=True, exist_ok=True)
        ini.write_text("[General]\nx=1\n\n[ndsuite]\ndams_folder=/somewhere\ndata_folder=%s\n\n[qgis]\n" % value,
                       encoding="utf-8")
        return cfg

    def config_json(self, value: str) -> None:
        (self.home / ".ndsuite").mkdir(exist_ok=True)
        (self.home / ".ndsuite" / "config.json").write_text(json.dumps({"ndsuite/data_folder": value}),
                                                            encoding="utf-8")

    def test_reads_the_start_here_setting_from_the_launchers_profile(self):
        cfg = self.profile(str(self.from_qgis))
        launcher = self.root / "qgis-process-dwr.sh"
        launcher.write_text("#!/bin/bash\nexport QGIS_CUSTOM_CONFIG_PATH='%s'\nexec qgis_process \"$@\"\n" % cfg,
                            encoding="utf-8")
        os.environ["QGIS_PROCESS"] = str(launcher)
        self.config_json(str(self.from_json))
        folder, source = layers.find_suite_folder_and_source()
        self.assertEqual(folder, str(self.from_qgis.resolve()))
        self.assertIn("ndsuite/data_folder", source)
        self.assertIn("QGIS3.ini", source)

    def test_env_beats_qgis_and_json(self):
        os.environ["QGIS_CUSTOM_CONFIG_PATH"] = str(self.profile(str(self.from_qgis)))
        self.config_json(str(self.from_json))
        os.environ["NDSUITE_DATA"] = str(self.from_env)
        self.assertEqual(layers.find_suite_folder_and_source(), (str(self.from_env.resolve()), "NDSUITE_DATA"))

    def test_json_when_qgis_has_none_or_a_missing_folder(self):
        os.environ["QGIS_CUSTOM_CONFIG_PATH"] = str(self.profile(str(self.root / "gone")))
        self.config_json(str(self.from_json))
        folder, source = layers.find_suite_folder_and_source()
        self.assertEqual(folder, str(self.from_json.resolve()))
        self.assertTrue(source.endswith("config.json"), source)

    def test_default_profile_folder_when_nothing_sets_the_path(self):
        if os.name == "nt":
            os.environ["APPDATA"] = str(self.root / "appdata")
        elif sys.platform != "darwin":
            os.environ["XDG_DATA_HOME"] = str(self.root / "xdg")
        ini = layers.default_qgis_profile_root() / "profiles" / "default" / "QGIS" / "QGIS3.ini"
        ini.parent.mkdir(parents=True)
        ini.write_text("[ndsuite]\ndata_folder=%s\n" % self.from_qgis, encoding="utf-8")
        self.assertEqual(layers.qgis_profile_ini(), ini)
        self.assertEqual(layers.find_suite_folder(), str(self.from_qgis.resolve()))

    def test_nothing_set(self):
        self.assertEqual(layers.find_suite_folder_and_source(), (None, ""))

    def test_ini_quoting_and_windows_backslashes(self):
        ini = self.root / "QGIS3.ini"
        ini.write_text('[ndsuite]\ndata_folder="C:\\\\Users\\\\nick\\\\ND data, 2026"\n[other]\ndata_folder=no\n',
                       encoding="utf-8")
        self.assertEqual(layers.qgis_ini_value(ini, "ndsuite/data_folder"), "C:\\Users\\nick\\ND data, 2026")
        self.assertEqual(layers.qgis_ini_value(ini, "ndsuite/pack_path"), "")
        self.assertEqual(layers.qgis_ini_value(self.root / "missing.ini", "ndsuite/data_folder"), "")


if __name__ == "__main__":
    unittest.main()
