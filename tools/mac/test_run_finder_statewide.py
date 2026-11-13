"""Tests of run_finder_statewide.py on small made-up data (seconds; no terrain, no pack needed).

    python3 tools/mac/test_run_finder_statewide.py                      (from the DWR_Plugins checkout)

The tests that need GDAL are skipped where there is none (the QGIS-free CI job); the tests that
call the engine's own functions look for DamSafety_Finder in $DAMSAFETY_PLUGINS, then two folders
above this file (the checkout), and are skipped when it is not there. Nothing is written outside
a temporary folder.
"""
import contextlib
import csv
import importlib.util
import io
import json
import os
import random
import shutil
import signal
import sys
import tempfile
import types
import unittest
import warnings

sys.dont_write_bytecode = True
warnings.filterwarnings("ignore")
HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location("run_finder_statewide", os.path.join(HERE, "run_finder_statewide.py"))
rfs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rfs)

import numpy as np                                                      # noqa: E402
try:
    from osgeo import gdal, ogr, osr                                    # noqa: E402
except ImportError:                                                     # the QGIS-free CI job
    gdal = ogr = osr = None
needs_gdal = unittest.skipIf(gdal is None, "GDAL (osgeo) is not installed here")


def _plugins():
    for p in (os.environ.get("DAMSAFETY_PLUGINS"), os.path.normpath(os.path.join(HERE, "..", ".."))):
        if p and os.path.isfile(os.path.join(p, rfs.ENGINE, "finder.py")):
            return p
    return None


def _tif(path, a, x0, y1, epsg=26914, nodata=-999999.0, overviews=False):
    ds = gdal.GetDriverByName("GTiff").Create(path, a.shape[1], a.shape[0], 1, gdal.GDT_Float32,
                                              options=["TILED=YES", "BLOCKXSIZE=256", "BLOCKYSIZE=256"])
    ds.SetGeoTransform((x0, 1.0, 0.0, y1, 0.0, -1.0))
    s = osr.SpatialReference()
    s.ImportFromEPSG(epsg)
    ds.SetProjection(s.ExportToWkt())
    b = ds.GetRasterBand(1)
    b.SetNoDataValue(nodata)
    b.WriteArray(a.astype("float32"))
    if overviews:
        ds.BuildOverviews("AVERAGE", [2, 4, 8, 16])
    ds = None


class NoGdal(unittest.TestCase):
    """What needs neither GDAL nor the plugin."""

    def test_lattice_touches_and_never_overlaps(self):
        box = (-381234.5, 2611111.0, -349000.0, 2640000.0)
        tiles = rfs.lattice(box, 10000.0)
        self.assertEqual(len(set(t.id for t in tiles)), len(tiles))
        for t in tiles:                                      # corners are whole multiples of the tile size
            self.assertTrue(all(v % 10000.0 == 0 for v in t.box))
            self.assertEqual((t.box[2] - t.box[0], t.box[3] - t.box[1]), (10000.0, 10000.0))
        rnd = random.Random(1)
        for _ in range(2000):                                # a point is in exactly one tile (west / south edge in)
            x, y = rnd.uniform(box[0], box[2]), rnd.uniform(box[1], box[3])
            n = sum(1 for t in tiles if t.box[0] <= x < t.box[2] and t.box[1] <= y < t.box[3])
            self.assertEqual(n, 1)
        # a box that ends on a lattice line does not take the next tile
        self.assertEqual(len(rfs.lattice((-370000.0, 2620000.0, -360000.0, 2630000.0), 10000.0)), 1)
        # the 10 km lattice sits on the engine's 5 km squares
        self.assertEqual(10000.0 % rfs.ENGINE_TILE_M, 0.0)

    def test_tile_id(self):
        self.assertEqual(rfs.tile_id(-37, 262, 10), "T10_W0370_N2620")
        self.assertEqual(rfs.tile_id(3, -1, 5), "T05_E0015_S0005")

    @needs_gdal
    def test_bbox(self):
        b = rfs.parse_bbox("-101.0,46.4,-100.5,46.8")
        self.assertTrue(-420000 < b[0] < b[2] < -330000 and 2590000 < b[1] < b[3] < 2680000)
        self.assertEqual(rfs.parse_bbox("-370000,2620000,-360000,2630000"), (-370000.0, 2620000.0, -360000.0, 2630000.0))
        for bad in ("1,2,3", "-100,46,-101,47", "a,b,c,d"):
            with self.assertRaises(rfs.UserError):
                rfs.parse_bbox(bad)
        self.assertEqual(rfs.fix_argv(["--bbox", "-101,46,-100,47", "--list"]), ["--bbox=-101,46,-100,47", "--list"])
        self.assertEqual(rfs.fix_argv(["--out", "-x", "--bbox=1,2,3,4"]), ["--out", "-x", "--bbox=1,2,3,4"])

    def test_inside(self):
        d = tempfile.mkdtemp()
        try:
            os.makedirs(os.path.join(d, "data", "pack"))
            self.assertTrue(rfs.inside(os.path.join(d, "data", "pack", "out"), os.path.join(d, "data")))
            self.assertTrue(rfs.inside(os.path.join(d, "data"), os.path.join(d, "data")))
            self.assertFalse(rfs.inside(os.path.join(d, "data2"), os.path.join(d, "data")))
            os.symlink(os.path.join(d, "data"), os.path.join(d, "link"))
            self.assertTrue(rfs.inside(os.path.join(d, "link", "out"), os.path.join(d, "data")))
        finally:
            shutil.rmtree(d)

    def test_inside_on_a_disk_that_ignores_case(self):
        """A Mac's disk: ~/nd_suite_data and ~/ND_Suite_Data are one folder. The path strings differ,
        so only asking the disk (os.path.samefile) can tell; here a stand-in that behaves like such
        a disk: two paths are the same file when they are equal without regard to case, and a path
        that is not there raises, as os.path.samefile does."""
        d = tempfile.mkdtemp()
        try:
            data = os.path.join(d, "ND_Suite_Data")
            os.makedirs(os.path.join(data, "ND_Dam_Suite_Pack"))
            there = {rfs.real(q).lower() for q in (d, data, os.path.join(data, "ND_Dam_Suite_Pack"))}
            asked = []

            def samefile(a, b):
                asked.append(a)
                for q in (a, b):
                    if q != os.sep and rfs.real(q).lower() not in there and not os.path.exists(q):
                        raise FileNotFoundError(q)
                return rfs.real(a).lower() == rfs.real(b).lower()

            low = os.path.join(d, "nd_suite_data")
            for out in (low, os.path.join(low, "out"), os.path.join(low, "a", "b", "c"),
                        os.path.join(d, "ND_SUITE_DATA", "nd_dam_suite_pack", "x")):
                self.assertFalse(rfs.inside(out, data, samefile=lambda a, b: False), out)   # the strings alone: no
                self.assertTrue(rfs.inside(out, data, samefile=samefile), out)             # the disk: yes
            # the pack, spelled otherwise, is found too; a sister folder is not
            self.assertTrue(rfs.inside(os.path.join(low, "ND_DAM_SUITE_PACK", "out"),
                                       os.path.join(data, "ND_Dam_Suite_Pack"), samefile=samefile))
            self.assertFalse(rfs.inside(os.path.join(d, "ND_Finder_Statewide"), data, samefile=samefile))
            self.assertFalse(rfs.inside(os.path.join(d, "nd_suite_data2", "out"), data, samefile=samefile))
            # the walk went up through folders that do not exist yet, to the first that does
            self.assertIn(rfs.real(low), asked)
            # the real os.path.samefile: a hard-to-spell twin made with a link is caught, nothing raises
            self.assertFalse(rfs.inside(os.path.join(d, "no", "such", "folder"), data))
            self.assertFalse(rfs.inside(os.path.join(d, "out"), os.path.join(d, "not there")))
        finally:
            shutil.rmtree(d)

    def test_time_estimate_counts_a_tile_by_its_terrain(self):
        """The fault found in review: the plain mean of the finished tiles, pulled down by the
        quick tiles at the edge of the terrain, was given as 'this computer's own figure'."""
        # the reviewer's 7 finished tiles: (seconds, share of terrain); the two left took 155 s and 28 s
        done = [(33.7, 0.12), (34.9, 0.10), (7.9, 0.005), (165.2, 0.95), (229.1, 0.91), (20.9, 0.04), (16.5, 0.007)]
        sec, n = rfs.full_tile_seconds(done)
        self.assertEqual((sec, n), (rfs.EST_SEC_PER_TILE, 2))             # two full tiles: not yet its own figure
        sec, n = rfs.full_tile_seconds(done + [(155.2, 0.86)])
        self.assertEqual(n, 3)
        self.assertAlmostEqual(sec, (165.2 + 229.1 + 155.2) / 3.0)        # the full tiles only, not the mean of all 8
        self.assertGreater(sec, 2.0 * sum(d[0] for d in done) / len(done))
        self.assertEqual(rfs.full_tile_seconds([(100.0, None), (0, 1.0), (None, 1.0)]), (rfs.EST_SEC_PER_TILE, 0))
        # a tile's cost: the start for a sliver, the full figure for a full or unknown one, in between by share
        self.assertEqual(rfs.tile_cost(0.0, 200.0), rfs.EST_TILE_START_SEC)
        self.assertEqual(rfs.tile_cost(1.0, 200.0), 200.0)
        self.assertEqual(rfs.tile_cost(None, 200.0), 200.0)
        self.assertEqual(rfs.tile_cost(0.5, 200.0), rfs.EST_TILE_START_SEC + 0.5 * (200.0 - rfs.EST_TILE_START_SEC))
        self.assertEqual(rfs.tile_cost(0.5, 10.0), 10.0)                  # never above the full figure
        # the two tiles left (a full one and a sliver), 2 at a time: the full one sets the time
        self.assertAlmostEqual(rfs.time_for([0.86, 0.03], 183.2, 2), rfs.tile_cost(0.86, 183.2))
        self.assertEqual(rfs.time_for([], 183.2, 2), 0.0)
        self.assertAlmostEqual(rfs.time_for([1.0] * 8, 100.0, 4), 200.0)

    def test_why_a_tile_failed(self):
        d = tempfile.mkdtemp()
        try:
            log = os.path.join(d, "t.log")
            with open(log, "w") as fh:
                fh.write("2026-11-13 20:48:24  83 stream line(s), 57 road / rail line(s), 98 known structure(s)\n")
            why = rfs.why_failed(-9, log)                    # killed: the last log line is NOT the reason
            self.assertIn("SIGKILL", why)
            self.assertIn("signal 9", why)
            self.assertNotIn("stream line", why)
            self.assertIn("SIGTERM", rfs.why_failed(-int(signal.SIGTERM), log))
            self.assertIn("exit code 137", rfs.why_failed(137, log))
            self.assertIn("83 stream line(s)", rfs.why_failed(137, log))     # named as the last line, not as the reason
            self.assertIn("--tile-timeout-min", rfs.why_failed(-9, log, "4 h 00 min"))
            with open(log, "a") as fh:
                fh.write("Traceback (most recent call last):\n  ...\n" + rfs.FAIL_MARK + "RuntimeError: no terrain\n\n")
            self.assertEqual(rfs.why_failed(1, log), "RuntimeError: no terrain")   # its own error line
            self.assertIn("exit code 1", rfs.why_failed(1, os.path.join(d, "no such.log")))
        finally:
            shutil.rmtree(d)

    def test_failed_log_goes_when_the_tile_finishes(self):
        d = tempfile.mkdtemp()
        try:
            os.makedirs(os.path.join(d, "failed"))
            for tid in ("T10_W0370_N2620", "T10_W0360_N2620"):
                with open(os.path.join(d, "failed", tid + ".log"), "w") as fh:
                    fh.write("x\n")
            os.makedirs(os.path.join(d, "tiles", "T10_W0370_N2620"))
            rfs.write_json(os.path.join(d, "tiles", "T10_W0370_N2620", "done.json"), {"id": "T10_W0370_N2620"})
            rfs.clear_failed(d)                              # one finished since, one still failing
            self.assertEqual(os.listdir(os.path.join(d, "failed")), ["T10_W0360_N2620.log"])
            os.makedirs(os.path.join(d, "tiles", "T10_W0360_N2620"))
            rfs.write_json(os.path.join(d, "tiles", "T10_W0360_N2620", "done.json"), {"id": "T10_W0360_N2620"})
            rfs.clear_failed(d, "T10_W0360_N2620")
            self.assertFalse(os.path.exists(os.path.join(d, "failed")))   # empty: the folder goes too
            rfs.clear_failed(d)                              # no folder: nothing to do
        finally:
            shutil.rmtree(d)

    def test_map_link(self):
        u = rfs.map_link(46.497756, -100.747986)
        self.assertTrue(u.startswith("https://www.google.com/maps/@?api=1&map_action=map&center=46.497756%2C-100.747986"))
        self.assertIn("basemap=satellite", u)
        self.assertNotIn(",", u)                             # safe in a CSV cell
        self.assertNotIn(" ", u)

    def test_ids_are_stable_from_0_7(self):
        self.assertFalse(rfs.ids_are_stable(types.SimpleNamespace(VERSION="0.6.0")))
        self.assertFalse(rfs.ids_are_stable(types.SimpleNamespace(VERSION="0.6.9")))
        self.assertTrue(rfs.ids_are_stable(types.SimpleNamespace(VERSION="0.7.0")))
        self.assertTrue(rfs.ids_are_stable(types.SimpleNamespace(VERSION="1.0.0")))
        self.assertTrue(rfs.ids_are_stable(types.SimpleNamespace(VERSION="0.6.0", lattice_squares=len)))
        self.assertFalse(rfs.ids_are_stable(types.SimpleNamespace(VERSION="dev")))

    def test_edge_distance_is_the_engines_when_it_gives_one(self):
        class Dem:
            asked = 0

            def edge_m(self, x, y):
                self.asked += 1
                return 12.3
        dem = Dem()
        old = [{"find_id": "FIND-1", "x": 1.0, "y": 2.0}, {"find_id": "FIND-2", "x": 3.0, "y": 4.0}]
        edge, how = rfs.edge_distances(old, dem)             # Finder 0.6: no edge_ft, measured here
        self.assertEqual(edge, [["FIND-1", 12.3], ["FIND-2", 12.3]])
        self.assertEqual(dem.asked, 2)
        self.assertIn("this script", how)
        new = [dict(old[0], edge_ft=100.0), dict(old[1], edge_ft=None)]
        edge, how = rfs.edge_distances(new, dem)             # Finder 0.7: its own figure, the DEM is not read
        self.assertEqual(edge, [["FIND-1", 30.5], ["FIND-2", None]])
        self.assertEqual(dem.asked, 2)
        self.assertIn("Finder", how)
        self.assertEqual(rfs.edge_distances([], dem)[0], [])

    def test_settings_key_separates_finder_versions(self):
        a = types.SimpleNamespace(tile_km=10)
        dem = types.SimpleNamespace(path="/x/ND_1m_26914.vrt", nx=10, ny=20, gt=(0.0, 1.0, 0.0, 0.0, 0.0, -1.0), epsg="26914")
        k6, k7 = rfs.settings_key("0.6.0", a, dem), rfs.settings_key("0.7.0", a, dem)
        self.assertNotEqual(k6, k7)
        self.assertEqual({k for k in k6 if k6[k] != k7[k]}, {"finder"})
        self.assertEqual(k6, json.loads(json.dumps(k6)))      # as it comes back from done.json
        self.assertIsNone(rfs.settings_key("0.6.0", a, None)["dem"])


@needs_gdal
class Plain(unittest.TestCase):
    def test_builtin_outline(self):
        g = rfs.builtin_outline()
        self.assertTrue(g.IsValid())
        self.assertTrue(182000 < g.GetArea() / 1e6 < 185000)             # North Dakota is 183,100 km2
        gb = g.Buffer(rfs.BUILTIN_BUFFER_M, 8)
        ct = rfs.transformer(rfs.srs_of(4326), rfs.srs_of(5070))
        places = {"Bismarck": (-100.78, 46.81, True), "Fargo": (-96.80, 46.88, True), "Williston": (-103.62, 48.15, True),
                  "Pembina": (-97.25, 48.966, True), "Bowman": (-103.39, 46.18, True),
                  "Moorhead MN": (-96.74, 46.87, False), "Sidney MT": (-104.16, 47.72, False),
                  "Lemmon SD": (-102.16, 45.92, False), "Emerson MB": (-97.21, 49.02, False),
                  "Breckenridge MN": (-96.57, 46.26, False),
                  # the south border is not one parallel: 45.9452 N in the west, 45.9351 N in the east.
                  # 1.2 km into South Dakota in the south-west (inside a straight line at 45.9350 N):
                  "SD, 1.2 km south of the line at 103.5 W": (-103.5, 45.9340, False),
                  "SD, 1.2 km south of the line at 100.9 W": (-100.9, 45.9320, False),
                  # North Dakota's southernmost roads (the pack's NG911 roads), west, middle and east:
                  "ND border road at 103.9 W": (-103.9, 45.9453, True), "ND border road at 102.1 W": (-102.1, 45.9441, True),
                  "ND border road at 100.6 W": (-100.6, 45.9419, True), "ND border road at 99.3 W": (-99.3, 45.9383, True),
                  "ND border road at 97.6 W": (-97.6, 45.9351, True)}
        xs, ys, want = [], [], []
        for lon, lat, w in places.values():
            x, y = ct.TransformPoint(lon, lat)[:2]
            xs.append(x)
            ys.append(y)
            want.append(w)
        self.assertEqual(rfs.points_in_rings(xs, ys, rfs.rings_of(gb)).tolist(), want)
        # the bare outline (no allowance) holds North Dakota's border roads too
        bare = rfs.points_in_rings(xs, ys, rfs.rings_of(g)).tolist()
        self.assertTrue(all(b for b, name in zip(bare, places) if name.startswith("ND border road")))
        # the south line runs west to east and ends under the first point of the east side
        lons = [q[0] for q in rfs.BUILTIN_SOUTH]
        self.assertEqual(lons, sorted(lons))
        self.assertEqual((lons[0], lons[-1]), (rfs.BUILTIN_W, rfs.BUILTIN_EAST[0][0]))

    def test_points_in_rings_is_ogr_contains(self):
        g = rfs.builtin_outline()
        e = g.GetEnvelope()
        rnd = random.Random(2)
        xs = [rnd.uniform(e[0] - 20000, e[1] + 20000) for _ in range(1500)]
        ys = [rnd.uniform(e[2] - 20000, e[3] + 20000) for _ in range(1500)]
        got = rfs.points_in_rings(xs, ys, rfs.rings_of(g))
        for x, y, v in zip(xs, ys, got):
            p = ogr.Geometry(ogr.wkbPoint)
            p.AddPoint_2D(x, y)
            self.assertEqual(bool(v), bool(g.Contains(p)))
        # a polygon with a hole
        outer = rfs.box_geom((0, 0, 10, 10))
        hole = rfs.box_geom((4, 4, 6, 6))
        d = outer.Difference(hole)
        self.assertEqual(rfs.points_in_rings([1, 5, 11], [1, 5, 5], rfs.rings_of(d)).tolist(), [True, False, False])


@needs_gdal
class Terrain(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="rfs test ")           # a space in the path, as on the SSD

    def tearDown(self):
        shutil.rmtree(self.d)

    def _two_tiles(self, overviews):
        """Two 2 km tiles side by side with a 1 km gap of nothing between them; the east one is
        half NoData; a 6 x 6 m hole and a 20 x 20 m hole in the west one."""
        a = np.full((2000, 2000), 500.0)
        a[1000:1006, 1000:1006] = -999999.0                          # 36 m2: not an edge
        a[300:320, 300:320] = -999999.0                              # 400 m2: an edge
        b = np.full((2000, 2000), 510.0)
        b[:, 1000:] = -999999.0
        _tif(os.path.join(self.d, "w.tif"), a, 360000.0, 5162000.0, overviews=overviews)
        _tif(os.path.join(self.d, "e.tif"), b, 363000.0, 5162000.0, overviews=overviews)
        vrt = os.path.join(self.d, "two tiles.vrt")
        gdal.BuildVRT(vrt, [os.path.join(self.d, "w.tif"), os.path.join(self.d, "e.tif")])
        return rfs.Dem(vrt)

    def test_where_the_terrain_is(self):
        for ovr in (True, False):
            dem = self._two_tiles(ovr)
            self.assertEqual(len(dem.rects), 2)
            self.assertEqual(bool(dem.overviews), ovr)
            self.assertTrue(dem.has_source((0, 0, 2000, 2000)) and dem.has_data((0, 0, 2000, 2000)))
            self.assertFalse(dem.has_source((2100, 0, 800, 2000)))               # the gap: no source file
            self.assertTrue(dem.has_source((3000, 0, 2000, 2000)))
            self.assertTrue(dem.has_data((3000, 0, 1000, 2000)))                 # the east tile's west half
            # its NoData half: a source file, no terrain. (Started 100 m in: read from an overview,
            # GDAL can take in the cell beside the window, so a window that touches terrain may
            # read as having some. That errs on the side of running the tile.)
            self.assertFalse(dem.has_data((4100, 0, 900, 2000)))
            self.assertTrue(dem.has_data((3900, 0, 1100, 2000)))                 # a 100 m strip of terrain is found

    def test_share_of_terrain_is_of_the_tile_not_of_its_window(self):
        """A tile half off the raster has a full window (the window is cut to the raster) and half
        a share: the time estimate must count it as half a tile."""
        for ovr in (True, False):
            dem = self._two_tiles(ovr)
            x, y = dem.from_dem.TransformPoint(361000.0, 5161000.0)[:2]      # the middle of the west tile
            inner = (x - 500, y - 500, x + 500, y + 500)                     # 1 km, all terrain
            has, share = dem.terrain(dem.window(inner), inner)
            self.assertTrue(has)
            self.assertGreater(share, 0.9)
            x, y = dem.from_dem.TransformPoint(360000.0, 5161000.0)[:2]      # on the raster's west edge
            half = (x - 500, y - 500, x + 500, y + 500)
            has, share = dem.terrain(dem.window(half), half)
            self.assertTrue(has)
            self.assertTrue(0.35 < share < 0.65, share)
            self.assertGreater(dem.terrain(dem.window(half))[1], 0.9)        # the window alone: full
            x, y = dem.from_dem.TransformPoint(364500.0, 5161000.0)[:2]      # the east tile's NoData half
            empty = (x - 300, y - 300, x + 300, y + 300)
            self.assertEqual(dem.terrain(dem.window(empty), empty), (False, 0.0))

    def test_terrain_that_cannot_be_read_is_told_apart(self):
        """A VRT whose source file is gone: not 'with terrain', not 'no terrain' - unreadable, with
        GDAL's reason, and nothing printed by GDAL itself."""
        dem = self._two_tiles(True)
        os.remove(os.path.join(self.d, "w.tif"))
        dem = rfs.Dem(dem.path)
        has, share = dem.terrain((0, 0, 2000, 2000))
        self.assertIsNone(has)
        self.assertIn("w.tif", dem.read_error)
        self.assertTrue(dem.has_data((0, 0, 2000, 2000)))                      # the old question: let the tile run
        self.assertEqual(dem.terrain((3000, 0, 1000, 2000))[0], True)          # the other source file is still there
        # the plan, on two source files 20 km apart with one of them gone: the tiles under the lost
        # file are 'unreadable' (still to run, not remembered), the others as before
        far = os.path.join(self.d, "far")
        os.makedirs(far)
        for name, x0 in (("w.tif", 360000.0), ("e.tif", 380000.0)):
            _tif(os.path.join(far, name), np.full((2000, 2000), 500.0), x0, 5162000.0, overviews=True)
        vrt = os.path.join(far, "far apart.vrt")
        gdal.BuildVRT(vrt, [os.path.join(far, "w.tif"), os.path.join(far, "e.tif")])
        os.remove(os.path.join(far, "w.tif"))
        dem = rfs.Dem(vrt)
        out = os.path.join(self.d, "out")
        os.makedirs(out)
        a = types.SimpleNamespace(tile_km=5)
        plan, shares = rfs.make_plan(a, dem, out, {"k": 1}, None, None, lambda m: None, cache_ok=True)
        states = [st for _t, st in plan]
        self.assertIn("unreadable", states)
        self.assertIn("todo", states)
        self.assertIn("nosource", states)
        self.assertIn("w.tif", dem.read_error)
        cache = rfs.read_json(os.path.join(out, "plan_cache.json"))["share"]
        for t, st in plan:
            self.assertEqual(t.id in cache, st in ("todo", "nodata"), (t.id, st))
            if st == "unreadable":
                self.assertIsNone(shares[t.id])
            elif st == "todo":
                self.assertTrue(0.0 < shares[t.id] <= 1.0)

    def test_plan_cache_of_script_1_0_is_read(self):
        dem = self._two_tiles(True)
        out = os.path.join(self.d, "out")
        os.makedirs(out)
        a = types.SimpleNamespace(tile_km=5)
        plan, shares = rfs.make_plan(a, dem, out, {"k": 1}, None, None, lambda m: None, cache_ok=True)
        new = rfs.read_json(os.path.join(out, "plan_cache.json"))
        # as script 1.0 wrote it: yes / no per tile, no shares
        rfs.write_json(os.path.join(out, "plan_cache.json"),
                       {"stamp": new["stamp"], "terrain": {k: v > 0 for k, v in new["share"].items()}})
        plan2, shares2 = rfs.make_plan(a, dem, out, {"k": 1}, None, None, lambda m: None, cache_ok=True)
        self.assertEqual(plan, plan2)
        self.assertEqual(shares, shares2)
        self.assertEqual(rfs.read_json(os.path.join(out, "plan_cache.json")), new)

    def test_window(self):
        dem = self._two_tiles(False)
        x, y = dem.from_dem.TransformPoint(361000.0, 5161000.0)[:2]
        w = dem.window((x - 100, y - 100, x + 100, y + 100))
        self.assertTrue(880 <= w[0] <= 900 and 880 <= w[1] <= 900 and 200 <= w[2] <= 240 and 200 <= w[3] <= 240)
        self.assertIsNone(dem.window((x + 50000, y, x + 51000, y + 1000)))
        big = dem.window((x - 100, y - 100, x + 100, y + 100), 50000.0)          # cut to the raster
        self.assertEqual(big, (0, 0, dem.nx, dem.ny))

    def test_edge_distance(self):
        dem = self._two_tiles(False)

        def at(col, row):                                    # the centre of a cell of the west tile, in EPSG:5070
            return dem.from_dem.TransformPoint(360000.0 + col + 0.5, 5162000.0 - row - 0.5)[:2]
        self.assertIsNone(dem.edge_m(*at(1000, 1500)))                          # the 36 m2 hole 500 m away, rim 500 m
        self.assertIsNone(dem.edge_m(*at(1000, 1010)))                          # beside the 36 m2 hole: ignored
        self.assertAlmostEqual(dem.edge_m(*at(400, 310)), 81.0, delta=1.5)      # 81 cells east of the 400 m2 hole
        self.assertAlmostEqual(dem.edge_m(*at(100, 1000)), 101.0, delta=1.5)    # the west end of the raster
        self.assertAlmostEqual(dem.edge_m(*at(1900, 1000)), 100.0, delta=1.5)   # the gap between the tiles (no source)
        self.assertIsNone(dem.edge_m(*at(1700, 1000)))                          # 300 m from it: beyond the search
        x, y = dem.from_dem.TransformPoint(363900.5, 5161000.5)[:2]             # east tile, 100 m from its NoData half
        self.assertAlmostEqual(dem.edge_m(x, y), 100.0, delta=1.5)

    def test_extract_keeps_order_ids_and_fields(self):
        if _plugins() is None:
            self.skipTest("DamSafety_Finder not found")
        finder = rfs.load_engine(_plugins())
        src = os.path.join(self.d, "lines src.gpkg")
        ds = ogr.GetDriverByName("GPKG").CreateDataSource(src)
        s = osr.SpatialReference()
        s.ImportFromEPSG(4326)
        s.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        lyr = ds.CreateLayer("flow", s, ogr.wkbMultiLineString)
        lyr.CreateField(ogr.FieldDefn("gnis_name", ogr.OFTString))
        lyr.CreateField(ogr.FieldDefn("status", ogr.OFTString))
        rnd = random.Random(3)
        for i in range(400):
            lon, lat = rnd.uniform(-101.6, -99.9), rnd.uniform(46.0, 47.2)
            g = ogr.Geometry(ogr.wkbMultiLineString)
            for _k in range(rnd.randint(1, 3)):
                ln = ogr.Geometry(ogr.wkbLineString)
                ln.AddPoint_2D(lon, lat)
                lon, lat = lon + rnd.uniform(-0.02, 0.02), lat + rnd.uniform(-0.02, 0.02)
                ln.AddPoint_2D(lon, lat)
                g.AddGeometry(ln)
            f = ogr.Feature(lyr.GetLayerDefn())
            f.SetGeometry(g)
            f.SetField("gnis_name", "creek {}".format(i))
            if i % 3:
                f.SetField("status", "Intact")
            lyr.CreateFeature(f)
        ds = None
        box = (-370000.0, 2620000.0, -360000.0, 2630000.0)
        dst = os.path.join(self.d, "lines cut.gpkg")
        n = rfs.extract(finder, src, dst, box)
        self.assertTrue(0 < n < 400)
        # what the engine reads for the tile is the same from the whole layer and from the extract
        self.assertEqual(finder.read_lines(src, box), finder.read_lines(dst, box))
        pad = rfs.grow(box, rfs.ENGINE_PAD_M)
        whole = [ln for ln in finder.read_lines(src, None) if finder._meets(ln, pad)]
        cut = [ln for ln in finder.read_lines(dst, None) if finder._meets(ln, pad)]
        self.assertEqual(whole, cut)
        self.assertTrue(len(cut) > 0)
        a, b = ogr.Open(src), ogr.Open(dst)
        la, lb = a.GetLayer(0), b.GetLayer(0)
        self.assertEqual(la.GetName(), lb.GetName())
        fids = [f.GetFID() for f in lb]
        self.assertEqual(fids, sorted(fids))
        for fid in fids[:25]:
            fa, fb = la.GetFeature(fid), lb.GetFeature(fid)
            self.assertEqual(fa.items(), fb.items())
            self.assertEqual(fa.GetGeometryRef().ExportToWkb(), fb.GetGeometryRef().ExportToWkb())


@needs_gdal
class Seams(unittest.TestCase):
    """The merge's rules across tile seams, on two made-up tiles that meet at x = -360000."""

    def setUp(self):
        if _plugins() is None:
            self.skipTest("DamSafety_Finder not found")
        self.finder = rfs.load_engine(_plugins())
        self.d = tempfile.mkdtemp(prefix="rfs seam ")

    def tearDown(self):
        shutil.rmtree(self.d)

    def _tile(self, tid, fills, key=None, finder_version=None, edge_ft=None, edge_m=None, out=None):
        """fills: [(x, y, height_ft, spill (x, y) or None, spill_ft, outlet_dist_ft, line_source)].
        edge_ft: a list, one per fill - the tile is then written as a Finder that measures edge_ft
        itself would (0.7.0 on); edge_m: what this script measured (done.json), one per fill."""
        f = self.finder
        out = out or self.d
        os.makedirs(os.path.join(out, "tiles", tid))
        rows, spills = [], []
        ll = rfs.transformer(rfs.srs_of(5070), rfs.srs_of(4326))
        for i, (x, y, h, sp, sft, od, src) in enumerate(fills):
            fid = f.find_id("FIND", x, y)
            lon, lat = ll.TransformPoint(x, y)[:2]
            rows.append({"x": x, "y": y, "find_id": fid, "kind": "dam", "status": "new", "height_ft": h,
                         "spill_ft": sft, "outlet_dist_ft": od, "spill_dist_ft": od, "line_source": src,
                         "storage_af": 1.5, "same_pool": 0, "notes": "a note", "lat": round(lat, 6),
                         "lon": round(lon, 6)})
            if edge_ft is not None:
                rows[-1]["edge_ft"] = edge_ft[i]
            if sp:
                spills.append({"x": sp[0], "y": sp[1], "find_id": fid, "kind": "overtop"})
        g = os.path.join(out, "tiles", tid, tid + ".gpkg")
        f.write_points(g, "embankments", f.EMB_FIELDS + ([("edge_ft", "float")] if edge_ft is not None else []), rows)
        f.write_points(g, "spillways", f.SPILL_FIELDS, spills)
        f.write_points(g, "ridges", f.RIDGE_FIELDS, [])
        edge = [[r["find_id"], None if edge_m is None else edge_m[i]] for i, r in enumerate(rows)]
        if edge_ft is not None:
            edge = rfs.edge_distances(rows, None)[0]
        rfs.write_json(os.path.join(out, "tiles", tid, "done.json"),
                       {"id": tid, "key": key or {"k": 1}, "fills": len(rows), "seconds": 1.0, "peak_rss_mb": 1.0,
                        "finder": finder_version or f.VERSION, "edge_m": edge})
        return [r["find_id"] for r in rows]

    def _merged(self, layer="embankments", out=None):
        ds = ogr.Open(os.path.join(out or self.d, rfs.OUT_GPKG))
        lyr = ds.GetLayerByName(layer)
        return [f.items() for f in lyr]

    def _csv(self, name, out=None):
        with open(os.path.join(out or self.d, name), newline="", encoding="utf-8") as fh:
            return list(csv.DictReader(fh))

    def _model(self):
        return os.path.join(_plugins(), rfs.ENGINE, "models", rfs.MODEL_NAME)

    def test_rules_across_a_seam(self):
        S = self.finder.LINE_STREAM
        west = self._tile("T10_W0370_N2620", [
            (-360010.0, 2625000.0, 12.0, (-360020.0, 2625030.0), 1600.00, 30.0, S),   # 0: 25 m from east fill 0, higher
            (-360005.0, 2626000.0, 9.0, (-360001.0, 2626050.0), 1650.00, 80.0, S),    # 1: shares a pool with east fill 1
            (-365000.0, 2623000.0, 7.0, None, 1700.0, None, S),                       # 2: far from any seam
            (-365000.0, 2623030.0, 6.0, None, 1700.0, None, S),                       # 3: 30 m from 2, the SAME tile
        ])
        east = self._tile("T10_W0360_N2620", [
            (-359990.0, 2625015.0, 10.0, (-359900.0, 2625100.0), 1601.00, 20.0, S),   # 0: the lower of the pair: goes
            (-359960.0, 2626090.0, 8.0, (-359999.0, 2626052.0), 1650.01, 15.0, S),    # 1: nearer the outlet: stays
            (-355000.0, 2628000.0, 20.0, None, 1500.0, None, S),                      # 2: untouched
        ])
        tiles, _other = rfs.finished_tiles(self.d, {"k": 1})
        self.assertEqual([t for t, _m in tiles], ["T10_W0360_N2620", "T10_W0370_N2620"])
        msgs = []
        drop, same, n = rfs.cross_tile_rules(self.finder, tiles, self.d, msgs.append)
        self.assertEqual(n, 7)
        # tiles are in id order: index 0 = the east tile, 1 = the west tile
        self.assertEqual(drop, {(0, 0), (1, 1)})
        self.assertEqual(same, {(0, 1): 1})                   # the kept fill counts the hump dropped on its pool
        self.assertIn("1 fill(s) on a pool", msgs[0])
        self.assertIn("1 within 60 m", msgs[0])
        self.assertTrue(west and east)

    def test_merge_writes_what_is_left(self):
        S = self.finder.LINE_STREAM
        self._tile("T10_W0370_N2620", [(-360010.0, 2625000.0, 12.0, None, 1600.0, None, S),
                                       (-365000.0, 2623000.0, 7.0, None, 1700.0, None, S)])
        self._tile("T10_W0360_N2620", [(-359990.0, 2625015.0, 10.0, None, 1601.0, None, S)])
        a = rfs.parser().parse_args(["--no-score", "--no-checksums"])
        out = []
        rfs.merge(a, self.finder, self.d, {"k": 1}, "/no/such/dem.vrt", None, "none", {}, {}, "", out.append)
        ds = ogr.Open(os.path.join(self.d, rfs.OUT_GPKG))
        ids = sorted(f.GetField("find_id") for f in ds.GetLayerByName("embankments"))
        self.assertEqual(len(ids), 2)
        self.assertEqual(ds.GetLayerByName("candidates").GetFeatureCount(), 2)
        heights = [f.GetField("height_ft") for f in ds.GetLayerByName("candidates")]
        self.assertEqual(heights, [12.0, 7.0])                # --no-score: ranked by height
        info = dict((f.GetField("key"), f.GetField("value")) for f in ds.GetLayerByName("run_info"))
        self.assertEqual(info["fills_removed_across_seams"], "1")
        self.assertTrue(os.path.isfile(os.path.join(self.d, rfs.OUT_CSV)))
        self.assertTrue(os.path.isfile(os.path.join(self.d, rfs.OUT_SUMMARY)))
        self.assertEqual(info["finder_version"], self.finder.VERSION)
        with open(os.path.join(self.d, rfs.OUT_SUMMARY), encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("Finder version {}".format(self.finder.VERSION), text)
        self.assertIn("find_id:", text)
        self.assertEqual(set(r["excluded"] for r in self._merged()), {"no"})   # no --exclude-polygons: all 'no'
        self.assertEqual(os.listdir(os.path.join(self.d, "work")), [])          # no part file left behind

    def _five(self):
        """One tile of five fills far from each other, of different heights."""
        S = self.finder.LINE_STREAM
        return self._tile("T10_W0370_N2620", [(-369000.0 + 1500.0 * i, 2621000.0 + 1500.0 * i, 4.0 + 3.0 * i, None,
                                               1600.0, None, S) for i in range(5)])

    def test_short_list_and_score_count(self):
        ids = self._five()
        a = rfs.parser().parse_args(["--no-checksums", "--top", "3"])
        out = []
        rfs.merge(a, self.finder, self.d, {"k": 1}, "/no/such/dem.vrt", None, "none", {}, {}, self._model(), out.append)
        ranked, top = self._csv(rfs.OUT_CSV), self._csv(rfs.OUT_TOP)
        self.assertEqual(len(ranked), 5)                      # the full list is kept whole
        self.assertEqual(len(top), 3)                         # the short list: the first --top rows of it
        self.assertEqual(list(top[0].keys()), rfs.TOP_COLUMNS)
        for r, t in zip(ranked, top):
            for c in ("rank", "score", "kind", "status", "height_ft", "storage_af", "lat", "lon", "tile", "find_id"):
                self.assertEqual(r[c], t[c])
            self.assertEqual(t["link"], rfs.map_link(float(t["lat"]), float(t["lon"])))
            self.assertIn("basemap=satellite", t["link"])
            self.assertTrue(46.0 < float(t["lat"]) < 47.0 and -101.5 < float(t["lon"]) < -100.0)
        scores = [float(r["score"]) for r in ranked]
        self.assertEqual(scores, sorted(scores, reverse=True))
        self.assertEqual(sorted(r["find_id"] for r in ranked), sorted(ids))
        # the summary and run_info count the candidates at 0.5 or more, as the plugin's own score has them
        from importlib import import_module
        sc = import_module(rfs.ENGINE + ".core.score")
        rows = self._merged()
        want = sum(1 for s_ in sc.score(sc.load(self._model()), rows) if round(float(s_), 3) >= 0.5)
        info = dict((r["key"], r["value"]) for r in self._merged("run_info"))
        self.assertEqual(info["candidates_score_0.5_or_more"], str(want))
        self.assertEqual(info["short_list_rows"], "3")
        self.assertIn("  with a score of 0.5 or more: {:,}".format(want), out)
        self.assertTrue(any(rfs.OUT_TOP in ln and "top 3" in ln for ln in out))
        # --top 0: no short list, and the one of an earlier merge is taken away
        a = rfs.parser().parse_args(["--no-checksums", "--top", "0"])
        rfs.merge(a, self.finder, self.d, {"k": 1}, "/no/such/dem.vrt", None, "none", {}, {}, self._model(), out.append)
        self.assertFalse(os.path.exists(os.path.join(self.d, rfs.OUT_TOP)))
        self.assertEqual(len(self._csv(rfs.OUT_CSV)), 5)
        # the default is 2,000 rows
        self.assertEqual(rfs.parser().parse_args([]).top, 2000)

    def test_exclude_polygons_hook(self):
        """--exclude-polygons FILE: a fill inside a polygon stays in 'embankments' with excluded =
        yes and is in none of the ranked lists; the others are untouched."""
        ids = self._five()
        # two polygons (lon / lat, another CRS than the fills): round fill 1 and round fill 3
        ll = rfs.transformer(rfs.srs_of(5070), rfs.srs_of(4326))
        path = os.path.join(self.d, "mine spoil.geojson")
        feats = []
        for i in (1, 3):
            x, y = -369000.0 + 1500.0 * i, 2621000.0 + 1500.0 * i
            ring = [list(ll.TransformPoint(x + dx, y + dy)[:2]) for dx, dy in
                    ((-200, -200), (200, -200), (200, 200), (-200, 200), (-200, -200))]
            feats.append({"type": "Feature", "properties": {"what": "spoil"},
                          "geometry": {"type": "Polygon", "coordinates": [ring]}})
        with open(path, "w") as fh:
            json.dump({"type": "FeatureCollection", "features": feats}, fh)
        a = rfs.parser().parse_args(["--no-checksums", "--exclude-polygons", path])
        geom, what = rfs.exclude_areas(a)
        self.assertIn("mine spoil.geojson", what)
        out = []
        rfs.merge(a, self.finder, self.d, {"k": 1}, "/no/such/dem.vrt", None, "none", {}, {}, self._model(), out.append,
                  exclude=(geom, what))
        emb = {r["find_id"]: r["excluded"] for r in self._merged()}
        self.assertEqual(len(emb), 5)                         # every fill is still in the full layer
        self.assertEqual([emb[i] for i in ids], ["no", "yes", "no", "yes", "no"])
        keep = sorted(ids[i] for i in (0, 2, 4))
        self.assertEqual(sorted(r["find_id"] for r in self._merged("candidates")), keep)
        self.assertEqual(sorted(r["find_id"] for r in self._csv(rfs.OUT_CSV)), keep)
        self.assertEqual(sorted(r["find_id"] for r in self._csv(rfs.OUT_TOP)), keep)
        info = dict((r["key"], r["value"]) for r in self._merged("run_info"))
        self.assertEqual(info["fills_excluded"], "2")
        self.assertEqual(info["candidates_ranked"], "3")
        self.assertIn("mine spoil.geojson", info["exclude_polygons"])
        self.assertTrue(any("excluded" in ln and ln.rstrip().endswith(" 2") for ln in out))
        # a wrong file name, or a file with no polygon, is said before anything runs
        with self.assertRaises(rfs.UserError):
            rfs.exclude_areas(rfs.parser().parse_args(["--exclude-polygons", os.path.join(self.d, "nope.gpkg")]))
        pts = os.path.join(self.d, "points.geojson")
        with open(pts, "w") as fh:
            json.dump({"type": "FeatureCollection", "features": [
                {"type": "Feature", "properties": {}, "geometry": {"type": "Point", "coordinates": [-100.7, 46.5]}}]}, fh)
        with self.assertRaises(rfs.UserError):
            rfs.exclude_areas(rfs.parser().parse_args(["--exclude-polygons", pts]))
        self.assertEqual(rfs.exclude_areas(rfs.parser().parse_args([])), (None, "none"))

    def test_tiles_of_two_finder_versions_are_never_merged_together(self):
        S = self.finder.LINE_STREAM
        now_key = {"finder": self.finder.VERSION, "tile_km": 10}
        old_key = {"finder": "0.5.9", "tile_km": 10}
        self._tile("T10_W0370_N2620", [(-365000.0, 2623000.0, 7.0, None, 1700.0, None, S)], key=now_key)
        self._tile("T10_W0360_N2620", [(-355000.0, 2623000.0, 9.0, None, 1700.0, None, S)], key=old_key,
                   finder_version="0.5.9")
        good, other = rfs.finished_tiles(self.d, now_key)
        self.assertEqual(([t for t, _m in good], [t for t, _m in other]), (["T10_W0370_N2620"], ["T10_W0360_N2620"]))
        self.assertEqual(rfs.other_versions(other), "Finder 0.5.9: 1 tile(s)")
        a = rfs.parser().parse_args(["--no-score", "--no-checksums"])
        out = []
        rfs.merge(a, self.finder, self.d, now_key, "/no/such/dem.vrt", None, "none", {}, {}, "", out.append)
        self.assertEqual(set(r["tile"] for r in self._merged()), {"T10_W0370_N2620"})   # the old tile is left out
        text = "\n".join(out)
        self.assertIn("Finder version {}".format(self.finder.VERSION), text)
        self.assertIn("left out, made with other settings", text)
        self.assertIn("Finder 0.5.9: 1 tile(s)", text)
        # asked for with the old version's key, the merge refuses: this Finder did not make that tile
        with self.assertRaises(rfs.UserError) as e:
            rfs.merge(a, self.finder, self.d, old_key, "/no/such/dem.vrt", None, "none", {}, {}, "", out.append)
        self.assertIn("never merged", str(e.exception))
        # a done.json changed by hand (the key of this run, another Finder inside): refused too
        m = rfs.read_json(os.path.join(self.d, "tiles", "T10_W0360_N2620", "done.json"))
        m["key"] = now_key
        rfs.write_json(os.path.join(self.d, "tiles", "T10_W0360_N2620", "done.json"), m)
        with self.assertRaises(rfs.UserError) as e:
            rfs.merge(a, self.finder, self.d, now_key, "/no/such/dem.vrt", None, "none", {}, {}, "", out.append)
        self.assertIn("0.5.9", str(e.exception))
        # only tiles of another version: nothing to merge, and it says which version they are
        m["key"] = old_key
        rfs.write_json(os.path.join(self.d, "tiles", "T10_W0360_N2620", "done.json"), m)
        shutil.rmtree(os.path.join(self.d, "tiles", "T10_W0370_N2620"))
        with self.assertRaises(rfs.UserError) as e:
            rfs.merge(a, self.finder, self.d, now_key, "/no/such/dem.vrt", None, "none", {}, {}, "", out.append)
        self.assertIn("Finder 0.5.9: 1 tile(s)", str(e.exception))

    def test_edge_ft_of_the_engine_is_used_when_the_rows_carry_it(self):
        """Finder 0.7.0 is to write edge_ft itself. A Finder whose fields hold edge_ft: the merged
        layer has ONE edge_ft, with the engine's figures, and at_data_edge follows them. A Finder
        without it (0.6.0): the figures this script measured for the tile (done.json)."""
        S = self.finder.LINE_STREAM
        fills = [(-365000.0, 2623000.0, 7.0, None, 1700.0, None, S), (-364000.0, 2624000.0, 9.0, None, 1700.0, None, S),
                 (-363000.0, 2625000.0, 5.0, None, 1700.0, None, S)]
        a = rfs.parser().parse_args(["--no-score", "--no-checksums"])
        # as 0.6.0: this script's own distances (m): 30 m is at the edge, 190 m is not (the limit is 150 m)
        ids = self._tile("T10_W0370_N2620", fills, edge_m=[30.0, 190.0, None])
        rfs.merge(a, self.finder, self.d, {"k": 1}, "/no/such/dem.vrt", None, "none", {}, {}, "", lambda m: None)
        got = {r["find_id"]: (r["edge_ft"], r["at_data_edge"]) for r in self._merged()}
        self.assertEqual([got[i] for i in ids], [(98.0, "yes"), (623.0, "no"), (None, "no")])
        # as 0.7.0: the engine's rows carry edge_ft (ft); what done.json holds is the same figures
        d2 = os.path.join(self.d, "new finder")
        f7 = types.SimpleNamespace(**{k: getattr(self.finder, k) for k in dir(self.finder) if not k.startswith("__")})
        f7.EMB_FIELDS = list(self.finder.EMB_FIELDS) + [("edge_ft", "float")]
        ids = self._tile("T10_W0370_N2620", fills, edge_ft=[120.0, 600.0, None], out=d2)
        rfs.merge(a, f7, d2, {"k": 1}, "/no/such/dem.vrt", None, "none", {}, {}, "", lambda m: None)
        ds = ogr.Open(os.path.join(d2, rfs.OUT_GPKG))
        names = rfs.field_names(ds.GetLayerByName("embankments"))
        self.assertEqual(names.count("edge_ft"), 1)           # not added a second time
        self.assertEqual(len(names), len(set(names)))
        ds = None
        got = {r["find_id"]: (r["edge_ft"], r["at_data_edge"]) for r in self._merged(out=d2)}
        self.assertEqual([got[i] for i in ids], [(120.0, "yes"), (600.0, "no"), (None, "no")])
        self.assertEqual(len(self._merged("candidates", out=d2)), 2)           # the one at the edge is not ranked

    def test_merge_only_without_the_terrain_file(self):
        """--merge-only with the terrain drive unplugged: the settings come from the finished
        tiles, as long as they are of this Finder and of one terrain raster."""
        S = self.finder.LINE_STREAM
        a = types.SimpleNamespace(tile_km=10)
        dem = types.SimpleNamespace(path="/Volumes/Extreme SSD/ND_DEM_1m/ND_1m_26914.vrt", nx=10, ny=20,
                                    gt=(0.0, 1.0, 0.0, 0.0, 0.0, -1.0), epsg="26914")
        key = rfs.settings_key(self.finder.VERSION, a, dem)
        with self.assertRaises(rfs.UserError):                # nothing finished: nothing to go by
            rfs.key_without_terrain(self.d, self.finder.VERSION, a)
        self._tile("T10_W0370_N2620", [(-365000.0, 2623000.0, 7.0, None, 1700.0, None, S)], key=key)
        self._tile("T10_W0360_N2620", [(-355000.0, 2623000.0, 9.0, None, 1700.0, None, S)],
                   key=rfs.settings_key("0.5.9", a, dem), finder_version="0.5.9")
        got, n = rfs.key_without_terrain(self.d, self.finder.VERSION, a)
        self.assertEqual((got, n), (key, 1))                  # the tile of the other Finder does not count
        with self.assertRaises(rfs.UserError):                # another tile size: none of these tiles
            rfs.key_without_terrain(self.d, self.finder.VERSION, types.SimpleNamespace(tile_km=5))
        dem2 = types.SimpleNamespace(path="/x/other.vrt", nx=11, ny=20, gt=dem.gt, epsg="26914")
        self._tile("T10_W0350_N2620", [(-345000.0, 2623000.0, 9.0, None, 1700.0, None, S)],
                   key=rfs.settings_key(self.finder.VERSION, a, dem2))
        with self.assertRaises(rfs.UserError) as e:           # two terrain rasters: the drive must say which
            rfs.key_without_terrain(self.d, self.finder.VERSION, a)
        self.assertIn("2 different terrain rasters", str(e.exception))


@needs_gdal
class WholeRun(unittest.TestCase):
    """main() from the command line down, on a made-up pack and made-up finished tiles (no tile is
    run: that needs real terrain and minutes)."""

    def setUp(self):
        if _plugins() is None:
            self.skipTest("DamSafety_Finder not found")
        self.finder = rfs.load_engine(_plugins())
        self.d = tempfile.mkdtemp(prefix="rfs run ")
        self.pack = os.path.join(self.d, "ND_Suite_Data", "ND_Dam_Suite_Pack")
        s = osr.SpatialReference()
        s.ImportFromEPSG(4326)
        for rel in [rfs.STREAMS, rfs.ROADS, rfs.RAILS] + [k[0] for k in rfs.KNOWN]:
            p = os.path.join(self.pack, *rel.split("/"))
            os.makedirs(os.path.dirname(p), exist_ok=True)
            ds = ogr.GetDriverByName("GPKG").CreateDataSource(p)
            ds.CreateLayer("layer", s, ogr.wkbPoint)
            ds = None
        self.out = os.path.join(self.d, "ND Finder Statewide")
        self.base = ["--data", os.path.join(self.d, "ND_Suite_Data"), "--plugins", _plugins(), "--no-checksums"]

    def tearDown(self):
        shutil.rmtree(self.d)

    def _main(self, args):
        so, se = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(so), contextlib.redirect_stderr(se):
            rc = rfs.main(self.base + args)
        return rc, so.getvalue(), se.getvalue()

    def _dem(self):
        """1 km of flat made-up terrain near Bismarck, as a VRT in a folder with a space."""
        folder = os.path.join(self.d, "terrain drive")
        os.makedirs(folder)
        _tif(os.path.join(folder, "a.tif"), np.full((1000, 1000), 500.0), 360000.0, 5162000.0, overviews=True)
        vrt = os.path.join(folder, "ND test.vrt")
        gdal.BuildVRT(vrt, [os.path.join(folder, "a.tif")])
        return vrt

    def test_out_inside_the_data_folder_is_refused_also_under_another_spelling(self):
        vrt = self._dem()
        data = os.path.join(self.d, "ND_Suite_Data")
        for bad in (os.path.join(data, "out"), os.path.join(self.pack, "x", "y"), os.path.join(os.path.dirname(vrt), "out")):
            rc, _so, se = self._main(["--dem", vrt, "--out", bad, "--dry-run"])
            self.assertEqual(rc, 2, bad)
            self.assertIn("nothing is ever written there", se)
        # a disk that ignores case (a Mac's): the same folder under a lower-case spelling. On this
        # disk the two spellings are two folders, so os.path.samefile is given such a disk's answer.
        low = os.path.join(self.d, "nd_suite_data", "out")
        real_same = os.path.samefile

        def samefile(p, q):
            if os.path.exists(p) and os.path.exists(q):
                return real_same(p, q)
            if p.lower() == q.lower():
                return True
            raise FileNotFoundError(p)
        rfs.os.path.samefile = samefile
        try:
            rc, _so, se = self._main(["--dem", vrt, "--out", low, "--dry-run"])
            self.assertEqual(rc, 2)
            self.assertIn("nothing is ever written there", se)
            rc, _so, se = self._main(["--dem", vrt, "--out", low, "--merge-only"])
            self.assertEqual(rc, 2)
        finally:
            rfs.os.path.samefile = real_same
        self.assertFalse(os.path.exists(os.path.join(self.d, "nd_suite_data")))
        self.assertEqual(sorted(os.listdir(data)), ["ND_Dam_Suite_Pack"])

    def test_dry_run_and_list_write_nothing(self):
        vrt = self._dem()
        before = sorted(os.listdir(self.d))
        for opt in ("--dry-run", "--list"):
            rc, so, _se = self._main(["--dem", vrt, "--out", self.out, opt, "--state-outline", "none"])
            self.assertEqual(rc, 0, so)
            self.assertIn("Nothing was run and nothing was written", so)
            self.assertIn("NOT timed on this computer", so)   # no finished full tile: the test box's figure, said so
            self.assertIn("0 so far", so)
        self.assertFalse(os.path.exists(self.out))
        self.assertEqual(sorted(os.listdir(self.d)), before)
        # a wrong --exclude-polygons is said at once, also in a dry run
        rc, _so, se = self._main(["--dem", vrt, "--out", self.out, "--dry-run", "--exclude-polygons", "/no/such.gpkg"])
        self.assertEqual(rc, 2)
        self.assertIn("--exclude-polygons", se)
        self.assertFalse(os.path.exists(self.out))

    def test_terrain_file_gone_is_said_and_stops_a_run_before_it_starts(self):
        vrt = self._dem()
        os.remove(os.path.join(os.path.dirname(vrt), "a.tif"))             # the VRT is there, its tile is not
        for opt in ("--dry-run", "--list"):
            rc, so, _se = self._main(["--dem", vrt, "--out", self.out, opt, "--state-outline", "none"])
            self.assertEqual(rc, 0, so)
            self.assertIn("0 with terrain (0 finished, 0 to run), ", so)
            self.assertIn("whose terrain COULD NOT BE READ", so)
            self.assertIn("a.tif", so)                        # GDAL's reason, once
        self.assertFalse(os.path.exists(self.out))
        rc, _so, se = self._main(["--dem", vrt, "--out", self.out, "--state-outline", "none"])
        self.assertEqual(rc, 2)                               # a real run: stopped before any tile is tried
        self.assertIn("can be read", se)
        self.assertFalse(os.path.exists(os.path.join(self.out, "run.lock")))
        self.assertEqual(os.listdir(os.path.join(self.out, "tiles")), [])

    def test_merge_only_with_the_terrain_drive_unplugged(self):
        seams = Seams("test_rules_across_a_seam")
        seams.finder, seams.d = self.finder, self.out
        S = self.finder.LINE_STREAM
        a = types.SimpleNamespace(tile_km=10)
        dem = types.SimpleNamespace(path="/Volumes/Extreme SSD/ND_DEM_1m/ND_1m_26914.vrt", nx=10, ny=20,
                                    gt=(0.0, 1.0, 0.0, 0.0, 0.0, -1.0), epsg="26914")
        gone = os.path.join(self.d, "not connected", "ND_1m_26914.vrt")
        # no finished tile: it says both things - the terrain is not there, and there is nothing to merge
        rc, _so, se = self._main(["--dem", gone, "--out", self.out, "--merge-only", "--no-score"])
        self.assertEqual(rc, 2)
        self.assertIn("terrain file was not found", se)
        self.assertIn("no finished tile", se)
        self.assertFalse(os.path.exists(self.out))            # and nothing was made
        seams._tile("T10_W0370_N2620", [(-365000.0, 2623000.0, 7.0, None, 1700.0, None, S)],
                    key=rfs.settings_key(self.finder.VERSION, a, dem))
        seams._tile("T10_W0360_N2620", [(-355000.0, 2623000.0, 9.0, None, 1700.0, None, S)],
                    key=rfs.settings_key("0.5.9", a, dem), finder_version="0.5.9")
        rc, so, se = self._main(["--dem", gone, "--out", self.out, "--merge-only", "--no-score",
                                 "--state-outline", "none"])
        self.assertEqual(rc, 0, so + se)
        self.assertIn("terrain  NOT READ", so)
        self.assertIn("Finder 0.5.9: 1 tile(s)", so)          # the other version's tile: left out, and said
        rows = seams._merged()
        self.assertEqual([r["tile"] for r in rows], ["T10_W0370_N2620"])
        self.assertTrue(os.path.isfile(os.path.join(self.out, rfs.OUT_TOP)))
        self.assertFalse(os.path.exists(os.path.join(self.out, "run.lock")))
        # without --merge-only the missing terrain stops the run, as before
        rc, _so, se = self._main(["--dem", gone, "--out", self.out])
        self.assertEqual(rc, 2)
        self.assertIn("terrain file was not found", se)
        # and a dry run needs the terrain too
        rc, _so, se = self._main(["--dem", gone, "--out", self.out, "--merge-only", "--dry-run"])
        self.assertEqual(rc, 2)


if __name__ == "__main__":
    unittest.main()
