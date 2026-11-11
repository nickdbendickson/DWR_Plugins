"""Finder engine on synthetic terrain (numpy + scipy, no QGIS, no GDAL).

    python -m <plugin folder>.tests.test_core
"""
import math
import unittest

import numpy as np

from ..core import crossings as cx
from ..core import embankments as em
from ..core import listed as ls
from ..core import water as wt

CELL = 3.0
N = 400
SLOPE = 0.004


def valley():
    """A valley running north to south (rows grow downstream), 0.4 % fall, 2 % side slopes."""
    y, x = np.mgrid[0:N, 0:N]
    return 100.0 - y * CELL * SLOPE + np.abs(x - N // 2) * CELL * 0.02, y


def bed(row):
    return 100.0 - row * CELL * SLOPE


def add_fill(dem, y, row, height, half=3):
    return np.where(np.abs(y - row) <= half, np.maximum(dem, bed(row) + height), dem)


GT = (0.0, CELL, 0.0, N * CELL, 0.0, -CELL)
XS = (N // 2) * CELL + 1.5
LINE = [(XS, N * CELL - 1.0), (XS, 1.0)]


def scene():
    """A dam with a flat pool (row 200, 4 m), a dry embankment (row 260, 2.5 m) and a road fill
    (row 320, 2.5 m, on a road)."""
    dem, y = valley()
    dem = add_fill(dem, y, 200, 4.0)
    pool = bed(200) + 3.0
    dem = np.where((y < 197) & (dem < pool), pool, dem)
    dem = add_fill(dem, y, 260, 2.5)
    dem = add_fill(dem, y, 320, 2.5, half=2)
    road = np.zeros(dem.shape, bool)
    road[318:323, :] = True
    return dem, road


class Embankments(unittest.TestCase):
    def test_three_fills_three_kinds(self):
        dem, road = scene()
        got = sorted(em.find(dem, GT, [LINE], road_mask=road), key=lambda e: -e.y)
        self.assertEqual([e.kind for e in got], [em.K_DAM, em.K_DRY, em.K_ROAD])
        dam, dry, rd = got
        self.assertAlmostEqual(dam.height_m, 4.0, delta=0.3)
        self.assertGreaterEqual(dam.crest_height_m, dam.height_m)
        self.assertTrue(dam.pool)
        self.assertGreater(dam.storage_m3, 50 * em.AF_M3)
        self.assertAlmostEqual(dry.height_m, 2.5, delta=0.3)
        self.assertFalse(dry.pool)
        self.assertTrue(rd.on_road)
        self.assertAlmostEqual(dam.y, N * CELL - 200.5 * CELL, delta=4 * CELL)

    def test_road_fill_holding_water(self):
        dem, y = valley()
        dem = add_fill(dem, y, 250, 3.0, half=2)
        pool = bed(250) + 2.0
        dem = np.where((y < 248) & (dem < pool), pool, dem)
        road = np.zeros(dem.shape, bool)
        road[248:253, :] = True
        got = em.find(dem, GT, [LINE], road_mask=road)
        self.assertEqual([e.kind for e in got], [em.K_ROAD_POND])

    def test_line_direction_does_not_matter(self):
        dem, road = scene()
        a = em.find(dem, GT, [LINE], road_mask=road)
        b = em.find(dem, GT, [LINE[::-1]], road_mask=road)
        self.assertEqual(sorted(e.kind for e in a), sorted(e.kind for e in b))

    def test_bank_wander_is_not_a_fill(self):
        dem, _y = valley()
        wander = [(XS, N * CELL - 1.0), (XS, 700.0), (XS + 60.0, 650.0), (XS, 600.0), (XS, 1.0)]
        self.assertEqual(em.find(dem, GT, [wander]), [])

    def test_low_fill_left_out_and_threshold_is_yours(self):
        dem, y = valley()
        dem = add_fill(dem, y, 200, 1.0)
        self.assertEqual(em.find(dem, GT, [LINE]), [])
        p = em.FinderParams(min_height_m=0.6)
        self.assertEqual(len(em.find(dem, GT, [LINE], params=p)), 1)

    def test_two_lines_over_one_fill_are_one(self):
        dem, road = scene()
        got = em.find(dem, GT, [LINE, [(XS + 6.0, N * CELL - 1.0), (XS + 6.0, 1.0)]], road_mask=road)
        self.assertEqual(len(got), 3)

    def test_spill_level_is_the_low_end_of_the_fill(self):
        # a fill whose west end is 2 m lower: water gets round it there
        dem, y = valley()
        _yy, x = np.mgrid[0:N, 0:N]
        top = bed(200) + 4.0
        fill = (np.abs(y - 200) <= 3)
        dem = np.where(fill, np.maximum(dem, np.where(x < N // 2 - 20, top - 2.0, top)), dem)
        e = em.find(dem, GT, [LINE])[0]
        self.assertAlmostEqual(e.spill_m - e.toe_m, 2.0, delta=0.4)
        self.assertGreater(e.crest_m - e.spill_m, 1.0)

    def test_nodata_breaks_the_walk(self):
        dem, road = scene()
        dem[:, :] = np.nan
        self.assertEqual(em.find(dem, GT, [LINE]), [])


class Listed(unittest.TestCase):
    def test_status(self):
        kn = ls.Known([(0.0, 0.0, "ND00199", "NID"), (1000.0, 0.0, "2801", "DWR dams")])
        self.assertEqual(ls.check(30.0, 0.0, kn, 50.0)["status"], ls.LISTED)
        self.assertEqual(ls.check(80.0, 0.0, kn, 50.0)["status"], ls.UNSURE)
        r = ls.check(500.0, 0.0, kn, 50.0)
        self.assertEqual((r["status"], r["known_id"]), (ls.NEW, ""))
        r = ls.check(990.0, 0.0, kn, 50.0)
        self.assertEqual((r["known_id"], r["known_src"], r["known_m"]), ("2801", "DWR dams", 10.0))

    def test_point_anywhere_in_the_pond_is_listed(self):
        ring = [(0, 0), (800, 0), (800, 400), (0, 400)]
        kn = ls.Known([(750.0, 350.0, "DUG-7", "dugouts")])
        self.assertEqual(ls.check(50.0, 50.0, kn, 30.0, polygons=[[ring]])["status"], ls.LISTED)
        self.assertEqual(ls.check(50.0, 50.0, kn, 30.0)["status"], ls.NEW)

    def test_hole(self):
        outer = [(0, 0), (100, 0), (100, 100), (0, 100)]
        hole = [(40, 40), (60, 40), (60, 60), (40, 60)]
        self.assertTrue(ls.point_in_polygon(10, 10, [outer, hole]))
        self.assertFalse(ls.point_in_polygon(50, 50, [outer, hole]))

    def test_empty_list_is_all_new(self):
        self.assertEqual(ls.check(0, 0, ls.Known([]), 100.0)["status"], ls.NEW)


class Crossings(unittest.TestCase):
    def test_grid(self):
        roads = [[(0.0, y), (1000.0, y)] for y in (100.0, 500.0, 900.0)]
        streams = [[(x, 0.0), (x, 1000.0)] for x in (250.0, 750.0)]
        got = cx.find(roads, streams)
        self.assertEqual(len(got), 6)
        self.assertTrue(all(abs(g["angle"] - 90.0) < 1e-6 for g in got))

    def test_divided_road_is_one(self):
        roads = [[(0.0, 100.0), (1000.0, 100.0)], [(0.0, 110.0), (1000.0, 110.0)]]
        streams = [[(500.0, 0.0), (500.0, 1000.0)]]
        self.assertEqual(len(cx.find(roads, streams)), 1)
        self.assertEqual(len(cx.find(roads, streams, dedupe_m=5.0)), 2)

    def test_long_segments_across_buckets(self):
        roads = [[(-5000.0, -5000.0), (5000.0, 5000.0)]]
        streams = [[(-5000.0, 5000.0), (5000.0, -5000.0)]]
        got = cx.find(roads, streams)
        self.assertEqual(len(got), 1)
        self.assertAlmostEqual(math.hypot(got[0]["x"], got[0]["y"]), 0.0, places=6)



def image(n=300):
    """(green, nir) of a 1 m scene: land (NDWI -0.43), a round pond of 40 m radius at the centre,
    a 4 m wide ditch, a 20 m metal roof (NDWI +0.2 but bright in NIR) and one wet cell."""
    g = np.full((n, n), 80.0)
    nir = np.full((n, n), 200.0)
    r, c = np.mgrid[0:n, 0:n]
    pond = (r - n / 2) ** 2 + (c - n / 2) ** 2 <= 40 ** 2
    ditch = (r >= 20) & (r < 24) & (c >= 20) & (c < 280)
    roof = (r >= 250) & (r < 270) & (c >= 30) & (c < 50)
    for m in (pond, ditch):
        g[m], nir[m] = 60.0, 20.0
    g[roof], nir[roof] = 150.0, 100.0
    g[5, 280], nir[5, 280] = 60.0, 20.0
    return g, nir, pond


class Water(unittest.TestCase):
    def test_ndwi(self):
        out = wt.ndwi([60.0, 0.0, np.nan], [20.0, 0.0, 5.0])
        self.assertAlmostEqual(out[0], 0.5)
        self.assertTrue(np.isnan(out[1]) and np.isnan(out[2]))

    def test_only_the_pond_is_water(self):
        g, nir, pond = image()
        idx = wt.ndwi(g, nir)
        dark = wt.dark_in_nir(nir, idx, 0.25)
        lab, n = wt.water_patches(idx, 0.15, 1, 50, 10.0, dark)
        self.assertEqual(n, 1)
        self.assertGreater((lab > 0)[pond].mean(), 0.97)
        self.assertEqual(int((lab > 0)[~pond].sum()), 0)

    def test_each_filter_does_its_part(self):
        g, nir, _pond = image()
        idx = wt.ndwi(g, nir)
        _l, n = wt.water_patches(idx, 0.15, 1, 50, 0.0, None)
        self.assertEqual(n, 3)                     # pond, ditch, roof (the lone cell is opened away)
        _l, n = wt.water_patches(idx, 0.15, 1, 50, 10.0, None)
        self.assertEqual(n, 2)                     # the ditch is too narrow
        _l, n = wt.water_patches(idx, 0.15, 1, 50, 0.0, wt.dark_in_nir(nir, idx, 0.25))
        self.assertEqual(n, 2)                     # the roof is not dark in NIR
        _l, n = wt.water_patches(idx, 0.6, 1, 50, 0.0, None)
        self.assertEqual(n, 0)                     # the threshold is yours

    def test_patch_stats(self):
        g, nir, pond = image()
        idx = wt.ndwi(g, nir)
        lab, n = wt.water_patches(idx, 0.15, 1, 50, 10.0, wt.dark_in_nir(nir, idx))
        mapped = np.zeros(pond.shape, bool)
        mapped[: pond.shape[0] // 2] = True
        cells, mean, share = wt.patch_stats(lab, n, idx, mapped)
        self.assertAlmostEqual(mean[1], 0.5, places=6)
        self.assertAlmostEqual(share[1], 0.5, delta=0.05)
        self.assertEqual(cells[1], (lab == 1).sum())


if __name__ == "__main__":
    unittest.main()
