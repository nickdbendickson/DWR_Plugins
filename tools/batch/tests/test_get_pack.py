"""get_offline_pack.py, and the run scripts calling it when the data folder has no pack.

Nothing is downloaded: the pack builder is replaced by a stand-in that records its arguments
and writes the manifest a real build ends with. What is tested is which area is asked for,
that a pack that is there is left alone, that a zip is unpacked only when it is a pack, and
that a run with no pack builds one first and still runs when the build fails.
"""
import csv
import io
import json
import os
import sys
import unittest
import zipfile
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_batch import BATCH, Scratch, load, opt                             # noqa: E402

BUILDER = r'''"""Stand-in for DamSafety_Data/pack_build.py."""
import json, os, sys
a = sys.argv[1:]
with open(os.environ["BUILDER_LOG"], "a") as fh:
    fh.write(json.dumps({"argv": a, "cwd": os.getcwd()}) + "\n")
print("[1/1] ND GIS Hub NG9-1-1 road centerlines")
if os.environ.get("BUILDER_FAIL"):
    print("  FAILED: the server did not answer")
    sys.exit(1)
out = a[a.index("--out") + 1]
box = next((x.split("=", 1)[1] for x in a if x.startswith("--bbox=")), "-104.15,45.83,-96.45,49.1")
pack = os.path.join(out, "ND_Dam_Suite_Pack")
os.makedirs(pack, exist_ok=True)
with open(os.path.join(pack, "manifest.json"), "w") as fh:
    json.dump({"area": "North Dakota", "bbox": [float(v) for v in box.split(",")],
               "created": "2026-11-13T10:00:00", "data_updater_build": True,
               "datasets": {"ND_roads_NG911": {"status": "complete"}}}, fh)
print("  done: 599 features in 1 s")
'''
STATE = [-104.15, 45.83, -96.45, 49.1]


def read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def write_pack(data_folder, bbox=STATE, datasets=None):
    pack = os.path.join(data_folder, "ND_Dam_Suite_Pack")
    os.makedirs(pack, exist_ok=True)
    with open(os.path.join(pack, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump({"area": "North Dakota", "bbox": bbox, "created": "2026-10-12T18:35:51",
                   "datasets": datasets or {"NSI_structures": {"status": "complete"}}}, fh)
    return pack


class PackScratch(Scratch):
    """Scratch plus a stand-in pack builder, found through $PACK_BUILDER."""

    def setUp(self):
        super().setUp()
        self.plugins = os.path.join(self.tmp, "plugins")
        os.makedirs(os.path.join(self.plugins, "DamSafety_Data"))
        os.makedirs(os.path.join(self.plugins, "DamSafety_Core", "damsuite"))
        open(os.path.join(self.plugins, "DamSafety_Data", "__init__.py"), "w").close()
        open(os.path.join(self.plugins, "DamSafety_Core", "damsuite", "packengine.py"), "w").close()
        with open(os.path.join(self.plugins, "DamSafety_Data", "pack_build.py"), "w", encoding="utf-8") as fh:
            fh.write(BUILDER)
        self.blog = os.path.join(self.tmp, "builder.log")
        self.data = os.path.join(self.home, "data")
        self.env.update(PACK_BUILDER=self.plugins, BUILDER_LOG=self.blog, PACK_PYTHON=sys.executable,
                        BATCH_GET_PACK="1")
        self.env.pop("BUILDER_FAIL", None)

    def builds(self):
        if not os.path.exists(self.blog):
            return []
        with open(self.blog, encoding="utf-8") as fh:
            return [json.loads(ln) for ln in fh if ln.strip()]

    def box_of(self, build):
        got = [x for x in build["argv"] if x.startswith("--bbox=")]
        return [float(v) for v in got[0].split("=", 1)[1].split(",")] if got else None


class UnitTest(unittest.TestCase):
    def setUp(self):
        self.g = load("get_offline_pack")
        with open(os.path.join(BATCH, "dams_44.csv"), newline="", encoding="utf-8-sig") as fh:
            self.rows = list(csv.DictReader(fh))

    def test_the_box_takes_in_what_get_data_reads_around_every_dam(self):
        two = [r for r in self.rows if r["dam_id"] in ("DWR-3034", "DWR-4031")]
        box = self.g.dams_bbox(two)
        self.assertEqual(box, self.g.dams_bbox(list(reversed(two))), "the same dams, the same box")
        for v in box:
            self.assertAlmostEqual(v * 100, round(v * 100), places=6, msg="rounded to 0.01 degree")
        for r in two:
            self.assertTrue(self.g.covers(box, self.g.box_around(float(r["lon"]), float(r["lat"]), 15.0)))
        self.assertEqual(self.g.not_covered({"bbox": box}, two), [])
        self.assertGreater(len(self.g.not_covered({"bbox": box}, self.rows)), 10)
        self.assertEqual(len(self.g.not_covered({"bbox": None}, two)), 2, "no recorded area covers nothing")
        self.assertIsNone(self.g.dams_bbox([{"dam_id": "x", "lat": "", "lon": ""}]))

    def test_all_44_dams_are_the_whole_state_and_two_are_not(self):
        self.assertTrue(self.g.is_statewide(self.g.dams_bbox(self.rows)))
        self.assertFalse(self.g.is_statewide(self.g.dams_bbox(self.rows[:1])))
        self.assertFalse(self.g.is_statewide((-90.0, 40.0, -89.0, 41.0)), "a box somewhere else")

    def test_the_command_for_each_builder(self):
        box = (-101.46, 46.72, -100.6, 47.31)
        cmd, cwd = self.g.build_command("dwr", "/p/plugins", "/py", "/data", box)
        self.assertEqual(cmd, ["/py", "-m", "DamSafety_Data.pack_build", "--out", "/data",
                               "--bbox=-101.46,46.72,-100.6,47.31"])
        self.assertEqual(cwd, "/p/plugins")
        cmd, cwd = self.g.build_command("nd", "/p/nd_data_updater/ndpack/build_nd_pack.py", "/py", "/data", box)
        self.assertEqual(cmd, ["/py", "/p/nd_data_updater/ndpack/build_nd_pack.py", "--out",
                               os.path.join("/data", "ND_Dam_Suite_Pack"), "--box=-101.46,46.72,-100.6,47.31"])
        cmd, _ = self.g.build_command("dwr", "/p", "/py", "/data", None, refresh=True)
        self.assertEqual(cmd[-3:], ["--out", "/data", "--refresh"], "no box: the whole State")
        odd = (-101.123456789, 46.1, -100.000000001, 47.0)
        cmd, _ = self.g.build_command("dwr", "/p", "/py", "/data", odd)
        back = tuple(float(v) for v in cmd[-1].split("=", 1)[1].split(","))
        self.assertEqual(back, odd, "the builder refuses to resume an area that differs in any digit")


class FindTest(PackScratch):
    def test_both_builders_are_recognised_and_an_empty_folder_is_not(self):
        g = load("get_offline_pack")
        self.assertEqual(g.builder_in(self.plugins), ("dwr", self.plugins))
        nd = os.path.join(self.tmp, "nd plugins")
        os.makedirs(os.path.join(nd, "nd_data_updater", "ndpack"))
        script = os.path.join(nd, "nd_data_updater", "ndpack", "build_nd_pack.py")
        open(script, "w").close()
        self.assertEqual(g.builder_in(nd), ("nd", script))
        self.assertEqual(g.builder_in(script), ("nd", script))
        self.assertIsNone(g.builder_in(self.tmp))
        self.assertEqual(g.find_builder([self.tmp, nd, self.plugins]), ("nd", script), "the first that has one")
        self.assertIsNone(g.find_builder([self.tmp]))
        os.remove(os.path.join(self.plugins, "DamSafety_Core", "damsuite", "packengine.py"))
        self.assertIsNone(g.builder_in(self.plugins), "DamSafety Data without Core beside it cannot build")

    def test_what_is_there(self):
        g = load("get_offline_pack")
        st = g.pack_status(self.data)
        self.assertFalse(st["found"])
        self.assertIn("no offline pack at", g.describe(st))
        os.makedirs(os.path.join(self.data, "ND_Dam_Suite_Pack"))
        self.assertFalse(g.pack_status(self.data)["found"], "a folder without a manifest is not a pack")
        write_pack(self.data, datasets={"A": {"status": "complete"}, "B": {"status": "partial"}, "C": {}})
        st = g.pack_status(self.data)
        self.assertTrue(st["found"])
        self.assertEqual((st["layers"], st["incomplete"], st["bbox"]), (3, ["B"], tuple(STATE)))
        self.assertIn("not complete: B", g.describe(st))
        with open(os.path.join(self.data, "ND_Dam_Suite_Pack", "manifest.json"), "w") as fh:
            fh.write("{ not json")
        self.assertFalse(g.pack_status(self.data)["found"])


class CommandTest(PackScratch):
    def test_two_dams_build_their_area_and_a_second_run_does_nothing(self):
        code, out = self.run_script("get_offline_pack.py", "--dams", os.path.join(BATCH, "dams_44.csv"),
                                    "--only", "DWR-3034,DWR-4031")
        self.assertEqual(code, 0, out)
        b = self.builds()
        self.assertEqual(len(b), 1)
        self.assertEqual(opt(b[0]["argv"], "--out"), [self.data])
        self.assertEqual(os.path.realpath(b[0]["cwd"]), os.path.realpath(self.plugins))
        box = self.box_of(b[0])
        self.assertTrue(box[0] < -101.44 and box[2] > -101.03 and box[1] < 47.0 and box[3] > 48.3, box)
        self.assertIn("offline pack at", out)
        self.assertIn("ND GIS Hub NG9-1-1 road centerlines", out, "the builder's own lines are passed on")
        code, out = self.run_script("get_offline_pack.py", "--dams", os.path.join(BATCH, "dams_44.csv"),
                                    "--only", "DWR-3034")
        self.assertEqual(code, 0, out)
        self.assertIn("It covers all 1 dam(s).", out)
        self.assertIn("Nothing to do.", out)
        self.assertEqual(len(self.builds()), 1, "a pack that is there is not built again")

    def test_all_44_dams_ask_for_the_whole_state(self):
        code, out = self.run_script("get_offline_pack.py", "--dams", os.path.join(BATCH, "dams_44.csv"))
        self.assertEqual(code, 0, out)
        self.assertIn("building the whole State", out)
        self.assertIsNone(self.box_of(self.builds()[0]), "no box: the builder's own North Dakota")

    def test_a_pack_for_another_area_is_not_replaced(self):
        pack = write_pack(self.data, bbox=[-98.0, 46.0, -97.0, 47.0])
        before = read(os.path.join(pack, "manifest.json"))
        code, out = self.run_script("get_offline_pack.py", "--bbox=-101.2,46.8,-100.8,47.2")
        self.assertEqual(code, 2, out)
        self.assertIn("A pack is never replaced here", out)
        self.assertEqual(self.builds(), [])
        self.assertEqual(read(os.path.join(pack, "manifest.json")), before)

    def test_update_keeps_the_area_of_an_area_pack(self):
        write_pack(self.data, bbox=[-98.0, 46.0, -97.0, 47.0])
        code, out = self.run_script("get_offline_pack.py", "--update")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.box_of(self.builds()[0]), [-98.0, 46.0, -97.0, 47.0])

    def test_check_and_dry_run_start_nothing(self):
        code, out = self.run_script("get_offline_pack.py", "--check")
        self.assertEqual(code, 1, out)
        self.assertIn("no offline pack at", out)
        code, out = self.run_script("get_offline_pack.py", "--dry-run", "--bbox=-101.2,46.8,-100.8,47.2")
        self.assertEqual(code, 0, out)
        self.assertIn("DamSafety_Data.pack_build", out)
        self.assertEqual(self.builds(), [])
        self.assertEqual(self.created(), [], "nothing made, not even the data folder")
        write_pack(self.data)
        self.assertEqual(self.run_script("get_offline_pack.py", "--check")[0], 0)

    def test_a_failed_build_says_to_run_again(self):
        code, out = self.run_script("get_offline_pack.py", BUILDER_FAIL="1")
        self.assertEqual(code, 1, out)
        self.assertIn("The pack was not written", out)
        self.assertIn("the server did not answer", out)

    def test_without_a_builder_it_says_where_it_looked(self):
        empty = os.path.join(self.tmp, "empty")
        os.makedirs(empty)
        g = load("get_offline_pack")
        old, g.plugin_folders = g.plugin_folders, lambda: [empty]
        try:
            with self.assertRaises(SystemExit) as e:
                with redirect_stdout(io.StringIO()):
                    g.main(["--data-folder", self.data])
        finally:
            g.plugin_folders = old
        self.assertIn("No pack builder found", str(e.exception))
        self.assertIn(empty, str(e.exception))

    def test_wrong_arguments_are_refused(self):
        for argv, want in ((["--bbox=1,2,3"], "--bbox needs west,south,east,north"),
                           (["--bbox=-100,47,-101,48"], "--bbox needs west,south,east,north"),
                           (["--only", "DWR-1"], "--only needs --dams"),
                           (["--dams", os.path.join(BATCH, "dams_44.csv"), "--only", "DWR-1"], "Not in dams_44.csv")):
            code, out = self.run_script("get_offline_pack.py", *argv)
            self.assertNotEqual(code, 0, argv)
            self.assertIn(want, out)
        self.assertEqual(self.builds(), [])


class ZipTest(PackScratch):
    def make_zip(self, name, members):
        path = os.path.join(self.tmp, name)
        with zipfile.ZipFile(path, "w") as zf:
            for n, data in members.items():
                zf.writestr(n, data)
        return path

    def manifest(self):
        return json.dumps({"area": "North Dakota", "bbox": STATE, "created": "2026-10-12T18:35:51",
                           "datasets": {"ND_roads_NG911": {"status": "complete"}}})

    def test_the_parts_of_a_pack_are_unpacked_into_one_folder(self):
        a = self.make_zip("part1.zip", {"ND_Dam_Suite_Pack/manifest.json": self.manifest(),
                                        "ND_Dam_Suite_Pack/hydro/3DHP_flowlines.gpkg": "x"})
        b = self.make_zip("part2.zip", {"ND_Dam_Suite_Pack/manifest.json": self.manifest(),
                                        "ND_Dam_Suite_Pack/roads/ND_roads_NG911.gpkg": "y"})
        code, out = self.run_script("get_offline_pack.py", "--zip", a, b)
        self.assertEqual(code, 0, out)
        pack = os.path.join(self.data, "ND_Dam_Suite_Pack")
        for rel in ("manifest.json", "hydro/3DHP_flowlines.gpkg", "roads/ND_roads_NG911.gpkg"):
            self.assertTrue(os.path.isfile(os.path.join(pack, *rel.split("/"))), rel)
        self.assertEqual(os.listdir(self.data), ["ND_Dam_Suite_Pack"], "the unpacking folder is gone")
        self.assertEqual(self.builds(), [], "nothing is built when a pack is unpacked")
        code, out = self.run_script("get_offline_pack.py", "--zip", a)
        self.assertNotEqual(code, 0)
        self.assertIn("is already there. It is not replaced", out)

    def test_a_zip_that_is_not_a_pack_unpacks_nothing(self):
        cases = {"outside.zip": {"ND_Dam_Suite_Pack/manifest.json": self.manifest(), "../evil.txt": "x"},
                 "other.zip": {"ND_Dam_Suite_Pack/manifest.json": self.manifest(), "somewhere/else.txt": "x"},
                 "nomanifest.zip": {"ND_Dam_Suite_Pack/roads/ND_roads_NG911.gpkg": "y"}}
        for name, members in cases.items():
            code, out = self.run_script("get_offline_pack.py", "--zip", self.make_zip(name, members))
            self.assertNotEqual(code, 0, name)
            self.assertIn("Nothing was unpacked", out, name)
            self.assertEqual(os.listdir(self.data), [], name)
        self.assertFalse(os.path.exists(os.path.join(self.home, "evil.txt")))
        code, out = self.run_script("get_offline_pack.py", "--zip", "http://example.com/pack.zip")
        self.assertNotEqual(code, 0)
        self.assertIn("Only https links are downloaded", out)


class RunScriptsTest(PackScratch):
    def test_no_pack_builds_one_for_the_dams_before_they_run(self):
        code, out = self.run_script("run_lisflood_44.py", "--only", "DWR-3034,DWR-4031", "--parallel", "1")
        self.assertEqual(code, 0, out)
        b = self.builds()
        self.assertEqual(len(b), 1)
        self.assertEqual(opt(b[0]["argv"], "--out"), [self.data])
        box = self.box_of(b[0])
        self.assertTrue(box[0] < -101.44 and box[3] > 48.3, box)
        self.assertLess(out.index("Building one for"), out.index("start DWR-"), "the pack comes first")
        self.assertIn("Built: offline pack at", out)
        self.assertEqual(len([c for c in self.calls() if "run" in c]), 2)
        code, out = self.run_script("run_lisflood_44.py", "--only", "DWR-3034", "--redo")
        self.assertEqual(code, 0, out)
        self.assertEqual(len(self.builds()), 1, "the pack is there now")
        self.assertNotIn("offline pack", out, "a pack that covers the dams is not mentioned")

    def test_a_pack_that_does_not_cover_a_dam_is_said_and_left_alone(self):
        write_pack(self.data, bbox=[-98.0, 46.0, -97.0, 47.0])
        code, out = self.run_script("run_lisflood_44.py", "--only", "DWR-3034")
        self.assertEqual(code, 0, out)
        self.assertIn("does not cover 1 of 1 dam(s) (DWR-3034)", out)
        self.assertEqual(self.builds(), [])

    def test_a_pack_chosen_outside_the_data_folder_is_not_doubled(self):
        other = write_pack(os.path.join(self.tmp, "ssd"))
        code, out = self.run_script("run_lisflood_44.py", "--only", "DWR-3034", NDSUITE_PACK=other)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.builds(), [], "$NDSUITE_PACK names a pack: none is built")
        prof = os.path.join(self.tmp, "profiles", "nd", "QGIS")
        os.makedirs(prof)
        with open(os.path.join(prof, "QGIS3.ini"), "w", encoding="utf-8") as fh:
            fh.write("[General]\nx=1\n\n[ndsuite]\ndata_folder=/somewhere\npack_path=%s\n" % other)
        roots = os.path.join(self.tmp, "profiles")
        code, out = self.run_script("run_lisflood_44.py", "--only", "DWR-3034", "--redo",
                                    DAMCHAIN_QGIS_PROFILES=roots)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.builds(), [], "the pack chosen in QGIS counts too")
        code, out = self.run_script("get_offline_pack.py", DAMCHAIN_QGIS_PROFILES=roots)
        self.assertEqual(code, 0, out)
        self.assertIn("chosen outside the data folder", out)
        self.assertEqual(self.builds(), [])
        code, out = self.run_script("run_lisflood_44.py", "--only", "DWR-3034", "--redo",
                                    NDSUITE_PACK=os.path.join(self.tmp, "not there"))
        self.assertEqual(len(self.builds()), 1, "a chosen pack that does not exist is no pack")

    def test_no_get_pack_and_the_environment_switch_only_warn(self):
        for args, env in ((["--no-get-pack"], {}), ([], {"BATCH_GET_PACK": "0"})):
            code, out = self.run_script("run_lisflood_44.py", "--only", "DWR-3034", "--redo", *args, **env)
            self.assertEqual(code, 0, out)
            self.assertIn("WARNING: no offline pack at", out)
            self.assertIn("Screening then stops at its check", out)
        self.assertEqual(self.builds(), [])

    def test_a_build_that_fails_does_not_stop_the_run(self):
        code, out = self.run_script("run_lisflood_44.py", "--only", "DWR-3034", BUILDER_FAIL="1")
        self.assertEqual(code, 0, out)
        self.assertIn("the pack was not built (exit 1", out)
        self.assertEqual(len([c for c in self.calls() if "run" in c]), 1)

    def test_a_dry_run_names_the_build_and_starts_none(self):
        code, out = self.run_script("run_lisflood_44.py", "--dry-run")
        self.assertEqual(code, 0, out)
        self.assertIn("The run would build one for the whole State first", out)
        self.assertEqual(self.builds(), [])
        self.assertEqual(self.created(), [])


COUNTY_COLS = ["kind", "dam_id", "name", "county", "status", "lat", "lon", "height_ft", "storage_acft",
               "dam_length_ft", "dwr_structure_id", "nid_id", "existing_hazard_class", "huc8", "has_height_storage"]


class CountyPackTest(PackScratch):
    def setUp(self):
        super().setUp()
        self.cdata = os.path.join(self.tmp, "county data")
        os.makedirs(self.cdata)
        with open(os.path.join(self.cdata, "nd_dams_dugouts.csv"), "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(COUNTY_COLS)
            w.writerow(["dam", "DWR-1", "Alpha", "Cass", "Intact", "46.9", "-97.2", "20", "100", "300", "1", "", "Low", "x", "yes"])
            w.writerow(["dam", "DWR-2", "Beta", "Cass", "Intact", "47.1", "-96.9", "15", "50", "300", "2", "", "Low", "x", "yes"])
            w.writerow(["dam", "DWR-9", "Far", "Williams", "Intact", "48.2", "-103.6", "15", "50", "300", "9", "", "Low", "x", "yes"])
        self.env["BATCH_DATA_DIR"] = self.cdata

    def test_a_county_builds_the_pack_for_its_own_area(self):
        code, out = self.run_script("run_county.py", "--county", "Cass", "--kind", "dams", "--parallel", "1")
        self.assertEqual(code, 0, out)
        b = self.builds()
        self.assertEqual(len(b), 1)
        box = self.box_of(b[0])
        self.assertTrue(box[0] < -97.2 and box[2] > -96.9 and box[0] > -99.0, "Cass, not Williams: %s" % box)
        self.assertEqual(sorted(opt(c, "--ids")[0] for c in self.calls() if "run" in c), ["DWR-1", "DWR-2"])

    def test_anuga_mode_and_no_get_pack_build_nothing(self):
        open(os.path.join(self.cdata, "nd_dams_dugouts.gpkg"), "w").close()
        self.run_script("run_county.py", "--county", "Cass", "--kind", "dams", "--mode", "anuga")
        self.run_script("run_county.py", "--county", "Cass", "--kind", "dams", "--no-get-pack")
        self.assertEqual(self.builds(), [])


if __name__ == "__main__":
    unittest.main()
