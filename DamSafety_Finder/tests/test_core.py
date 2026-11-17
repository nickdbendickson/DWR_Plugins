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


def hill_pond(water=6, n=120, r0=50, c0=57):
    """(DEM, water mask, water level) of a small pond in a draw (sides of 10 %) falling south at
    6 %: water x water cells of flat water (6 x 6 = 324 m2, 4 x 4 = 144 m2) 0.3 m below the
    draw's floor at its uphill end, ringed by a fill two cells wide whose top is 1.1 m above the
    water (the draw's sides are higher than that beside the pond: the fill is the dam across)."""
    y, x = np.mgrid[0:n, 0:n]
    mid = c0 + (water - 1) / 2.0
    z = 100.0 - y * CELL * 0.06 + np.abs(x - mid) * CELL * 0.10
    lvl = 100.0 - r0 * CELL * 0.06 - 0.3
    ring = (y >= r0 - 2) & (y < r0 + water + 2) & (x >= c0 - 2) & (x < c0 + water + 2)
    wat = (y >= r0) & (y < r0 + water) & (x >= c0) & (x < c0 + water)
    z = np.where(ring, np.maximum(z, lvl + 1.1), z)
    return np.where(wat, lvl, z), wat, lvl


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
        # nor ordinary micro-relief on the floor (8 cm): a 4 m valley dam whose pool runs off the
        # grid is not 'dug' (the fit used to stop at the first 15 cm bump, on the face remnant)
        from scipy import ndimage as ndi_
        for seed in range(6):
            dem, y = valley()
            dem = fill(dem, y, 200, 4.0)
            noise = ndi_.gaussian_filter(np.random.default_rng(seed).normal(0, 1, dem.shape), 2.0)
            dem = dem + 0.08 * noise / noise.std()
            lvl = bed(200) + 3.0
            dem = np.where((y < 197) & (dem < lvl), lvl, dem)
            got = em.find(dem, GT, [LINE], params=em.FinderParams(min_height_m=0.5, dedupe_m=1.0))
            d = [e for e in got if abs((N * CELL - e.y) / CELL - 200) < 4]
            self.assertTrue(d, seed)
            self.assertFalse(any("(dug)" in n for n in d[0].notes), (seed, d[0].below_frac))
            self.assertLess(d[0].below_frac, 0.3, seed)

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
            # ... and at the foot, not a sample up the face (the window that stops the walk)
            self.assertGreater(crest - zz[kd3], crest - (200.0 - slope * 101.0) - 0.35, (slope, cell))
            self.assertLess(x[kd3] - x[e], 25.0)
            self.assertFalse(open3)

    def draw_dam(self, slope, h, phase, pool_below=0.6, face=3.0, n=300, cell=3.0):
        """A fill with `face`:1 faces and a 4 m top across a draw falling `slope`, a flat pool
        pool_below m under the crest behind it; the fill centre shifted by phase of a cell."""
        y, x = np.mgrid[0:n, 0:n]
        yc, xm = (y + 0.5) * cell, (n // 2 + 0.5) * cell
        dem = 100.0 - yc * slope + np.abs((x + 0.5) * cell - xm) * 0.03
        y0 = 150 * cell + phase * cell
        b0 = 100.0 - y0 * slope
        dem = np.maximum(dem, b0 + h - np.maximum(0.0, np.abs(yc - y0) - 2.0) / face)
        if pool_below is not None:
            lvl = b0 + h - pool_below
            dem = np.where((yc < y0) & (dem < lvl), lvl, dem)
        gt = (0.0, cell, 0.0, n * cell, 0.0, -cell)
        line = [(xm, n * cell - 1.0), (xm, 1.0)]
        return dem, gt, line, b0 - slope * (2.0 + face * h)

    def test_a_pooled_dam_on_a_gentle_draw_keeps_its_height(self):
        # 3:1 faces on a 2 % draw at 3 m cells: the toe used to stop a sample up the face, so a
        # 7-8 ft dam fell under the 5 ft minimum on some sample phases
        for h in (2.3, 3.0):
            for phase in (0.0, 0.2, 0.4, 0.6, 0.8):
                dem, gt, line, foot = self.draw_dam(0.02, h, phase)
                got = em.find(dem, gt, [line])
                self.assertEqual(len(got), 1, (h, phase))
                true = (100.0 - (150 + phase) * 3.0 * 0.02 + h) - foot
                self.assertAlmostEqual(got[0].crest_height_m, true, delta=0.4, msg=(h, phase))

    def test_side_slopes_of_a_full_pond_are_not_read_flatter(self):
        # the pool 0.6-1.0 m under the crest leaves a face about a sample long: NaN or near the
        # true 3:1 + 3:1, never much flatter
        for below in (0.6, 1.0):
            for phase in (0.0, 0.25, 0.5, 0.75):
                dem, gt, line, _f = self.draw_dam(0.004, 3.0, phase, pool_below=below)
                e = em.find(dem, gt, [line])[0]
                if math.isfinite(e.side_sum):
                    self.assertLess(e.side_sum, 7.0, (below, phase))
                    self.assertGreater(e.side_sum, 5.0, (below, phase))
        dem, gt, line, _f = self.draw_dam(0.004, 3.0, 0.0, pool_below=None)
        self.assertAlmostEqual(em.find(dem, gt, [line])[0].side_sum, 6.0, delta=0.3)

    def test_a_line_that_leaves_the_grid_and_comes_back(self):
        # the stretch off the grid is not profile spacing: a 4:1 fill on such a line is still found,
        # with the same length as on a straight line
        dem, gt, line, _f = self.draw_dam(0.004, 3.0, 0.0, pool_below=1.0, face=4.0)
        xm = line[0][0]
        top, bot = line[0][1], line[1][1]
        out = [(xm, top), (xm, top - 60 * 3.0), (xm + 2000.0, top - 62 * 3.0), (xm, top - 64 * 3.0), (xm, bot)]
        a, b = em.find(dem, gt, [line]), em.find(dem, gt, [out])
        self.assertEqual((len(a), len(b)), (1, 1))
        self.assertAlmostEqual(a[0].len_m, b[0].len_m, delta=1.0)
        self.assertAlmostEqual(a[0].height_m, b[0].height_m, delta=0.05)

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


class EdgeOfTheTerrainData(unittest.TestCase):
    """Finder 0.7 (core/embankments.py step 13): missing DEM cells stand as a wall in the spill
    test, so a fill beside them is kept but is not a candidate."""

    def test_fills_beside_missing_terrain_are_kept_but_are_not_candidates(self):
        dem, road = scene()
        dem[:, 230:] = np.nan                                # the east half is outside the data: 90 m off
        got = sorted(em.find(dem, GT, [LINE], road_mask=road), key=lambda e: -e.y)
        # each fill's pool at its spill level (the valley is 2 % to each side) runs into the missing
        # cells: all three are at the edge
        self.assertEqual([e.kind for e in got], [em.K_EDGE] * 3)
        for e, was in zip(got, (em.K_DAM, em.K_DRY, em.K_ROAD)):
            self.assertAlmostEqual(e.edge_m, 30 * CELL, delta=CELL)
            self.assertTrue(e.edge_pool)
            self.assertTrue(e.notes[0].startswith("at the edge of the terrain data: missing terrain acts as a wall here"))
            self.assertIn("which its pool reaches", e.notes[0])
            self.assertTrue(e.notes[0].endswith("it would otherwise read as: " + was), e.notes[0])
            self.assertEqual(e.confidence, "low")
        self.assertIn(em.K_EDGE, em.KINDS)
        # the readings are as they were (the wall and all): only the kind and the notes say so
        off = sorted(em.find(dem, GT, [LINE], road_mask=road, params=em.FinderParams(edge_m_max=0.0)), key=lambda e: -e.y)
        self.assertEqual([e.kind for e in off], [em.K_DAM, em.K_DRY, em.K_ROAD])
        self.assertTrue(all(math.isnan(e.edge_m) for e in off))
        self.assertEqual([(e.x, e.y, e.height_m, e.storage_m3) for e in got], [(e.x, e.y, e.height_m, e.storage_m3) for e in off])
        # your distance: 60 m leaves all three (90 m off) as they are, with the distance written; the
        # dam's flat pool runs into the missing cells, and it says so
        far = sorted(em.find(dem, GT, [LINE], road_mask=road, params=em.FinderParams(edge_m_max=60.0)), key=lambda e: -e.y)
        self.assertEqual([e.kind for e in far], [em.K_DAM, em.K_DRY, em.K_ROAD])
        self.assertTrue(all(abs(e.edge_m - 90.0) <= CELL for e in far))
        self.assertTrue(far[0].edge_pool)
        self.assertTrue(any("the pool reaches missing terrain" in t for t in far[0].notes))
        self.assertEqual(far[0].confidence, "low")

    def test_a_fill_near_the_edge_whose_pool_stays_clear_of_it_keeps_its_kind(self):
        # a fill 2 m high: its pool at the spill level ends about 100 m to each side of the valley
        # line. The terrain is missing from 135 m east of the line: within 150 m of the crest, but no
        # missing cell touches the pool, so the wall changes nothing - the same kind and readings,
        # with the edge distance written
        dem, y = valley()
        dem = add_fill(dem, y, 200, 2.0)
        whole = em.find(dem, GT, [LINE])
        self.assertEqual(len(whole), 1)
        dem[:, 245:] = np.nan
        got = em.find(dem, GT, [LINE])
        self.assertEqual(len(got), 1)
        self.assertNotEqual(got[0].kind, em.K_EDGE)
        self.assertAlmostEqual(got[0].edge_m, 135.0, delta=CELL)
        self.assertFalse(got[0].edge_pool)
        self.assertFalse(any("missing terrain" in t for t in got[0].notes))
        self.assertEqual([(e.kind, e.height_m, e.storage_m3) for e in got], [(e.kind, e.height_m, e.storage_m3) for e in whole])
        # edge_needs_pool off: every fill within the distance is set aside
        every = em.find(dem, GT, [LINE], params=em.FinderParams(edge_needs_pool=False))
        self.assertEqual([e.kind for e in every], [em.K_EDGE])
        self.assertNotIn("which its pool reaches", every[0].notes[0])

    def test_missing_terrain_that_touches_the_pool_only_at_a_corner_reaches_it(self):
        # the pool spreads through edges (4 neighbours) but water can pass between two cells that
        # meet at a corner, so missing cells diagonal to the pool's corner cell count as reached
        # (the rim of the missing cells is taken with all 8 neighbours); one cell farther off they
        # do not
        dem, y = valley()
        dem = add_fill(dem, y, 200, 2.0)
        e0 = em.find(dem, GT, [LINE])[0]
        r, c = int((GT[3] - e0.y) / CELL), int((e0.x - GT[0]) / CELL)
        r0, r1, c0, c1 = em._window(dem, r, c, int(round(1200.0 / CELL)))
        win = dem[r0:r1, c0:c1]
        u = (int((GT[3] - e0.up_xy[1]) / CELL) - r0, int((e0.up_xy[0] - GT[0]) / CELL) - c0)
        pool = np.zeros(dem.shape, bool)
        pool[r0:r1, c0:c1] = em.pool_mask(win, u, e0.spill_m)
        east = int(np.nonzero(pool.any(axis=0))[0].max())
        top = int(np.nonzero(pool[:, east])[0].min())           # the pool's north-east corner cell
        diag = dem.copy()
        diag[top - 6:top, east + 1:east + 7] = np.nan            # a 6 x 6 block off that corner
        self.assertFalse(pool[top - 6:top + 1, east + 1:east + 7].any())          # no edge contact
        self.assertFalse(pool[top - 7:top, east:east + 7].any())
        got = em.find(diag, GT, [LINE])
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].kind, em.K_EDGE)
        self.assertTrue(got[0].edge_pool)
        off = dem.copy()
        off[top - 7:top - 1, east + 2:east + 8] = np.nan         # one cell farther: no contact at all
        got = em.find(off, GT, [LINE])
        self.assertEqual(len(got), 1)
        self.assertNotEqual(got[0].kind, em.K_EDGE)
        self.assertFalse(got[0].edge_pool)

    def test_missing_terrain_far_from_the_fill_and_its_pool_changes_nothing(self):
        dem, road = scene()
        whole = sorted(em.find(dem, GT, [LINE], road_mask=road), key=lambda e: -e.y)
        self.assertTrue(all(math.isnan(e.edge_m) and not e.edge_pool for e in whole))      # no missing cell
        dem[:, 300:] = np.nan                                # 300 m east of the valley line
        got = sorted(em.find(dem, GT, [LINE], road_mask=road), key=lambda e: -e.y)
        self.assertEqual([e.kind for e in got], [em.K_DAM, em.K_DRY, em.K_ROAD])
        self.assertTrue(all(abs(e.edge_m - 300.0) <= CELL and not e.edge_pool for e in got))
        self.assertEqual([(e.height_m, e.storage_m3) for e in got], [(e.height_m, e.storage_m3) for e in whole])
        # not measured beyond the search distance
        near = em.find(dem, GT, [LINE], road_mask=road, params=em.FinderParams(edge_search_m=200.0))
        self.assertTrue(all(math.isnan(e.edge_m) for e in near))
        # a hole of a few cells is not an edge of the data (under 100 m2: 3 x 3 cells = 81 m2)
        dem, road = scene()
        dem[150:153, 210:213] = np.nan
        self.assertTrue(all(math.isnan(e.edge_m) for e in em.find(dem, GT, [LINE], road_mask=road)))
        dem[150:154, 210:214] = np.nan                       # 4 x 4 cells = 144 m2: it is
        self.assertTrue(any(math.isfinite(e.edge_m) for e in em.find(dem, GT, [LINE], road_mask=road)))

    def test_a_pool_only_missing_terrain_holds(self):
        # A spur beside the data edge. West of the spur the ground falls away west; east of it a draw
        # runs north out of the data. With the missing cells as a wall the draw is a pool 4 m deep
        # "held" by the spur: a false dam. The crest is 180 m from the missing cells (not within
        # 150 m), so the distance rule does not reach it: a known limit, and it says so in its notes.
        n = 300
        y, x = np.mgrid[0:n, 0:n]
        gt = (0.0, CELL, 0.0, n * CELL, 0.0, -CELL)
        draw = 104.0 + (y - 60) * CELL * 0.01                # the draw's floor falls north (to row 60 and out)
        z = np.where(x >= 150, draw + np.abs(x - 190) * CELL * 0.05, 0.0)
        west = 100.0 - (150 - x) * CELL * 0.03               # the ground west of the spur falls west
        z = np.where(x < 150, west, z)
        spur = (np.abs(x - 150) <= 3)
        z = np.where(spur, np.maximum(z, draw + 5.0), z)     # the spur: 5 m over the draw's floor
        z[:60, :] = np.nan                                   # north of row 60: no terrain data
        line = [((190 + 0.5) * CELL, (n - 120.5) * CELL), ((60 + 0.5) * CELL, (n - 120.5) * CELL)]   # east to west, over the spur
        got = em.find(z, gt, [line])
        self.assertEqual(len(got), 1)
        e = got[0]
        self.assertGreater(e.edge_m, 150.0)
        self.assertTrue(e.edge_pool)
        # the missing cells are a wall (the default): it keeps its kind, with a plain warning
        self.assertNotEqual(e.kind, em.K_EDGE)
        self.assertEqual(e.confidence, "low")
        self.assertTrue(any("the pool reaches missing terrain at its lowest levels" in t for t in e.notes))
        # edge_open (off by default): the missing cells are open ground, the pool gets out at once
        opened = em.find(z, gt, [line], params=em.FinderParams(edge_open=True))[0]
        self.assertEqual(opened.kind, em.K_EDGE)
        self.assertIn("its pool reaches missing terrain and gets out there", opened.notes[0])
        self.assertEqual((opened.height_m, opened.storage_m3), (e.height_m, e.storage_m3))
        # the same ground with the terrain there (north of row 55 the draw turns west round the end
        # of the spur and runs down to the lower ground): no pool, no fill
        z2 = z.copy()
        z2[:55, :] = (100.0 + (x - 150) * CELL * 0.03)[:55, :]
        z2[55:60, :] = np.where(x >= 150, draw + np.abs(x - 190) * CELL * 0.05, west)[55:60, :]
        self.assertEqual(em.find(z2, gt, [line]), [])

    def test_a_dam_whose_pool_reaches_missing_terrain_higher_up_keeps_its_kind(self):
        # the scene's dam, with the terrain missing 165 m east of the valley line: its pool at the
        # spill level (4 m deep, 200 m to each side) reaches the missing cells, but on the ground the
        # DEM has it still holds over 3 m: a dam, with a note that its height is a maximum
        dem, road = scene()
        dem[:, 255:] = np.nan
        dam = [e for e in em.find(dem, GT, [LINE], road_mask=road) if e.y > N * CELL - 230 * CELL][0]
        self.assertEqual(dam.kind, em.K_DAM)
        self.assertTrue(dam.edge_pool)
        self.assertAlmostEqual(dam.edge_m, 165.0, delta=CELL)
        self.assertTrue(any("the pool reaches missing terrain" in t and "maximum" in t for t in dam.notes))
        self.assertEqual(dam.confidence, "low")

    def test_missing_cells_as_open_ground_in_the_spill_test(self):
        # spill_level with a rim: the pool has got out once it holds a ground cell next to missing terrain
        win = np.full((40, 40), 10.0)
        win[10:30, 5:20] = 5.0                               # a basin
        win[10:30, 20:22] = 8.0                              # its east wall, 3 m high
        win[10:30, 22:35] = 4.0                              # lower ground beyond
        u, d = (20, 10), (20, 30)
        self.assertAlmostEqual(em.spill_level(win, u, d, 5.01, 10.0), 8.0, delta=0.05)
        hole = win.copy()
        hole[0:10, 5:20] = np.nan                            # missing terrain along the basin's north side
        hole[10, 5:20] = 6.0                                 # the ground next to it: 1 m over the floor
        edge, rim = em.missing_terrain(hole, 3.0)
        self.assertTrue(rim[10, 10] and edge[9, 10] and not rim[20, 10])
        self.assertAlmostEqual(em.spill_level(hole, u, d, 5.01, 10.0), 8.0, delta=0.05)              # a wall
        self.assertAlmostEqual(em.spill_level(hole, u, d, 5.01, 10.0, rim=rim), 6.0, delta=0.05)     # open
        hole[10, 5:20] = 5.0                                 # the floor runs straight into it
        edge, rim = em.missing_terrain(hole, 3.0)
        self.assertIsNone(em.spill_level(hole, u, d, 5.01, 10.0, rim=rim))
        self.assertEqual(em.missing_terrain(win, 3.0), (None, None))
        self.assertAlmostEqual(em.edge_distance(edge, 20, 10, 3.0, 1000.0), 33.0, delta=0.01)
        self.assertTrue(math.isnan(em.edge_distance(edge, 20, 10, 3.0, 30.0)))


class PoolLinesAndRescue(unittest.TestCase):
    """Finder 0.6 (core/embankments.py steps 11-12): pool-outlet lines and brim-full ponds."""

    POOL_LINE = [(XS, N * CELL - 150 * CELL - 1.0), (XS, 1.0)]     # from inside the pool (row 150)

    def test_a_pool_line_tests_only_the_pools_own_fill(self):
        dem, road = scene()
        # the line leaves the pool at row 197: within 50 m of that, the dam at row 200 is tested;
        # the dry embankment (row 260) and road fill (row 320) further down are not
        lim = {0: (197 - 150) * CELL + 50.0}
        got = em.find(dem, GT, [self.POOL_LINE], road_mask=road, line_lim=lim)
        self.assertEqual([e.kind for e in got], [em.K_DAM])
        self.assertTrue(got[0].pool_line)
        self.assertTrue(any("line out of the pool" in t for t in got[0].notes))
        # the same line as an ordinary line finds all three, none marked pool_line
        allf = em.find(dem, GT, [self.POOL_LINE], road_mask=road)
        self.assertEqual(sorted(e.kind for e in allf), sorted([em.K_DAM, em.K_DRY, em.K_ROAD]))
        self.assertFalse(any(e.pool_line for e in allf))

    def test_a_pool_line_is_never_turned_round(self):
        # given upstream-first on purpose: an ordinary line is turned round (orient_down), a pool
        # line is taken as given, so its reach is measured from its first point
        dem, _road = scene()
        rev = self.POOL_LINE[::-1]
        # the reach covers the whole line: taken as given (rising all the way) it has no hump; turned
        # round it would find the dam and both fills
        self.assertEqual(em.find(dem, GT, [rev], line_lim={0: 5000.0}), [])
        self.assertTrue(em.find(dem, GT, [rev]))

    def test_pool_rules(self):
        dem, road = scene()
        lim = {0: (197 - 150) * CELL + 50.0}
        ok = em.find(dem, GT, [self.POOL_LINE], line_lim=lim, pool_rules=dict(min_area_m2=400.0))
        self.assertEqual(len(ok), 1)
        big = ok[0].pool_area_m2
        self.assertEqual(em.find(dem, GT, [self.POOL_LINE], line_lim=lim,
                                 pool_rules=dict(min_area_m2=big * 2.0)), [])
        # a road over the outlet
        rmask = np.zeros(dem.shape, bool)
        rmask[198:203, :] = True
        self.assertEqual(em.find(dem, GT, [self.POOL_LINE], road_mask=rmask, line_lim=lim,
                                 pool_rules=dict(skip_road=True)), [])
        # drains a lot at the crest: a draw walks it
        self.assertEqual(em.find(dem, GT, [self.POOL_LINE], line_lim=lim, drain_area=lambda x, y: 1e9,
                                 pool_rules=dict(max_drain_m2=50 * em.AC_M2)), [])
        self.assertEqual(len(em.find(dem, GT, [self.POOL_LINE], line_lim=lim, drain_area=lambda x, y: 10.0,
                                     pool_rules=dict(max_drain_m2=50 * em.AC_M2))), 1)

    def test_a_pool_lines_drainage_is_read_on_its_own_path(self):
        # a creek beside the fill (drain_area: the biggest draw within a few cells) is not the pond's
        # drainage: with line_area the rule and drain_m2 read the line's own path, and at least all
        # that drains into its pool (pool_drain)
        dem, _road = scene()
        lim = {0: (197 - 150) * CELL + 50.0}
        creek = lambda x, y: 800 * em.AC_M2
        two = len(self.POOL_LINE)
        rules = dict(max_drain_m2=50 * em.AC_M2, line_area={0: [2 * em.AC_M2] * two})
        self.assertEqual(em.find(dem, GT, [self.POOL_LINE], line_lim=lim, drain_area=creek,
                                 pool_rules=dict(max_drain_m2=50 * em.AC_M2)), [])
        got = em.find(dem, GT, [self.POOL_LINE], line_lim=lim, drain_area=creek, pool_rules=rules)
        self.assertEqual(len(got), 1)
        self.assertAlmostEqual(got[0].drain_m2, 2 * em.AC_M2)
        got = em.find(dem, GT, [self.POOL_LINE], line_lim=lim, drain_area=creek,
                      pool_rules=dict(rules, pool_drain={0: 7 * em.AC_M2}))
        self.assertAlmostEqual(got[0].drain_m2, 7 * em.AC_M2)
        # the pool the fill holds (closed, its outlet at the fill): all that drains into it, read on
        # its cells at the spill level
        seen = []

        def inflow(xs, ys, near):
            seen.append((len(xs), near))
            return 9 * em.AC_M2

        dem2, y = valley()
        dem2 = add_fill(dem2, y, 200, 1.7)              # low enough that its pool ends on the floor
        dem2 = np.where((y < 197) & (dem2 < bed(200) + 1.0), bed(200) + 1.0, dem2)
        got = em.find(dem2, GT, [self.POOL_LINE], line_lim=lim, drain_area=creek,
                      pool_rules=dict(rules, inflow=inflow))
        self.assertEqual(len(got), 1)
        self.assertFalse(got[0].open_pool)
        self.assertAlmostEqual(got[0].drain_m2, 9 * em.AC_M2)
        self.assertAlmostEqual(seen[-1][0] * CELL * CELL, got[0].pool_area_m2)
        # only the water that leaves the pool by this fill (near it)
        nx, ny, rad = seen[-1][1]
        self.assertLess(math.hypot(nx - got[0].x, ny - got[0].y), CELL)
        self.assertGreaterEqual(rad, em.FinderParams().outlet_m + 0.5 * got[0].len_m)
        self.assertLessEqual(rad, em.FinderParams().outlet_m + 0.5 * got[0].len_m + 20.0 + CELL)
        # the same pool on a grid whose window does not start at its corner: the points are the
        # pool's own cells
        pts = []

        def at(xs, ys, near):
            pts.append((np.asarray(xs), np.asarray(ys)))
            return 9 * em.AC_M2

        pad = 500
        big = np.pad(dem2, ((pad, 0), (pad, 0)), mode="edge")
        gt_big = (-pad * CELL, CELL, 0.0, (N + pad) * CELL, 0.0, -CELL)
        got = em.find(big, gt_big, [self.POOL_LINE], line_lim=lim, drain_area=creek,
                      pool_rules=dict(rules, inflow=at))
        self.assertEqual(len(got), 1)
        xs, ys = pts[-1]
        self.assertAlmostEqual(float(xs.mean()), got[0].pool_xy[0], places=6)
        self.assertAlmostEqual(float(ys.mean()), got[0].pool_xy[1], places=6)
        self.assertTrue(0.0 < xs.min() and xs.max() < N * CELL and 0.0 < ys.min() and ys.max() < N * CELL)
        # the pool also gets out by a bypass channel round the east end of the fill (a notch 100 m
        # upstream, 0.2 m under the crest, back to the valley below the dam): it is read while that
        # outlet is within outlet_m of the fill and not when it is further
        yy, xx = np.mgrid[0:N, 0:N]
        sad = bed(200) + 1.5
        by = np.where((np.abs(yy - 167) <= 2) & (xx >= N // 2) & (xx <= N // 2 + 43), np.minimum(dem2, sad), dem2)
        side = (yy >= 165) & (yy <= 235) & (xx >= N // 2 + 40) & (xx <= N // 2 + 43)
        by = np.where(side, np.minimum(by, sad - np.maximum(0, yy - 169) * 0.012), by)
        back = (np.abs(yy - 233) <= 2) & (xx >= N // 2) & (xx <= N // 2 + 43)
        by = np.where(back, np.minimum(by, sad - (233 - 169) * 0.012), by)
        seen[:] = []
        got = em.find(by, GT, [self.POOL_LINE], line_lim=lim, drain_area=creek, pool_rules=dict(rules, inflow=inflow))
        self.assertEqual(len(got), 1)
        self.assertFalse(got[0].open_pool)
        self.assertTrue(10.0 < got[0].outlet_dist_m <= em.FinderParams().outlet_m)
        self.assertEqual(len(seen), 1)
        seen[:] = []
        got = em.find(by, GT, [self.POOL_LINE], line_lim=lim, drain_area=creek, params=em.FinderParams(outlet_m=10.0),
                      pool_rules=dict(rules, inflow=inflow))
        self.assertEqual(len(got), 1)
        self.assertFalse(got[0].open_pool)
        self.assertEqual(seen, [])
        self.assertAlmostEqual(got[0].drain_m2, 2 * em.AC_M2)
        # the window cuts the pool (a valley floor, not a pond): not read
        got = em.find(dem2, GT, [self.POOL_LINE], line_lim=lim, drain_area=creek,
                      params=em.FinderParams(window_m=150.0), pool_rules=dict(rules, inflow=inflow))
        self.assertTrue(got and got[0].open_pool)
        self.assertAlmostEqual(got[0].drain_m2, 2 * em.AC_M2)
        # its own path drains a lot at the crest: a draw walks it
        self.assertEqual(em.find(dem, GT, [self.POOL_LINE], line_lim=lim, drain_area=lambda x, y: 0.0,
                                 pool_rules=dict(rules, line_area={0: [90 * em.AC_M2] * two})), [])

    def test_a_long_fills_drainage_on_a_pool_line(self):
        # Finder 0.7 looked at giving long fills less drainage (not taking the seed's pool when the
        # held pool's inflow was read) and left the rule alone: a bank right across the valley
        # (1.2 km: a long fill) holding a closed pool gets the most of its own path, the seed's
        # pool and the held pool's inflow, as a dam does - and the reading decides nothing: the
        # same fill, kind and height whatever it is
        dem, y = valley()
        dem = np.where(np.abs(y - 200) <= 4, dem + 1.7, dem)
        dem = np.where((y < 196) & (dem < bed(200) + 1.0), bed(200) + 1.0, dem)
        lim = {0: (197 - 150) * CELL + 50.0}
        creek = lambda x, y: 800 * em.AC_M2                  # a big draw beside it: not its drainage
        rules = dict(max_drain_m2=50 * em.AC_M2, line_area={0: [2 * em.AC_M2] * len(self.POOL_LINE)})
        held = lambda xs, ys, near: 9 * em.AC_M2
        seen = []
        for extra, want in (({}, 2), ({"pool_drain": {0: 7 * em.AC_M2}}, 7),
                            ({"pool_drain": {0: 7 * em.AC_M2}, "inflow": held}, 9),
                            ({"pool_drain": {0: 12 * em.AC_M2}, "inflow": held}, 12)):
            got = em.find(dem, GT, [self.POOL_LINE], line_lim=lim, drain_area=creek, pool_rules=dict(rules, **extra))
            self.assertEqual([e.kind for e in got], [em.K_LONG])
            self.assertFalse(got[0].open_pool)
            self.assertAlmostEqual(got[0].drain_m2, want * em.AC_M2)
            seen.append((got[0].x, got[0].y, got[0].height_m, got[0].storage_m3, got[0].ridge_len_m))
        self.assertEqual(len(set(seen)), 1)

    def test_a_pool_line_toe_stays_near_the_fill(self):
        # a flat pad on a hillside: the ground below its lip falls 10 % for 400 m, steeper than the
        # 8 degree face test, so an ordinary toe walk runs on down the hill (150 m) and takes its
        # fall as the fill's height; on a pool line the toe is looked for within pool_toe_m
        y, x = np.mgrid[0:N, 0:N]
        z = 100.0 - np.maximum(0, y - 101) * CELL * 0.20 + np.abs(x - N // 2) * CELL * 0.02
        z = np.where(y < 100, 100.0, z)                           # the pad
        z = np.where((y >= 100) & (y <= 101), 100.6, z)          # a 0.6 m lip at its edge
        line = [(XS, N * CELL - 50 * CELL - 1.0), (XS, 1.0)]
        p = em.FinderParams(min_height_m=0.5)
        far = em.find(z, GT, [line], params=p)
        near = em.find(z, GT, [line], params=p, line_lim={0: 1000.0})
        self.assertTrue(far and near)
        self.assertGreater(far[0].height_m, 20.0)                # 150 m of a 20 % hillside
        self.assertLess(near[0].height_m, 0.6 + p.pool_toe_m * 0.20 + 1.0)

    def test_draw_readings_are_kept_before_pool_line_readings(self):
        def e(x, h, pl, outlet=10.0):
            return em.Embankment(x=x, y=0.0, kind=em.K_DAM, on_road=False, crest_m=10, spill_m=9.0,
                                 toe_m=5, up_m=8, height_m=h, crest_height_m=5, head_m=3, valley_drop_m=0,
                                 len_m=20, storage_m3=100, pool_area_m2=100, pool=False, open_pool=False,
                                 confidence="medium", line=0, spill_xy=(50.0, 50.0), outlet_dist_m=outlet,
                                 pool_line=pl)
        draw, pool = e(0.0, 3.0, False, outlet=30.0), e(20.0, 5.0, True, outlet=1.0)
        self.assertEqual(em.one_per_pool([pool, draw]), [draw])            # same pool: the draw's
        self.assertEqual(em.dedupe([pool, draw], 60.0), [draw])            # 20 m apart: the draw's
        far = e(500.0, 5.0, True)
        far.spill_xy = (900.0, 900.0)
        got = em.dedupe(em.one_per_pool([pool, draw, far]), 60.0)
        self.assertEqual(got, [far, draw])                                 # highest first

    @staticmethod
    def brim_full(notch=0.05):
        """The scene's dam with its pool full to a notch `notch` above the water: the line's last
        cell before the dam is a 12 cm beach (within tol_m of the water), above the notch."""
        dem, y = valley()
        x = np.mgrid[0:N, 0:N][1]
        dem = add_fill(dem, y, 200, 4.0)
        lvl = bed(200) + 3.0
        dem = np.where((y < 196) & (dem < lvl), lvl, dem)
        dem = np.where((y == 196) & (dem < lvl + 0.12), lvl + 0.12, dem)
        cut = (y >= 196) & (y <= 204) & (x >= 205) & (x <= 207)          # 18 m off the line
        return np.where(cut, np.minimum(dem, lvl + notch), dem)

    def test_a_brim_full_pond_is_tested_from_its_lowest_ground(self):
        dem = self.brim_full()
        self.assertEqual(em.find(dem, GT, [LINE], params=em.FinderParams(rescue_joined=False)), [])
        got = em.find(dem, GT, [LINE])
        self.assertEqual(len(got), 1)
        d = got[0]
        self.assertTrue(d.rescued)
        self.assertEqual(d.kind, em.K_DAM)
        self.assertAlmostEqual(d.spill_m, bed(200) + 3.05, delta=0.05)
        self.assertTrue(d.spill_on_fill)
        self.assertTrue(any("tested from the pool's lowest ground" in t for t in d.notes))
        # a fill that drains a lot (a floodplain beside a creek reads the same way) is not rescued
        self.assertEqual(em.find(dem, GT, [LINE], drain_area=lambda x, y: 1e9), [])
        self.assertEqual(len(em.find(dem, GT, [LINE], drain_area=lambda x, y: 1000.0)), 1)

    def test_a_rescued_dam_reads_the_ground_under_its_pond(self):
        # the same dam read the ordinary way (its notch above the beach) and rescued: the original
        # ground, the share of the pool below grade and the flat water surface agree - the beach at
        # the dam face is not the pool's far end, and the dam is not a dug pond
        normal = em.find(self.brim_full(notch=0.30), GT, [LINE])
        rescued = em.find(self.brim_full(), GT, [LINE])
        self.assertEqual(len(normal), 1)
        self.assertEqual(len(rescued), 1)
        n, r = normal[0], rescued[0]
        self.assertFalse(n.rescued)
        self.assertTrue(r.rescued)
        self.assertGreater(n.ag_head_m, 2.5)
        self.assertAlmostEqual(r.ag_head_m, n.ag_head_m, delta=0.3)
        self.assertLess(r.below_frac, 0.1)
        self.assertTrue(r.pool)
        self.assertFalse([t for t in r.notes if "dug" in t])

    def test_a_rescue_needs_a_pool_that_gets_out_at_the_fill(self):
        dem = self.brim_full()
        # a pool under min_pool_m2 is not a pond
        self.assertEqual(em.find(dem, GT, [LINE], params=em.FinderParams(min_pool_m2=1e9)), [])
        # the pool gets out 300 m round the side, not at this fill: something else holds it
        dem2, y = valley()
        x = np.mgrid[0:N, 0:N][1]
        dem2 = add_fill(dem2, y, 200, 4.0)
        lvl = bed(200) + 3.0
        dem2 = np.where((y < 196) & (dem2 < lvl), lvl, dem2)
        dem2 = np.where((y == 196) & (dem2 < lvl + 0.12), lvl + 0.12, dem2)
        side = (y >= 90) & (y <= 240) & (x >= 300) & (x <= 302)            # a channel round it,
        dem2 = np.where(side, np.minimum(dem2, lvl + 0.05 - (y - 90) * 0.02), dem2)
        dem2 = np.where((y >= 85) & (y <= 91) & (x > 200) & (x < 300) & (dem2 > lvl), lvl, dem2)  # from the pool
        cross = (y >= 238) & (y <= 240) & (x >= 200) & (x <= 302)          # back to the valley below the dam
        dem2 = np.where(cross, np.minimum(dem2, lvl + 0.05 - 150 * 0.02), dem2)
        # the scene does take the rescue path: with no outlet rule it is a rescued dam ...
        self.assertTrue([e for e in em.find(dem2, GT, [LINE], params=em.FinderParams(outlet_m=1e9))
                         if e.rescued])
        # ... whose pool gets out 400 m away: nothing at this fill
        self.assertEqual(em.find(dem2, GT, [LINE]), [])


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

    def test_the_lists_own_status_rides_along(self):
        kn = ls.Known([(0.0, 0.0, "20061", "DWR dams", "Breached"), (1000.0, 0.0, "ND00199", "NID")])
        self.assertEqual(ls.check(10.0, 0.0, kn, 50.0)["known_state"], "Breached")
        self.assertEqual(ls.check(990.0, 0.0, kn, 50.0)["known_state"], "")
        self.assertEqual(ls.check(500.0, 0.0, kn, 50.0)["known_state"], "")


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


class PoolSeeds(unittest.TestCase):
    def test_a_small_pond_is_kept_on_a_line_out_of_its_own_flat_water(self):
        # Finder 0.7 (F0, from Iowa's census of pond dams: most missed short dams had a pond under
        # 400 m2). On a pool line the fill's pool must reach min_area_m2 (400 m2) - unless the line
        # comes out of a flat water surface and the pool the fill holds contains that seed point:
        # then own_pool_m2 (100 m2) is enough. A line out of a closed depression keeps the 400 m2.
        n = 120
        gt = (0.0, CELL, 0.0, n * CELL, 0.0, -CELL)
        rules = dict(min_area_m2=400.0)
        for water, area in ((4, 144.0), (6, 324.0)):
            z, wat, _lvl = hill_pond(water)
            rr, cc = np.nonzero(wat)
            r, c = int(rr.mean()), int(cc.mean())                # a water cell near the middle
            sx, sy = (c + 0.5) * CELL, (n - r - 0.5) * CELL
            line = [(sx, sy), (sx, 30.0)]                        # from the water down the hillside
            lim = {0: (rr.max() - r + 1) * CELL + 50.0}
            self.assertEqual(em.find(z, gt, [line], line_lim=lim, pool_rules=rules), [])     # as 0.6
            got = em.find(z, gt, [line], line_lim=lim, pool_rules=dict(rules, seed={0: (sx, sy, "flat")}))
            self.assertEqual(len(got), 1)
            self.assertEqual(got[0].kind, em.K_DAM)
            self.assertAlmostEqual(got[0].pool_area_m2, area, delta=1.0)
            self.assertGreaterEqual(got[0].height_m, 5.0 * em.FT)
            self.assertTrue(any("small pond" in t for t in got[0].notes))
            # the same pond, the same line, but out of a closed depression: not kept
            self.assertEqual(em.find(z, gt, [line], line_lim=lim,
                                     pool_rules=dict(rules, seed={0: (sx, sy, "depression")})), [])
            # a line can start from several seeds (a depression and the flat water in its cell)
            both = [(sx, sy, "depression"), (sx + 3.0, sy, "flat")]
            self.assertEqual(len(em.find(z, gt, [line], line_lim=lim, pool_rules=dict(rules, seed={0: both}))), 1)
            # a flat seed the fill's pool does not hold (it lies on the hillside beside the pond)
            self.assertEqual(em.find(z, gt, [line], line_lim=lim,
                                     pool_rules=dict(rules, seed={0: (sx + 60.0, sy, "flat")})), [])
            # own_pool_m2 is yours: above the pond's size nothing is kept
            self.assertEqual(em.find(z, gt, [line], line_lim=lim, params=em.FinderParams(own_pool_m2=area + 50.0),
                                     pool_rules=dict(rules, seed={0: (sx, sy, "flat")})), [])
        z, wat, _lvl = hill_pond(3)                              # 81 m2: under own_pool_m2
        rr, cc = np.nonzero(wat)
        sx, sy = (int(cc.mean()) + 0.5) * CELL, (n - int(rr.mean()) - 0.5) * CELL
        self.assertEqual(em.find(z, gt, [[(sx, sy), (sx, 30.0)]], line_lim={0: 60.0},
                                 pool_rules=dict(rules, seed={0: (sx, sy, "flat")})), [])

    def test_flat_water_from_100_m2_seeds_a_line(self):
        # 6 x 6 cells of water: 4 x 4 of them (144 m2) pass the 3 x 3 flat test (a flat's own edge
        # cells do not). A flat seed since 0.7 (100 m2); not at the 0.6 size (300 m2)
        z, wat, _lvl = hill_pond(6)
        gt = (0.0, CELL, 0.0, z.shape[0] * CELL, 0.0, -CELL)
        flat = [s for s in tr.pool_seeds(z, gt)[0] if s[3] == "flat"]
        self.assertEqual(len(flat), 1)
        self.assertAlmostEqual(flat[0][2], 144.0, delta=1.0)
        c, r = int(flat[0][0] / CELL), int((z.shape[0] * CELL - flat[0][1]) / CELL)
        self.assertTrue(wat[r, c])
        self.assertEqual([s for s in tr.pool_seeds(z, gt, flat_min_m2=300.0)[0] if s[3] == "flat"], [])
        self.assertEqual([s for s in tr.pool_seeds(hill_pond(4)[0], gt)[0] if s[3] == "flat"], [])   # 2 x 2 flat

    def test_the_toe_reach_is_off_unless_asked_for(self):
        # pool_toe_reach_m (0 = off, the default): on a pool line the toe is at least the lowest
        # ground that far below the hump. Off, every reading is as it was; on, the toe can only be
        # lower and the fill higher.
        self.assertEqual(em.FinderParams().pool_toe_reach_m, 0.0)
        n = 120
        gt = (0.0, CELL, 0.0, n * CELL, 0.0, -CELL)
        z, wat, _lvl = hill_pond(6)
        rr, cc = np.nonzero(wat)
        sx, sy = (int(cc.mean()) + 0.5) * CELL, (n - int(rr.mean()) - 0.5) * CELL
        line, lim = [(sx, sy), (sx, 30.0)], {0: 70.0}
        rules = dict(min_area_m2=100.0)

        def read(**kw):
            got = em.find(z, gt, [line], line_lim=lim, pool_rules=rules, params=em.FinderParams(**kw))
            self.assertEqual(len(got), 1)
            return got[0]

        off, zero, on = read(), read(pool_toe_reach_m=0.0), read(pool_toe_reach_m=10.0)
        self.assertEqual((off.toe_m, off.height_m, off.spill_m, off.toe_xy), (zero.toe_m, zero.height_m, zero.spill_m, zero.toe_xy))
        self.assertLess(on.toe_m, off.toe_m)                     # 10 m further down the 6 % hillside
        self.assertGreater(on.height_m, off.height_m)
        self.assertEqual(on.spill_m, off.spill_m)
        # an ordinary line (a draw or a stream) is never given the reach
        a = em.find(z, gt, [line], params=em.FinderParams(pool_toe_reach_m=10.0))
        b = em.find(z, gt, [line])
        self.assertEqual([(e.toe_m, e.height_m) for e in a], [(e.toe_m, e.height_m) for e in b])

    def test_depression_and_flat_water_seeds(self):
        # valley sides of 10 %: a 2 % side slope on 3 m cells spans under 15 cm in 3 x 3 and
        # would read as one flat with the pool (the ring test then rejects the lot)
        y, x = np.mgrid[0:N, 0:N]
        dem = 100.0 - y * CELL * SLOPE + np.abs(x - N // 2) * CELL * 0.10
        # a closed pit 1 m deep, 30 m across, on the valley side
        pit = (np.hypot(y - 100, x - 300) <= 5)
        dem = np.where(pit, dem - 1.0, dem)
        # a flat water surface held by a fill: the scene's pool
        dem = add_fill(dem, y, 200, 4.0)
        lvl = bed(200) + 3.0
        dem = np.where((y < 197) & (dem < lvl), lvl, dem)
        seeds, mask = tr.pool_seeds(dem, GT)
        kinds = {k for *_xy, _a, k in seeds}
        self.assertEqual(kinds, {"flat", "depression"})
        near_pit = [s for s in seeds if s[3] == "depression" and abs(s[0] - (300.5 * CELL)) <= 6 * CELL
                    and abs(s[1] - (N - 100.5) * CELL) <= 6 * CELL]
        self.assertTrue(near_pit)
        self.assertTrue(mask[pit].any() and mask[150, N // 2])
        self.assertFalse(mask[300, N // 2])
        # a sloping field is no pool
        flat_seeds, _m = tr.pool_seeds(valley()[0], GT)
        self.assertEqual(flat_seeds, [])

    def test_a_pond_on_gentle_ground_is_a_pool_and_a_bench_is_not(self):
        y, x = np.mgrid[0:240, 0:240]
        for shore in (0.01, 0.02):
            # 1 % fall, gentle sides: the pond and its shores pass the 3 x 3 test as one flat whose
            # rim reads lower than its median; tried again on its level, the water is a pool
            z = 100.0 - y * CELL * 0.01 + np.abs(x - 120) * CELL * shore
            lvl = 100.0 - 150 * CELL * 0.01 + 2.0
            z = np.where(np.abs(y - 150) <= 2, np.maximum(z, lvl + 1.0), z)
            pool = (y < 148) & (z < lvl)
            z = np.where(pool, lvl, z)
            z = np.where((np.abs(y - 150) <= 2) & (np.abs(x - 126) <= 1), lvl + 0.10, z)     # a notch
            lab, keep = tr.flat_pools(z, CELL)
            self.assertIn(lab[130, 120], keep)
            m = lab == lab[130, 120]
            self.assertTrue(m[pool].mean() > 0.9)                      # the water, ...
            self.assertGreater((np.abs(z[m] - lvl) <= 0.03).mean(), 0.9)  # ... not its shores
        # not pools: gentle sloping swales, ...
        for fall, side in ((0.01, 0.0), (0.005, 0.002), (0.003, 0.002), (0.001, 0.0005)):
            self.assertEqual(tr.flat_pools(100.0 - y * CELL * fall + np.abs(x - 120) * CELL * side, CELL)[1], [])
        # ... a sharp-edged level bench on a 10 % valley side (the 3 x 3 test leaves its edge cells
        # out, so the rim is read beyond them), a level pad on a 2 % slope, a level hilltop
        yy, xx = np.mgrid[0:N, 0:N]
        side = 100.0 - yy * CELL * SLOPE + np.abs(xx - N // 2) * CELL * 0.10
        bench = side.copy()
        bench[50:81, 300:341] = side[65, 320]
        self.assertEqual(tr.flat_pools(bench, CELL)[1], [])
        pad = 100.0 - yy * CELL * 0.02
        pad[100:140, 100:160] = pad[120, 130]
        self.assertEqual(tr.flat_pools(pad, CELL)[1], [])
        r = np.hypot(yy - 200, xx - 200)
        self.assertEqual(tr.flat_pools(100.0 - np.maximum(r, 15) * CELL * 0.05, CELL)[1], [])
        # ... nor a level pit with lower ground along a fifth of its rim (a ditch beside it; at most
        # 0.16 of the rim may be lower: an Iowa pond flush with its dam crest for a few metres reads 0.15)
        pit = np.full((120, 120), 100.0)
        pit[40:80, 40:80] = 99.0
        lab, keep = tr.flat_pools(pit, CELL)
        self.assertIn(lab[60, 60], keep)
        pit[80, 42:72] = 98.5
        lab, keep = tr.flat_pools(pit, CELL)
        self.assertNotIn(lab[60, 60], keep)
        # ... nor, on its own water level, a pond whose rim runs 150 m lower than the water (the
        # retry's rim share is 0.10: on real ground a contour band round a field hollow has a lower
        # side over 10-25 % of its rim)
        y, x = np.mgrid[0:240, 0:240]
        z = 100.0 - y * CELL * 0.01 + np.abs(x - 120) * CELL * 0.01
        lvl = 100.0 - 150 * CELL * 0.01 + 2.0
        z = np.where(np.abs(y - 150) <= 2, np.maximum(z, lvl + 1.0), z)
        z = np.where((y < 148) & (z < lvl), lvl, z)
        lab, keep = tr.flat_pools(z, CELL)
        self.assertIn(lab[130, 120], keep)
        z = np.where((y >= 148) & (y <= 152) & (np.abs(x - 120) <= 25), lvl - 0.05, z)
        lab, keep = tr.flat_pools(z, CELL)
        self.assertNotIn(lab[130, 120], keep)
        # ... nor a tilted floor ringed by higher ground (not level: it falls 3 m; only a level strip
        # of it, its low end against the wall, can be one)
        tilt = np.where((yy < 100) | (yy > 300) | (xx < 100) | (xx > 300), 110.0, 100.0 - yy * CELL * 0.005)
        lab, keep = tr.flat_pools(tilt, CELL)
        self.assertTrue(all(np.ptp(tilt[lab == k]) <= 2 * 0.03 for k in keep))

    def test_pool_inflow_counts_each_cell_once(self):
        # a chain 0 -> 1 -> ... -> 6 -> out whose path leaves the pool {2, 3, 5} at 3 and comes back at
        # 5: the cells reaching the pool are 0-5, not area[3] + area[5]; a pool whose water leaves by
        # two cells (flat routing) has both; a sink in a pool counts
        rec = np.array([1, 2, 3, 4, 5, 6, -1, -1, 10, 11, 12, 13, -1, -1])
        area = np.array([1, 2, 3, 4, 5, 6, 7, 1, 1, 1, 2, 2, 3, 3], float)
        self.assertEqual(tr.pool_inflow([2, 3, 5], rec, area), 6.0)
        self.assertEqual(tr.pool_inflow([10, 11], rec, area), 4.0)
        self.assertEqual(tr.pool_inflow([7], rec, area), 1.0)
        # near: only what leaves by a pool cell that close (row, col, radius on a 2-D grid)
        a2 = area.reshape(2, 7)
        self.assertEqual(tr.pool_inflow([10, 11], rec, a2, near=(1, 3, 0.5)), 2.0)
        self.assertEqual(tr.pool_inflow([10, 11], rec, a2, near=(1, 4, 0.5)), 2.0)
        self.assertEqual(tr.pool_inflow([10, 11], rec, a2, near=(1, 3.5, 1.0)), 4.0)
        self.assertEqual(tr.pool_inflow([2, 3, 5], rec, a2, near=(0, 0, 1.0)), 0.0)
        # on routed terrain, against every cell's path walked out
        rng = np.random.default_rng(3)
        dem = 100.0 + np.cumsum(rng.normal(0, 0.3, (60, 60)), axis=0) * 0.1 + rng.normal(0, 0.05, (60, 60))
        rec, order, _f = tr.flow(dem)
        area, _l = tr.accumulate(rec, order, dem.shape, 1.0)
        for r0, c0 in ((10, 10), (30, 40), (45, 20)):
            pool = {r * 60 + c for r in range(r0, r0 + 6) for c in range(c0, c0 + 4)}
            reach = 0
            for i in range(3600):
                k = i
                while k >= 0:
                    if k in pool:
                        reach += 1
                        break
                    k = int(rec[k])
            self.assertAlmostEqual(tr.pool_inflow(pool, rec, area), float(reach))

    def test_pool_lines_leave_their_own_pool(self):
        # with pools (each seed's own cells) a line leaves its pool where its own pool ends, not where
        # a pool beside it does; with_index names each line's seed
        rec = np.array([1, 2, 3, 4, 5, 6, 7, 8, 9, -1])
        gt = (0.0, 10.0, 0.0, 10.0, 0.0, -10.0)
        seeds = [(5.0, 5.0, 100.0, "flat")]
        lines, exits = tr.pool_lines(seeds, rec, gt, (1, 10), pool=np.array([[True] * 6 + [False] * 4]),
                                     min_cells=2)
        self.assertEqual(exits, [60.0])
        lines, exits, idx = tr.pool_lines(seeds, rec, gt, (1, 10), pools=[{0, 1}], min_cells=2, with_index=True)
        self.assertEqual((exits, idx), ([20.0], [0]))

    def test_pool_lines_leave_the_pool_and_go_on(self):
        dem, _road = scene()
        o = tr.open_ridges(dem, CELL, 60.0)
        rec, _order, _f = tr.flow(o)
        seeds, mask = tr.pool_seeds(dem, GT)
        seeds.sort(key=lambda sd: -sd[2])
        lines, exits = tr.pool_lines(seeds, rec, GT, dem.shape, pool=mask, down_m=150.0)
        self.assertEqual(len(lines), len(exits))
        k = max(range(len(lines)), key=lambda i: exits[i])        # the big pool behind the dam
        self.assertGreater(exits[k], 0.0)
        ys = [p[1] for p in lines[k]]
        self.assertLess(min(ys), (N - 200) * CELL)                 # it crosses the dam (row 200)
        # and stops about down_m below where it left the pool
        run = sum(math.hypot(a[0] - b[0], a[1] - b[1]) for a, b in zip(lines[k][:-1], lines[k][1:]))
        self.assertAlmostEqual(run - exits[k], 150.0, delta=2 * CELL * 1.5)
        # no pool mask: the line leaves 'the pool' at once
        l2, e2 = tr.pool_lines(seeds[:1], rec, GT, dem.shape, down_m=60.0)
        self.assertEqual(e2, [0.0])


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
        from unittest import mock
        with mock.patch.object(sc.ch, "bagging_pu", wraps=sc.ch.bagging_pu) as bp:
            m = sc.train(rows, features=sc.ALL_FEATURES, bags=True, learner="trees", n_bags=3,
                         extra=[(dict(rows[1]), 1.0, 20.0), (dict(rows[2]), 0.5, 20.0)])
        self.assertEqual((m["version"], m["learner"]), (3, "pu-gbm"))
        self.assertEqual(m["clean_positives"]["bags"], 80)
        self.assertEqual(m["trusted_labels"], 2)
        # what the learner got: the yes mark, and the unsure one as both a dam and not, half each
        tr = np.asarray(bp.call_args.kwargs["trusted"], bool)
        self.assertEqual(np.asarray(bp.call_args.args[1])[tr].tolist(), [1.0, 1.0, 0.0])
        self.assertEqual(np.asarray(bp.call_args.kwargs["sw"])[tr].tolist(), [20.0, 10.0, 10.0])
        test = self.rows05(seed=9)
        self.assertGreater(sc.ranked_auc(sc.score(m, test), sc.labels(test)), 0.85)
        p = os.path.join(tempfile.mkdtemp(), "t.json")
        sc.save(m, p)
        self.assertTrue(np.allclose(sc.score(sc.load(p), test), sc.score(m, test), atol=1e-3))
        self.assertEqual(len(sc.score_detail(m, test[:3])), 3)
        with self.assertRaises(ValueError):
            sc.train(rows, learner="forest")

    def test_the_shipped_models_load_and_rank(self):
        """The default model brand.py names is the 0.5 trees kind, and every model in models/
        loads and ranks dams above the rest. A build that ships no model is skipped."""
        import re
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        mdir = os.path.join(here, "models")
        if not os.path.isdir(mdir):
            self.skipTest("this build ships no score model")
        with open(os.path.join(here, "brand.py"), encoding="utf-8") as fh:
            dm = re.search(r'^DEFAULT_MODEL = "([^"]+)"', fh.read(), re.M).group(1)
        self.assertTrue(os.path.exists(os.path.join(here, dm)), dm)
        self.assertEqual(sc.load(os.path.join(here, dm)).get("learner"), "pu-gbm")
        test = self.rows05(seed=9)
        y = sc.labels(test)
        for f in sorted(os.listdir(mdir)):
            s = sc.score(sc.load(os.path.join(mdir, f)), test)
            self.assertTrue(np.all(np.isfinite(s)) and s.min() >= 0.0 and s.max() <= 1.0, f)
            self.assertGreater(sc.ranked_auc(s, y), 0.9, f)

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
        # most listed dams unmeasured (200), a few measured (about 7 per group): the unmeasured
        # ones may be in any group, so no 'only 7' note - unless even all of them are too few
        m = sc.train(self.rows(n=600) + self.rows05(n=60))
        self.assertGreater(m["group_positives"].get("", 0), 150)
        notes = [msg for _s, msg in sc.score_detail(m, self.rows05(n=30))]
        self.assertFalse(any("terrain" in msg for msg in notes))
        notes = [msg for _s, msg in sc.score_detail(m, self.rows05(n=30), min_group_positives=1000)]
        self.assertTrue(any("terrain: only" in msg for msg in notes))

    def test_a_yes_on_a_listed_fill_holds_its_bag(self):
        # a listed dam's bag: the fill on it and a road fill 250 ft off. A 'yes' on the dam's
        # fill is that dam's positive - the road fill must not become a second one; a 'no' on it
        # leaves the road fill in, to be promoted
        rows = self.rows05()
        for i, r in enumerate(rows):
            r["find_id"] = "F{}".format(i)
        rows.append(dict(rows[0], find_id="FD", on_road="yes", known_ft=250.0))
        ids = lambda rr: {r["find_id"] for r in rr}
        for t, bags, has_decoy in ((1.0, True, False), (0.5, True, False), (0.0, True, True), (1.0, False, True)):
            kept, _bg = sc._without_marked(rows, None, [(dict(rows[0]), t, 20.0)], ("dam",), bags)
            self.assertNotIn("F0", ids(kept))
            self.assertEqual("FD" in ids(kept), has_decoy, (t, bags))
            self.assertEqual(len(kept), len(rows) - (1 if has_decoy else 2))
        m = sc.train(rows, features=sc.ALL_FEATURES, bags=True, extra=[(dict(rows[0]), 1.0, 20.0)])
        self.assertEqual(m["clean_positives"]["bags"], 79)              # D0 is the mark now
        # a mark on the bag's farther fill (the road fill) replaces that fill alone: the fill at
        # the listed point stays the dam's positive, whatever the mark
        rows[0]["known_ft"] = 20.0
        for t in (1.0, 0.5, 0.0):
            kept, _bg = sc._without_marked(rows, None, [(dict(rows[-1]), t, 20.0)], ("dam",), True)
            self.assertIn("F0", ids(kept), t)
            self.assertNotIn("FD", ids(kept), t)

    def test_trees_background_counts_the_marked_dams(self):
        # 3 listed dams and 30 'yes' marks: the background is 5 x 33, not 5 x 3
        from ..core import challenger as ch
        rng = np.random.default_rng(0)
        x = rng.normal(size=(400, 3))
        y = np.zeros(400)
        y[:3] = 1.0
        tr = np.zeros(400, bool)
        tr[3:33] = True
        y[3:33] = 1.0
        with mock_fit(ch) as fit:
            ch.bagging_pu(x, y, n_bags=2, trusted=tr)
        self.assertEqual(len(fit.call_args_list[0].args[1]), 3 + 5 * 33 + 30)
        with self.assertRaises(ValueError):                 # every row a dam: a constant model
            ch.bagging_pu(x[:40], np.ones(40), n_bags=2)

    def test_listed_dams_the_list_says_are_not_there_are_left_out(self):
        rows = self.rows(n=120)
        gone = [r for r in rows if r["status"] == "listed"][1:9]
        for r, st in zip(gone, ("Breached", "Not Built", "Removed", "Location Incon", "Submerged",
                                "Breached", "Pending", "Inconclusive")):
            r["known_state"] = st
        rows[0]["known_state"] = "Intact"
        self.assertTrue(all(sc.off_ground(r) for r in gone))
        self.assertFalse(sc.off_ground(rows[0]) or sc.off_ground(rows[1]))   # intact; not listed
        self.assertEqual(int(sc.labels(rows).sum()), 40 - 8)
        self.assertEqual(sc.labels(gone).tolist(), [0.0] * 8)
        for kw in ({}, {"features": sc.ALL_FEATURES, "learner": "trees", "n_bags": 2}):
            m = sc.train(rows, **kw)
            self.assertEqual((m["not_on_ground"], m["n_listed"]), (8, 32), kw)
            m2 = sc.train([r for r in rows if not sc.off_ground(r)], **kw)
            self.assertTrue(np.allclose(sc.score(m, rows), sc.score(m2, rows)), kw)

    def test_round_trip(self):
        import os
        import tempfile
        m = sc.train(self.rows())
        p = os.path.join(tempfile.mkdtemp(), "m.json")
        sc.save(m, p)
        self.assertEqual(sc.load(p)["weights"], m["weights"])


def mock_fit(ch):
    from unittest import mock
    return mock.patch.object(ch, "fit_gbm", wraps=ch.fit_gbm)


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
        # chances that differ only by Monte Carlo noise are one stratum, not one stratum a card
        jit = [1.0] * 10 + [0.1 + 1e-3 * ((-1) ** i) * (i % 3) for i in range(20)]
        hj = ev.ht_share(y, jit)
        self.assertAlmostEqual(hj["lo"], h["lo"], delta=0.01)
        self.assertAlmostEqual(hj["hi"], h["hi"], delta=0.01)

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

    # ------------------------------------------------- review_order: what is below, a second sort --
    def test_review_order_is_score_band_first_then_what_is_below(self):
        #         0     1     2     3     4     5
        score = [0.95, 0.91, 0.84, 0.31, 0.39, 0.30]
        below = [0.0,  6.0,  40.0, 2.0,  0.0,  9.0]
        # band 0.9-1.0: the fill with buildings below first; a lower band never passes it, however
        # much is below (fill 2); band 0.3-0.4 by what is below
        self.assertEqual(evd.review_order(score, below), [2, 1, 3, 5, 6, 4])
        # no consequence computed: by score
        self.assertEqual(evd.review_order(score), [1, 2, 3, 5, 4, 6])
        self.assertEqual(evd.review_order(score, [None] * 6), [1, 2, 3, 5, 4, 6])
        self.assertEqual(evd.review_order([]), [])
        with self.assertRaises(ValueError):
            evd.review_order([0.5, 0.6], [1.0])

    def test_review_order_ties_and_band_edges(self):
        # same band, same consequence: the higher score first; all the same: as they came (stable)
        self.assertEqual(evd.review_order([0.52, 0.58, 0.55], [3.0, 3.0, 3.0]), [3, 1, 2])
        self.assertEqual(evd.review_order([0.5, 0.5, 0.5], [1.0, 1.0, 1.0]), [1, 2, 3])
        self.assertEqual(evd.review_order([0.5, 0.5, 0.5]), [1, 2, 3])
        # a band edge is in the band above it, whatever the float says (0.3 / 0.1 is 2.999...), and
        # a score of exactly 1 is in the top band, not one of its own
        self.assertEqual(evd.review_order([0.3, 0.39, 0.2999], [0.0, 5.0, 50.0]), [2, 1, 3])
        self.assertEqual(evd.review_order([0.7, 0.79, 0.6999], [0.0, 5.0, 50.0]), [2, 1, 3])
        self.assertEqual(evd.review_order([1.0, 0.92], [0.0, 4.0]), [2, 1])
        # the band width is the caller's: with bands 0.5 wide these three are tied on score
        self.assertEqual(evd.review_order([0.95, 0.91, 0.55], [0.0, 1.0, 7.0], band=0.5), [3, 2, 1])
        # every fill gets a different number, 1 to n
        rng = np.random.default_rng(5)
        got = evd.review_order(rng.random(200).round(2), rng.integers(0, 4, 200).astype(float))
        self.assertEqual(sorted(got), list(range(1, 201)))

    def test_review_order_missing_consequence_and_missing_score(self):
        # a fill whose consequence was not computed counts as nothing found below: the score
        # decides between it and a fill with 0 below, and a fill with something below is first
        self.assertEqual(evd.review_order([0.41, 0.45, 0.43], [None, 0.0, 1.0]), [3, 2, 1])
        self.assertEqual(evd.review_order([0.41, 0.45], [float("nan"), 0.0]), [2, 1])
        # a fill with no score comes after every scored fill, whatever is below it; two of them
        # by what is below, then as they came
        self.assertEqual(evd.review_order([None, 0.05, float("nan"), 0.6, None], [99.0, 0.0, 120.0, 0.0, 99.0]),
                         [4, 2, 3, 1, 5])

    def test_bare_ground_and_class_share(self):
        m = evd.disk((100, 100), self.GT1, 50.0, 50.0, 30.0)
        red = np.where(m, 200.0, 60.0)
        nir = np.where(m, 190.0, 160.0)                     # bare: NIR ~ red; grass: NIR >> red
        self.assertGreater(evd.bare_share(red, red, red, nir, m), 0.9)
        # an image that holds a sliver of the disk is no reading
        sliver = red.copy()
        sliver[:, 25:] = np.nan
        self.assertTrue(np.isnan(evd.bare_share(sliver, red, red, nir, m)))
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

    def test_a_later_round_samples_the_cards_not_yet_reviewed(self):
        # the documented flow: the same seed, the earlier batches as reviewed. A round samples the
        # pool of unreviewed cards (reviewed cards never come back); its chances add up to the
        # pool, and the earlier cards at chance 1 plus this batch estimate the whole list
        rows = self.cands()
        trait = {r["find_id"]: r["score"] > 0.7 for r in rows}
        ratio, est = [], []
        for seed in range(40):
            r1 = rv.pick_batch(rows, 90, seed=seed)
            done = {r["find_id"] for r in r1}
            r2 = rv.pick_batch(rows, 90, seed=seed, reviewed=done)
            self.assertFalse(done & {r["find_id"] for r in r2})
            self.assertEqual({r["pool_n"] for r in r2}, {600 - len(done)})
            ratio.append(sum(1.0 / r["incl_prob"] for r in r2) / (600 - len(done)))
            h = ev.ht_share([trait[r["find_id"]] for r in r1 + r2], [1.0] * len(r1) + [r["incl_prob"] for r in r2])
            est.append(h["share"])
        self.assertAlmostEqual(float(np.mean(ratio)), 1.0, delta=0.07)
        self.assertAlmostEqual(float(np.mean(est)), float(np.mean(list(trait.values()))), delta=0.03)

    def test_the_whole_list_interval_covers_after_two_rounds(self):
        # dams concentrated near the top (as a score makes them); round 2 with the same seed; the
        # whole list = round 1 at chance 1 + round 2. The interval must not sit above the truth:
        # small strata's Agresti-Coull pseudo-counts must not lift the lower bound
        rows = self.cands()
        s = np.array([r["score"] for r in rows])
        rank = np.empty(len(rows), int)
        rank[np.argsort(-s)] = np.arange(1, len(rows) + 1)
        rng = np.random.default_rng(12)
        dam = {r["find_id"]: bool(rng.random() < 0.03 + 0.57 * np.exp(-(k - 1) / 80.0)) for r, k in zip(rows, rank)}
        truth = float(np.mean(list(dam.values())))
        low = high = 0
        n = 120
        for seed in range(n):
            r1 = rv.pick_batch(rows, 90, seed=seed, per_tile_cap=1000)
            r2 = rv.pick_batch(rows, 90, seed=seed, per_tile_cap=1000, reviewed={r["find_id"] for r in r1})
            h = ev.ht_share([dam[r["find_id"]] for r in r1 + r2], [1.0] * len(r1) + [r["incl_prob"] for r in r2])
            low += truth < h["lo"]
            high += truth > h["hi"]
        self.assertLessEqual(low, 0.08 * n)
        self.assertLessEqual(high, 0.08 * n)

    def test_a_batch_that_takes_the_whole_pool_is_a_census(self):
        rows = self.cands(n=200)
        s = np.array([r["score"] for r in rows])
        order = np.argsort(-s)
        keep = {rows[i]["find_id"] for i in list(order[:5]) + [order[-1]]}
        done = {r["find_id"] for r in rows} - keep
        for seed in range(5):
            b = rv.pick_batch(rows, 90, seed=seed, reviewed=done)
            self.assertEqual({r["find_id"] for r in b}, keep)
            self.assertTrue(all(r["incl_prob"] == 1.0 for r in b))
        small = self.cands(n=10)
        for seed in range(5):
            b = rv.pick_batch(small, 90, seed=seed)
            self.assertEqual(len(b), 10)
            self.assertTrue(all(r["incl_prob"] == 1.0 for r in b), seed)

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
        # a reviewer's own blind second look does not replace their first mark (named or not)
        for rv_ in ("", "ann"):
            d3 = tempfile.mkdtemp()
            ds.add(d3, "FIND-1", "yes", reviewer=rv_)
            if rv_:
                ds.add(d3, "FIND-1", "no", reviewer="bob")
            ds.add(d3, "FIND-1", "no", {"second_look": True}, reviewer=rv_)
            st3 = ds.stats(d3)
            self.assertEqual((st3["cards"], st3["yes"], st3["no"], st3["second_looks"]), (1, 1, 0, 1), rv_)
            self.assertEqual(sorted(r["label"] for r in ds.load(d3) if r["reviewer"] == rv_), ["no", "yes"])


if __name__ == "__main__":
    unittest.main()
