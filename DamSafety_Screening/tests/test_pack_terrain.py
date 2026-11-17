"""Configured offline pack is the default screening terrain; 3DEP when the pack is missing, does
not cover the dam, or the settings chose 3DEP (0.20.3 and its review fixes)."""
import json
import os
import sys
import tempfile
import types
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "qgis_mock"))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))

from nd_dam_screening.core import pack_terrain as PT  # noqa: E402

DEFAULTS = {"terrain_source": 0, "larger_clips": 1, "local_dem": ""}


class TestPackTerrain(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="pack_terrain_")
        self.work = os.path.join(self.tmp, "work")
        os.makedirs(self.work)
        self.addCleanup(self._cleanup)
        self.env = mock.patch.dict(os.environ, {
            "NDSUITE_PACK": "", "NDSUITE_DATA": "", "NDSUITE_SOURCE": "",
        }, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)
        # no QGIS settings / ~/.ndsuite of the machine running the tests
        self._real_config = PT._config_values
        p1 = mock.patch.object(PT, "_suitedata", return_value=None)
        p2 = mock.patch.object(PT, "_config_values", return_value=[])
        p1.start(); p2.start()
        self.addCleanup(p1.stop); self.addCleanup(p2.stop)

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

    def _fake_wrap(self, dem, work):
        folder = os.path.join(work, PT.WRAP_DIR)
        os.makedirs(folder, exist_ok=True)
        return folder

    def apply(self, s, **kw):
        kw.setdefault("work", self.work)
        with mock.patch.object(PT, "wrap_pack_dem", side_effect=self._fake_wrap):
            return PT.apply_offline_pack_terrain(dict(s), **kw)

    def test_no_pack_keeps_3dep_and_logs(self):
        notes = []
        s = self.apply(DEFAULTS, log=notes.append)
        self.assertEqual(s["terrain_source"], 0)
        self.assertEqual(s["larger_clips"], 1)
        self.assertTrue(any("USGS 3DEP" in n for n in notes), notes)

    def test_configured_pack_goes_through_a_wrapper_in_the_work_folder(self):
        folder, tif = self._pack_with_dem()
        notes = []
        with mock.patch.dict(os.environ, {"NDSUITE_DATA": folder}):
            s = self.apply(DEFAULTS, log=notes.append)
        self.assertEqual(s["terrain_source"], 1)
        self.assertEqual(s["larger_clips"], 2)
        # never <pack>/dem itself: tool 2 mosaics every raster under local_dem
        self.assertEqual(s["local_dem"], os.path.join(self.work, PT.WRAP_DIR))
        self.assertTrue(any("offline pack" in n and tif in n for n in notes), notes)
        self.assertTrue(s["_terrain_source_used"].startswith("offline_pack:"))
        self.assertEqual(PT.pop_notes(s), notes)
        self.assertNotIn("_terrain_source_notes", s)

    def test_without_a_work_folder_the_settings_stay_on_3dep(self):
        folder, _ = self._pack_with_dem()
        with mock.patch.dict(os.environ, {"NDSUITE_DATA": folder}), \
                mock.patch.object(PT, "wrap_pack_dem", return_value=""):
            s = PT.apply_offline_pack_terrain(dict(DEFAULTS))
        self.assertEqual(s["terrain_source"], 0)
        self.assertTrue(s["_terrain_source_used"].startswith("3dep:no_wrapper"))

    def test_settings_that_chose_3dep_are_kept(self):
        folder, _ = self._pack_with_dem()
        notes = []
        with mock.patch.dict(os.environ, {"NDSUITE_DATA": folder}):
            s = self.apply(DEFAULTS, explicit=("terrain_source",), log=notes.append)
        self.assertEqual((s["terrain_source"], s["larger_clips"], s["local_dem"]), (0, 1, ""))
        self.assertTrue(any("chosen in the settings" in n for n in notes), notes)

    def test_the_pack_default_can_be_turned_off(self):
        folder, _ = self._pack_with_dem()
        with mock.patch.dict(os.environ, {"NDSUITE_DATA": folder}):
            s = self.apply(dict(DEFAULTS, offline_pack_terrain=False))
        self.assertEqual(s["terrain_source"], 0)
        self.assertEqual(s["_terrain_source_used"], "settings:pack_default_off")

    def test_already_set_local_dem_does_not_download_3dep_for_larger_clips(self):
        notes = []
        s = self.apply({"terrain_source": 1, "larger_clips": 1, "local_dem": "/some/lidar"}, log=notes.append)
        self.assertEqual(s["larger_clips"], 2)
        self.assertEqual(s["local_dem"], "/some/lidar")
        self.assertTrue(any("local DEM already set" in n for n in notes), notes)

    def test_larger_clips_set_in_the_settings_is_kept(self):
        s = self.apply({"terrain_source": 1, "larger_clips": 1, "local_dem": "/some/lidar"},
                       explicit=("larger_clips",))
        self.assertEqual(s["larger_clips"], 1)

    def test_missing_dem_in_pack_falls_back_to_3dep(self):
        folder = os.path.join(self.tmp, "empty")
        os.makedirs(folder)
        with mock.patch.dict(os.environ, {"NDSUITE_DATA": folder}):
            s = self.apply(DEFAULTS)
        self.assertEqual(s["larger_clips"], 1)
        self.assertEqual(s["_terrain_source_used"], "3dep:no_pack")

    def test_a_raster_at_the_top_of_the_data_folder_is_not_terrain(self):
        folder = os.path.join(self.tmp, "data")
        os.makedirs(folder)
        open(os.path.join(folder, "NLCD_2021.tif"), "w").close()
        self.assertEqual(PT.pack_dem(folder), "")

    def test_several_rasters_and_no_vrt_is_not_one_dem(self):
        folder = os.path.join(self.tmp, "data")
        os.makedirs(os.path.join(folder, "dem"))
        for n in ("a.tif", "b.tif"):
            open(os.path.join(folder, "dem", n), "w").close()
        self.assertEqual(PT.pack_dem(folder), "")

    def test_pack_path_comes_from_config_json_not_a_hard_coded_box_path(self):
        folder, tif = self._pack_with_dem("from_config")
        cfg = os.path.join(self.tmp, "home", ".ndsuite", "config.json")
        os.makedirs(os.path.dirname(cfg))
        with open(cfg, "w") as fh:
            json.dump({"ndsuite/data_folder": folder}, fh)
        real_expand = os.path.expanduser
        with mock.patch.object(os.path, "expanduser", side_effect=lambda p: p.replace("~", os.path.join(self.tmp, "home")) if isinstance(p, str) and p.startswith("~") else real_expand(p)):
            folders = self._real_config((os.path.join("~", ".ndsuite", "config.json"), ("ndsuite/data_folder",)))
        self.assertIn(os.path.abspath(folder), [os.path.abspath(f) for f in folders])

    def test_the_qgis_data_folder_is_read_through_the_plugin_damsuite(self):
        folder, _ = self._pack_with_dem("from_qgis")
        fake = types.SimpleNamespace(data_folder=lambda: folder, pack_location=lambda: "", active_pack=lambda: "")
        with mock.patch.object(PT, "_suitedata", return_value=fake):
            self.assertIn(folder, PT.configured_pack_folders())

    def test_does_not_cover_falls_back_to_3dep(self):
        folder, tif = self._pack_with_dem()
        notes = []
        with mock.patch.dict(os.environ, {"NDSUITE_DATA": folder}):
            with mock.patch.object(PT, "pack_covers", return_value=False):
                s = self.apply(DEFAULTS, xy=(-100.0, 46.8), log=notes.append)
        self.assertEqual(s["larger_clips"], 1)
        self.assertTrue(any("does not cover" in n for n in notes), notes)

    def test_wrapper_is_written_in_the_work_folder_and_reused(self):
        folder, tif = self._pack_with_dem()
        calls = []

        def build_vrt(path, srcs):
            calls.append((path, list(srcs)))
            with open(path, "w") as fh:
                fh.write("<VRTDataset/>")
            return object()

        fake_osgeo = types.ModuleType("osgeo")
        fake_osgeo.gdal = types.SimpleNamespace(BuildVRT=build_vrt)
        with mock.patch.dict(sys.modules, {"osgeo": fake_osgeo, "osgeo.gdal": fake_osgeo.gdal}):
            out = PT.wrap_pack_dem(tif, self.work)
            again = PT.wrap_pack_dem(tif, self.work)
        self.assertEqual(out, os.path.join(self.work, PT.WRAP_DIR))
        self.assertEqual(again, out)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1], [os.path.abspath(tif)])
        self.assertTrue(os.path.isfile(os.path.join(out, PT.WRAP_NAME)))
        self.assertEqual(os.listdir(os.path.join(folder, "dem")), ["mosaic.vrt"])   # pack not written


class TestHoleAtTheDam(unittest.TestCase):
    """terrain_cache: a local clip with no ground at the dam is redone from 3DEP (0.20.3)."""
    def test_no_ground_at_centre(self):
        import numpy as np
        from osgeo import gdal
        from nd_dam_screening.algorithms.terrain_cache import _no_ground_at_centre
        tmp = tempfile.mkdtemp(prefix="hole_")
        def save(arr, name):
            p = os.path.join(tmp, name)
            ds = gdal.Dataset(arr, (0, 10, 0, 100, 0, -10), -9999.0, p)
            ds.FlushCache()
            return p
        hole = np.full((9, 9), 300.0); hole[3:6, 3:6] = -9999.0
        edge = np.full((9, 9), 300.0); edge[0:2, :] = -9999.0
        self.assertTrue(_no_ground_at_centre(gdal, save(hole, "hole.npz")))
        self.assertFalse(_no_ground_at_centre(gdal, save(edge, "edge.npz")))


if __name__ == "__main__":
    unittest.main()
