#!/usr/bin/env python3
"""The Edenville -> Sanford 2020 cascade case: the published geometry hangs together, the Runner's
own breach and flood code reads the dam inputs, and the scoring reads a water surface the way it
says. No internet, no LISFLOOD-FP: numpy only (GDAL only for the one scoring test, skipped without).

    python3 -W error tools/validation/edenville_2020/test_case.py"""
import os
import sys
import unittest

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
sys.path.insert(0, HERE)
sys.path.insert(0, REPO)
import cascade_case as C  # noqa: E402

FT = C.FT
ACFT = 1233.4818375475  # m3
ACRE = 4046.8564224     # m2


def _curve_normal_storage(d):
    """Storage (ac-ft) at normal pool from the Runner's elevation-volume curve
    V = Vtop (y/H)^m, m = area_top * H / Vtop (breach.Reservoir)."""
    h, vt, area = d["dam_height_ft"], d["storage_top_acft"], d["surface_area_top_ac"]
    below = d["top_of_dam_ft"] - d["normal_pool_ft"]
    m = area * h / vt
    return vt * ((h - below) / h) ** m


class Published(unittest.TestCase):
    def test_the_water_runs_downhill_from_dam_to_dam_to_gauge(self):
        e, s, g = C.EDENVILLE, C.SANFORD, C.MIDLAND
        self.assertGreater(e["top_of_dam_ft"], e["normal_pool_ft"])
        self.assertGreater(s["top_of_dam_ft"], s["normal_pool_ft"])
        # pools and the observed peak fall downstream
        self.assertGreater(e["normal_pool_ft"], s["normal_pool_ft"])
        self.assertGreater(s["normal_pool_ft"], g["measured_stage_ft"])
        # and the dams sit upstream (north) of the gauge
        self.assertGreater(e["lat"], s["lat"])
        self.assertGreater(s["lat"], g["lat"])

    def test_the_gauge_stage_is_its_datum_plus_gage_height(self):
        g = C.MIDLAND
        self.assertAlmostEqual(g["datum_ft"] + g["gage_height_ft"], g["measured_stage_ft"],
                               places=2)

    def test_the_datum_shift_is_applied_to_every_elevation(self):
        self.assertAlmostEqual(C._navd(675.8), 675.8 + C.NGVD29_TO_NAVD88_FT, places=2)
        self.assertEqual(C.EDENVILLE["normal_pool_ft"], C._navd(675.8))
        self.assertEqual(C.SANFORD["normal_pool_ft"], C._navd(630.8))
        self.assertLess(C.NGVD29_TO_NAVD88_FT, 0.0)        # NGVD29 is above NAVD88 here

    def test_the_storage_curve_passes_through_the_published_normal_pool_storage(self):
        for d in (C.EDENVILLE, C.SANFORD):
            v = _curve_normal_storage(d)
            self.assertAlmostEqual(v, d["storage_normal_acft"], delta=1500.0)
            self.assertLess(d["storage_normal_acft"], d["storage_top_acft"])   # pool below crest

    def test_the_asce_benchmarks_are_the_paper_numbers(self):
        self.assertAlmostEqual(C.ASCE_EDENVILLE_BREACH_CFS * C.CFS, 4323.0, places=1)  # m3/s
        self.assertAlmostEqual(C.ASCE_WAVE_AT_SANFORD_CFS,
                               C.ASCE_EDENVILLE_BREACH_CFS * 0.65, places=1)
        self.assertTrue(150000 < C.ASCE_EDENVILLE_BREACH_CFS < 155000)


class RunnerReadsIt(unittest.TestCase):
    """The Runner's own breach/flood code on the dam inputs this case builds: a renamed field or a
    changed routing shows here, not on the day someone runs the cascade."""

    def test_the_dam_data_validates_and_edenville_starts_at_its_normal_pool(self):
        e = C._dam_data(C.EDENVILLE)
        e.validate()                                       # raises if a field is wrong
        self.assertAlmostEqual(e.top_of_dam_ft - e.start_below_top_ft,
                               C.EDENVILLE["normal_pool_ft"], places=2)
        s = C._dam_data(C.SANFORD)
        s.validate()
        self.assertIsNone(s.start_below_top_ft)            # Sanford's pool comes from the flood

    def test_edenville_sunny_day_breach_is_a_large_real_dam_break(self):
        from DamSafety_Runner.core import breach as brmod
        hy = brmod.make_hydrograph(C._dam_data(C.EDENVILLE), brmod.METHOD_PIPING)
        s = hy.summary()
        self.assertIn("Froehlich", s["breach_parameters"])
        self.assertAlmostEqual(s["starting_wse_ft"], C.EDENVILLE["normal_pool_ft"], delta=0.5)
        # tens of thousands to a few hundred thousand cfs: the right order for a 54 ft, 40,000
        # ac-ft dam-break, bracketing the ASCE 2-D model (~152,600 cfs)
        self.assertTrue(80000 < hy.peak_cfs < 400000, hy.peak_cfs)

    def test_sanford_overtops_under_the_wave_and_holds_without_it(self):
        from DamSafety_Runner.core import breach as brmod, flood as floodmod
        dam = C._dam_data(C.SANFORD)
        sp = floodmod.Spillway.from_weir(C.SANFORD["normal_pool_ft"],
                                         C.SANFORD["spillway_length_ft"])
        # a big inflow (stand-in for the Edenville wave) overtops it
        big = floodmod.FloodInputs.triangle(80000.0, 1.0, 6.0, normal_pool_ft=C.SANFORD["normal_pool_ft"],
                                            spillway=sp, trigger=floodmod.TRIGGER_WSE,
                                            trigger_value=C.SANFORD["top_of_dam_ft"])
        fr = floodmod.route_flood(dam, brmod.METHOD_PIPING, brmod.BreachOverrides(), big)
        self.assertTrue(fr.failed)
        # its own near-zero inflow does not overtop it
        calm = floodmod.FloodInputs.triangle(50.0, 1.0, 6.0, normal_pool_ft=C.SANFORD["normal_pool_ft"],
                                             spillway=sp, trigger=floodmod.TRIGGER_WSE,
                                             trigger_value=C.SANFORD["top_of_dam_ft"])
        fr2 = floodmod.route_flood(dam, brmod.METHOD_PIPING, brmod.BreachOverrides(), calm)
        self.assertFalse(fr2.failed)


class Scoring(unittest.TestCase):
    """_read_stage reads the flooded cells around the gauge; score() builds the table."""

    def _grid(self, wse_ft, nodata=-9999.0):
        from DamSafety_Runner.core.grid import Grid
        # 30 m grid, gauge near the middle
        x0, y0 = 720000.0, 4832000.0
        data = np.full((80, 80), nodata)
        data[30:50, 20:60] = wse_ft                        # a wet band across the gauge
        return Grid(data, x0, y0, 30.0)

    def _gauge_xy(self):
        # a cell centre inside the wet band of _grid
        return (720000.0 + 40 * 30.0 + 15.0, 4832000.0 - 40 * 30.0 - 15.0)

    def test_a_point_reads_the_water_surface_around_it(self):
        g = self._grid(614.6)
        z = C._read_stage(g, self._gauge_xy())
        self.assertAlmostEqual(z, 614.6, places=1)

    def test_a_dry_point_reads_nothing(self):
        g = self._grid(614.6)
        far = (720000.0 + 70 * 30.0, 4832000.0 - 5 * 30.0)   # outside the wet band
        self.assertIsNone(C._read_stage(g, far))
        off = (999999.0, 0.0)                                # off the raster
        self.assertIsNone(C._read_stage(g, off))

    def test_the_score_table_names_the_model_and_the_references(self):
        import json
        import tempfile
        res = {"rule": "overtop", "wall_s": 1.0, "decisions": [
                   {"dam": "Sanford", "fails": True, "reason": "the flood from upstream overtops it"}],
               "warnings": [], "dams": {
                   "Edenville": {"modelled": True, "peak_breach_cfs": 221000.0, "flooded_acres": 5000.0,
                                 "max_depth_ft": 40.0, "gauge_to_Sanford_peak_cfs": 110000.0},
                   "Sanford": {"modelled": True, "peak_breach_cfs": 90000.0, "flooded_acres": 3000.0,
                               "max_depth_ft": 25.0, "gauge_midland_peak_cfs": 60000.0}},
               "midland_stage_ft": 616.1, "midland_observed_ft": 614.61, "midland_diff_ft": 1.49,
               "midland_peak_cfs": 60000.0, "midland_observed_cfs": 51800.0,
               "references": {"asce_edenville_breach_cfs": 152665.0,
                              "asce_wave_at_sanford_cfs": 99232.0}}
        with tempfile.TemporaryDirectory() as d:
            case = os.path.join(d, "Edenville")
            os.makedirs(case)
            with open(os.path.join(case, "cascade.json"), "w", encoding="utf-8") as f:
                json.dump({"dams": {}}, f)
            with open(os.path.join(case, "result.json"), "w", encoding="utf-8") as f:
                json.dump(res, f)
            C.score(d, say=lambda *a, **k: None)
            with open(os.path.join(d, "Edenville_Sanford_2020_Score.md"), encoding="utf-8") as f:
                text = f.read()
        self.assertIn("221,000 cfs", text)
        self.assertIn("152,665 cfs", text)                 # ASCE breach reference
        self.assertIn("**616.10 ft** (+1.49)", text)       # modelled stage and difference
        self.assertIn("614.61 ft NAVD88", text)            # the observed anchor
        self.assertIn("fails", text)


if __name__ == "__main__":
    unittest.main()
