"""Local DEM terrain (localdem.py and GET /api/dem): settings, request parsing, the gdalwarp window, the endpoint.

    python3 crest-desktop/tests/test_localdem.py
"""

from __future__ import annotations

import http.client
import json
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

DESKTOP = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(DESKTOP))

import crest.__main__ as m  # noqa: E402
from crest import localdem  # noqa: E402

GDAL = shutil.which("gdalwarp") and shutil.which("gdal_translate")


def make_dem(folder: Path) -> Path:
    """A 200 x 200 m EPSG:26914 GeoTIFF near Fargo, 1 m cells, elevation = 300 + x/100 (rises to the east)."""
    asc = folder / "dem.asc"
    rows = [" ".join(f"{300 + c / 100:.2f}" for c in range(200)) for _ in range(200)]
    asc.write_text("ncols 200\nnrows 200\nxllcorner 660000\nyllcorner 5190000\ncellsize 1\nNODATA_value -999999\n" + "\n".join(rows) + "\n")
    tif = folder / "dem.tif"
    subprocess.run(["gdal_translate", "-q", "-a_srs", "EPSG:26914", "-of", "GTiff", str(asc), str(tif)], check=True)
    return tif


class SettingsAndRequestTests(unittest.TestCase):
    def test_dem_path_round_trip_keeps_other_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = Path(tmp) / "crest-settings.json"
            settings.write_text(json.dumps({"last_dir": "/x"}))
            dem = Path(tmp) / "a.vrt"
            dem.write_text("<VRTDataset/>")
            localdem.write_dem_path(settings, str(dem))
            self.assertEqual(localdem.read_dem_path(settings), str(dem))
            self.assertEqual(json.loads(settings.read_text())["last_dir"], "/x")
            dem.unlink()  # the drive was unplugged: no DEM, so the desk falls back to 3DEP
            self.assertIsNone(localdem.read_dem_path(settings))
            localdem.write_dem_path(settings, None)
            self.assertNotIn("dem_path", json.loads(settings.read_text()))

    def test_unreadable_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "s.json"
            bad.write_text("{not json")
            self.assertIsNone(localdem.read_dem_path(bad))
            self.assertIsNone(localdem.read_dem_path(None))

    def test_parse_request(self):
        box, w, h, cell = localdem.parse_request({"bbox": ["-97.1,46.8,-97.0,46.9"], "w": ["100"], "h": ["120"], "cell": ["5"]})
        self.assertEqual(box, (-97.1, 46.8, -97.0, 46.9))
        self.assertEqual((w, h, cell), (100, 120, 5.0))
        for q in (
            {"bbox": ["1,2,3"], "w": ["1"], "h": ["1"]},
            {"bbox": ["-97,46.9,-97.1,46.8"], "w": ["1"], "h": ["1"]},
            {"bbox": ["-97.1,46.8,-97.0,46.9"], "w": ["0"], "h": ["1"]},
            {"bbox": ["-97.1,46.8,-97.0,46.9"], "w": ["4000"], "h": ["4000"]},
            {"bbox": ["-97.1,46.8,-97.0,46.9"], "w": ["x"], "h": ["1"]},
            {},
        ):
            with self.assertRaises(ValueError, msg=q):
                localdem.parse_request(q)

    def test_resampling(self):
        args = localdem.warp_args("in.vrt", (-97.1, 46.8, -97.0, 46.9), 10, 10, 5, "out.bin")
        self.assertEqual(args[args.index("-r") + 1], "average")
        args = localdem.warp_args("in.vrt", (-97.1, 46.8, -97.0, 46.9), 10, 10, 1, "out.bin")
        self.assertEqual(args[args.index("-r") + 1], "bilinear")
        self.assertEqual(args[-2:], ["in.vrt", "out.bin"])


@unittest.skipUnless(GDAL, "gdalwarp / gdal_translate not installed")
class WindowTests(unittest.TestCase):
    def test_reads_window_in_lon_lat(self):
        with tempfile.TemporaryDirectory() as tmp:
            tif = make_dem(Path(tmp))
            # The centre of the DEM in lon/lat, a 60 x 60 m box around it.
            out = subprocess.run(["gdaltransform", "-s_srs", "EPSG:26914", "-t_srs", "EPSG:4326", "-output_xy"], input="660100 5190100\n", capture_output=True, text=True, check=True)
            lon, lat = (float(v) for v in out.stdout.split()[:2])
            dlon, dlat = 30 / 76000, 30 / 111000
            raw = localdem.read_window(str(tif), (lon - dlon, lat - dlat, lon + dlon, lat + dlat), 12, 12, 5)
            self.assertEqual(len(raw), 12 * 12 * 4)
            z = struct.unpack("<144f", raw)
            self.assertTrue(all(300 < v < 302 for v in z), z[:12])
            self.assertGreater(z[11], z[0])  # rises to the east
            self.assertAlmostEqual(z[66], 301.0, delta=0.15)

    def test_outside_is_nodata(self):
        with tempfile.TemporaryDirectory() as tmp:
            tif = make_dem(Path(tmp))
            raw = localdem.read_window(str(tif), (-100.0, 47.0, -99.999, 47.001), 4, 4, 5)
            self.assertTrue(all(v == localdem.NODATA for v in struct.unpack("<16f", raw)))


class EndpointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        web = Path(cls.tmp.name) / "web"
        web.mkdir()
        (web / "index.html").write_text("<!doctype html><html><head><title>t</title></head><body></body></html>")
        cls.settings = Path(cls.tmp.name) / "crest-settings.json"
        cls.patch = mock.patch.object(m, "SETTINGS_FILE", cls.settings)
        cls.patch.start()
        cls.server = m.serve(web, port=0)
        cls.port = cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.patch.stop()
        cls.tmp.cleanup()

    def get(self, path, token=True):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        h = {"Host": f"127.0.0.1:{self.port}"}
        if token:
            h[m.TOKEN_HEADER] = m.API_TOKEN
        conn.request("GET", path, headers=h)
        r = conn.getresponse()
        data = r.read()
        conn.close()
        return r.status, data

    def setUp(self):
        localdem.write_dem_path(self.settings, None)

    def test_token_required(self):
        self.assertEqual(self.get("/api/dem?bbox=-97.1,46.8,-97.0,46.9&w=2&h=2", token=False)[0], 403)

    def test_no_dem_chosen(self):
        self.assertEqual(self.get("/api/dem?bbox=-97.1,46.8,-97.0,46.9&w=2&h=2")[0], 409)

    def test_bad_request(self):
        dem = Path(self.tmp.name) / "x.vrt"
        dem.write_text("<VRTDataset/>")
        localdem.write_dem_path(self.settings, str(dem))
        self.assertEqual(self.get("/api/dem?bbox=1,2&w=2&h=2")[0], 400)

    @unittest.skipUnless(GDAL, "gdalwarp / gdal_translate not installed")
    def test_serves_window(self):
        tif = make_dem(Path(self.tmp.name))
        localdem.write_dem_path(self.settings, str(tif))
        status, data = self.get("/api/dem?bbox=-97.1,46.8,-97.0,46.9&w=3&h=2&cell=5")
        self.assertEqual(status, 200)
        self.assertEqual(len(data), 3 * 2 * 4)


if __name__ == "__main__":
    unittest.main()
