"""Finder engine on synthetic terrain (numpy + scipy, no QGIS, no GDAL).

    python -m <plugin folder>.tests.test_core
"""
import math
import os
import unittest

import numpy as np

from ..core import crossings as cx
from ..core import embankments as em
from ..core import evaluate as ev
from ..core import evidence as evd
from ..core import review as rv
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

    def test_a_fill_below_does_not_make_a_valley_dam_dug(self):
        # dams in series on one draw: the fill 18 rows below must not steepen the 'valley fall'
        # (below_frac read 0.63 'dug' for a plain valley dam before 0.5's review fixes)
        def fill(dem, y, row, h, top=2):
            dist = np.maximum(0, np.abs(y - row) - top / 2) * CELL
            return np.maximum(dem, bed(row) + h - dist / 3.0)
        for below in (None, 5.0):
            dem, y = valley()
            if below:
                dem = fill(dem, y, 200, below)
            dem = fill(dem, y, 182, 3.0)
            got = em.find(dem, GT, [LINE], params=em.FinderParams(min_height_m=0.5, dedupe_m=1.0))
            b = [e for e in got if abs((N * CELL - e.y) / CELL - 182) < 3][0]
            self.assertLess(b.below_frac, 0.1, below)
            self.assertFalse(any("(dug)" in n for n in b.notes))
            self.assertAlmostEqual(b.orig_m, bed(182), delta=0.15)
        # nor a fill above: the pool's end on its face is not the valley side
        dem, y = valley()
        dem = fill(fill(dem, y, 160, 5.0), y, 182, 3.0)
        got = em.find(dem, GT, [LINE], params=em.FinderParams(min_height_m=0.5, dedupe_m=1.0))
        b = [e for e in got if abs((N * CELL - e.y) / CELL - 182) < 3][0]
        self.assertAlmostEqual(b.orig_m, bed(182), delta=0.15)

    def test_profile_measures_do_not_depend_on_the_line_angle(self):
        # a 3 m fill with 3:1 faces across a 0.5 % valley, the stream line at several angles to
        # the grid: profile samples are 1 to 1.41 cells apart, so distances come from the line
        import math
        n, c = 500, 1.0
        gt = (0.0, c, 0.0, n * c, 0.0, -c)
        sides, tops = [], []
        for t_deg, off in ((0, 0.0), (45, 0.0), (45, 0.5), (30, 0.0), (60, 0.2)):
            t = math.radians(t_deg)
            R, Cc = np.mgrid[0:n, 0:n]
            X, Yn = Cc + 0.5, n - (R + 0.5)
            cx, cy = n / 2 + off, n / 2
            al = (X - cx) * math.cos(t) + (Yn - cy) * math.sin(t)
            ac = -(X - cx) * math.sin(t) + (Yn - cy) * math.cos(t)
            z = 100.0 - 0.005 * al + np.abs(ac) * 0.03
            fz = 103.0 - np.maximum(0.0, np.abs(al) - 2.0) / 3.0
            z = np.maximum(z, np.where(np.abs(ac) < 60, fz, -1e9))
            line = [(cx - 200 * math.cos(t), cy - 200 * math.sin(t)), (cx + 200 * math.cos(t), cy + 200 * math.sin(t))]
            got = em.find(z, gt, [line], params=em.FinderParams(min_height_m=0.5))
            self.assertEqual(len(got), 1, t_deg)
            sides.append(got[0].side_sum)
            tops.append(got[0].top_w_m)
            self.assertLess(got[0].below_frac, 0.05)
            self.assertAlmostEqual(got[0].len_m, 21.0, delta=2.5)
        self.assertLess(max(sides) - min(sides), 0.6, sides)               # 6 (3:1 + 3:1)
        self.assertAlmostEqual(float(np.mean(sides)), 6.0, delta=0.5)
        self.assertLess(max(tops) - min(tops), 1.0, tops)

    def test_spoil_mounds_do_not_depend_on_the_window(self):
        for w in (1200.0, 500.0, 305.0):
            got = em.find(dug_pit(), GT, [LINE], params=em.FinderParams(min_height_m=0.3, window_m=w))
            self.assertTrue(got)
            self.assertEqual(got[0].mounds, 1, w)

    def test_one_per_pool_twice_adds_up(self):
        def mk(x, outlet):
            return em.Embankment(x=x, y=0.0, kind="dam", on_road=False, crest_m=10, spill_m=9.0, toe_m=5,
                                 up_m=8, height_m=4.0, crest_height_m=5, head_m=3, valley_drop_m=0, len_m=30,
                                 storage_m3=100, pool_area_m2=100, pool=True, open_pool=False,
                                 confidence="high", line=0, spill_xy=(500.0, 500.0), spill_dist_m=50.0,
                                 outlet_dist_m=outlet)
        a1, a2, b1 = mk(0.0, 20.0), mk(100.0, 40.0), mk(4000.0, 5.0)        # a pool across two tiles
        out = em.one_per_pool(em.one_per_pool([a1, a2], 1.0) + em.one_per_pool([b1], 1.0), 1.0)
        self.assertEqual(len(out), 1)
        self.assertEqual((out[0].x, out[0].same_pool), (4000.0, 2))
        notes = [t for t in out[0].notes if "one fill per pool" in t]
        self.assertEqual(notes, ["2 other hump(s) on this pool dropped (one fill per pool)"])

    def test_toe_reaches_the_foot_past_a_bench(self):
        # a downstream face with a bench part-way down: the toe is the ground at the foot, not the
        # top of the bench (Absaraka read 17 ft instead of ~28 ft when it stopped on a bench)
        crest = [110.0, 108.0, 106.0]                      # steep face
        bench = [105.8, 105.7, 105.6]                      # a short flat shelf
        more = [104.0, 102.0, 100.0]                       # the face carries on
        floor = [99.8 - 0.1 * i for i in range(20)]        # the valley floor (gentle)
        z = np.array(crest + bench + more + floor)
        kd, still = em.face_toe(z, 0, len(z), CELL, 8.0)
        self.assertLessEqual(z[kd], 100.2)                 # the foot, well past the 105.6 bench top
        self.assertGreaterEqual(z[kd], 99.0)               # and not on down the floor
        self.assertFalse(still)
        # the floor flattening right at the end of the search: it may still fall, say so
        _kd, still = em.face_toe(np.array(crest + bench + more + floor[:4]), 0, 13, CELL, 8.0)
        self.assertTrue(still)
        # a face that flattens straight onto a gentle floor stops there, not on down the valley
        z2 = np.array([110.0, 107.0, 104.0, 101.0, 100.8, 100.6, 100.4, 100.2, 100.0])
        kd2, _ = em.face_toe(z2, 0, len(z2), CELL, 8.0)
        self.assertLessEqual(z2[kd2], 101.0)
        self.assertGreaterEqual(z2[kd2], 100.0)
        # a 3 m fill with 3:1 faces on a 4 % draw: the toe is at the foot, not 100 m on down the
        # draw (the drop over 40 m of a steep draw is not a bench; it read 8 m high before 0.5)
        for slope, cell in ((0.04, 3.0), (0.02, 1.0), (0.06, 3.0)):
            x = np.arange(0, 300, cell)
            ground = 200.0 - slope * x
            fill = np.clip(3.0 - np.abs(x - 90.0 - 0.0) / 3.0 + 2.0 / 3.0, 0, 3.0)   # 4 m top
            zz = ground + fill
            e = int(np.flatnonzero((x > 90) & (fill == 0))[0])
            kd3, open3 = em.face_toe(zz, e - 3, int(150 / cell), cell, 8.0)
            crest = zz[int(np.argmax(fill))]
            self.assertLess(crest - zz[kd3], 3.0 + slope * 20 + 0.6, (slope, cell))
            self.assertLess(x[kd3] - x[e], 25.0)
            self.assertFalse(open3)

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

    def test_near_mask_and_water_only(self):
        # a dike beside a channel (holds water) and one out in a field (dry): near a channel and
        # water-holding keeps only the first, which is what the default dike pass looks for
        z, gt = self.flat()
        z[40:160, 98:104] = 102.0                          # dike A, by the channel, holds water
        z[40:160, 150:156] = 102.0                         # dike B, dry, out in the field
        depth = np.zeros(z.shape)
        depth[40:160, 80:98] = 1.0                         # a pool on dike A's west side
        channel = np.zeros(z.shape, bool)
        channel[:, 72:74] = True                           # a stream down the west side
        from scipy import ndimage as ndi
        near = ndi.binary_dilation(channel, iterations=28)  # its buffer reaches dike A, not dike B
        got = rd.find_ridges(z, gt, depth=depth, near_mask=near, water_only=True)
        self.assertEqual([d["kind"] for d in got], [rd.K_LEVEE])
        self.assertAlmostEqual(got[0]["x"], 101.0, delta=4.0)
        self.assertEqual(rd.find_ridges(z, gt, depth=depth, water_only=True)[0]["kind"], rd.K_LEVEE)
        self.assertEqual(len(rd.find_ridges(z, gt, depth=depth)), 2)   # both, without the filters

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

    def rows05(self, n=240, seed=3):
        """0.5 rows: dams hold water above original ground (ag_head) in every terrain; in flat
        ground dug pits (below_frac high) look like dams on height alone."""
        rng = np.random.default_rng(seed)
        out = self.rows(n, seed)
        for i, r in enumerate(out):
            dam = r["status"] == "listed"
            terr = ("flat", "rolling", "cut")[i % 3 if i % 3 else (i // 3) % 3]
            r["relief_ft"] = {"flat": 20.0, "rolling": 70.0, "cut": 200.0}[terr] + float(rng.normal(0, 5))
            r["ag_head_ft"] = float(rng.normal(6 if dam else 1, 1.5))
            r["below_frac"] = float(rng.uniform(0, 0.2) if dam else rng.uniform(0.2, 1.0))
            r["spill_on_fill"] = "yes" if dam or rng.random() < 0.4 else "no"
            r["known_id"] = "D{}".format(i // 3) if dam else ""
            r["known_ft"] = float(rng.uniform(10, 290)) if dam else None
        return out

    def test_v2_clean_positives_trusted_marks_terrain(self):
        rows = self.rows05()
        # a second fill in a listed dam's bag (a road fill 250 ft off the listed point)
        decoy = dict(rows[0], on_road="yes", ag_head_ft=0.5, below_frac=0.5, known_ft=250.0)
        rows[0]["known_ft"] = 20.0
        rows.append(decoy)
        marks = [(dict(rows[1], status="new"), 1.0, 20.0), (dict(rows[2]), 0.0, 20.0),
                 (dict(rows[4]), 0.5, 20.0)]                       # yes, no, unsure
        m = sc.train(rows, features=sc.ALL_FEATURES, bags=True, extra=marks, splines=True, groups=True)
        self.assertEqual(m["version"], 2)
        self.assertEqual(m["clean_positives"]["bags"], 80)
        self.assertEqual(m["trusted_labels"], 3)
        self.assertIn("ag_head", m["features"])
        self.assertTrue(any("@flat" in c for c in m["columns"]))
        self.assertTrue(any(">" in c for c in m["columns"]))
        y = sc.labels(rows)
        self.assertGreater(sc.ranked_auc(sc.score(m, self.rows05(seed=9)), sc.labels(self.rows05(seed=9))), 0.9)
        # one positive per listed dam (81 listed fills in 80 bags) + the yes mark + the unsure's
        # positive half
        self.assertEqual(int(y.sum()), 81)
        self.assertEqual(m["n_listed"], 80 + 1 + 1)
        # background limited to trusted tiles: unlabelled rows outside are left out
        bg = np.array([i % 2 == 0 for i in range(len(rows))])
        m2 = sc.train(rows, features=sc.ALL_FEATURES, background=bg)
        self.assertLess(m2["n_other"], int((y == 0).sum()))
        det = sc.score_detail(m, [dict(rows[3], height_ft=500.0), rows[3]], min_group_positives=10)
        self.assertIn("log_height", det[0][1])
        self.assertEqual(det[1][1], "")
        few = sc.score_detail(m, [rows[3]], min_group_positives=1000)[0][1]
        self.assertIn("listed dam(s) to learn from", few)
        # listed fills with no id each count as their own dam, not as one bag
        noid = [dict(r, known_id=None) for r in self.rows05()]
        self.assertEqual(sc.train(noid, features=sc.ALL_FEATURES, bags=True)["n_listed"], int(sc.labels(noid).sum()))
        self.assertEqual(sc.terrain_group({"relief_ft": 10}), "flat")
        self.assertEqual(sc.terrain_group({"relief_ft": 300}), "cut")
        self.assertEqual(sc.terrain_group({}), "")

    def test_challenger_finds_an_interaction(self):
        from ..core import challenger as ch
        rng = np.random.default_rng(5)
        x = rng.random((1500, 3))
        y = ((x[:, 0] > 0.5) ^ (x[:, 1] > 0.5)).astype(float)       # an XOR a logistic cannot see
        y[rng.random(1500) < 0.5] = 0.0                              # half the dams unlisted (PU)
        m = ch.bagging_pu(x[:1000], y[:1000], n_bags=3, n_trees=60)
        s = ch.predict(m, x[1000:])
        truth = ((x[1000:, 0] > 0.5) ^ (x[1000:, 1] > 0.5)).astype(float)
        self.assertGreater(ev.auc(s, truth), 0.85)
        self.assertIn("finder-pu-gbm", ch.dumps(m))

    def test_trees_as_a_score_model(self):
        import os
        import tempfile
        rows = self.rows05()
        rows.append(dict(rows[0], on_road="yes", known_ft=250.0))      # a second fill in a dam's bag
        m = sc.train(rows, features=sc.ALL_FEATURES, bags=True, learner="trees", n_bags=3,
                     extra=[(dict(rows[1]), 1.0, 20.0), (dict(rows[2]), 0.5, 20.0)])
        self.assertEqual((m["version"], m["learner"]), (3, "pu-gbm"))
        self.assertEqual(m["clean_positives"]["bags"], 80)
        self.assertEqual(m["trusted_labels"], 2)                        # the unsure mark counts as both
        test = self.rows05(seed=9)
        self.assertGreater(sc.ranked_auc(sc.score(m, test), sc.labels(test)), 0.85)
        p = os.path.join(tempfile.mkdtemp(), "t.json")
        sc.save(m, p)
        self.assertTrue(np.allclose(sc.score(sc.load(p), test), sc.score(m, test), atol=1e-3))
        self.assertEqual(len(sc.score_detail(m, test[:3])), 3)
        with self.assertRaises(ValueError):
            sc.train(rows, learner="forest")

    def test_trees_with_few_listed_dams_still_split(self):
        rows = self.rows05(n=600)
        few, seen = [], 0
        for r in rows:
            if r["status"] == "listed":
                seen += 1
                if seen > 5:
                    r["status"], r["known_layer"], r["known_id"] = "new", "", ""
            few.append(r)
        m = sc.train(few, features=sc.ALL_FEATURES, learner="trees", n_bags=3)
        self.assertEqual(m["n_listed"], 5)
        test = self.rows05(n=300, seed=9)
        s = sc.score(m, test)
        self.assertGreater(len(set(np.round(s, 6).tolist())), 10)
        self.assertGreater(sc.ranked_auc(s, sc.labels(test)), 0.6)

    def test_a_mark_replaces_the_rows_own_label(self):
        # 15 road fills wrongly listed, marked 'no': the mark wins, with both learners, and its
        # weight matters with trees too
        rows = self.rows05(n=300)
        for i, r in enumerate(rows):
            r["find_id"] = "F{}".format(i)
        bad = [r for r in rows if r["status"] == "new" and r["on_road"] == "yes"][:15]
        for r in bad:
            r["status"], r["known_layer"], r["known_id"] = "listed", "DWR dams", "B" + r["find_id"]
        test = [dict(r) for r in bad]
        for learner in ("logistic", "trees"):
            kw = {"features": sc.ALL_FEATURES, "learner": learner, "n_bags": 3}
            m0 = sc.train(rows, **kw)
            m1 = sc.train(rows, extra=[(r, 0.0, 1.0) for r in bad], **kw)
            m2 = sc.train(rows, extra=[(r, 0.0, 50.0) for r in bad], **kw)
            self.assertEqual(m1["n_listed"], m0["n_listed"] - 15)
            s0, s1, s2 = (float(np.mean(sc.score(m, test))) for m in (m0, m1, m2))
            self.assertLess(s1, s0)
            self.assertLess(s2, s1)

    def test_no_terrain_note_when_the_model_never_measured_terrain(self):
        m = sc.train(self.rows(n=120))                   # pre-0.5 rows: no relief_ft
        notes = [msg for _s, msg in sc.score_detail(m, self.rows05(n=30))]
        self.assertFalse(any("terrain" in msg for msg in notes))

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

    def test_shadow_is_not_water(self):
        # a cast shadow (dark in every band) has a positive NDWI and is dark in NIR, so the old
        # filters passed it; the green-brightness test drops it, the pond stays
        g, nir, pond = image()
        g[200:230, 200:230], nir[200:230, 200:230] = 15.0, 5.0   # a 30 m shadow
        idx = wt.ndwi(g, nir)
        dark = wt.dark_in_nir(nir, idx, 0.25)
        _l, n = wt.water_patches(idx, 0.15, 1, 50, 10.0, dark)
        self.assertEqual(n, 2)                     # pond + shadow, with only the old filters
        bright = wt.bright_in_green(g, idx, 0.35)
        lab, n = wt.water_patches(idx, 0.15, 1, 50, 10.0, dark, bright=bright)
        self.assertEqual(n, 1)                     # the shadow is gone
        self.assertGreater((lab > 0)[pond].mean(), 0.97)

    def test_reject_mask_drops_patches(self):
        g, nir, pond = image()
        idx = wt.ndwi(g, nir)
        dark = wt.dark_in_nir(nir, idx, 0.25)
        built = np.zeros(g.shape, bool)
        built[108:192, 108:192] = True             # a built-up block over the whole pond
        _l, n = wt.water_patches(idx, 0.15, 1, 50, 10.0, dark, reject=built)
        self.assertEqual(n, 0)                     # the pond is on the built-up mask, dropped

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


def write_las(path, x, y, cls, z=None, scale=0.01, offset=(0.0, 0.0, 0.0), fmt=0, rec_len=20,
              version=(1, 2)):
    """Write a minimal uncompressed LAS (point format 0 by default) for the reader tests."""
    import struct
    x = np.asarray(x, float)
    y = np.asarray(y, float)
    cls = np.asarray(cls, np.uint8)
    z = np.zeros(len(x), float) if z is None else np.asarray(z, float)
    n = len(x)
    header_size = 227 if version == (1, 2) else 375
    hdr = bytearray(header_size)
    hdr[0:4] = b"LASF"
    hdr[24], hdr[25] = version
    struct.pack_into("<H", hdr, 94, header_size)
    struct.pack_into("<I", hdr, 96, header_size)          # point data right after the header
    struct.pack_into("<I", hdr, 100, 0)                   # no VLRs
    hdr[104] = fmt
    struct.pack_into("<H", hdr, 105, rec_len)
    struct.pack_into("<I", hdr, 107, n)                   # legacy point count
    struct.pack_into("<3d", hdr, 131, scale, scale, scale)
    struct.pack_into("<3d", hdr, 155, *offset)
    struct.pack_into("<6d", hdr, 179, float(x.max()), float(x.min()),
                     float(y.max()), float(y.min()), 0.0, 0.0)
    if version[1] >= 4:
        struct.pack_into("<Q", hdr, 247, n)
    body = bytearray()
    class_off = 15 if fmt < 6 else 17
    for i in range(n):
        rec = bytearray(rec_len)
        struct.pack_into("<i", rec, 0, int(round((x[i] - offset[0]) / scale)))
        struct.pack_into("<i", rec, 4, int(round((y[i] - offset[1]) / scale)))
        struct.pack_into("<i", rec, 8, int(round((z[i] - offset[2]) / scale)))
        rec[class_off] = int(cls[i])
        body += rec
    with open(path, "wb") as fh:
        fh.write(hdr)
        fh.write(body)


class Lidar(unittest.TestCase):
    def _cloud(self):
        # water (class 9) in a 20 x 20 m block at (50,50); ground (class 2) scattered elsewhere
        from .. core import lidar as li  # noqa
        wx, wy = np.meshgrid(np.arange(50, 70, 1.0), np.arange(50, 70, 1.0))
        wx, wy = wx.ravel(), wy.ravel()
        gx = np.linspace(0, 100, 60)
        gy = np.full(60, 10.0)
        x = np.concatenate([wx, gx])
        y = np.concatenate([wy, gy])
        cls = np.concatenate([np.full(wx.size, 9, np.uint8), np.full(60, 2, np.uint8)])
        return x, y, cls

    def test_reads_points_and_classes(self):
        from ..core import lidar as li
        import tempfile, os
        x, y, cls = self._cloud()
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "t.las")
            write_las(p, x, y, cls)
            ax, ay, acls, hdr = li.read_points(p)
            self.assertEqual(len(ax), len(x))
            self.assertTrue(np.allclose(sorted(ax), sorted(x), atol=0.02))
            self.assertEqual(int((acls == 9).sum()), 400)
            # a class filter reads only the water points
            wx, wy, wcls, _h = li.read_points(p, {9})
            self.assertEqual(len(wx), 400)
            self.assertTrue(np.all(wcls == 9))

    def test_extended_class_byte_format6(self):
        from ..core import lidar as li
        import tempfile, os
        x, y, cls = self._cloud()
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "t6.las")
            write_las(p, x, y, cls, fmt=6, rec_len=30, version=(1, 4))
            _ax, _ay, acls, _h = li.read_points(p, {9})
            self.assertEqual(int((acls == 9).sum()), 400)   # full byte, no 5-bit mask

    def test_point_grid_marks_only_water(self):
        from ..core import lidar as li
        x, y, cls = self._cloud()
        gt = (0.0, 1.0, 0.0, 100.0, 0.0, -1.0)             # 100 x 100 m, 1 m cells, top-left origin
        grid = li.point_grid(x, y, cls, (9,), gt, (100, 100), min_pts=1)
        # the water block is wet
        self.assertEqual(grid[40, 55], 1.0)                 # row = 100 - 60 (y=60) -> 40; col 55
        self.assertEqual(grid[45, 60], 1.0)
        # a ground-only cell is dry (0.0), not NaN
        self.assertEqual(grid[90, 50], 0.0)
        # a cell with no points at all is no-data
        self.assertTrue(np.isnan(grid[5, 5]))
        self.assertEqual(int((grid == 1.0).sum()), 400)

    def test_min_pts_threshold(self):
        from ..core import lidar as li
        x = np.array([10.0, 10.0, 20.0])
        y = np.array([10.0, 10.0, 20.0])
        cls = np.array([9, 9, 9], np.uint8)
        gt = (0.0, 1.0, 0.0, 100.0, 0.0, -1.0)
        g2 = li.point_grid(x, y, cls, (9,), gt, (100, 100), min_pts=2)
        self.assertEqual(g2[90, 10], 1.0)                  # the two-point cell survives
        self.assertEqual(g2[80, 20], 0.0)                  # the one-point cell does not

    def test_reads_z_when_asked(self):
        from ..core import lidar as li
        import tempfile, os
        x = np.array([10.0, 20.0, 30.0])
        y = np.array([10.0, 20.0, 30.0])
        z = np.array([100.0, 200.0, 300.0])
        cls = np.array([9, 9, 2], np.uint8)
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "z.las")
            write_las(p, x, y, cls, z=z)
            ax, ay, az, acls, _h = li.read_points(p, with_z=True)
            self.assertTrue(np.allclose(sorted(az), [100.0, 200.0, 300.0], atol=0.02))
            wx, wy, wz, wcls, _h2 = li.read_points(p, {9}, with_z=True)
            self.assertEqual(len(wz), 2)
            self.assertTrue(np.allclose(sorted(wz), [100.0, 200.0], atol=0.02))

    def test_rasterize_counts_count_and_mean_elevation(self):
        from ..core import lidar as li
        # three water points in one 1 m cell at z 100/102/104; one water point in another;
        # a ground point that must not count
        x = np.array([5.2, 5.4, 5.6, 8.5, 5.3])
        y = np.array([5.2, 5.4, 5.6, 8.5, 5.3])
        z = np.array([100.0, 102.0, 104.0, 50.0, 999.0])
        cls = np.array([9, 9, 9, 9, 2], np.uint8)
        gt = (0.0, 1.0, 0.0, 10.0, 0.0, -1.0)              # 10 x 10 m, 1 m cells, top-left origin
        count, meanz = li.rasterize_counts(x, y, z, cls, (9,), gt, (10, 10))
        # cell holding the three water points: row = 10 - 5 = 5 (y~5), col 5
        self.assertEqual(count[4, 5], 3)
        self.assertAlmostEqual(meanz[4, 5], 102.0, places=4)   # ground point ignored
        # the single-water-point cell
        self.assertEqual(count[1, 8], 1)
        self.assertAlmostEqual(meanz[1, 8], 50.0, places=4)
        # a cell with no water points: count 0, mean NaN
        self.assertEqual(count[0, 0], 0)
        self.assertTrue(np.isnan(meanz[0, 0]))
        self.assertEqual(int(count.sum()), 4)                  # only the four class-9 points

    def test_laz_is_refused_by_the_pure_reader(self):
        from ..core import lidar as li
        import tempfile, os, struct
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "t.laz")
            write_las(p, [0.0], [0.0], [9])
            with open(p, "rb") as fh:
                buf = bytearray(fh.read())
            buf[104] |= 0x80                               # mark the format compressed
            with open(p, "wb") as fh:
                fh.write(buf)
            with self.assertRaises(li.LasCompressedError):
                li.read_points(p)


def dug_pit():
    """A pit 60 x 33 m, 3 m deep, dug into a 2 % slope, spoil (1.5 m) along both long sides and
    across its downhill end - a dugout, no fill across a valley."""
    y, x = np.mgrid[0:N, 0:N]
    z = 100.0 - y * CELL * 0.02
    pit = (y >= 180) & (y < 200) & (np.abs(x - N // 2) <= 5)
    z = np.where(pit, (100.0 - 190 * CELL * 0.02) - 3.0, z)
    sp = (((y >= 201) & (y <= 203) & (np.abs(x - N // 2) <= 9))
          | ((np.abs(x - N // 2) >= 7) & (np.abs(x - N // 2) <= 9) & (y >= 178) & (y < 201)))
    return np.where(sp, z + 1.5, z)


class Evidence(unittest.TestCase):
    """Finder 0.5 evidence fields: outlet, original ground, shape, terrain."""

    def test_dam_stands_above_original_ground(self):
        dem, road = scene()
        dam = sorted(em.find(dem, GT, [LINE], road_mask=road), key=lambda e: -e.y)[0]
        self.assertEqual(dam.kind, em.K_DAM)
        # the NRCS 378 test: water held 3 ft or more above the old valley floor at the fill
        self.assertGreater(dam.ag_head_m, 0.8 * dam.height_m)
        self.assertGreater(dam.ag_head_m, 3.0 * em.FT)
        self.assertLess(dam.below_frac, 0.1)            # the pool sits on the old valley floor
        self.assertTrue(dam.spill_on_fill)              # it overtops the fill: the fill holds it
        self.assertGreater(dam.pool_xy[1], dam.y)       # the pool is upstream (north)
        self.assertTrue(math.isfinite(dam.relief_m) and dam.relief_m > 0)
        self.assertGreater(dam.slope_pct, 0)
        self.assertFalse(dam.fill_road)
        self.assertTrue(math.isnan(dam.drain_m2))       # no flow grid given

    def test_dug_pit_is_below_grade_and_square(self):
        got = em.find(dug_pit(), GT, [LINE], params=em.FinderParams(min_height_m=0.3))
        self.assertEqual(len(got), 1)
        pit = got[0]
        self.assertGreater(pit.below_frac, 0.5)          # most of its volume is below the ground
        self.assertGreater(pit.rect, 0.9)                # a rectangle
        self.assertGreater(pit.wall_slope, 0.3)          # steep inner walls
        self.assertIn("below the ground round it", " ".join(pit.notes))

    def test_original_ground_from_the_pool_end_to_the_toe(self):
        bed = 100.0 - np.arange(60) * CELL * 0.05          # a 5 % draw
        spill = bed[31] + 2.0
        z = bed.copy()
        z[30:33] = bed[31] + 3.0                           # a 3 m fill
        pool = bed < spill
        pool[30:] = False
        z[pool] = spill - 0.5                              # a pool surface (never read)
        orig, rise, how = em.original_ground(z, 30, 31, 34, spill, CELL)
        self.assertEqual(how, "pool end to toe")
        self.assertAlmostEqual(orig, bed[31], delta=0.05)  # the old floor under the fill
        self.assertGreater(rise, 0.0)

    def test_one_fill_per_pool(self):
        def e(x, outlet, h):
            return em.Embankment(x=x, y=0.0, kind=em.K_DAM, on_road=False, crest_m=10, spill_m=9.0,
                                 toe_m=5, up_m=8, height_m=h, crest_height_m=5, head_m=3, valley_drop_m=0,
                                 len_m=20, storage_m3=100, pool_area_m2=100, pool=False, open_pool=False,
                                 confidence="medium", line=0, spill_xy=(50.0, 50.0), outlet_dist_m=outlet)
        a, b = e(0.0, 5.0, 2.0), e(200.0, 80.0, 4.0)
        other = e(400.0, 3.0, 3.0)
        other.spill_xy = (900.0, 900.0)
        got = em.one_per_pool([b, a, other])
        self.assertEqual(len(got), 2)
        kept = [g for g in got if g.spill_xy == (50.0, 50.0)][0]
        self.assertIs(kept, a)                           # the one at the outlet, not the higher one
        self.assertEqual(kept.same_pool, 1)


class Evaluate(unittest.TestCase):
    def test_wilson(self):
        p, lo, hi = ev.wilson(10, 60)
        self.assertAlmostEqual(p, 1 / 6.0)
        self.assertAlmostEqual(lo, 0.0929, places=3)
        self.assertAlmostEqual(hi, 0.2804, places=3)
        p, lo, hi = ev.wilson(0, 30)
        self.assertEqual((p, lo), (0.0, 0.0))
        self.assertAlmostEqual(hi, 0.1135, places=3)

    def test_auc_matches_the_score_module_and_has_an_interval(self):
        rng = np.random.default_rng(1)
        y = (rng.random(200) < 0.2).astype(float)
        s = y * 0.8 + rng.random(200)
        s[:10] = 0.5                                      # ties count half
        self.assertAlmostEqual(ev.auc(s, y), sc.ranked_auc(s, y), places=12)
        a, lo, hi, se = ev.auc_ci(s, y)
        self.assertLess(lo, a)
        self.assertLess(a, hi)
        self.assertGreater(se, 0)
        y2 = y.copy()
        y2[:5] = np.nan                                   # unreviewed cards are left out
        self.assertTrue(math.isfinite(ev.auc(s, y2)))

    def test_precision_at_k(self):
        s = np.arange(10, dtype=float)
        y = np.array([0, 0, 0, 0, 0, 0, 0, 1, 1, 1], float)
        pk = ev.precision_at(s, y, 5)
        self.assertEqual((pk["k"], pk["hits"]), (5, 3))
        self.assertAlmostEqual(pk["p"], 0.6)

    def test_delong_paired(self):
        rng = np.random.default_rng(2)
        y = (rng.random(300) < 0.15).astype(float)
        s1 = y * 0.5 + rng.random(300)
        same = ev.delong(s1, s1, y)
        self.assertAlmostEqual(same["diff"], 0.0)
        self.assertEqual(same["p"], 1.0)
        s2 = y * 2.0 + rng.random(300)                    # a much better score
        d = ev.delong(s1, s2, y)
        self.assertAlmostEqual(d["auc1"], ev.auc(s1, y), places=10)
        self.assertAlmostEqual(d["auc2"], ev.auc(s2, y), places=10)
        self.assertGreater(d["diff"], 0)
        self.assertLess(d["p"], 0.01)
        self.assertLess(d["lo"], d["diff"])

    def test_paired_bootstrap_within_groups(self):
        rng = np.random.default_rng(3)
        y = (rng.random(240) < 0.15).astype(float)
        g = np.repeat(["a", "b", "c"], 80)
        s1 = y * 0.3 + rng.random(240)
        s2 = y * 1.5 + rng.random(240)
        b = ev.paired_bootstrap(s1, s2, y, n=300, groups=g)
        self.assertGreater(b["diff"], 0)
        self.assertGreater(b["lo"], 0)
        self.assertLess(b["p_le0"], 0.05)
        b0 = ev.paired_bootstrap(s1, s1, y, n=50)
        self.assertEqual((b0["diff"], b0["lo"], b0["hi"]), (0.0, 0.0, 0.0))
        pk = ev.paired_bootstrap(s1, s2, y, n=100, metric=lambda s, t: ev.precision_at(s, t, 30)["p"])
        self.assertGreaterEqual(pk["diff"], 0)

    def test_listed_recall_and_ht_share(self):
        r = ev.listed_recall([(0, 0), (1000, 0), (5000, 5000)], [(30, 0), (1010, 40)], 91.44)
        self.assertEqual((r["found"], r["total"]), (2, 3))
        # 10 top cards taken with chance 1 (5 dams), 20 random cards at chance 0.1 (2 dams):
        # the list is 10 + 200 cards, with 5 + 20 dams
        y = [1] * 5 + [0] * 5 + [1] * 2 + [0] * 18
        pi = [1.0] * 10 + [0.1] * 20
        h = ev.ht_share(y, pi)
        self.assertAlmostEqual(h["n_list"], 210.0)
        self.assertAlmostEqual(h["dams"], 25.0)
        self.assertAlmostEqual(h["share"], 25.0 / 210.0)
        self.assertLess(h["lo"], h["share"])
        # a random control with no dams in it still leaves room for some (not 0 +/- 0), and the
        # dams found in the top slice are a floor
        h0 = ev.ht_share([1] * 4 + [0] * 56 + [0] * 30, [1.0] * 60 + [30 / 8923.0] * 30)
        self.assertAlmostEqual(h0["dams"], 4.0)
        self.assertGreater(h0["hi"] * h0["n_list"], 300)
        self.assertGreaterEqual(h0["lo"] * h0["n_list"], 4.0 - 1e-6)
        # ... and the upper bound sits at least where Wilson puts 0 of 30 (0.114 of the rest)
        self.assertGreater(h0["hi"], 0.11 * 8863 / h0["n_list"])

    def test_platt_is_monotone_and_folds_leave_one_out(self):
        rng = np.random.default_rng(4)
        s = rng.random(200)
        y = (rng.random(200) < s * 0.5).astype(float)
        ab = ev.platt(s, y)
        self.assertGreater(ab[0], 0)
        p = ev.platt_apply(ab, [0.1, 0.5, 0.9])
        self.assertTrue(p[0] < p[1] < p[2])
        # scores far outside 0-1 (raw log-odds) fit the same calibration, rescaled, not P = 1
        a10 = ev.platt(10 * s, y)
        self.assertAlmostEqual(a10[0] * 10, ab[0], delta=0.05)
        self.assertAlmostEqual(a10[1], ab[1], delta=0.05)
        a5 = ev.platt(s + 5, y)
        self.assertAlmostEqual(a5[1], ab[1] - 5 * a5[0], delta=0.05)
        for k, c in ((3.0, -4.0), (2.0, 2.0)):               # log-odds-like ranges
            q = ev.platt(k * s + c, y)
            self.assertAlmostEqual(q[0] * k, ab[0], delta=0.05)
            self.assertLess(ev.platt_apply(q, [k + c])[0], 0.999)
        folds = ev.group_folds(["w", "w", "m", "b", "m"])
        self.assertEqual([f[0] for f in folds], ["b", "m", "w"])
        self.assertEqual(list(folds[1][2]), [2, 4])
        self.assertIn("AUC", ev.describe("x", ev.summary(s, y, ks=(30,))))


class EvidenceSources(unittest.TestCase):
    """core/evidence.py: water years, NWI codes, LiDAR drop-out, consequence."""
    GT1 = (0.0, 1.0, 0.0, 100.0, 0.0, -1.0)                 # 100 x 100 m, 1 m cells

    def test_water_years(self):
        m = evd.disk((100, 100), self.GT1, 50.0, 50.0, 20.0)
        green = np.full((100, 100), 80.0)
        dry_nir = np.full((100, 100), 120.0)                # vegetation: NIR above green
        wet_nir = np.where(m, 20.0, 120.0)                  # water swallows NIR
        seen = {2014: evd.wet(evd.ndwi(green, dry_nir), m)[0],
                2016: evd.wet(evd.ndwi(green, wet_nir), m)[0],
                2018: evd.wet(evd.ndwi(green, wet_nir), m)[0],
                2020: evd.wet(evd.ndwi(np.full((100, 100), np.nan), wet_nir), m)[0]}
        self.assertEqual(seen, {2014: False, 2016: True, 2018: True, 2020: None})
        h = evd.water_history(seen)
        self.assertEqual((h["years"], h["wet_years"], h["first_wet"], h["wet_list"]), (3, 2, 2016, "2016,2018"))

    def test_nwi_codes(self):
        self.assertEqual(evd.nwi_parse("PUBHh"), {"code": "PUBHh", "system": "P", "regime": "H", "h": True, "x": False})
        self.assertTrue(evd.nwi_parse("PUBFx")["x"])
        self.assertEqual(evd.nwi_parse("PEM1C")["regime"], "C")
        self.assertEqual(evd.nwi_parse("not a code")["system"], "")

    def test_lidar_dropout_is_a_contrast(self):
        pts = np.full((100, 100), 6.0)
        pool = evd.disk((100, 100), self.GT1, 50.0, 50.0, 15.0)
        rng_ = evd.ring((100, 100), self.GT1, 50.0, 50.0, 20.0, 40.0)
        pts[pool] = 0.0                                     # no returns off the water
        pts[0:3, :] = 0.0                                   # a scan gap elsewhere
        water = np.zeros((100, 100))
        water[pool] = 1.0
        d = evd.dropout(pts, pool, rng_, water)
        self.assertEqual(d["pool_empty"], 1.0)
        self.assertLess(d["ring_empty"], 0.1)
        self.assertGreater(d["contrast"], 0.9)
        self.assertEqual(d["class9"], 1.0)

    def test_consequence_down_the_flow_path(self):
        dem, _y = valley()
        rec, _o, _f = tr.flow(dem)
        start = 100 * N + N // 2                            # on the valley floor, row 100
        path = evd.flow_path(rec, dem.shape, start)
        xy, dist = evd.path_xy(path, dem.shape, GT)
        self.assertGreater(len(path), 200)                  # runs on down the valley
        z = dem.ravel()[path]
        y0 = xy[60, 1]
        blds = [(xy[60, 0] + 20.0, y0, z[60] + 0.5),         # beside the channel, low: floods
                (xy[60, 0] + 250.0, y0, z[60] + 5.0)]        # up the valley side: does not
        got = evd.flooded_buildings(xy, dist, z, blds, height_m=3.0)
        self.assertEqual(len(got), 1)
        road = [[(0.0, y0 - 30.0), (N * CELL, y0 - 30.0)]]   # a road across the valley below
        rx = evd.road_crossings(xy, dist, road)
        self.assertEqual(len(rx), 1)
        c = evd.consequence(got, rx, storage_af=20.0)
        self.assertEqual(c["bld_1mi"], 1)
        self.assertEqual(c["roads_1mi"], 1)
        self.assertEqual(c["conseq"], 4.0)
        self.assertTrue(c["permit_check"])
        self.assertFalse(evd.consequence([], [], 3.0)["permit_check"])

    def test_bare_ground_and_class_share(self):
        m = evd.disk((100, 100), self.GT1, 50.0, 50.0, 30.0)
        red = np.where(m, 200.0, 60.0)
        nir = np.where(m, 190.0, 160.0)                     # bare: NIR ~ red; grass: NIR >> red
        self.assertGreater(evd.bare_share(red, red, red, nir, m), 0.9)
        cdl = np.where(m, 176, 1).astype(float)
        self.assertEqual(evd.share_of_classes(cdl, m, (176,)), 1.0)


class Review(unittest.TestCase):
    def cands(self, n=600, seed=7):
        rng = np.random.default_rng(seed)
        return [{"find_id": "FIND-{:08X}".format(i), "x": float(rng.uniform(0, 50000)),
                 "y": float(rng.uniform(0, 50000)), "score": float(rng.random())} for i in range(n)]

    def test_batch_design(self):
        rows = self.cands()
        b = rv.pick_batch(rows, 90, seed=1)
        self.assertEqual(len(b), 90)
        self.assertEqual(len({r["find_id"] for r in b}), 90)
        st = [r["stratum"] for r in b]
        self.assertEqual((st.count("random"), st.count("top")), (16, 33))
        self.assertEqual(st.count("near"), 41)
        self.assertEqual(sorted(r["n"] for r in b), list(range(1, 91)))
        tops = sorted(r["rank"] for r in b if r["stratum"] == "top")
        self.assertEqual(tops[:3], [r for r in tops[:3]])
        self.assertTrue(all(r["incl_prob"] == 1.0 for r in b if r["stratum"] == "top"))
        self.assertTrue(all(0 < r["incl_prob"] < 1 for r in b if r["stratum"] == "near"))
        self.assertAlmostEqual(min(r["incl_prob"] for r in b), 16 / 600.0, places=4)
        self.assertEqual(sum(1 for r in b if r["second"] == "yes"), 11)
        # the random control is the same set when the scores change (a new model version)
        for r in rows:
            r["score"] = 1.0 - r["score"]
        b2 = rv.pick_batch(rows, 90, seed=1)
        self.assertEqual({r["find_id"] for r in b if r["stratum"] == "random"},
                         {r["find_id"] for r in b2 if r["stratum"] == "random"})
        # already-reviewed cards are not picked again for the top slice
        done = {r["find_id"] for r in b if r["stratum"] == "top"}
        b3 = rv.pick_batch(rows, 90, seed=1, reviewed=done)
        self.assertFalse(done & {r["find_id"] for r in b3 if r["stratum"] == "top"})
        # an honest estimate from the batch: the share of a known trait in the whole list
        trait = {r["find_id"]: (r["score"] > 0.9) for r in rows}
        h = ev.ht_share([trait[r["find_id"]] for r in b], [r["incl_prob"] for r in b])
        self.assertLess(h["lo"], np.mean(list(trait.values())) + 0.15)

    def test_per_tile_cap_and_committee(self):
        rows = self.cands()
        for r in rows[:200]:
            r["x"], r["y"] = 100.0, 100.0                  # one crowded tile
        b = rv.pick_batch(rows, 90, per_tile_cap=3, seed=2)
        near_in = sum(1 for r in b if r["stratum"] == "near" and r["x"] == 100.0)
        self.assertLessEqual(near_in, 3)
        comm = [np.array([r["score"] for r in rows]), np.random.default_rng(0).random(len(rows))]
        self.assertEqual(len(rv.pick_batch(rows, 60, committee=comm)), 60)

    def test_inclusion_chances_are_honest(self):
        # over many draws a card's picks / its stated chance averages 1, also in a crowded tile
        rows = self.cands()
        for r in rows[:200]:
            r["x"], r["y"] = 100.0, 100.0
        ids = [r["find_id"] for r in rows]
        acc = dict.fromkeys(ids, 0.0)
        S = 60
        for seed in range(S):
            for r in rv.pick_batch(rows, 90, seed=seed):
                acc[r["find_id"]] += 1.0 / r["incl_prob"] / S
        s = np.array([r["score"] for r in rows])
        rank = np.empty(600, int)
        rank[np.argsort(-s)] = np.arange(1, 601)
        crowd = np.array([r["x"] == 100.0 for r in rows])
        for sel in ((rank >= 40) & (rank <= 99) & crowd, (rank >= 40) & (rank <= 99) & ~crowd, rank <= 33):
            self.assertAlmostEqual(np.mean([acc[ids[i]] for i in np.where(sel)[0]]), 1.0, delta=0.15)
        # the committee picks are deterministic: chance 1 for the cards it takes
        comm = [s, np.random.default_rng(0).random(len(rows))]
        b = rv.pick_batch(rows, 90, committee=comm, seed=3)
        self.assertTrue(all(r["incl_prob"] == 1.0 for r in b if r["stratum"] == "near"))

    def test_reviewed_cards_only_come_back_through_the_random_control(self):
        rows = self.cands()
        r1 = rv.pick_batch(rows, 90, seed=0)
        r2 = rv.pick_batch(rows, 90, seed=0, reviewed={r["find_id"] for r in r1})
        p_rand = 16 / 600.0
        for r in r2:
            if r["stratum"] == "random":
                self.assertAlmostEqual(r["incl_prob"], p_rand, places=5)

    def test_dense_area_still_gets_a_near_slice(self):
        # the tile cap counts near-the-cut cards only, not the random and top cards there
        rng = np.random.default_rng(5)
        rows = [{"find_id": "FIND-{:08X}".format(i), "x": float(rng.uniform(0, 11000)),
                 "y": float(rng.uniform(0, 850)), "score": float(rng.random())} for i in range(270)]
        b = rv.pick_batch(rows, 90, per_tile_cap=3, seed=0)
        near = [r for r in b if r["stratum"] == "near"]
        self.assertGreaterEqual(len(near), 6)
        per = {}
        for r in near:
            t = int(r["x"] // 5000.0)
            per[t] = per.get(t, 0) + 1
        self.assertLessEqual(max(per.values()), 3)

    def test_random_control_differs_by_seed(self):
        rows = self.cands()
        a = {r["find_id"] for r in rv.pick_batch(rows, 90, seed=1) if r["stratum"] == "random"}
        b = {r["find_id"] for r in rv.pick_batch(rows, 90, seed=2) if r["stratum"] == "random"}
        self.assertLessEqual(len(a & b), 4)

    def test_marks(self):
        marks = {"a": {"mark": "yes"}, "b": {"mark": "no"}, "c": {"mark": "unsure"},
                 "d": {"mark": "offset", "px": 240 + 48, "py": 240}, "e": {"mark": None}}
        self.assertEqual(rv.marks_to_labels(marks), {"a": 1.0, "b": 0.0, "c": 0.5})
        off = rv.offsets(marks, {"d": {"x": 1000.0, "y": 2000.0}})
        self.assertEqual(off[0][0], "d")
        self.assertAlmostEqual(off[0][1], 1060.0)                   # 48 px x 1.25 m east
        self.assertAlmostEqual(off[0][3], 60.0)
        k = rv.agreement({"a": {"mark": "yes"}, "b": {"mark": "no"}, "c": {"mark": "yes"}},
                         {"a": {"mark": "yes"}, "b": {"mark": "no"}, "c": {"mark": "no"}})
        self.assertEqual(k["n"], 3)
        self.assertAlmostEqual(k["agree"], 2 / 3.0)


class Dataset(unittest.TestCase):
    def test_accumulates_and_keeps_the_latest(self):
        import tempfile
        from ..core import dataset as ds
        d = tempfile.mkdtemp()
        ds.add(d, "FIND-1", "unsure", {"county": "Wells"}, {"aerial": b"\xff\xd8x"})
        ds.add(d, "FIND-1", "yes", {"county": "Wells"}, {"aerial": b"\xff\xd8y"})   # marked again
        ds.add(d, "FIND-2", "no", {"county": "McLean"})
        ds.add(d, "FIND-1", "no", {"county": "Wells", "second_look": True}, reviewer="second")
        recs = ds.load(d)
        self.assertEqual(len(recs), 3)
        st = ds.stats(d, ready_at=1)                  # each card once, by its first reviewer
        self.assertEqual((st["cards"], st["yes"], st["no"], st["second_looks"]), (2, 1, 1, 1))
        self.assertTrue(st["ready"])
        self.assertEqual(st["by_county"]["Wells"], {"yes": 1})
        with open(os.path.join(d, "chips", "FIND-1_aerial.jpg"), "rb") as fh:
            self.assertEqual(fh.read(), b"\xff\xd8y")
        # a later reviewer does not take over a card: ann marked it first (and last), bob between
        d2 = tempfile.mkdtemp()
        ds.add(d2, "FIND-1", "no", reviewer="ann")
        ds.add(d2, "FIND-1", "yes", reviewer="bob")
        st2 = ds.stats(d2)
        self.assertEqual((st2["yes"], st2["no"]), (0, 1))
        ds.add(d2, "FIND-1", "yes", reviewer="ann")             # ann changes the mark: the latest counts
        self.assertEqual(ds.stats(d2)["yes"], 1)


if __name__ == "__main__":
    unittest.main()
