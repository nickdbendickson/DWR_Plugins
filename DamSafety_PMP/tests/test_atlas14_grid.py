"""NOAA Atlas 14 GIS grids in the shared core (noaa14/gisgrid.py, gridstats.py): the grid parts of DamIT PMP's
test_gis_rain.py, ported to the DWR copy of the core, plus a basin-statistics check. NOAA-shaped zips made here, a
fake downloader: no network, no QGIS. GDAL + numpy needed for the clip / statistics tests.

Run:  python3 -W error DamSafety_PMP/tests/test_atlas14_grid.py
"""
import http.server
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
import zipfile
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))

from DamSafety_PMP.noaa14 import atlas14 as A  # noqa: E402
from DamSafety_PMP.noaa14 import gisgrid as G  # noqa: E402
from DamSafety_PMP import noaa14_http  # noqa: E402
from DamSafety_PMP.noaa14_http import HttpError  # noqa: E402

try:
    import numpy as np  # noqa: F401
    from osgeo import gdal  # noqa: F401
    HAVE_GDAL = True
except ImportError:                                            # pragma: no cover
    HAVE_GDAL = False

FIX = os.path.join(HERE, "fixtures_atlas14")
# the duration menu of https://hdsc.nws.noaa.gov/pfds/pfds_gis.html (copied 2026-11-12)
NOAA_DUR_MENU = ["05m", "10m", "15m", "30m", "60m", "02h", "03h", "06h", "12h", "24h", "48h", "03d", "04d", "07d",
                 "10d", "20d", "30d", "45d", "60d"]
WGS84_WKT = ('GEOGCS["WGS 84",DATUM["WGS_1984",SPHEROID["WGS 84",6378137,298.257223563]],'
             'PRIMEM["Greenwich",0],UNIT["degree",0.0174532925199433]]')


# the .prj NOAA ships (full ESRI WKT). The two test scripts that wrote a cut-down
# GEOGCS["GCS_North_American_1983"] were the two that stalled at their first grid read on GDAL 3.4
# (QGIS 3.34 LTR image, CI 2026-11-18), so the fixtures use the real text.
NAD83_PRJ = ('GEOGCS["GCS_North_American_1983",DATUM["D_North_American_1983",SPHEROID["GRS_1980",6378137.0,'
             '298.257222101]],PRIMEM["Greenwich",0.0],UNIT["Degree",0.0174532925199433]]')


def fake_noaa_zip(path, values, x0=-97.5, y0=46.9, cell=0.0083333, nodata=-9, name="mw100yr24ha"):
    """A zip shaped like NOAA's: <name>.asc (ESRI ASCII, thousandths of an inch), .prj and .xml."""
    nr, nc = len(values), len(values[0])
    body = "ncols {}\nnrows {}\nxllcorner {}\nyllcorner {}\ncellsize {}\nNODATA_value {}\n".format(
        nc, nr, x0, y0, cell, nodata) + "\n".join(" ".join(str(v) for v in row) for row in values) + "\n"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(name + ".asc", body)
        z.writestr(name + ".prj", NAD83_PRJ)
        z.writestr(name + ".xml", "<metadata><title>Precipitation Frequency for Midwestern states, USA - NOAA Atlas 14 "
                                  "Volume 8</title><pubdate>20130415</pubdate></metadata>")


class Urls(unittest.TestCase):
    def test_dwr_2801_example(self):
        self.assertEqual(G.grid_url("mw", 100, "24-hr"), "https://hdsc.nws.noaa.gov/pub/hdsc/data/mw/mw100yr24ha.zip")

    def test_duration_codes_match_the_noaa_menu(self):
        self.assertEqual([G.DUR_CODES[d] for d in G.DURATION_LABELS], NOAA_DUR_MENU)
        self.assertEqual(G.DUR_CODES["2-day"], "48h")                  # not 02d

    def test_choices(self):
        self.assertTrue(G.zip_name("mw", 500, "6-hr").endswith("mw500yr06ha.zip"))
        self.assertEqual(G.zip_name("mw", 1, "60-min"), "mw1yr60ma.zip")
        self.assertEqual(G.zip_name("orb", 1000, "60-day", "upper90", "ams"), "orb1000yr60dau_ams.zip")
        self.assertEqual(G.zip_name("tx", 2, "5-min", "lower90"), "tx2yr05mal.zip")

    def test_frequencies_checked(self):
        with self.assertRaises(A.Atlas14Error):
            G.zip_name("mw", 3, "24-hr")
        with self.assertRaises(A.Atlas14Error):
            G.zip_name("mw", 1, "24-hr", series="ams")                  # no 1-year AEP file
        with self.assertRaises(A.Atlas14Error):
            G.zip_name("mw", 100, "7-hr")
        with self.assertRaises(A.Atlas14Error):
            G.zip_name("zz", 100, "24-hr")
        for y in A.PDS_ARI:
            G.zip_name("mw", y, "24-hr")
        for y in A.AMS_AEP_YEARS:
            G.zip_name("mw", y, "24-hr", series="ams")

    def test_round_trip_every_combination(self):
        n = 0
        for d in G.DURATION_LABELS:
            for series, ys in (("pds", A.PDS_ARI), ("ams", A.AMS_AEP_YEARS)):
                for y in ys:
                    for b, _ in G.BOUNDS:
                        got = G.parse_zip_name(G.zip_name("mw", y, d, b, series))
                        self.assertEqual(got, {"region": "mw", "frequency_years": y, "duration": d, "bound": b,
                                               "series": series})
                        n += 1
        self.assertEqual(n, 19 * 19 * 3)
        self.assertIsNone(G.parse_zip_name("mw100yr24.zip"))

    def test_region_from_pfds(self):
        with open(os.path.join(FIX, "pfds_pds_english_fargo_nd.txt"), "rb") as fh:
            self.assertEqual(G.region_from_pfds(A.parse(fh.read())), "mw")
        with self.assertRaises(A.Atlas14Error):
            G.region_from_pfds({"region_code": "xx", "region": "Nowhere"})


class Cache(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.calls = []

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def dl(self, url, dest):
        self.calls.append(url)
        fake_noaa_zip(dest, [[6342, 6350], [6400, -9]])

    def test_download_once_then_cache(self):
        p, rec = G.fetch_zip("mw", 100, "24-hr", "estimate", "pds", self.tmp, self.dl)
        self.assertFalse(rec["from_cache"])
        self.assertEqual(rec["url"], "https://hdsc.nws.noaa.gov/pub/hdsc/data/mw/mw100yr24ha.zip")
        self.assertEqual(len(rec["sha256"]), 64)
        p2, rec2 = G.fetch_zip("mw", 100, "24-hr", "estimate", "pds", self.tmp, self.dl)
        self.assertTrue(rec2["from_cache"])
        self.assertEqual(rec2["fetched"], rec["fetched"])
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(p, p2)

    def test_offline(self):
        with self.assertRaises(A.Atlas14Error) as cm:
            G.fetch_zip("mw", 100, "24-hr", "estimate", "pds", self.tmp, self.dl, offline=True)
        self.assertIn("offline", str(cm.exception))
        self.assertEqual(self.calls, [])
        G.fetch_zip("mw", 100, "24-hr", "estimate", "pds", self.tmp, self.dl)
        G.fetch_zip("mw", 100, "24-hr", "estimate", "pds", self.tmp, self.dl, offline=True)     # cached: fine

    def test_404_is_not_published(self):
        def nf(url, dest):
            raise HttpError(url, 404, "Not Found")             # what noaa14_http.download_file raises
        with self.assertRaises(G.NotPublished) as cm:
            G.fetch_zip("sa", 100, "24-hr", "estimate", "ams", self.tmp, nf)
        self.assertIn("has not published sa100yr24ha_ams.zip", str(cm.exception))

    def test_network_error_passes_through(self):
        def down(url, dest):
            raise HttpError(url, None, "network is unreachable")
        with self.assertRaises(IOError):
            G.fetch_zip("mw", 100, "24-hr", "estimate", "pds", self.tmp, down)

    def test_not_a_zip(self):
        def junk(url, dest):
            with open(dest, "w") as fh:
                fh.write("<html>captive portal</html>")
        with self.assertRaises(A.Atlas14Error):
            G.fetch_zip("mw", 100, "24-hr", "estimate", "pds", self.tmp, junk)
        self.assertFalse(os.path.exists(G.cache_zip_path(self.tmp, "mw", "mw100yr24ha.zip")))


class _NotFound(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        self.send_error(404, "Not Found")

    def log_message(self, *args):
        pass


def _open_fds():
    """Number of open file descriptors of this process (Linux), else None."""
    d = "/proc/self/fd"
    return len(os.listdir(d)) if os.path.isdir(d) else None


class RealDownload(unittest.TestCase):
    """noaa14_http.download_file itself, against a local server that answers 404 (no internet)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _NotFound)
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.thread.start()
        self.url = "http://127.0.0.1:{}/pub/hdsc/data/sa/sa100yr24ha_ams.zip".format(self.srv.server_address[1])
        env = mock.patch.dict(os.environ, {"no_proxy": "127.0.0.1,localhost", "NO_PROXY": "127.0.0.1,localhost"})
        env.start()
        self.addCleanup(env.stop)

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.thread.join(5)
        shutil.rmtree(self.tmp)

    def test_404_keeps_its_status_and_leaves_no_file_or_descriptor(self):
        dest = os.path.join(self.tmp, "sa", "sa100yr24ha_ams.zip")
        before = _open_fds()
        for _ in range(3):
            with self.assertRaises(HttpError) as cm:
                noaa14_http.download_file(self.url, dest, timeout=10)
            self.assertEqual(cm.exception.status, 404)
        self.assertEqual(os.listdir(os.path.dirname(dest)), [])           # no .part_ file, no half zip
        if before is not None:
            self.assertEqual(_open_fds(), before)                         # the temporary file was closed

    def test_404_through_fetch_zip_is_not_published(self):
        def dl(url, dest):
            return noaa14_http.download_file(self.url, dest, timeout=10)
        with self.assertRaises(G.NotPublished):
            G.fetch_zip("sa", 100, "24-hr", "estimate", "ams", self.tmp, dl)
        folder = os.path.dirname(G.cache_zip_path(self.tmp, "sa", "x.zip"))
        self.assertEqual(os.listdir(folder) if os.path.isdir(folder) else [], [])


@unittest.skipUnless(HAVE_GDAL, "GDAL / numpy not importable")
class Clip(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.zip = os.path.join(self.tmp, "mw100yr24ha.zip")
        # 4 x 4 cells, top-left (-97.5, 46.9 + 4 * 0.0083333); thousandths of an inch; one NODATA cell
        self.vals = [[6300, 6310, 6320, 6330], [6340, 6342, 6350, 6360], [6400, 6410, 6420, -9], [6500, 6510, 6520, 6530]]
        fake_noaa_zip(self.zip, self.vals)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_scale_thousandths_to_inches(self):
        arr, gt, epsg = G.read_clip(self.zip)
        self.assertEqual(arr.dtype.name, "float32")
        self.assertAlmostEqual(float(arr[1, 1]), 6.342, places=4)                  # 6342 -> 6.342 in
        self.assertEqual(float(arr[2, 3]), G.NODATA_OUT)                           # -9 is empty
        self.assertEqual(epsg, 4269)
        st = G.array_stats(arr)
        self.assertEqual(st["cells"], 15)
        self.assertEqual((st["min"], st["max"]), (6.3, 6.53))

    def test_window_and_value_at(self):
        top = 46.9 + 4 * 0.0083333
        lon, lat = -97.5 + 1.5 * 0.0083333, top - 1.5 * 0.0083333
        arr, gt, _ = G.read_clip(self.zip, (lon - 0.001, lat - 0.001, lon + 0.001, lat + 0.001))
        self.assertEqual(arr.shape, (1, 1))
        self.assertAlmostEqual(G.value_at(arr, gt, lon, lat), 6.342, places=4)
        self.assertIsNone(G.value_at(arr, gt, lon + 1, lat))

    def test_outside_the_grid_fails_clearly(self):
        with self.assertRaises(A.Atlas14Error) as cm:
            G.read_clip(self.zip, (-100.0, 40.0, -99.0, 41.0))
        self.assertIn("outside this Atlas 14 grid", str(cm.exception))

    def test_all_nodata_is_a_coverage_error(self):
        fake_noaa_zip(self.zip, [[-9, -9], [-9, -9]])
        with self.assertRaises(A.CoverageError):
            G.read_clip(self.zip)

    def test_mask_polygon(self):
        top = 46.9 + 4 * 0.0083333
        wkt = "POLYGON((-97.5 {t},-97.4834 {t},-97.4834 {b},-97.5 {b},-97.5 {t}))".format(t=top, b=46.9)
        arr, gt, _ = G.read_clip(self.zip, mask_wkt=wkt)
        self.assertEqual(G.array_stats(arr)["cells"], 8)
        self.assertEqual(float(arr[0, 3]), G.NODATA_OUT)

    def test_geotiff_round_trip_and_gridstats(self):
        from osgeo import gdal as g
        from DamSafety_PMP.noaa14 import gridstats
        arr, gt, epsg = G.read_clip(self.zip)
        tif = os.path.join(self.tmp, "out.tif")
        G.write_geotiff(tif, arr, gt, epsg)
        ds = g.Open(tif)
        self.assertEqual(ds.GetRasterBand(1).GetNoDataValue(), G.NODATA_OUT)
        self.assertAlmostEqual(float(ds.GetRasterBand(1).ReadAsArray()[1, 1]), 6.342, places=4)
        ds = None
        # a fetchgrid tif is read as inches (scale 1) only with its sidecar; NOAA's own file keeps 0.001
        self.assertEqual(gridstats.grid_scale(tif, 0.001), 0.001)
        with open(tif + ".json", "w") as fh:
            json.dump({"schema": "damit.pmp.atlas14_gis", "units": "in"}, fh)    # the shared file-format name
        self.assertEqual(gridstats.grid_scale(tif, 0.001), 1.0)
        mid = gridstats.point_value(tif, -97.5 + 1.5 * 0.0083333, 46.9 + 4 * 0.0083333 - 1.5 * 0.0083333, WGS84_WKT, 1.0)
        self.assertAlmostEqual(mid, 6.342, places=3)

    def test_basin_stats_of_noaa_file_and_geotiff(self):
        from DamSafety_PMP.noaa14 import gridstats
        top = 46.9 + 4 * 0.0083333
        left_two = "POLYGON((-97.4999 {t},-97.4835 {t},-97.4835 {b},-97.4999 {b},-97.4999 {t}))".format(
            t=top - 0.0001, b=46.9001)
        member = "/vsizip/{}/mw100yr24ha.asc".format(self.zip)
        st = gridstats.basin_stats(member, left_two, WGS84_WKT, 0.001)          # NOAA's thousandths of an inch
        cells = [6300, 6310, 6340, 6342, 6400, 6410, 6500, 6510]
        self.assertEqual(st["cells"], 8)
        self.assertAlmostEqual(st["mean"], round(sum(cells) / 8000.0, 4), places=4)
        self.assertEqual((st["min"], st["max"]), (6.3, 6.51))
        arr, gt, epsg = G.read_clip(self.zip)
        tif = os.path.join(self.tmp, "inches.tif")
        G.write_geotiff(tif, arr, gt, epsg)
        self.assertEqual(gridstats.basin_stats(tif, left_two, WGS84_WKT, 1.0)["mean"], st["mean"])
        with self.assertRaises(ValueError):
            gridstats.basin_stats(tif, "POLYGON((-90 30,-89 30,-89 31,-90 31,-90 30))", WGS84_WKT, 1.0)

    def test_zip_metadata(self):
        self.assertEqual(G.zip_metadata(self.zip)["pubdate"], "20130415")


@unittest.skipUnless(HAVE_GDAL, "GDAL / numpy not importable")
class GridNames(unittest.TestCase):
    def test_parse_name(self):
        from DamSafety_PMP.noaa14 import gridstats as S
        self.assertEqual(S.parse_name("mw100yr24ha.asc"), {"region_code": "mw", "frequency_years": 100,
                                                          "duration": "24-hr", "series": "pds"})
        self.assertEqual(S.parse_name("mw100yr48hau.asc")["duration"], "2-day")
        self.assertEqual(S.parse_name("mw100yr48hau.asc")["bound"], "upper90")
        self.assertEqual(S.parse_name("sw1000yr60da_ams.asc"), {"region_code": "sw", "frequency_years": 1000,
                                                               "duration": "60-day", "series": "ams"})
        self.assertEqual(S.parse_name("mw5yr05mal.zip")["duration"], "5-min")
        for lab, code in G.DUR_CODES.items():
            self.assertEqual(S.parse_name("mw2yr{}a.asc".format(code))["duration"], lab)


if __name__ == "__main__":
    unittest.main()
