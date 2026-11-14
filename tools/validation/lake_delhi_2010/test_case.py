#!/usr/bin/env python3
"""The Lake Delhi 2010 case: the published numbers hang together, the Runner's inputs are made
from them as the README says, the Runner still reads those inputs, and the scoring reads a
flood the way it says. No internet, no QGIS, no GDAL: numpy only.

    python3 -W error tools/validation/lake_delhi_2010/test_case.py"""
import json
import math
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
sys.path.insert(0, HERE)
import build_case as B  # noqa: E402
import score as S  # noqa: E402

DAM_XY = (380526.2, 2165878.3)          # the dam and the outflow point in EPSG:5070 (the tests
OUT_XY = (380661.3, 2165939.1)          # have no GDAL to project them)


def _cli():
    sys.path.insert(0, os.path.join(REPO, "DamSafety_Runner", "scripts"))
    import ndbreach_cli
    return ndbreach_cli


def _case(folder, factor=None):
    """The case's inputs and configurations in `folder`, without terrain and without GDAL."""
    ob = B.observed()
    old = B.to_5070
    B.to_5070 = lambda lon, lat: (380000.0 + (lon + 91.35) * 82000.0, 2165000.0 + (lat - 42.4) * 111000.0)
    try:
        B.write_inputs(ob, folder, factor)
    finally:
        B.to_5070 = old
    for name, cfg in B.configs(ob, folder, DAM_XY, inflow_xy=OUT_XY).items():
        with open(os.path.join(folder, name + ".json"), "w", encoding="utf-8") as f:
            json.dump(cfg, f)
    return ob


class Published(unittest.TestCase):
    def setUp(self):
        self.ob = B.observed()

    def test_marks_run_down_the_river_and_down_in_elevation(self):
        m = self.ob["observed"]["high_water_marks"]
        self.assertEqual([x["id"] for x in m], ["below_dam", "hopkinton_d47", "monticello_hwy38",
                                                "monticello_main", "monticello_us151"])
        for a, b in zip(m, m[1:]):
            self.assertLess(a["miles_below_dam"], b["miles_below_dam"])
            self.assertGreater(a["downstream_ft_ngvd29"], b["downstream_ft_ngvd29"])
        for x in m:
            self.assertAlmostEqual(x["miles_below_dam"], 101.58 - x["river_mile"], places=2)
            if x["upstream_ft_ngvd29"] is not None:      # a bridge backs the water up
                self.assertGreaterEqual(x["upstream_ft_ngvd29"], x["downstream_ft_ngvd29"])
                self.assertEqual(sorted(x["points"]), ["downstream", "upstream"])
                self.assertEqual(len(x["bridge"]), 2)

    def test_every_point_is_inside_the_terrain_with_room(self):
        # rough: a degree of longitude is 82 km and of latitude 111 km here
        x0, y0, x1, y1 = self.ob["terrain"]["box"]
        w, h = (x1 - x0) / 82000.0, (y1 - y0) / 111000.0
        lon0, lat1 = -91.3454 - 2500.0 / 82000.0, 42.4081 + 2700.0 / 111000.0
        for mid, _side, _loc, _mi, lon, lat, _z in S.marks(self.ob):
            self.assertTrue(lon0 + 0.01 < lon < lon0 + w - 0.01, (mid, lon))
            self.assertTrue(lat1 - h + 0.01 < lat < lat1 - 0.005, (mid, lat))
        self.assertEqual(((x1 - x0) / 10.0, (y1 - y0) / 10.0), (2236.0, 2403.0))

    def test_the_gauge_file_is_the_record_the_report_quotes(self):
        rows = B.gauge_rows()
        self.assertEqual(len(rows), 574)
        t, q = max(rows, key=lambda r: r[1])
        self.assertEqual(q, self.ob["observed"]["gauge_peak_cfs"])
        self.assertEqual(t, B.when(self.ob["observed"]["gauge_peak_cdt"]))
        steps = {(b[0] - a[0]).total_seconds() for a, b in zip(rows, rows[1:])}
        self.assertEqual(min(steps), 900.0)

    def test_marks_are_moved_to_the_terrain_datum_by_the_stated_shift(self):
        ob = json.loads(json.dumps(self.ob))
        ob["datum"]["ngvd29_to_navd88_ft"] = -0.3
        by = {(m[0], m[1]): m[6] for m in S.marks(ob)}
        self.assertAlmostEqual(by[("hopkinton_d47", "upstream")], 846.51 - 0.3)
        self.assertEqual(len(by), 9)                       # 4 bridges x 2 sides + the dam


class Inputs(unittest.TestCase):
    def setUp(self):
        self.ob = B.observed()

    def test_inflow_is_the_gauge_times_the_factor_from_noon_to_noon(self):
        rows = B.inflow_rows(self.ob)
        self.assertEqual(rows[0][0], 0.0)
        self.assertAlmostEqual(rows[-1][0], 48.0)
        self.assertAlmostEqual(max(q for _t, q in rows), 26600.0 * B.INFLOW_FACTOR)
        self.assertAlmostEqual(dict(B.inflow_rows(self.ob, 1.0))[24.5], 26600.0)   # 12:30, 24 July
        late = B.inflow_rows(self.ob, 1.0, lag_hr=1.5)
        self.assertAlmostEqual(dict(late)[26.0], 26600.0)
        self.assertAlmostEqual(B.area_factor(self.ob), 347.0 / 275.0)
        self.assertLess(B.INFLOW_FACTOR, B.area_factor(self.ob))

    def test_three_open_gates_pass_the_published_capacity_at_the_top_of_dam(self):
        sp = self.ob["spillway"]
        one = B.gate_cfs(904.8, 879.8, 25.0, None, 32000.0 / 3, 904.8)
        self.assertAlmostEqual(3 * one, sp["capacity_all_open_at_top_cfs"])
        stuck = B.gate_cfs(904.8, 879.8, 25.0, 4.25, 32000.0 / 3, 904.8)
        self.assertTrue(2000.0 < stuck < 3500.0, stuck)    # Gate 3: an orifice 4.25 ft high
        self.assertAlmostEqual(B.gates_cfs(self.ob, 904.8), 2 * one + stuck)
        self.assertEqual(B.gate_cfs(879.0, 879.8, 25.0, None, one, 904.8), 0.0)
        # a gate lip above the water changes nothing
        self.assertEqual(B.gate_cfs(882.0, 879.8, 25.0, 4.25, one, 904.8),
                         B.gate_cfs(882.0, 879.8, 25.0, None, one, 904.8))

    def test_the_rating_holds_the_normal_pool_and_only_rises(self):
        rate = B.rating_rows(self.ob)
        z = [r[0] for r in rate]
        q = [r[1] for r in rate]
        self.assertEqual(z, sorted(z))
        self.assertEqual(q, sorted(q))
        self.assertEqual([v for zz, v in rate if zz < 896.0], [0.0, 0.0])
        self.assertGreater(dict(rate)[896.0], 12000.0)
        self.assertGreaterEqual(z[-1], 904.8 + 5.0)

    def test_the_storage_curve_passes_through_both_published_storages(self):
        dam = self.ob["dam"]
        area = B.surface_area_top_ac(self.ob)
        h, vt = dam["embankment_height_ft"], dam["storage_at_top_acft"]
        m = area * h / vt                                   # the Runner's exponent (breach.Reservoir)
        v_normal = vt * ((h - 8.8) / h) ** m
        self.assertAlmostEqual(v_normal, dam["storage_at_normal_pool_acft"], delta=1.0)
        self.assertTrue(900.0 < area < 1050.0, area)

    def test_the_two_runs_differ_only_in_the_breach(self):
        runs = B.configs(self.ob, "x", DAM_XY, inflow_xy=OUT_XY)
        self.assertEqual(sorted(runs), ["as_observed", "blind"])
        a, b = runs["blind"], runs["as_observed"]
        self.assertEqual(a["breach"], {"method": "overtopping", "overrides": {}})
        self.assertEqual(b["breach"]["overrides"], {"avg_width_ft": 235.0, "formation_hr": 2.167})
        for k in ("dem", "dam", "scenario", "model", "layers"):
            self.assertEqual(a[k], b[k], k)
        self.assertEqual(a["scenario"]["trigger"], "time")
        self.assertEqual(a["scenario"]["trigger_value"], 24.5)      # 12:30 p.m., 24 July
        self.assertEqual(a["model"]["sim_time_hr"], 48.0)
        self.assertFalse(os.path.isabs(a["dem"]["path"]))
        self.assertEqual(B.breach_hours(self.ob), (24.5, 130.0 / 60.0))


class RunnerReadsIt(unittest.TestCase):
    """The Runner's own code, on the files this case writes: a renamed setting or a changed
    routing shows here, not on the day someone runs the case."""

    def _route(self, factor=None, run="as_observed"):
        C = _cli()
        with tempfile.TemporaryDirectory() as d:
            _case(d, factor)
            cfg = C.load_config(os.path.join(d, run + ".json"))
            dam = C.build_dam(C.dam_dict(cfg), need_top=True)
            fi = C.build_flood(cfg, dam, {})
            fr = C.floodmod.route_flood(dam, cfg.get("breach", "method"),
                                        C.breach_overrides(cfg), fi)
        return dam, fr

    def _pool(self, fr, hours):
        import numpy as np
        return float(np.interp(hours, fr.stage_t_s / 3600.0, fr.stage_nb_m / 0.3048))

    def test_the_lake_behaves_as_it_was_seen_to(self):
        import numpy as np
        dam, fr = self._route()
        t, z = fr.stage_t_s / 3600.0, fr.stage_nb_m / 0.3048
        self.assertAlmostEqual(float(z[0]), 896.0, places=2)
        self.assertLess(abs(self._pool(fr, 6.0) - 896.0), 3.0)
        over = float(t[np.nonzero(z >= dam.top_of_dam_ft)[0][0]])
        # water over the road was seen at 9:40 a.m. (21.67 h); the USGS gives 10:30 (22.5 h)
        self.assertTrue(20.5 < over < 23.0, over)
        # the surveyed peak pool is 905.55 ft; the breach speeds up at 24.5 h
        self.assertLess(abs(self._pool(fr, 24.5) - 905.55), 0.5)
        self.assertAlmostEqual(fr.t_breach_s / 3600.0, 24.5, places=2)

    def test_the_plain_drainage_area_ratio_overtops_the_dam_hours_early(self):
        import numpy as np
        ob = B.observed()
        dam, fr = self._route(B.area_factor(ob))
        t, z = fr.stage_t_s / 3600.0, fr.stage_nb_m / 0.3048
        over = float(t[np.nonzero(z >= dam.top_of_dam_ft)[0][0]])
        self.assertLess(over, 17.0)                        # before 5 a.m.: it was 9:40 a.m.
        self.assertGreater(self._pool(fr, 24.5), 905.55 + 1.0)

    def test_the_blind_breach_is_the_runners_own(self):
        _dam, fr = self._route(run="blind")
        s = fr.with_breach.summary()
        self.assertIn("Froehlich (2008)", s["breach_parameters"])
        self.assertNotIn("user", s["breach_parameters"])
        _dam, fo = self._route(run="as_observed")
        so = fo.with_breach.summary()
        self.assertAlmostEqual(so["avg_breach_width_ft"], 235.0, places=1)
        self.assertAlmostEqual(so["formation_time_hr"], 2.167, places=2)


class Scoring(unittest.TestCase):
    def _rasters(self, wse_ft=846.0):
        import numpy as np
        gt = (1000.0, 30.0, 0.0, 2000.0, 0.0, -30.0)
        wse = np.full((20, 20), -9999.0)
        dep = np.full((20, 20), -9999.0)
        wse[8:12, :] = wse_ft
        dep[8:12, :] = 6.0
        dep[8, :] = 0.2                                    # a wet edge, too shallow to count
        wse[8, :] = wse_ft + 9.0
        return S.Raster(wse, gt, -9999.0), S.Raster(dep, gt, -9999.0)

    def test_a_point_reads_the_flooded_cells_around_it(self):
        wse, dep = self._rasters()
        z, n = S.model_wse(wse, dep, 1300.0, 2000.0 - 10 * 30.0)
        self.assertEqual(z, 846.0)                         # the shallow edge row is not read
        self.assertGreater(n, 5)
        z, n = S.model_wse(wse, dep, 1300.0, 2000.0 - 2 * 30.0)
        self.assertIsNone(z)                               # dry there
        self.assertEqual(S.model_wse(wse, dep, 99999.0, 0.0), (None, 0))   # off the raster

    def test_the_radius_is_never_less_than_a_cell_and_a_half(self):
        import numpy as np
        gt = (0.0, 100.0, 0.0, 1000.0, 0.0, -100.0)
        wse, dep = np.full((10, 10), 800.0), np.full((10, 10), 3.0)
        z, n = S.model_wse(S.Raster(wse, gt), S.Raster(dep, gt), 500.0, 500.0, radius=10.0)
        self.assertEqual(z, 800.0)
        self.assertGreaterEqual(n, 4)

    def test_the_summary_leaves_out_the_mark_at_the_dam(self):
        rows = [{"model_minus_surveyed_ft": 9.0, "miles_below_dam": 0.0, "model_ft": 1.0},
                {"model_minus_surveyed_ft": 2.0, "miles_below_dam": 9.4, "model_ft": 1.0},
                {"model_minus_surveyed_ft": -1.0, "miles_below_dam": 18.6, "model_ft": 1.0},
                {"model_minus_surveyed_ft": None, "miles_below_dam": 20.1, "model_ft": None}]
        s = S.summarize(rows)
        self.assertEqual((s["marks"], s["dry"]), (2, 1))
        self.assertEqual((s["mean_ft"], s["mean_abs_ft"], s["largest_ft"]), (0.5, 1.5, 2.0))
        self.assertEqual(S.summarize([rows[3]]), {"marks": 0, "dry": 1})

    def test_the_report_names_the_runners_values(self):
        rep = {"hydrograph": {"avg_breach_width_ft": 236.6, "formation_time_hr": 1.5,
                              "peak_cfs": 140000.0, "starting_wse_ft": 905.7},
               "summary": {"final_model": "LISFLOOD-FP 8.3"}, "elapsed_min": 12.0}
        f = S.breach_facts(rep)
        self.assertEqual((f["breach_width_ft"], f["formation_hr"], f["peak_outflow_cfs"],
                          f["pool_at_breach_ft"], f["engine"]),
                         (236.6, 1.5, 140000.0, 905.7, "LISFLOOD-FP 8.3"))
        self.assertIsNone(S.breach_facts({})["breach_width_ft"])
        ob = B.observed()
        res = [{"run": "blind", "facts": f, "gauges": {"hopkinton_d47": (51000.0, 28.5, 48.0)},
                "marks": [{"location": "County Road D47", "miles_below_dam": 9.41, "side": "upstream",
                           "surveyed_ft": 846.51, "model_ft": 848.0,
                           "model_minus_surveyed_ft": 1.49},
                          {"location": "U.S. Highway 151", "miles_below_dam": 20.97, "side": "upstream",
                           "surveyed_ft": 806.46, "model_ft": None,
                           "model_minus_surveyed_ft": None}],
                "summary": {"marks": 1, "dry": 1, "mean_ft": 1.49, "mean_abs_ft": 1.49,
                            "largest_ft": 1.49}}]
        text = S.markdown(ob, res, {"inflow_factor": 1.1, "cell_m": 30.0})
        for want in ("| Breach width, ft | 235 (largest) | 237 |", "| County Road D47 | 9.41 | upstream "
                     "| 846.51 | 848.00 | +1.49 |", "dry in the model", "69,000", "1 mark(s) dry",
                     "| hopkinton_d47 | 51,000 | 28.5 | 48.0 |", "Open-File Report 2011-1301"):
            self.assertIn(want, text)

    def test_a_run_that_is_not_finished_is_said_so(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "runs", "blind", S.DAM_FOLDER))
            with self.assertRaises(S.ScoreError) as e:
                S.score_run(d, "blind", B.observed())
            self.assertIn("not finished", str(e.exception))
            self.assertEqual(S.main([d]), 1)
            self.assertEqual(S.main([os.path.join(d, "nothing")]), 2)


if __name__ == "__main__":
    unittest.main()
