"""The batch scripts (44 dams with LISFLOOD-FP, 44 dams with ANUGA and culverts, a county at
a time) and their watcher.

Why this exists: these scripts travelled in zip files, three copies of the same helpers, and
nothing ran them between one use and the next. By the time they came into the repository they
looked for a conda env and a watcher under names the local AI had not had for two renames,
and the watcher asked a model that no longer existed.

No QGIS, no solver: dam_chain.py is replaced by a stand-in that records its arguments and
writes the state file the real one writes, so what is tested is the command each dam is run
with and how the script reads the result.
"""
import csv
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
BATCH = os.path.dirname(HERE)
REPO = os.path.dirname(os.path.dirname(BATCH))

STUB = r'''#!/usr/bin/env python3
"""Stand-in for dam_chain.py: appends its argv to $STUB_LOG and, for `run --set nd`, writes
the dam's _chain_nd/state.json the way the real chain does."""
import json, os, sys
a = sys.argv[1:]
with open(os.environ["STUB_LOG"], "a") as fh:
    fh.write(json.dumps(a) + "\n")
if "--dry-run" in a:
    print("STUB dry " + " ".join(a))
    sys.exit(0)
if "run" in a and "--set" in a and a[a.index("--set") + 1] == "nd":
    folder, did = a[a.index("--dams-folder") + 1], a[a.index("--ids") + 1]
    fail = os.environ.get("STUB_FAIL", "").split(",")
    d = os.path.join(folder, did, "_chain_nd")
    os.makedirs(d, exist_ok=True)
    st = ({"status": "failed", "error": "Runner: no flood reached the model edge\nsee log"}
          if did in fail else {"status": "done", "nd_class": "High"})
    if did not in os.environ.get("STUB_NO_STATE", "").split(","):
        with open(os.path.join(d, "state.json"), "w") as fh:
            json.dump(st, fh)
    print("chain ran " + did)
sys.exit(int(os.environ.get("STUB_RC", "0")))
'''


def load(name):
    spec = importlib.util.spec_from_file_location("batch_%s_test" % name, os.path.join(BATCH, name + ".py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class Scratch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="batch test ")       # a space, on purpose
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home)
        self.stub = os.path.join(self.tmp, "dam_chain.py")
        with open(self.stub, "w", encoding="utf-8") as fh:
            fh.write(STUB)
        self.log = os.path.join(self.tmp, "stub.log")
        self.env = dict(os.environ, HOME=self.home, DAMCHAIN_PY=self.stub, STUB_LOG=self.log,
                        NDSUITE_DATA=os.path.join(self.home, "data"), PATH=os.environ.get("PATH", ""))
        for k in ("BATCH_DATA_DIR", "STUB_FAIL", "STUB_RC", "STUB_NO_STATE"):
            self.env.pop(k, None)

    def run_script(self, name, *args, **env):
        r = subprocess.run([sys.executable, os.path.join(BATCH, name)] + list(args), capture_output=True,
                           text=True, env=dict(self.env, **env), cwd=self.tmp)
        return r.returncode, r.stdout + r.stderr

    def calls(self):
        if not os.path.exists(self.log):
            return []
        with open(self.log, encoding="utf-8") as fh:
            return [json.loads(ln) for ln in fh if ln.strip()]

    def created(self):
        return sorted(os.listdir(self.home))


def opt(argv, flag):
    """Every value given with `flag` in an argv list."""
    return [argv[i + 1] for i, a in enumerate(argv) if a == flag]


class Dams44CsvTest(unittest.TestCase):
    def test_the_44_dams(self):
        with open(os.path.join(BATCH, "dams_44.csv"), newline="", encoding="utf-8-sig") as fh:
            rows = list(csv.DictReader(fh))
        self.assertEqual(len(rows), 44)
        self.assertEqual(len({r["dam_id"] for r in rows}), 44)
        need = load("run_lisflood_44").SEED_COLS
        for r in rows:
            for c in need:
                self.assertIn(c, r)
            self.assertRegex(r["dam_id"], r"^DWR-\d+$")
            self.assertEqual(r["dam_id"], "DWR-" + r["dwr_structure_id"])
            lat, lon = float(r["lat"]), float(r["lon"])
            self.assertTrue(45.9 < lat < 49.1 and -104.1 < lon < -96.5, r["dam_id"])   # North Dakota


class Lisflood44Test(Scratch):
    def test_each_dam_is_run_with_the_recommended_settings_passed_explicitly(self):
        code, out = self.run_script("run_lisflood_44.py", "--only", "DWR-2801,DWR-2645", "--parallel", "1")
        self.assertEqual(code, 0, out)
        seed, *runs = self.calls()
        self.assertEqual(seed[0], "seed")
        self.assertEqual(sorted(opt(r, "--ids")[0] for r in runs), ["DWR-2645", "DWR-2801"])
        for r in runs:
            self.assertEqual(opt(r, "--set"), ["nd"])
            self.assertEqual(opt(r, "--breach-param"),
                             ["SOLVER=1", "TIER=0", "CELL=10", "CREST_BURN=true", "GRID_REFINE=true",
                              "GRID_CHECK_CELL=0", "REFINE_NEAR=false", "THREADS=1"])
            self.assertEqual(opt(r, "--threads"), ["1"])
            self.assertNotIn("--getdata-param", r)
        self.assertIn("finished: 2 ok, 0 failed", out)
        with open(os.path.join(self.home, "ND_Dams", "batch44_lisflood_summary.csv"), newline="") as fh:
            rows = list(csv.DictReader(fh))
        self.assertEqual([(r["dam_id"], r["status"]) for r in rows],
                         [("DWR-2645", "ok, ND class High"), ("DWR-2801", "ok, ND class High")])

    def test_a_failed_dam_fails_the_batch_even_when_the_chain_exits_zero(self):
        """dam_chain before 1.5.2 exited 0 on a failed dam. The dam's own state file decides."""
        code, out = self.run_script("run_lisflood_44.py", "--only", "DWR-2801,DWR-2645", "--parallel", "1",
                                    STUB_FAIL="DWR-2801")
        self.assertEqual(code, 1, out)
        self.assertIn("finished: 1 ok, 1 failed", out)
        self.assertIn("FAILED: Runner: no flood reached the model edge see log", out)

    def test_no_state_file_is_a_failure_not_a_pass(self):
        code, out = self.run_script("run_lisflood_44.py", "--only", "DWR-2801", STUB_NO_STATE="DWR-2801")
        self.assertEqual(code, 1, out)
        self.assertIn("FAILED (no state.json)", out)

    def test_a_non_zero_exit_is_a_failure(self):
        code, out = self.run_script("run_lisflood_44.py", "--only", "DWR-2801", STUB_RC="3")
        self.assertEqual(code, 1, out)

    def test_dem_goes_in_as_a_runner_parameter_with_overwrite_and_survives_a_space(self):
        dem = os.path.join(self.tmp, "Extreme SSD", "ND 1m.vrt")
        os.makedirs(os.path.dirname(dem))
        open(dem, "w").close()
        code, out = self.run_script("run_lisflood_44.py", "--only", "DWR-2801", "--dem", dem)
        self.assertEqual(code, 0, out)
        run = self.calls()[1]
        self.assertIn("DEM=" + dem, opt(run, "--breach-param"), "one argument, space and all")
        self.assertEqual(opt(run, "--getdata-param"), ["OVERWRITE=true"],
                         "without it Get data keeps the DEM an earlier run recorded")

    def test_a_dem_that_is_not_there_stops_before_anything_runs(self):
        code, out = self.run_script("run_lisflood_44.py", "--only", "DWR-2801", "--dem", "/Volumes/gone/x.vrt")
        self.assertNotEqual(code, 0)
        self.assertIn("does not exist (is the drive mounted?)", out)
        self.assertEqual(self.calls(), [])

    def test_options_change_only_what_they_name(self):
        self.run_script("run_lisflood_44.py", "--only", "DWR-2801", "--cell", "5", "--check-5m", "--redo")
        run = self.calls()[1]
        p = opt(run, "--breach-param")
        self.assertIn("CELL=5", p)
        self.assertNotIn("CELL=10", p)
        self.assertIn("GRID_CHECK_CELL=5", p)
        self.assertNotIn("GRID_CHECK_CELL=0", p)
        self.assertIn("--redo", run)
        self.assertEqual(len(p), 8)

    def test_an_unknown_id_is_refused(self):
        code, out = self.run_script("run_lisflood_44.py", "--only", "DWR-1")
        self.assertNotEqual(code, 0)
        self.assertIn("Not in dams_44.csv: DWR-1", out)

    def test_a_dry_run_creates_nothing(self):
        code, out = self.run_script("run_lisflood_44.py", "--dry-run", "--only", "DWR-2801")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.created(), [], "it used to make the dams folder and a log folder")
        self.assertTrue(all("--dry-run" in c for c in self.calls()))
        self.assertIn("--breach-param SOLVER=1", out)


class Anuga44Test(Scratch):
    def gpkg(self):
        d = os.path.join(self.tmp, "data dir")
        os.makedirs(d, exist_ok=True)
        open(os.path.join(d, "dams_44.gpkg"), "w").close()
        return d

    def test_the_dams_layer_is_found_in_the_data_folder(self):
        d = self.gpkg()
        code, out = self.run_script("run_anuga_culverts_44.py", "--only", "DWR-2645,DWR-2801,DWR-3034",
                                    "--parallel", "2", "--culvert-inventory", "/x/a.gpkg",
                                    "--culvert-inventory", "/x/b.gpkg", BATCH_DATA_DIR=d)
        self.assertEqual(code, 0, out)
        runs = self.calls()
        self.assertEqual(len(runs), 2)
        self.assertEqual(sorted(opt(r, "--ids")[0] for r in runs), ["DWR-2645,DWR-3034", "DWR-2801"])
        for r in runs:
            self.assertEqual(opt(r, "--set"), ["dwr"])
            self.assertEqual(opt(r, "--dams"), [os.path.join(d, "dams_44.gpkg")])
            p = opt(r, "--runner-param")
            self.assertEqual(p[:8], ["SOLVER=1", "CELL=10", "CREST_BURN=true", "GRID_REFINE=true",
                                     "GRID_CHECK_CELL=0", "REFINE_NEAR=false", "CULVERT_BURN_GUESSES=true",
                                     "CULVERT_BURN_MATCHED=true"])
            self.assertEqual(p[8], "CULVERT_INVENTORY=/x/a.gpkg;;/x/b.gpkg")
            for flag in ("--no-screen", "--anuga"):
                self.assertIn(flag, r)
            self.assertEqual(opt(r, "--stop-after"), ["runner"])
            self.assertEqual(opt(r, "--dem-units"), ["meters"])
        self.assertEqual(sorted(os.path.basename(opt(r, "--out")[0]) for r in runs), ["part1", "part2"])

    def test_a_dry_run_prints_the_command_and_creates_nothing(self):
        d = self.gpkg()
        code, out = self.run_script("run_anuga_culverts_44.py", "--dry-run", "--only", "DWR-2801", BATCH_DATA_DIR=d)
        self.assertEqual(code, 0, out)
        self.assertIn("--set dwr --dams %s" % os.path.join(d, "dams_44.gpkg"), out)
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.created(), [])

    def test_a_failed_group_fails_the_batch(self):
        code, out = self.run_script("run_anuga_culverts_44.py", "--only", "DWR-2801", BATCH_DATA_DIR=self.gpkg(),
                                    STUB_RC="1")
        self.assertEqual(code, 1, out)
        self.assertIn("1 with a failed dam", out)

    def test_without_the_layer_it_says_where_it_looked(self):
        code, out = self.run_script("run_anuga_culverts_44.py", "--dry-run", "--only", "DWR-2801")
        self.assertNotEqual(code, 0)
        self.assertIn("dams_44.gpkg not found", out)
        self.assertIn("BATCH_DATA_DIR", out)
        self.assertEqual(self.calls(), [])


COUNTY_ROWS = [
    # kind, dam_id, name, county, status, height, storage, has_hv
    ("dam", "DWR-1", "A Dam", "Cass", "Intact", "20", "100", "yes"),
    ("dam", "DWR-2", "B Dam", "Cass", "Intact", "", "", "no"),
    ("dam", "DWR-3", "C Dam", "Cass", "Breached", "10", "50", "yes"),
    ("dugout", "DUG-9", "D Dugout", "Cass", "", "8", "4", "yes"),
    ("dam", "DWR-4", "E Dam", "Grand Forks", "Intact", "15", "80", "yes"),
]


class CountyTest(Scratch):
    def setUp(self):
        super().setUp()
        self.data = os.path.join(self.tmp, "county data")
        os.makedirs(self.data)
        cols = ["kind", "dam_id", "name", "county", "status", "lat", "lon", "height_ft", "storage_acft",
                "dam_length_ft", "dwr_structure_id", "nid_id", "existing_hazard_class", "huc8",
                "has_height_storage"]
        with open(os.path.join(self.data, "nd_dams_dugouts.csv"), "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(cols)
            for kind, did, name, county, status, h, s, hv in COUNTY_ROWS:
                w.writerow([kind, did, name, county, status, "47.0", "-97.0", h, s, "300", did.split("-")[1],
                            "", "Low", "x", hv])
        open(os.path.join(self.data, "nd_dams_dugouts.gpkg"), "w").close()
        self.env["BATCH_DATA_DIR"] = self.data

    def test_list_counts_intact_dams_and_dugouts(self):
        code, out = self.run_script("run_county.py", "--list")
        self.assertEqual(code, 0, out)
        self.assertRegex(out, r"Cass\s+2 \(\s*1\)\s+1 \(\s*1\)")
        self.assertRegex(out, r"TOTAL\s+3 \(\s*2\)\s+1 \(\s*1\)")

    def test_a_county_runs_its_intact_dams_only_and_reports_per_dam(self):
        code, out = self.run_script("run_county.py", "--county", "cass", "--kind", "dams", "--parallel", "1",
                                    STUB_FAIL="DWR-2")
        self.assertEqual(code, 1, out)
        runs = [c for c in self.calls() if "run" in c]
        self.assertEqual(sorted(opt(r, "--ids")[0] for r in runs), ["DWR-1", "DWR-2"], "not the breached DWR-3")
        folder = os.path.join(self.home, "ND_County_Runs", "Cass", "dams")
        self.assertEqual(opt(runs[0], "--dams-folder"), [folder])
        with open(os.path.join(folder, "county_lisflood_summary.csv"), newline="") as fh:
            got = {r["dam_id"]: r["status"] for r in csv.DictReader(fh)}
        self.assertEqual(got["DWR-1"], "ok, ND class High")
        self.assertTrue(got["DWR-2"].startswith("FAILED: Runner"))
        self.assertIn("finished: 1 failed", out)

    def test_anuga_mode_skips_structures_without_height_and_storage(self):
        code, out = self.run_script("run_county.py", "--county", "Cass", "--kind", "both", "--mode", "anuga")
        self.assertEqual(code, 0, out)
        self.assertIn("Cass dams: 1 to run (anuga mode); 1 without height/storage skipped", out)
        runs = self.calls()
        self.assertEqual(opt(runs[0], "--dams"), [os.path.join(self.data, "nd_dams_dugouts.gpkg") + "|layername=dams"])
        self.assertEqual(opt(runs[1], "--dams"), [os.path.join(self.data, "nd_dams_dugouts.gpkg") + "|layername=dugouts"])
        self.assertIn("CULVERT_BURN_GUESSES=true", opt(runs[0], "--runner-param"))
        with open(opt(runs[0], "--ids-csv")[0], encoding="utf-8") as fh:
            self.assertEqual(fh.read().split(), ["dam_id", "DWR-1"], "DWR-2 has no height or storage")
        with open(os.path.join(self.home, "ND_County_Runs", "Cass", "dams_ANUGA",
                               "skipped_no_height_storage.csv"), newline="") as fh:
            self.assertEqual([r["dam_id"] for r in csv.DictReader(fh)], ["DWR-2"])

    def test_a_dry_run_creates_nothing_in_either_mode(self):
        for mode in ("lisflood", "anuga"):
            code, out = self.run_script("run_county.py", "--county", "Cass", "--kind", "both", "--mode", mode,
                                        "--dry-run")
            self.assertEqual(code, 0, out)
            self.assertEqual(self.created(), [], mode)

    def test_an_unknown_county_is_refused_with_the_list(self):
        code, out = self.run_script("run_county.py", "--county", "Narnia")
        self.assertNotEqual(code, 0)
        self.assertIn("Unknown county: Narnia. Counties: Cass, Grand Forks", out)

    def test_without_the_data_it_says_where_it_looked(self):
        self.env.pop("BATCH_DATA_DIR")
        code, out = self.run_script("run_county.py", "--list")
        self.assertNotEqual(code, 0)
        self.assertIn("nd_dams_dugouts.csv not found", out)

    def test_data_flag_names_the_csv_and_the_gpkg_beside_it(self):
        self.env.pop("BATCH_DATA_DIR")
        code, out = self.run_script("run_county.py", "--data", os.path.join(self.data, "nd_dams_dugouts.csv"),
                                    "--county", "Cass", "--mode", "anuga")
        self.assertEqual(code, 0, out)
        self.assertTrue(opt(self.calls()[0], "--dams")[0].startswith(os.path.join(self.data, "nd_dams_dugouts.gpkg")))


class CommonTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.c = load("batch_common")

    def test_the_env_is_found_under_its_current_name_first(self):
        tmp = tempfile.mkdtemp(prefix="batch-env-")
        self.addCleanup(shutil.rmtree, tmp, True)
        for name in ("ndheim", "ndlumo"):
            os.makedirs(os.path.join(tmp, name, "bin"))
            open(os.path.join(tmp, name, "bin", "python"), "w").close()
        listing = "# conda environments:\n#\nbase  *  /opt/conda\nndlumo     %s/ndlumo\nndheim     %s/ndheim\n" % (tmp, tmp)
        self.assertEqual(self.c.env_python(listing=listing), os.path.join(tmp, "ndheim", "bin", "python"))
        old_only = "ndlumo     %s/ndlumo\n" % tmp
        self.assertEqual(self.c.env_python(listing=old_only), os.path.join(tmp, "ndlumo", "bin", "python"))
        self.assertIsNone(self.c.env_python(listing="base  *  /opt/conda\n"))
        self.assertIsNone(self.c.env_python(listing="ndheim   /no/such/env\n"))

    def test_chain_status(self):
        tmp = tempfile.mkdtemp(prefix="batch-state-")
        self.addCleanup(shutil.rmtree, tmp, True)
        d = os.path.join(tmp, "DWR-1", "_chain_nd")
        os.makedirs(d)
        self.assertEqual(self.c.chain_status(tmp, "DWR-1", 0), "FAILED (no state.json)")
        with open(os.path.join(d, "state.json"), "w") as fh:
            fh.write("{not json")
        self.assertEqual(self.c.chain_status(tmp, "DWR-1", 0), "FAILED (no state.json)")
        with open(os.path.join(d, "state.json"), "w") as fh:
            json.dump({"status": "done", "nd_class": "Low"}, fh)
        self.assertEqual(self.c.chain_status(tmp, "DWR-1", 0), "ok, ND class Low")
        self.assertEqual(self.c.chain_status(tmp, "DWR-1", 2), "FAILED (exit 2)")

    def test_no_script_carries_a_name_the_local_ai_no_longer_has(self):
        for name in ("run_lisflood_44.py", "run_anuga_culverts_44.py", "run_county.py"):
            with open(os.path.join(BATCH, name), encoding="utf-8") as fh:
                text = fh.read().lower()
            for old in ("lumo", "pierre"):
                self.assertNotIn(old, text, name)

    def test_use_local_terrain_looks_here_first(self):
        with open(os.path.join(REPO, "tools", "mac", "use_local_terrain.sh"), encoding="utf-8") as fh:
            sh = fh.read()
        a = sh.index("find_b44() {")
        body = sh[a:sh.index("\n}\n", a)]
        self.assertIn('"$HERE/../batch"', body)
        self.assertLess(body.index('"$HERE/../batch"'), body.index("batch-44-dams-2026-11-06"))


REPORT = {
    "summary": {"dam_id": "DWR-2801", "flooded_acres": 0, "breach_occurs": False, "max_depth_ft": 0,
                "cell_m": 10, "lisflood_solver": "ACC"},
    "warnings": ["The flood touches a gap in the DEM (NoData) on the north edge",
                 "Run limit reached with the edge still moving"],
    "passes": [{"max_volume_error_pct": 0.2}, {"max_volume_error_pct": -3.4}],
    "culvert_guesses_cut": [{"id": 1}, {"id": 2}],
}


class WatchTest(unittest.TestCase):
    def setUp(self):
        self.w = load("batch_watch")
        self.tmp = tempfile.mkdtemp(prefix="batch-watch-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        d = os.path.join(self.tmp, "DWR-2801", "run1")
        os.makedirs(d)
        with open(os.path.join(d, "run_report.json"), "w") as fh:
            json.dump(REPORT, fh)
        os.makedirs(os.path.join(self.tmp, "_county_logs", "20261113"))
        with open(os.path.join(self.tmp, "_county_logs", "20261113", "DWR-2801.console.log"), "w") as fh:
            fh.write("get data: ok\nERROR: Runner stopped: no breach\n")

    def watch(self, *args):
        r = subprocess.run([sys.executable, os.path.join(BATCH, "batch_watch.py"), self.tmp, "--once",
                            "--no-notify"] + list(args), capture_output=True, text=True,
                           env=dict(os.environ, OLLAMA_URL="http://127.0.0.1:9"))
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(os.path.join(self.tmp, "BATCH_FINDINGS.md"), encoding="utf-8") as fh:
            return fh.read()

    def test_the_rules_find_what_is_wrong_with_no_model(self):
        dam, facts, find = self.w.check_report(os.path.join(self.tmp, "DWR-2801", "run1", "run_report.json"))
        self.assertEqual(dam, "DWR-2801")
        text = "\n".join(find)
        for needle in ("volume error up to -3.4 %", "no breach occurred", "flooded area is 0 ac",
                       "DEM gap:", "domain: Run limit", "2 culvert(s) cut in as UNVERIFIED guesses"):
            self.assertIn(needle, text)

    def test_by_default_nothing_is_asked_of_a_model(self):
        said = self.watch()
        self.assertIn("rules only", said)
        self.assertIn("RULE: no breach occurred", said)
        self.assertNotIn("heim", said.lower().replace("rules only", ""))
        self.assertNotIn("model unavailable", said)

    def test_a_county_runs_log_lines_are_read(self):
        said = self.watch()
        self.assertIn("ERROR: Runner stopped: no breach", said)
        self.assertNotIn("get data: ok", said)

    def test_with_model_is_advisory_and_a_dead_server_does_not_lose_the_rules(self):
        said = self.watch("--with-model", "--timeout", "2")
        self.assertIn("heim (advisory):", said)
        self.assertIn("model unavailable", said)
        self.assertIn("RULE: flooded area is 0 ac", said)

    def test_a_batch_started_under_the_old_watcher_is_not_reported_twice(self):
        path = os.path.join(self.tmp, "DWR-2801", "run1", "run_report.json")
        with open(os.path.join(self.tmp, ".lumo_watch_state.json"), "w") as fh:
            json.dump({"log_pos": {}, "reports": {path: os.path.getmtime(path)}}, fh)
        said = self.watch()
        self.assertNotIn("RULE:", said, "the old state says this report was already reported")
        self.assertTrue(os.path.exists(os.path.join(self.tmp, ".batch_watch_state.json")))


if __name__ == "__main__":
    unittest.main()
