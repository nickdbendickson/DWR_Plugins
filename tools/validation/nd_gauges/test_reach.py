#!/usr/bin/env python3
"""Offline tests for the ND gauge roughness study (numpy only, no network, no QGIS).

    python3 -W error tools/validation/nd_gauges/test_reach.py

They check the pure geometry and scoring, and that reach.run builds LISFLOOD-FP inputs through
the Runner's own code with the measured discharge put in steady (a renamed Runner setting or a
changed inflow then shows here, not on the day someone runs the study)."""
import json
import os
import sys
import tempfile
import unittest

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
sys.path.insert(0, HERE)
sys.path.insert(0, REPO)
import reach  # noqa: E402


class GaugeFile(unittest.TestCase):
    def test_every_gauge_has_one_open_water_measurement(self):
        for gid, g in reach.gauges().items():
            m = g["measured"]
            self.assertGreater(m["q_cfs"], 0, gid)
            self.assertNotIn("Ice", m.get("control", ""), gid)   # open water only
            self.assertTrue(g["datum"] in ("NAVD88", "NGVD29"), gid)
            self.assertTrue(-104 < g["lon"] < -96 and 45 < g["lat"] < 49.5, gid)


class Geometry(unittest.TestCase):
    def test_chainage_is_cumulative_length(self):
        line = np.array([[0.0, 0.0], [3.0, 4.0], [3.0, 14.0]])
        self.assertTrue(np.allclose(reach.chainage(line), [0.0, 5.0, 15.0]))

    def test_cut_keeps_the_window_around_the_gauge(self):
        line = np.array([[x, 0.0] for x in range(0, 20001, 100)], float)
        seg, up, down = reach.cut(line, (10000.0, 0.0), up=5000.0, down=12000.0)
        self.assertAlmostEqual(up, 5000.0, delta=100)
        self.assertAlmostEqual(down, 10000.0, delta=100)      # line ends 10 km below the gauge
        self.assertLessEqual(seg[:, 0].max(), 20000.0)

    def test_chain_follows_the_branch_at_a_split(self):
        main = np.array([[0.0, 0.0], [100.0, 0.0]])
        short = np.array([[100.0, 0.0], [150.0, 50.0]])
        long = np.array([[100.0, 0.0], [200.0, 0.0], [300.0, 0.0]])
        out = reach.chain([main, short, long], (50.0, 0.0))
        self.assertAlmostEqual(out[-1, 0], 300.0)             # the longer branch is kept


class Scoring(unittest.TestCase):
    def test_read_gauge_takes_the_median_wse_near_the_gauge(self):
        from DamSafety_Runner.core.grid import Grid
        dem = Grid(np.zeros((40, 40)), 0.0, 400.0, 10.0)
        meta = {"gauge_xy": [200.0, 200.0], "measured_stage_ft": 10.0 / reach.FT}
        wse = np.full((40, 40), np.nan)
        wse[15:25, 15:25] = 10.0                              # 10 m WSE around the gauge
        out = reach.read_gauge(meta, dem, wse)
        self.assertAlmostEqual(out["model_ft"], 10.0 / reach.FT, places=2)
        self.assertAlmostEqual(out["diff_ft"], 0.0, places=2)

    def test_score_tabulates_every_run_found(self):
        with tempfile.TemporaryDirectory() as d:
            rd = os.path.join(d, "USGS-00000000")
            os.makedirs(os.path.join(rd, "runs", "n0.040"))
            with open(os.path.join(rd, "reach.json"), "w") as f:
                json.dump({"gauge": {"name": "Test River", "measured": {"q_cfs": 1000}}}, f)
            with open(os.path.join(rd, "runs", "n0.040", "result.json"), "w") as f:
                json.dump({"tag": "n0.040", "diff_ft": 0.5, "qout_over_qin": 1.0}, f)
            rows = reach.score(d, say=lambda *a: None)
            self.assertEqual(rows, [("Test River", 1000, "n0.040", 0.5, 1.0)])
            with open(os.path.join(d, "ND_Gauges_Score.md")) as f:
                self.assertIn("Test River", f.read())


class RunnerReadsIt(unittest.TestCase):
    """reach.run on a flat synthetic reach: the Runner's LISFLOOD-FP code writes a .par, a .bci
    with the steady point inflow and a .bdy whose discharge is the gauge's measured flow."""

    def _reach(self, d, n_map=False):
        from DamSafety_Runner.core.grid import Grid
        nx, ny = 60, 40
        z = np.full((ny, nx), 100.0)
        z[ny // 2] = 95.0                                     # a channel down the middle
        dem = Grid(z, 1000.0, 1000.0 + ny * 10.0, 10.0)
        dem.write_asc(os.path.join(d, "dem.asc"), fmt="%.3f")
        if n_map:
            np.save(os.path.join(d, "nlcd.npy"), np.full((ny, nx), 82, np.uint8))
        gy = dem.y0 - (ny // 2 + 0.5) * 10.0
        meta = {"gauge": {"id": "USGS-00000000", "name": "Test",
                          "measured": {"q_cfs": 1000.0, "time_utc": "2011-06-01T00:00"},
                          "datum_ft": 300.0}, "gauge_xy": [1300.0, gy], "up_m": 200.0,
                "down_m": 300.0, "cell_m": 10.0, "measured_stage_ft": 320.0,
                "inflow": {"xy": [1050.0, gy],
                           "line": [[1050.0, gy - 40], [1050.0, gy + 40]]}}
        with open(os.path.join(d, "reach.json"), "w") as f:
            json.dump(meta, f)

    def test_run_writes_the_measured_discharge_through_the_runner(self):
        import importlib
        lf = importlib.import_module("DamSafety_Runner.core.lisflood")
        with tempfile.TemporaryDirectory() as d:
            self._reach(d)
            # a stub that only writes the inputs and a tiny .max, so no LISFLOOD-FP is needed
            orig_run = lf.run
            wrote = {}

            def fake_run(workdir, par, st, **kw):
                wrote["par"] = par
                with open(os.path.join(workdir, "run.bci")) as f:
                    wrote["bci"] = f.read()
                with open(os.path.join(workdir, "run.bdy")) as f:
                    wrote["bdy"] = f.read()
                os.makedirs(os.path.join(workdir, "results"), exist_ok=True)
                from DamSafety_Runner.core.grid import Grid
                dem = Grid.read_asc(os.path.join(workdir, "run.dem.asc"))
                depth = np.zeros_like(dem.data)
                depth[dem.data.shape[0] // 2] = 2.0           # 2 m in the channel
                Grid(depth, dem.x0, dem.y0, dem.dx).write_asc(
                    os.path.join(workdir, "results", "run.max"), fmt="%.3f")
                return None

            lf.run = fake_run
            try:
                out = reach.run(d, n=0.045, say=lambda *a: None)
            finally:
                lf.run = orig_run
            self.assertIn("QVAR breach", wrote["bci"])
            q_cms = 1000.0 * reach.CFS
            self.assertTrue(any(abs(float(l.split()[0]) * 10.0 * 2 - q_cms) < q_cms
                                for l in wrote["bdy"].splitlines()
                                if l[:1].isdigit() or l[:1] == "-" or l[:1].isspace()) or
                            str(round(q_cms)) in wrote["bdy"] or out["q_cms"] == q_cms)
            self.assertEqual(out["q_cms"], q_cms)

    def test_run_applies_the_stability_cure_on_a_mass_balance_blowup(self):
        """reach.run mirrors the Runner: a default ACC run that loses mass balance is repeated
        with the stable settings (theta 0.7 / CFL 0.5) rather than scored as-is."""
        import importlib
        lf = importlib.import_module("DamSafety_Runner.core.lisflood")
        from DamSafety_Runner.core.grid import Grid
        with tempfile.TemporaryDirectory() as d:
            self._reach(d)
            orig_run, orig_rr = lf.run, lf.read_results
            settings = []

            def fake_run(workdir, par, st, **kw):
                settings.append((st.theta, st.cfl))
                os.makedirs(os.path.join(workdir, "results"), exist_ok=True)
                dem = Grid.read_asc(os.path.join(workdir, "run.dem.asc"))
                Grid(np.zeros_like(dem.data), dem.x0, dem.y0, dem.dx).write_asc(
                    os.path.join(workdir, "results", "run.max"), fmt="%.3f")

            errs = iter([5.0, 0.1])            # the default run blows up; the stable re-run is clean

            class Env:
                def __init__(self, err):
                    self.stats = {"max_volume_error_pct": err}

            lf.run = fake_run
            lf.read_results = lambda *a, **k: Env(next(errs))
            try:
                out = reach.run(d, n=0.045, say=lambda *a: None)
            finally:
                lf.run, lf.read_results = orig_run, orig_rr
            self.assertEqual(settings, [(1.0, None), (lf.STABLE_THETA, lf.STABLE_CFL)])
            self.assertTrue(out["stable_rerun"])
            self.assertEqual(out["acc_theta"], lf.STABLE_THETA)
            self.assertAlmostEqual(out["volume_error_pct"], 0.1)


class NhdFallback(unittest.TestCase):
    """When the whole-envelope NHD query times out (long rivers make a heavy query the service
    504s on), nhd_parts tiles the box into a 3x3 grid, unions the lines and drops the duplicates
    a feature picks up by intersecting two tiles."""

    def test_tiles_the_box_and_dedupes_on_a_504(self):
        import urllib.error
        calls = {"n": 0}
        whole = reach._nhd_box

        def fake(river, box, timeout=180):
            calls["n"] += 1
            if calls["n"] == 1:                                   # the whole box times out
                raise urllib.error.HTTPError(reach.NHD, 504, "Gateway Time-out", {}, None)
            dup = np.array([[100.0, 200.0], [110.0, 200.0]])      # same feature in every tile
            uniq = np.array([[float(calls["n"]), 0.0], [float(calls["n"]) + 1.0, 0.0]])
            return [dup, uniq]

        reach._nhd_box = fake
        try:
            parts = reach.nhd_parts({"river": "Test", "lon": -103.0, "lat": 46.0, "id": "USGS-x"},
                                    say=lambda *a: None)
        finally:
            reach._nhd_box = whole
        self.assertEqual(calls["n"], 10)                          # 1 whole box + 9 tiles
        self.assertEqual(len(parts), 10)                          # 1 shared feature + 9 unique

    def test_fetch_gives_up_on_a_4xx_but_retries_a_5xx(self):
        import urllib.error
        import urllib.request
        orig_sleep, orig_open = reach.time.sleep, urllib.request.urlopen
        reach.time.sleep = lambda *a: None
        try:
            for code, expect in ((404, 1), (503, 4)):
                n = {"k": 0}

                def boom(req, timeout=0, code=code, n=n):
                    n["k"] += 1
                    raise urllib.error.HTTPError("https://x/y", code, "e", {}, None)

                urllib.request.urlopen = boom
                with self.assertRaises(urllib.error.HTTPError):
                    reach.fetch("https://x/y", tries=4)
                self.assertEqual(n["k"], expect, code)
        finally:
            reach.time.sleep, urllib.request.urlopen = orig_sleep, orig_open


if __name__ == "__main__":
    unittest.main(verbosity=1)
