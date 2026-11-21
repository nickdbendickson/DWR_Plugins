"""The local DEM FOLDER Crest writes for the screening holds the chosen DEM only (2026-11-21, PR #447).

crest_settings.json carried local_dem = the DEM's own folder (terrain_source 1). DamSafety_Screening's tool 2
(terrain.list_local_tiles) mosaics every raster under that folder, and the Absaraka test folder held hmax_ft.tif,
vmax_fps.tif, arr_hr.tif and a second DEM next to cass_10m_3dep.vrt. The folder is now <work>/screen_dem with the DEM
alone (DamIT Crest's same fix, damit #163). Nothing is run.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from crest import layers  # noqa: E402

TILE_VRT = """<VRTDataset rasterXSize="2" rasterYSize="2">
  <VRTRasterBand dataType="Float32" band="1">
    <SimpleSource><SourceFilename relativeToVRT="1">tiles/a &amp; b.tif</SourceFilename></SimpleSource>
    <SimpleSource><SourceFilename relativeToVRT="0">/abs/c.tif</SourceFilename></SimpleSource>
  </VRTRasterBand>
</VRTDataset>
"""
RASTER_EXT = (".tif", ".tiff", ".img", ".vrt")   # what DamSafety_Screening's terrain.list_local_tiles picks up


def tiles_the_screening_would_mosaic(folder: Path) -> list[str]:
    """The same walk as DamSafety_Screening core/terrain.list_local_tiles (recursive, by extension)."""
    out = []
    for root, _d, files in os.walk(folder):
        out += [f for f in files if f.lower().endswith(RASTER_EXT)]
    return sorted(out)


class ScreenDemFolderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.inputs = self.root / "inputs"
        self.inputs.mkdir()
        self.work = self.root / "work"
        self.work.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def settings(self, dem, **kw):
        layers.write_settings(self.work, None, str(dem), **kw)
        return json.loads((self.work / "crest_settings.json").read_text(encoding="utf-8"))

    def test_a_decoy_raster_next_to_the_dem_is_not_mosaicked(self):
        dem = self.inputs / "cass_10m_3dep.vrt"
        dem.write_text('<VRTDataset><SourceFilename relativeToVRT="0">/dems/t.tif</SourceFilename></VRTDataset>')
        (self.inputs / "cass_10m_3dep.vrt.aux.xml").write_text("<PAMDataset/>")
        decoys = ("hmax_ft.tif", "vmax_fps.tif", "arr_hr.tif", "absaraka_3dep_r3000m.tif")
        for other in decoys:
            (self.inputs / other).write_bytes(b"not terrain")
        (self.inputs / "run_folder").mkdir()
        (self.inputs / "run_folder" / "depth_max.tif").write_bytes(b"not terrain")
        s = self.settings(dem)
        folder = Path(s["local_dem"])
        self.assertEqual(folder.resolve(), (self.work / "screen_dem").resolve())
        self.assertNotEqual(folder.resolve(), self.inputs.resolve())
        self.assertEqual(tiles_the_screening_would_mosaic(folder), ["cass_10m_3dep.vrt"],
                         "only the chosen DEM; no results raster or second DEM")
        self.assertEqual(sorted(x.name for x in folder.iterdir()), ["cass_10m_3dep.vrt", "cass_10m_3dep.vrt.aux.xml"])
        self.assertEqual((folder / "cass_10m_3dep.vrt").read_text(), dem.read_text())
        self.assertEqual(s["terrain_source"], 1)
        self.assertEqual(s["larger_clips"], 2)
        self.assertEqual(sorted(x.name for x in self.inputs.iterdir()),
                         sorted(["cass_10m_3dep.vrt", "cass_10m_3dep.vrt.aux.xml", *decoys, "run_folder"]),
                         "the DEM's folder is only read")

    def test_the_old_behaviour_would_have_mixed_the_decoy_in(self):
        # The guard for this test file: the DEM's own folder really does hold more than one raster for the walk.
        dem = self.inputs / "dem.tif"
        dem.write_bytes(b"II*\x00")
        (self.inputs / "hmax_ft.tif").write_bytes(b"decoy")
        self.assertEqual(tiles_the_screening_would_mosaic(self.inputs), ["dem.tif", "hmax_ft.tif"])
        self.assertEqual(tiles_the_screening_would_mosaic(Path(self.settings(dem)["local_dem"])), ["dem.tif"])

    def test_a_vrt_with_relative_tiles_is_written_with_absolute_ones(self):
        dem = self.inputs / "mosaic.vrt"
        dem.write_text(TILE_VRT)
        folder = Path(self.settings(dem)["local_dem"])
        staged = folder / "mosaic.vrt"
        self.assertFalse(staged.is_symlink())
        text = staged.read_text()
        tile = (self.inputs / "tiles" / "a & b.tif").resolve()
        self.assertIn(f'relativeToVRT="0">{str(tile).replace("&", "&amp;")}</SourceFilename>', text)
        self.assertIn('relativeToVRT="0">/abs/c.tif</SourceFilename>', text)
        self.assertNotIn('relativeToVRT="1"', text)

    def test_a_new_dem_replaces_the_old_one_and_a_geotiff_keeps_its_world_file(self):
        first = self.inputs / "first.vrt"
        first.write_text("<VRTDataset/>")
        self.settings(first)
        dem = self.inputs / "lidar.tif"
        dem.write_bytes(b"II*\x00")
        (self.inputs / "lidar.tfw").write_text("1\n0\n0\n-1\n0\n0\n")
        folder = Path(self.settings(dem)["local_dem"])
        self.assertEqual(sorted(x.name for x in folder.iterdir()), ["lidar.tfw", "lidar.tif"])
        self.assertEqual((folder / "lidar.tif").read_bytes(), b"II*\x00")

    def test_the_staged_dem_itself_can_be_chosen(self):
        dem = self.inputs / "d.vrt"
        dem.write_text("<VRTDataset/>")
        folder = Path(self.settings(dem)["local_dem"])
        again = Path(self.settings(folder / "d.vrt")["local_dem"])
        self.assertEqual(again.resolve(), folder.resolve())
        self.assertEqual([x.name for x in folder.iterdir()], ["d.vrt"])
        self.assertEqual((folder / "d.vrt").read_text(), "<VRTDataset/>")

    def test_the_work_folders_own_dem_tif_is_staged_too(self):
        # Get layers > Download writes <work>/dem.tif; the work folder also holds every screening output raster.
        dem = self.work / "dem.tif"
        dem.write_bytes(b"II*\x00")
        (self.work / "7_simulations").mkdir()
        (self.work / "7_simulations" / "DWR-2801_depth.tif").write_bytes(b"decoy")
        folder = Path(self.settings(dem)["local_dem"])
        self.assertEqual(tiles_the_screening_would_mosaic(folder), ["dem.tif"])

    def test_the_pack_dem_folder_is_not_staged(self):
        pack_dem = self.root / "pack" / "dem"
        pack_dem.mkdir(parents=True)
        (pack_dem / "mosaic.vrt").write_text("<VRTDataset/>")
        s = self.settings(pack_dem / "mosaic.vrt", stage=False)
        self.assertEqual(Path(s["local_dem"]), pack_dem.resolve())
        self.assertFalse((self.work / "screen_dem").exists())

    def test_a_copy_is_made_when_links_are_refused(self):
        # Windows without the symlink right, or a DEM on another drive (no hard link): the DEM is copied.
        dem = self.inputs / "big.tif"
        dem.write_bytes(b"II*\x00" * 4)
        real_symlink, real_link = os.symlink, os.link

        def refuse(*_a, **_k):
            raise OSError("not allowed")
        os.symlink = os.link = refuse
        try:
            folder = Path(self.settings(dem)["local_dem"])
        finally:
            os.symlink, os.link = real_symlink, real_link
        staged = folder / "big.tif"
        self.assertFalse(staged.is_symlink())
        self.assertEqual(staged.read_bytes(), dem.read_bytes())

    @unittest.skipIf(os.name == "nt", "a symlink needs a right on Windows; there the DEM is hard-linked or copied")
    def test_a_geotiff_is_linked_not_copied(self):
        dem = self.inputs / "big.tif"
        dem.write_bytes(b"II*\x00" * 10)
        folder = Path(self.settings(dem)["local_dem"])
        self.assertTrue((folder / "big.tif").is_symlink())
        self.assertEqual(os.path.realpath(folder / "big.tif"), os.path.realpath(dem))


if __name__ == "__main__":
    unittest.main()
