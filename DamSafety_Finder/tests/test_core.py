"""Finder engine on synthetic terrain (numpy + scipy, no QGIS, no GDAL).

    python -m <plugin folder>.tests.test_core
"""
import math
import unittest

import numpy as np

from ..core import crossings as cx
from ..core import embankments as em
from ..core import listed as ls
from ..core import ridges as rd
from ..core import score as sc
from ..core import terrain as tr
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

    def test_long_fill_off_the_road_layer_is_not_a_dam(self):
        # a highway / canal bank 2.5 m above the ground the whole way across (1.2 km), no road
        # layer: a long fill, not a dam; a farm fill (ends on the valley sides) stays short
        dem, y = valley()
        dem = np.where(np.abs(y - 250) <= 4, dem + 2.5, dem)
        dem = add_fill(dem, y, 150, 3.0)
        got = sorted(em.find(dem, GT, [LINE]), key=lambda e: -e.y)
        self.assertEqual([e.kind for e in got], [em.K_DRY, em.K_LONG])
        self.assertLess(got[0].ridge_len_m, 300.0)
        self.assertGreaterEqual(got[1].ridge_len_m, 600.0)
        self.assertIn("road layers miss", " ".join(got[1].notes))
        p = em.FinderParams(long_fill_m=5000.0)          # the threshold is yours
        self.assertNotIn(em.K_LONG, [e.kind for e in em.find(dem, GT, [LINE], params=p)])

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

    def test_spillway_is_found_at_the_low_end(self):
        dem, y = valley()
        _yy, x = np.mgrid[0:N, 0:N]
        top = bed(200) + 4.0
        fill = (np.abs(y - 200) <= 3)
        dem = np.where(fill, np.maximum(dem, np.where(x < N // 2 - 20, top - 2.0, top)), dem)
        e = em.find(dem, GT, [LINE])[0]
        self.assertIsNotNone(e.spill_xy)
        self.assertLess(e.spill_xy[0], XS - 20 * CELL + CELL)          # on the low (west) end
        self.assertGreater(e.spill_dist_m, 30.0)
        dem, road = scene()
        dam = [f for f in em.find(dem, GT, [LINE], road_mask=road) if f.kind == em.K_DAM][0]
        self.assertIsNotNone(dam.spill_xy)

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



class Terrain(unittest.TestCase):
    def test_drainage_follows_the_valley_and_area_grows(self):
        dem, _y = valley()
        rec, order, filled = tr.flow(dem)
        area, length = tr.accumulate(rec, order, dem.shape, CELL)
        self.assertEqual(len(order), N * N)
        self.assertGreater(area[-2, N // 2], area[N // 2, N // 2])
        lines = tr.drainage_lines(rec, area, GT, 20000.0)
        main = max(lines, key=len)
        xs = [p[0] for p in main[10:-10]]
        self.assertLess(max(abs(x - (N // 2 + 0.5) * CELL) for x in xs), 2 * CELL)

    def test_pit_is_filled_and_drains_over_its_rim(self):
        dem, y = valley()
        dem = add_fill(dem, y, 200, 4.0)
        rec, order, filled = tr.flow(dem)
        d = tr.depth(dem, filled)
        # the pool would reach the top of the grid, 2.4 m above the bed at the fill: it drains out
        # there before it gets over the 4 m fill
        self.assertAlmostEqual(float(d[195, N // 2]), 200 * CELL * SLOPE - 5 * CELL * SLOPE, delta=0.05)
        self.assertAlmostEqual(float(d[0, N // 2]), 0.0)
        self.assertEqual(float(d[300, N // 2]), 0.0)

    def test_lines_run_through_a_fill_when_opened(self):
        dem, road = scene()
        o = tr.open_ridges(dem, CELL, 60.0)
        rec, order, _f = tr.flow(o)
        area, _l = tr.accumulate(rec, order, dem.shape, CELL)
        lines = tr.drainage_lines(rec, area, GT, 20000.0)
        got = em.find(dem, GT, lines, road_mask=road)
        self.assertEqual(sorted(e.kind for e in got), sorted([em.K_DAM, em.K_DRY, em.K_ROAD]))


class Ridges(unittest.TestCase):
    def flat(self, n=200):
        return np.full((n, n), 100.0), (0.0, 1.0, 0.0, float(n), 0.0, -1.0)

    def test_levee_holding_water(self):
        z, gt = self.flat()
        z[40:160, 98:104] = 102.0                          # a 2 m dike, 120 m long
        depth = np.zeros(z.shape)
        depth[40:160, 80:98] = 1.0                         # a pool on its west side
        got = rd.find_ridges(z, gt, depth=depth)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["kind"], rd.K_LEVEE)
        self.assertAlmostEqual(got[0]["length_m"], 120.0, delta=15.0)
        self.assertEqual(rd.find_ridges(z, gt)[0]["kind"], rd.K_RIDGE)
        self.assertEqual(rd.find_ridges(z, gt, exclude=[(100.0, 100.0)]), [])
        road = np.zeros(z.shape, bool)
        road[:, 96:106] = True
        self.assertEqual(rd.find_ridges(z, gt, road_mask=road), [])

    def test_round_hump_and_bend_are_not_ridges(self):
        z, gt = self.flat()
        r, c = np.mgrid[0:200, 0:200]
        z[(r - 100) ** 2 + (c - 100) ** 2 < 15 ** 2] = 102.0
        self.assertEqual(rd.find_ridges(z, gt), [])
        z, gt = self.flat()
        z[40:46, 40:160] = 102.0
        z[40:160, 154:160] = 102.0                          # an L: fills little of its rectangle
        self.assertEqual(rd.find_ridges(z, gt), [])

    def test_dugout_with_spoil(self):
        z, gt = self.flat()
        z[60:72, 60:90] = 97.0                              # a 12 x 30 m pit, 3 m deep
        z[54:60, 60:90] = z[72:78, 60:90] = 101.0           # spoil along the long sides
        pp = rd.RidgeParams(dug_close_m=40.0)                # the test grid is small
        got = rd.find_dugouts(z, gt, params=pp)
        self.assertEqual(len(got), 1)
        self.assertAlmostEqual(got[0]["depth_m"], 4.0, delta=0.3)       # below the spoil tops
        self.assertGreater(got[0]["rect"], 0.9)
        z[54:60, 60:90] = z[72:78, 60:90] = 100.0           # no spoil: a pit, not a dugout
        self.assertEqual(rd.find_dugouts(z, gt, params=pp), [])
        z, gt = self.flat()
        r, c = np.mgrid[0:200, 0:200]
        z[(r - 100) ** 2 / 400.0 + (c - 100) ** 2 / 100.0 < 1] = 98.0  # a pothole, ragged-free oval
        z[((r - 100) ** 2 / 900.0 + (c - 100) ** 2 / 400.0 < 1) & (z == 100.0)] = 100.6
        self.assertEqual(rd.find_dugouts(z, gt, params=pp), [])   # round: a pothole

    def test_min_rect(self):
        rr, cc = np.nonzero(np.ones((10, 30), bool))
        a, lo, sh = rd.min_rect(rr, cc, 2.0)
        self.assertAlmostEqual(a, 20 * 60, delta=1e-6)
        self.assertEqual((lo, sh), (60.0, 20.0))


class Score(unittest.TestCase):
    def rows(self, n=60, seed=1):
        rng = np.random.default_rng(seed)
        out = []
        for i in range(n):
            dam = i % 3 == 0
            out.append({"status": "listed" if dam else "new", "known_layer": "DWR dams" if dam else "",
                        "height_ft": float(rng.normal(14 if dam else 7, 2)),
                        "crest_height_ft": float(rng.normal(16 if dam else 8, 2)),
                        "head_ft": float(rng.normal(6 if dam else 1, 1.5)), "valley_drop_ft": 0.5,
                        "storage_af": float(rng.lognormal(3 if dam else 0.5, 0.7)),
                        "area_ac": float(rng.lognormal(1 if dam else -1, 0.5)), "length_ft": 300.0,
                        "pool": "yes" if dam and rng.random() < 0.6 else "no",
                        "on_road": "no" if dam else ("yes" if rng.random() < 0.8 else "no"),
                        "open_pool": "no", "spillway": "beside" if dam else "over"})
        return out

    def test_learns_and_ranks(self):
        rows = self.rows()
        m = sc.train(rows)
        y = sc.labels(rows)
        self.assertEqual(int(y.sum()), 20)
        self.assertGreater(sc.ranked_auc(sc.score(m, self.rows(seed=2)), y), 0.9)
        self.assertGreater(sc.score(m, [rows[0]])[0], sc.score(m, [rows[1]])[0])

    def test_an_older_model_still_scores(self):
        rows = self.rows()
        m = sc.train(rows)
        k = m["features"].index("log_ridge_len")
        for key in ("features", "mean", "std"):
            m[key] = m[key][:k] + m[key][k + 1:]
        m["weights"] = m["weights"][:k + 1] + m["weights"][k + 2:]
        self.assertEqual(len(sc.score(m, rows)), len(rows))
        m["features"][0] = "no_such_feature"
        with self.assertRaises(ValueError):
            sc.score(m, rows)

    def test_needs_examples(self):
        rows = self.rows()[:4]
        with self.assertRaises(ValueError):
            sc.train(rows)

    def test_round_trip(self):
        import os
        import tempfile
        m = sc.train(self.rows())
        p = os.path.join(tempfile.mkdtemp(), "m.json")
        sc.save(m, p)
        self.assertEqual(sc.load(p)["weights"], m["weights"])


class Pipes(unittest.TestCase):
    def test_rational_and_pipe(self):
        from ..core import crossings as c
        self.assertAlmostEqual(c.rational_cfs(0.35, 2.0, 100.0), 70.0)
        d, size = c.pipe_for(70.0, 8.0)
        self.assertAlmostEqual(d, 12 * math.sqrt(4 * 70 / (math.pi * 8)), places=6)
        self.assertEqual(size, 42)
        self.assertIsNone(c.pipe_for(5000.0, 8.0)[1])
        self.assertAlmostEqual(c.kirpich_tc_min(1000.0, 0.01), 0.0078 * 1000 ** 0.77 * 0.01 ** -0.385)


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

    def test_persistence_keeps_the_pond_drops_a_wet_year(self):
        g, nir, pond = image()
        g2, nir2 = g.copy(), nir.copy()
        field = np.zeros(pond.shape, bool)
        field[200:260, 150:280] = True                     # flooded in one year only
        g2[field], nir2[field] = 60.0, 20.0
        gt = (0, 1, 0, 300, 0, -1)
        mean, share, _gt = wt.persistence([({2: g, 4: nir}, gt, 2, 4), ({2: g2, 4: nir2}, gt, 2, 4),
                                           ({2: g, 4: nir}, gt, 2, 4)])
        lab, n = wt.water_patches(share, 0.5, 1, 50, 10.0, None)
        self.assertEqual(n, 1)
        self.assertGreater((lab > 0)[pond].mean(), 0.97)

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
