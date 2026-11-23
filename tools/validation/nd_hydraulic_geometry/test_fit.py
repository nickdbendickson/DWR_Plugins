#!/usr/bin/env python3
"""The ND hydraulic-geometry fit (fit.py): its statistics, the per-gauge rules, and that the
curve the Runner carries (prism.ND_USGS_2026, sgc.ND_USGS_2026) is the one results.json holds.
No network: the committed results.json is read, nothing is downloaded."""
import importlib.util
import json
import math
import os
import random
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
_spec = importlib.util.spec_from_file_location("nd_hg_fit", os.path.join(HERE, "fit.py"))
F = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(F)


class Statistics(unittest.TestCase):
    def test_loglog_fit_recovers_a_power_law(self):
        xs = [1, 10, 100, 1000]
        a, b, r2, n, se = F.loglog_fit(xs, [2.0 * x ** 0.4 for x in xs])
        self.assertAlmostEqual(a, 2.0, places=9)
        self.assertAlmostEqual(b, 0.4, places=9)
        self.assertAlmostEqual(r2, 1.0, places=9)
        self.assertEqual(n, 4)

    def test_lp3_with_no_skew_is_the_lognormal_quantile(self):
        # symmetric log10 sample: skew 0, so Q(T) = 10^(mean + z_p * sd)
        logs = [2.0 + d for d in (-0.3, -0.1, 0.0, 0.1, 0.3)] * 3
        q = F.lp3_quantile([10 ** x for x in logs], 2.0)
        self.assertAlmostEqual(math.log10(q), 2.0, places=9)        # the median at T = 2

    def test_lp3_q15_is_below_the_median_flow(self):
        rnd = random.Random(1)
        peaks = [10 ** rnd.gauss(3.0, 0.3) for _ in range(60)]
        self.assertLess(F.lp3_quantile(peaks, 1.5), F.lp3_quantile(peaks, 2.0))


def _site(**kw):
    s = {"id": "USGS-00000001", "monitoring_location_name": "TEST CREEK NR TOWN, ND",
         "drainage_area": "100", "contributing_drainage_area": "40"}
    s.update(kw)
    return s


def _peaks(n=30, q=1000.0, qualifier=None):
    rnd = random.Random(2)
    return [{"value": str(q * 10 ** rnd.gauss(0, 0.25)), "qualifier": qualifier} for _ in range(n)]


def _meas(qbf, n=12, month="07", mtype="Wading"):
    """Measurements on W = 10 Q^0.3 ft, A = 0.5 Q^0.8 ft2 between Q1.5 / 5 and 1.5 Q1.5."""
    out = []
    for i in range(n):
        q = qbf * (0.2 + 1.3 * i / (n - 1))
        out.append({"time": "2001-{}-01T00:00:00+00:00".format(month), "channel_flow": str(q),
                    "channel_width": str(10 * q ** 0.3), "channel_area": str(0.5 * q ** 0.8),
                    "measurement_type": mtype})
    return out


class Gauge(unittest.TestCase):
    def test_width_and_depth_at_q15_from_the_at_a_station_curves(self):
        peaks = _peaks()
        qbf = F.lp3_quantile([float(p["value"]) for p in peaks], 1.5)
        g = F.gauge_geometry(_site(), peaks, _meas(qbf))
        self.assertNotIn("excluded", g)
        self.assertAlmostEqual(g["width_m"], 10 * qbf ** 0.3 * F.FT, places=6)
        self.assertAlmostEqual(g["depth_m"], 0.5 * qbf ** 0.8 / (10 * qbf ** 0.3) * F.FT, places=6)
        self.assertAlmostEqual(g["da_km2"], 40 * F.SQMI_KM2)          # the contributing area

    def test_rules_that_leave_a_gauge_out(self):
        qbf = 1000.0
        self.assertIn("REGULATED", F.gauge_geometry(
            _site(), _peaks(qualifier=["REGULATED"]), _meas(qbf))["excluded"])
        self.assertIn("Missouri", F.gauge_geometry(
            _site(monitoring_location_name="MISSOURI R AT BISMARCK, ND"), _peaks(), _meas(qbf))["excluded"])
        self.assertIn("usable annual peaks", F.gauge_geometry(_site(), _peaks(n=5), _meas(qbf))["excluded"])
        # winter, ice-cover and (in the no-bridge variant) bridge measurements do not count
        self.assertIn("open-water", F.gauge_geometry(_site(), _peaks(), _meas(qbf, month="01"))["excluded"])
        self.assertIn("open-water", F.gauge_geometry(_site(), _peaks(), _meas(qbf, mtype="Ice"))["excluded"])
        self.assertNotIn("excluded", F.gauge_geometry(_site(), _peaks(), _meas(qbf, mtype="BridgeCrane")))
        self.assertIn("open-water", F.gauge_geometry(
            _site(), _peaks(), _meas(qbf, mtype="BridgeCrane"), bridges=False)["excluded"])


class Committed(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(HERE, "results.json"), encoding="utf-8") as f:
            self.res = json.load(f)

    def test_the_runner_carries_the_fitted_curve(self):
        c = self.res["curve_nd"]
        src = open(os.path.join(REPO, "DamSafety_Runner", "core", "metalterrain", "prism.py"),
                   encoding="utf-8").read()
        self.assertIn('ND_USGS_2026 = {{"a_w": {a_w}, "b_w": {b_w}, "a_d": {a_d}, "b_d": {b_d}}}'
                      .format(**c), src)
        sgc = open(os.path.join(REPO, "DamSafety_Runner", "core", "sgc.py"), encoding="utf-8").read()
        self.assertIn("ND_USGS_2026 = ({a_w}, {b_w}, {a_d}, {b_d})".format(**c), sgc)

    def test_fit_is_on_enough_gauges_and_a_wide_range_of_areas(self):
        fit = self.res["fit"]
        self.assertGreaterEqual(fit["n_gauges"], 50)
        self.assertLess(fit["da_km2_min"], 100)
        self.assertGreater(fit["da_km2_max"], 5000)


if __name__ == "__main__":
    sys.exit(unittest.main())
