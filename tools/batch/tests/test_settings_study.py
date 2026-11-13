"""run_settings_study_44.py: the 44 dams under more than one model setting, and the page that
says for which dams the class depends on the setting.

No QGIS and no solver: dam_chain.py is a stand-in that records its arguments and writes the
summary (and Hazard's snapshot) the real DamSafety chain writes, with the class it is told to
give each dam under each setting. What is tested is the command each setting is run with and
how the report reads and compares the results.
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

STUB = r'''#!/usr/bin/env python3
"""Stand-in for dam_chain.py `run --set dwr`: appends its argv to $STUB_LOG and writes
<out>/Chain_Batch_Summary.csv plus each dam's outputs/QA/Last_Run_Snapshot.json.
$STUB_CLASSES is JSON {setting: {dam: class}}; "FAIL" fails the dam, "" finishes with no class;
a dam not named gets High. The setting is the --out folder's name (or its parent for partN)."""
import csv, json, os, sys
a = sys.argv[1:]
with open(os.environ["STUB_LOG"], "a") as fh:
    fh.write(json.dumps(a) + "\n")
if "--dry-run" in a:
    print("STUB dry")
    sys.exit(0)
out, ids = a[a.index("--out") + 1], a[a.index("--ids") + 1].split(",")
setting = os.path.basename(out)
if setting.startswith("part"):
    setting = os.path.basename(os.path.dirname(out))
want = json.loads(os.environ.get("STUB_CLASSES", "{}")).get(setting, {})
os.makedirs(out, exist_ok=True)
rows, bad = [], 0
for did in ids:
    cls = want.get(did, "High")
    folder = os.path.join(out, did)
    if cls == "FAIL":
        bad += 1
        rows.append({"Dam": did, "Folder": folder, "Status": "failed", "Message": "Runner: no terrain"})
        continue
    qa = os.path.join(folder, "outputs", "QA")
    os.makedirs(qa, exist_ok=True)
    n = {"High": 3, "Medium": 1, "Low": 0}.get(cls, 0)
    with open(os.path.join(qa, "Last_Run_Snapshot.json"), "w") as fh:
        json.dump({"nd": cls, "nd_reasons": ["reason for " + cls], "lij": 1.5 * n,
                   "structures": [{"outcome": "Inundated", "persons": 2, "depth": 1.0 + i} for i in range(n)]
                                 + [{"outcome": "Not inundated", "persons": 9, "depth": None}],
                   "roads": [{"name": "ND 18"}]}, fh)
    rows.append({"Dam": did, "Folder": folder, "Status": "done", "ND_Class": cls,
                 "TM11_Class": cls, "Minutes": "12.5", "Message": ""})
with open(os.path.join(out, "Chain_Batch_Summary.csv"), "w", newline="") as fh:
    w = csv.DictWriter(fh, fieldnames=["Dam", "Folder", "Status", "Message", "ND_Class", "TM11_Class", "Minutes"])
    w.writeheader()
    w.writerows(rows)
sys.exit(1 if bad else 0)
'''


def load():
    spec = importlib.util.spec_from_file_location("settings_study_test", os.path.join(BATCH, "run_settings_study_44.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def opt(argv, flag):
    return [argv[i + 1] for i, a in enumerate(argv) if a == flag]


class Scratch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="study test ")              # a space, on purpose
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home)
        self.stub = os.path.join(self.tmp, "dam_chain.py")
        with open(self.stub, "w", encoding="utf-8") as fh:
            fh.write(STUB)
        self.gpkg = os.path.join(self.tmp, "dams_44.gpkg")
        open(self.gpkg, "w").close()
        self.log = os.path.join(self.tmp, "stub.log")
        self.study = os.path.join(self.home, "DamSafety", "Settings_Study_44")
        self.env = dict(os.environ, HOME=self.home, DAMCHAIN_PY=self.stub, STUB_LOG=self.log,
                        BATCH_DATA_DIR=self.tmp, NDSUITE_DATA=os.path.join(self.home, "data"))
        self.env.pop("STUB_CLASSES", None)

    def run_script(self, *args, classes=None):
        env = dict(self.env)
        if classes is not None:
            env["STUB_CLASSES"] = json.dumps(classes)
        r = subprocess.run([sys.executable, os.path.join(BATCH, "run_settings_study_44.py")] + list(args),
                           capture_output=True, text=True, env=env, cwd=self.tmp)
        return r.returncode, r.stdout + r.stderr

    def calls(self):
        if not os.path.exists(self.log):
            return []
        with open(self.log, encoding="utf-8") as fh:
            return [json.loads(ln) for ln in fh if ln.strip()]

    def table(self):
        with open(os.path.join(self.study, "Settings_Study.csv"), newline="", encoding="utf-8") as fh:
            return {r["dam_id"]: r for r in csv.DictReader(fh)}

    def page(self):
        with open(os.path.join(self.study, "Settings_Study.html"), encoding="utf-8") as fh:
            return fh.read()


class Commands(Scratch):
    def test_by_default_the_two_lisflood_settings_each_in_its_own_folder(self):
        code, out = self.run_script("--only", "DWR-2801,DWR-2645")
        self.assertEqual(code, 0, out)
        a, b = self.calls()
        self.assertEqual(opt(a, "--out"), [os.path.join(self.study, "lf10")])
        self.assertEqual(opt(b, "--out"), [os.path.join(self.study, "lf20")])
        for c in (a, b):
            self.assertEqual(opt(c, "--set"), ["dwr"])
            self.assertEqual(opt(c, "--dams"), [self.gpkg])
            self.assertEqual(opt(c, "--ids"), ["DWR-2645,DWR-2801"])       # the CSV's order
            self.assertEqual(opt(c, "--hec"), ["skip"])
            self.assertNotIn("--anuga", c)
            self.assertNotIn("--stop-after", c)                            # the whole chain, with the class
            self.assertNotIn("--no-screen", c)
        self.assertEqual(opt(a, "--runner-param"), ["SOLVER=1", "CREST_BURN=true", "GRID_CHECK_CELL=0",
                                                    "REFINE_NEAR=false", "CELL=10", "GRID_REFINE=true"])
        # at 20 m the automatic finer grid is off, or an over-spread run is redone at 10 m
        self.assertEqual(opt(b, "--runner-param")[-2:], ["CELL=20", "GRID_REFINE=false"])

    def test_all_runs_the_four_settings_and_anuga_gets_its_triangles(self):
        code, out = self.run_script("--only", "DWR-2801", "--variants", "all")
        self.assertEqual(code, 0, out)
        calls = self.calls()
        self.assertEqual([os.path.basename(opt(c, "--out")[0]) for c in calls], ["lf10", "lf20", "an10", "an20"])
        self.assertEqual(["--anuga" in c for c in calls], [False, False, True, True])
        self.assertEqual(opt(calls[2], "--runner-param")[-4:],
                         ["CELL=10", "GRID_REFINE=true", "ANUGA_FINE=100", "ANUGA_COARSE=1000"])
        self.assertEqual(opt(calls[3], "--runner-param")[-4:],
                         ["CELL=20", "GRID_REFINE=false", "ANUGA_FINE=400", "ANUGA_COARSE=4000"])
        self.assertIn("ANUGA settings are in the list", out)

    def test_parallel_gives_each_group_its_own_folder_and_no_dam_is_run_twice(self):
        code, out = self.run_script("--only", "DWR-2801,DWR-2645,DWR-2710", "--variants", "lf10", "--parallel", "2")
        self.assertEqual(code, 1, out)                 # one setting: nothing to compare yet
        calls = self.calls()
        self.assertEqual(sorted(os.path.basename(opt(c, "--out")[0]) for c in calls), ["part1", "part2"])
        ids = sorted(i for c in calls for i in opt(c, "--ids")[0].split(","))
        self.assertEqual(ids, ["DWR-2645", "DWR-2710", "DWR-2801"])
        self.assertEqual({r["lf10_nd_class"] for r in self.table().values()}, {"High"})

    def test_options_reach_the_chain(self):
        dem = os.path.join(self.tmp, "nd 1m.vrt")
        open(dem, "w").close()
        self.run_script("--only", "DWR-2801", "--variants", "lf10", "--dem", dem, "--no-screen", "--redo",
                        "--dem-units", "feet")
        (c,) = self.calls()
        self.assertEqual(opt(c, "--dem"), [dem])
        self.assertEqual(opt(c, "--dem-units"), ["feet"])
        self.assertIn("--no-screen", c)
        self.assertIn("--redo", c)

    def test_a_dry_run_creates_nothing(self):
        code, out = self.run_script("--dry-run", "--variants", "all")
        self.assertEqual(code, 0, out)
        self.assertEqual(os.listdir(self.home), [])
        self.assertEqual(self.calls(), [])                                 # printed, not started
        for v in ("lf10", "lf20", "an10", "an20"):
            self.assertIn("--out %s" % os.path.join(self.study, v), out)
        self.assertEqual(out.count("run --set dwr"), 4)

    def test_wrong_names_are_refused_before_anything_runs(self):
        code, out = self.run_script("--variants", "lf10,lf5")
        self.assertNotEqual(code, 0)
        self.assertIn("lf5 is not one of", out)
        code, out = self.run_script("--only", "DWR-1")
        self.assertNotEqual(code, 0)
        self.assertIn("Not in dams_44.csv: DWR-1", out)
        code, out = self.run_script("--dem", os.path.join(self.tmp, "gone.vrt"))
        self.assertNotEqual(code, 0)
        self.assertIn("does not exist", out)
        self.assertEqual(self.calls(), [])

    def test_without_the_dams_layer_it_says_where_it_looked(self):
        os.remove(self.gpkg)
        code, out = self.run_script("--only", "DWR-2801")
        self.assertNotEqual(code, 0)
        self.assertIn("dams_44.gpkg not found", out)
        self.assertEqual(self.calls(), [])


class Report(Scratch):
    ONLY = "DWR-2801,DWR-2645,DWR-2710,DWR-3034"

    def test_a_dam_whose_class_differs_is_listed_and_counted(self):
        code, out = self.run_script("--only", self.ONLY,
                                    classes={"lf20": {"DWR-2801": "Medium", "DWR-3034": "Low"}})
        self.assertEqual(code, 0, out)
        self.assertIn("4 dam(s): 2 change class with the settings, 2 keep it, 0 cannot be judged yet", out)
        t = self.table()
        self.assertEqual({d: r["verdict"] for d, r in t.items()},
                         {"DWR-2801": "changes", "DWR-3034": "changes", "DWR-2645": "same", "DWR-2710": "same"})
        self.assertEqual((t["DWR-2801"]["lf10_nd_class"], t["DWR-2801"]["lf20_nd_class"]), ("High", "Medium"))
        # the counts come from Hazard's snapshot: flooded structures only, their people, the deepest
        self.assertEqual((t["DWR-2801"]["lf10_structures"], t["DWR-2801"]["lf10_persons"],
                          t["DWR-2801"]["lf10_max_depth_ft"]), ("3", "6", "3"))
        self.assertEqual(t["DWR-2801"]["lf20_structures"], "1")
        self.assertEqual(t["DWR-2801"]["dwr_listed_class"], "Significant")
        page = self.page()
        self.assertIn("<b>2 of 4</b> dams have a different class under at least one setting", page)
        self.assertIn("cell size, LISFLOOD-FP (10 m against 20 m)</td><td>4</td><td><b>2</b></td><td>0</td><td>2</td>", page)
        self.assertLess(page.index("Dams whose class changes"), page.index("DWR-2801"))
        self.assertLess(page.index("DWR-2801"), page.index("Dams with the same class"))
        self.assertIn('class="diff"><b>Medium</b>', page)
        self.assertIn("nothing on this page decides a class", page)

    def test_a_failed_dam_cannot_be_judged_and_the_study_is_not_whole(self):
        code, out = self.run_script("--only", self.ONLY, classes={"lf20": {"DWR-2645": "FAIL", "DWR-2710": ""}})
        self.assertEqual(code, 1, out)
        t = self.table()
        self.assertEqual(t["DWR-2645"]["verdict"], "incomplete")
        self.assertEqual((t["DWR-2645"]["lf20_status"], t["DWR-2645"]["lf20_nd_class"]), ("failed", ""))
        self.assertIn("no terrain", t["DWR-2645"]["lf20_message"])
        # a chain that finished without a class is not a result either
        self.assertEqual((t["DWR-2710"]["verdict"], t["DWR-2710"]["lf20_status"]), ("incomplete", "failed"))
        self.assertEqual(t["DWR-2801"]["verdict"], "same")
        self.assertIn("<b>2</b> cannot be judged yet", self.page())
        self.assertIn('<td class="fail">failed</td>', self.page())

    def test_a_dam_that_already_differs_is_listed_even_while_a_setting_is_missing(self):
        code, out = self.run_script("--only", "DWR-2801", "--variants", "all",
                                    classes={"lf20": {"DWR-2801": "Medium"}, "an10": {"DWR-2801": "FAIL"}})
        self.assertEqual(self.table()["DWR-2801"]["verdict"], "changes")
        self.assertEqual(code, 0, out)

    def test_settings_added_later_join_the_earlier_ones_in_the_report(self):
        self.run_script("--only", "DWR-2801")
        os.remove(self.log)
        code, out = self.run_script("--only", "DWR-2801", "--variants", "an10", classes={"an10": {"DWR-2801": "Low"}})
        self.assertEqual(len(self.calls()), 1)                         # only the new setting ran
        t = self.table()["DWR-2801"]
        self.assertEqual((t["lf10_nd_class"], t["lf20_nd_class"], t["an10_nd_class"]), ("High", "High", "Low"))
        self.assertEqual(t["verdict"], "changes")
        self.assertIn("engine at 10 m (LISFLOOD-FP against ANUGA)</td><td>1</td><td><b>1</b></td><td>0</td><td>1</td>",
                      self.page())

    def test_report_only_runs_nothing_and_reads_what_is_there(self):
        self.run_script("--only", "DWR-2801", classes={"lf20": {"DWR-2801": "Medium"}})
        os.remove(self.log)
        os.remove(os.path.join(self.study, "Settings_Study.html"))
        code, out = self.run_script("--only", "DWR-2801", "--report-only")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.calls(), [])
        self.assertIn("<b>1 of 1</b>", self.page())

    def test_report_only_with_nothing_run_says_so(self):
        code, out = self.run_script("--report-only")
        self.assertNotEqual(code, 0)
        self.assertIn("No finished run", out)

    def test_a_dam_not_run_under_a_setting_is_not_run_not_failed(self):
        self.run_script("--only", "DWR-2801,DWR-2645", "--variants", "lf10")
        self.run_script("--only", "DWR-2801", "--variants", "lf20")
        code, out = self.run_script("--only", "DWR-2801,DWR-2645", "--report-only")
        t = self.table()
        self.assertEqual((t["DWR-2645"]["lf20_status"], t["DWR-2645"]["verdict"]), ("not run", "incomplete"))
        self.assertIn('<td class="na">not run</td>', self.page())
        self.assertEqual(code, 1, out)


class Logic(unittest.TestCase):
    def setUp(self):
        self.m = load()

    def r(self, **classes):
        return {v: ({"status": "done", "nd": c} if c not in ("FAIL", None) else
                    {"status": "failed" if c == "FAIL" else "not run", "nd": ""}) for v, c in classes.items()}

    def test_verdict(self):
        v = self.m.verdict
        self.assertEqual(v(self.r(lf10="High", lf20="High"), ["lf10", "lf20"]), "same")
        self.assertEqual(v(self.r(lf10="High", lf20="high"), ["lf10", "lf20"]), "same")        # the spelling is not a change
        self.assertEqual(v(self.r(lf10="High", lf20="Medium"), ["lf10", "lf20"]), "changes")
        self.assertEqual(v(self.r(lf10="High", lf20="FAIL"), ["lf10", "lf20"]), "incomplete")
        self.assertEqual(v(self.r(lf10="High", lf20=None), ["lf10", "lf20"]), "incomplete")
        self.assertEqual(v(self.r(lf10="High"), ["lf10"]), "incomplete")                       # one setting compares nothing

    def test_pair_counts_say_which_way_the_class_moved(self):
        rows = [{"results": self.r(lf10="Low", lf20="High", an10="Low")},
                {"results": self.r(lf10="High", lf20="Medium", an10="High")},
                {"results": self.r(lf10="Medium", lf20="Medium", an10="FAIL")}]
        got = {t: rest for t, *rest in self.m.pair_counts(rows, ["lf10", "lf20", "an10"])}
        self.assertEqual(got["cell size, LISFLOOD-FP (10 m against 20 m)"], [3, 2, 1, 1])
        self.assertEqual(got["engine at 10 m (LISFLOOD-FP against ANUGA)"], [2, 0, 0, 0])
        self.assertEqual(len(got), 2)                                      # an20 was not run: no pair with it

    def test_the_chain_summary_name_and_fields_are_the_runners(self):
        """The report reads the Runner's own summary: if the Runner renames it or its columns,
        this fails here instead of every dam reading 'not run' on a study that took days."""
        with open(os.path.join(os.path.dirname(os.path.dirname(BATCH)), "DamSafety_Runner", "core", "chain.py"),
                  encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn('SUMMARY_NAME = "%s"' % self.m.SUMMARY_NAME, src)
        for col in ("Dam", "Folder", "Status", "Message", "ND_Class", "TM11_Class", "Minutes"):
            self.assertIn('"%s"' % col, src[src.index("SUMMARY_FIELDS = "):src.index("SUMMARY_FIELDS = ") + 400], col)
        hz = os.path.join(os.path.dirname(os.path.dirname(BATCH)), "DamSafety_Hazard")
        with open(os.path.join(hz, "history.py"), encoding="utf-8") as fh:
            his = fh.read()
        self.assertIn('SNAPSHOT_FILE = "Last_Run_Snapshot.json"', his)
        for key in ('"nd_reasons"', '"structures"', '"lij"', '"outcome"', '"persons"', '"depth"'):
            self.assertIn(key, his, key)
        with open(os.path.join(hz, "constants.py"), encoding="utf-8") as fh:
            con = fh.read()
        self.assertIn('OUTPUT_FOLDER_NAME = "outputs"', con)
        self.assertIn('SUBDIR_QA = "QA"', con)

    def test_the_runner_has_every_parameter_the_settings_name(self):
        with open(os.path.join(os.path.dirname(os.path.dirname(BATCH)), "DamSafety_Runner", "algorithms.py"),
                  encoding="utf-8") as fh:
            src = fh.read()
        names = {p.split("=")[0] for p in self.m.COMMON} | {p.split("=")[0] for v in self.m.VARIANTS.values() for p in v[2]}
        for n in sorted(names):
            self.assertIn('"%s"' % n, src, n)


if __name__ == "__main__":
    unittest.main()
