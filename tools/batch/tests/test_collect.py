"""collect_results_44.py against a made-up dams folder: what goes in the zips and what does not."""
import csv
import importlib.util
import io
import json
import os
import shutil
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
BATCH = os.path.dirname(HERE)


def load(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(BATCH, name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def put(path, data=b"x"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data)


class CollectTest(unittest.TestCase):
    def setUp(self):
        self.c = load("collect_results_44")
        self.tmp = tempfile.mkdtemp(prefix="batch-collect-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.dams = os.path.join(self.tmp, "ND_Dams")
        self.out = os.path.join(self.tmp, "out")
        # DWR-3034: finished, all four rasters, a report, a big snapshot file that must stay behind.
        d = os.path.join(self.dams, "DWR-3034")
        for r in self.c.RASTERS:
            put(os.path.join(d, r), os.urandom(4000))
        put(os.path.join(d, "dam.json"), b'{"dam_id": "DWR-3034"}')
        put(os.path.join(d, "_breach_model", "run_report.json"), json.dumps({"summary": {"cell_m": 10}}).encode())
        put(os.path.join(d, "_breach_model", "run_report.md"), b"# report")
        put(os.path.join(d, "_breach_model", "depth_snapshots.npz"), os.urandom(50000))
        put(os.path.join(d, "_breach_model", "huge.csv"), b"0" * (self.c.TEXT_MAX + 1))
        put(os.path.join(d, "_breach_model", "lisflood_hmax_ft.tif"), os.urandom(5000))
        put(os.path.join(d, "data", "dem.tif"), os.urandom(50000))
        put(os.path.join(d, "_chain_nd", "state.json"), json.dumps({"status": "done", "nd_class": "Low"}).encode())
        # DWR-4031: failed in the chain, no rasters.
        d = os.path.join(self.dams, "DWR-4031")
        put(os.path.join(d, "dam.json"), b"{}")
        put(os.path.join(d, "_chain_nd", "state.json"), json.dumps({"status": "failed", "error": "3DEP down\nagain"}).encode())
        # DWR-2865: rasters only in a run subfolder (and a model folder's copy that is not a result).
        d = os.path.join(self.dams, "DWR-2865")
        put(os.path.join(d, "run1", "Depth (Max).tif"), os.urandom(3000))
        put(os.path.join(d, "_breach_model", "Depth (Max).tif"), os.urandom(3000))
        put(os.path.join(d, "dam.json"), b"{}")
        put(os.path.join(self.dams, "batch44_lisflood_summary.csv"), b"dam_id,status\n")

    def run_main(self, *argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = self.c.main(["--dams-folder", self.dams, "--out", self.out, *argv])
        return rc, buf.getvalue()

    def names(self):
        got = {}
        for f in sorted(os.listdir(self.out)):
            with zipfile.ZipFile(os.path.join(self.out, f)) as zf:
                got[f] = zf.namelist()
        return got

    def test_packs_results_and_small_text_only(self):
        rc, text = self.run_main("--only", "DWR-3034,DWR-4031,DWR-2865")
        self.assertEqual(rc, 0)
        zips = self.names()
        self.assertEqual(len(zips), 1)
        names = set(next(iter(zips.values())))
        for r in self.c.RASTERS:
            self.assertIn("DWR-3034/" + r, names)
        for keep in ("DWR-3034/dam.json", "DWR-3034/_breach_model/run_report.json", "DWR-3034/_breach_model/run_report.md",
                     "DWR-3034/_chain_nd/state.json", "DWR-4031/dam.json", "DWR-4031/_chain_nd/state.json",
                     "DWR-2865/Depth (Max).tif", "batch44_lisflood_summary.csv", "MANIFEST.csv"):
            self.assertIn(keep, names)
        for left in ("DWR-3034/_breach_model/depth_snapshots.npz", "DWR-3034/_breach_model/huge.csv",
                     "DWR-3034/_breach_model/lisflood_hmax_ft.tif", "DWR-3034/data/dem.tif",
                     "DWR-2865/_breach_model/Depth (Max).tif"):
            self.assertNotIn(left, names)
        self.assertIn("1 of 3 dams finished; 1 of them have all four rasters.", text)

    def test_manifest_says_what_each_dam_has(self):
        self.run_main("--only", "DWR-3034,DWR-4031,DWR-2865,DWR-2801")
        path = os.path.join(self.out, sorted(os.listdir(self.out))[0])
        with zipfile.ZipFile(path) as zf:
            rows = {r["dam_id"]: r for r in csv.DictReader(io.StringIO(zf.read("MANIFEST.csv").decode()))}
        self.assertEqual(rows["DWR-3034"]["status"], "done")
        self.assertEqual(rows["DWR-3034"]["nd_class"], "Low")
        self.assertEqual(rows["DWR-3034"]["cell_m"], "10")
        self.assertEqual(rows["DWR-3034"]["rasters"], "4")
        self.assertEqual(rows["DWR-3034"]["missing"], "")
        self.assertEqual(rows["DWR-4031"]["status"], "failed: 3DEP down again")
        self.assertEqual(rows["DWR-4031"]["rasters"], "0")
        self.assertEqual(rows["DWR-2865"]["status"], "started, not finished")
        self.assertIn("Velocity (Max).tif", rows["DWR-2865"]["missing"])
        self.assertEqual(rows["DWR-2801"]["status"], "no folder (not run)")

    def test_starts_a_new_zip_past_the_size_and_never_splits_a_dam(self):
        self.run_main("--only", "DWR-3034,DWR-2865,DWR-4031", "--max-mb", "0.001")
        zips = self.names()
        self.assertEqual(len(zips), 3)
        for names in zips.values():
            self.assertIn("MANIFEST.csv", names)
            self.assertEqual(len({n.split("/")[0] for n in names if "/" in n}), 1)

    def test_list_writes_nothing_and_the_dams_folder_is_untouched(self):
        before = sorted(os.path.join(r, f) for r, _d, fs in os.walk(self.dams) for f in fs)
        rc, text = self.run_main("--list")
        self.assertEqual(rc, 0)
        self.assertFalse(os.path.exists(self.out))
        self.assertIn("DWR-3034", text)
        self.run_main()
        self.assertEqual(before, sorted(os.path.join(r, f) for r, _d, fs in os.walk(self.dams) for f in fs))

    def test_stops_with_a_reason(self):
        with self.assertRaises(SystemExit) as e:
            self.c.main(["--dams-folder", os.path.join(self.tmp, "nope"), "--out", self.out])
        self.assertIn("is not a folder", str(e.exception))
        with self.assertRaises(SystemExit) as e:
            self.c.main(["--dams-folder", self.dams, "--out", self.out, "--only", "DWR-1"])
        self.assertIn("Not in dams_44.csv", str(e.exception))


if __name__ == "__main__":
    unittest.main()
