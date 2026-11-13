"""Tests of run_finder_statewide.py on small made-up data (seconds; no terrain, no pack needed).

    python3 -m unittest tools/mac/test_run_finder_statewide.py          (from the DWR_Plugins checkout)

The tests that call the engine's own one_per_pool / dedupe look for DamSafety_Finder in
$DAMSAFETY_PLUGINS, then two folders above this file (the checkout); they are skipped when it is
not there. Nothing is written outside a temporary folder.
"""
import importlib.util
import json
import os
import random
import shutil
import sys
import tempfile
import unittest
import warnings

sys.dont_write_bytecode = True
warnings.filterwarnings("ignore")
HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location("run_finder_statewide", os.path.join(HERE, "run_finder_statewide.py"))
rfs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rfs)

import numpy as np                                                      # noqa: E402
from osgeo import gdal, ogr, osr                                        # noqa: E402


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


class Plain(unittest.TestCase):
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
                  "Breckenridge MN": (-96.57, 46.26, False)}
        xs, ys, want = [], [], []
        for lon, lat, w in places.values():
            x, y = ct.TransformPoint(lon, lat)[:2]
            xs.append(x)
            ys.append(y)
            want.append(w)
        self.assertEqual(rfs.points_in_rings(xs, ys, rfs.rings_of(gb)).tolist(), want)

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


class Seams(unittest.TestCase):
    """The merge's rules across tile seams, on two made-up tiles that meet at x = -360000."""

    def setUp(self):
        if _plugins() is None:
            self.skipTest("DamSafety_Finder not found")
        self.finder = rfs.load_engine(_plugins())
        self.d = tempfile.mkdtemp(prefix="rfs seam ")

    def tearDown(self):
        shutil.rmtree(self.d)

    def _tile(self, tid, fills):
        """fills: [(x, y, height_ft, spill (x, y) or None, spill_ft, outlet_dist_ft, line_source)]"""
        f = self.finder
        os.makedirs(os.path.join(self.d, "tiles", tid))
        rows, spills = [], []
        for x, y, h, sp, sft, od, src in fills:
            fid = f.find_id("FIND", x, y)
            rows.append({"x": x, "y": y, "find_id": fid, "kind": "dam", "status": "new", "height_ft": h,
                         "spill_ft": sft, "outlet_dist_ft": od, "spill_dist_ft": od, "line_source": src,
                         "same_pool": 0, "notes": "a note", "lat": 46.5, "lon": -100.7})
            if sp:
                spills.append({"x": sp[0], "y": sp[1], "find_id": fid, "kind": "overtop"})
        g = os.path.join(self.d, "tiles", tid, tid + ".gpkg")
        f.write_points(g, "embankments", f.EMB_FIELDS, rows)
        f.write_points(g, "spillways", f.SPILL_FIELDS, spills)
        f.write_points(g, "ridges", f.RIDGE_FIELDS, [])
        rfs.write_json(os.path.join(self.d, "tiles", tid, "done.json"),
                       {"id": tid, "key": {"k": 1}, "fills": len(rows), "seconds": 1.0, "peak_rss_mb": 1.0,
                        "edge_m": [[r["find_id"], None] for r in rows]})
        return [r["find_id"] for r in rows]

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


if __name__ == "__main__":
    unittest.main()
