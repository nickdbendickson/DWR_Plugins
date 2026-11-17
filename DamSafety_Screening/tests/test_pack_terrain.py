"""Configured offline pack is the default screening terrain; 3DEP only when the pack is missing or does not cover."""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "qgis_mock"))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))

from nd_dam_screening.core import pack_terrain as PT  # noqa: E402

class TestPackTerrain(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="pack_terrain_")
        self.addCleanup(self._cleanup)
        self.env = mock.patch.dict(os.environ, {
            "NDSUITE_PACK": "", "NDSUITE_DATA": "", "NDSUITE_SOURCE": "",
        }, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)

    def _cleanup(self):
        import shutil
        shutil.rmtree(self.tmp, True)

    def _pack_with_dem(self, name="pack"):
        root = os.path.join(self.tmp, name)
        dem = os.path.join(root, "dem")
        os.makedirs(dem)
        tif = os.path.join(dem, "mosaic.vrt")
        with open(tif, "w") as fh:
            fh.write("<VRTDataset></VRTDataset>")
        return root, tif

    def test_no_pack_keeps_3dep_and_logs(self):
        notes = []
        s = PT.apply_offline_pack_terrain({"terrain_source": 0, "larger_clips": 1, "local_dem": ""}, log=notes.append)
        self.assertEqual(s["terrain_source"], 0)
        self.assertEqual(s["larger_clips"], 1)
        self.assertTrue(any("USGS 3DEP" in n and "no configured" in n for n in notes), notes)

    def test_configured_pack_is_used_and_larger_clips_go_local(self):
        folder, tif = self._pack_with_dem()
        notes = []
        with mock.patch.dict(os.environ, {"NDSUITE_DATA": folder}):
            s = PT.apply_offline_pack_terrain({"terrain_source": 0, "larger_clips": 1, "local_dem": ""}, log=notes.append)
        self.assertEqual(s["terrain_source"], 1)
        self.assertEqual(s["larger_clips"], 2)
        self.assertEqual(s["local_dem"], os.path.dirname(tif))
        self.assertTrue(any("offline pack" in n and folder in n for n in notes), notes)
        self.assertTrue(s["_terrain_source_used"].startswith("offline_pack:"))

    def test_already_set_local_dem_does_not_download_3dep_for_larger_clips(self):
        notes = []
        s = PT.apply_offline_pack_terrain(
            {"terrain_source": 1, "larger_clips": 1, "local_dem": "/some/lidar"}, log=notes.append)
        self.assertEqual(s["larger_clips"], 2)
        self.assertEqual(s["local_dem"], "/some/lidar")
        self.assertTrue(any("local DEM already set" in n for n in notes), notes)

    def test_missing_dem_in_pack_falls_back_to_3dep(self):
        folder = os.path.join(self.tmp, "empty")
        os.makedirs(folder)
        notes = []
        with mock.patch.dict(os.environ, {"NDSUITE_DATA": folder}):
            s = PT.apply_offline_pack_terrain({"terrain_source": 0, "larger_clips": 1, "local_dem": ""}, log=notes.append)
        self.assertEqual(s["larger_clips"], 1)
        self.assertTrue(any("USGS 3DEP" in n for n in notes), notes)

    def test_pack_path_comes_from_config_json_not_a_hard_coded_box_path(self):
        folder, tif = self._pack_with_dem("from_config")
        cfg = os.path.join(self.tmp, "home", ".ndsuite", "config.json")
        os.makedirs(os.path.dirname(cfg))
        with open(cfg, "w") as fh:
            json.dump({"ndsuite/data_folder": folder}, fh)
        with mock.patch.object(os.path, "expanduser", side_effect=lambda p: p.replace("~", os.path.join(self.tmp, "home")) if isinstance(p, str) and p.startswith("~") else p):
            folders = PT.configured_pack_folders()
        self.assertIn(os.path.abspath(folder), [os.path.abspath(f) for f in folders])
        self.assertFalse(any("offline_packs_20261029_full" in f for f in folders))

    def test_does_not_cover_falls_back_to_3dep(self):
        folder, tif = self._pack_with_dem()
        notes = []
        with mock.patch.dict(os.environ, {"NDSUITE_DATA": folder}):
            with mock.patch.object(PT, "pack_covers", return_value=False):
                s = PT.apply_offline_pack_terrain(
                    {"terrain_source": 0, "larger_clips": 1, "local_dem": ""},
                    xy=(-100.0, 46.8), log=notes.append)
        self.assertEqual(s["larger_clips"], 1)
        self.assertTrue(any("does not cover" in n for n in notes), notes)


if __name__ == "__main__":
    unittest.main()
