"""Dam folder, preflight gate, and the one-dam flood button. No QGIS required."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from crest.handoff import HandoffError, write_dam_folder, write_named_files  # noqa: E402
from crest.layers import download_aoi, use_offline_pack  # noqa: E402
from crest.qgis_run import run_folder, run_model, run_screening  # noqa: E402

DAM = {
    "nid": "ND00199",
    "name": "Swan Buffalo",
    "county": "Kidder",
    "heightFt": 27.5,
    "storageAcFt": 1615.5,
    "lengthFt": 1700,
    "statedDatum": "NAVD88",
    "method": "froehlich",
    "failureMode": "piping",
    "geometry": {
        "damLine": {
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": [[-100.1, 46.8], [-100.0, 46.81]]},
            "properties": {},
        },
        "breachPoint": {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [-100.05, 46.805]},
            "properties": {},
        },
        "reservoirPoint": {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [-100.06, 46.81]},
            "properties": {},
        },
        "aoi": {
            "type": "Feature",
            "geometry": {
                "type": "Polygon",
                "coordinates": [[[-100.1, 46.7], [-99.9, 46.7], [-99.9, 46.8], [-100.1, 46.7]]],
            },
            "properties": {},
        },
    },
}
ANALYSIS = {
    "datum": {"crestNavdFt": 1071.09, "poolNavdFt": 1060.0},
    "breach": {"peakCfs": 20933, "widthFt": 80, "series": [{"tHr": 0, "qCfs": 0}, {"tHr": 1, "qCfs": 100}]},
    "screen": {"level": "High", "label": "High (flagged)", "reasons": ["desk screen"]},
}

QGIS = r"""#!/usr/bin/env python3
import os, sys
from pathlib import Path
args = sys.argv[1:]
Path(os.environ["CALLS"]).open("a").write("\n".join(args) + "\n---\n")
mode = os.environ.get("QGIS_MODE", "fail")
joined = " ".join(args)
if "preflight" in joined:
    report = next((a.split("=", 1)[1] for a in args if a.startswith("--REPORT=")), "")
    if mode == "report-fail":
        if report:
            Path(report).write_text("Preflight: not ready: no DEM\n", encoding="utf-8")
        sys.exit(0)
    if mode == "report-warn":
        if report:
            Path(report).write_text("Preflight: ready, 2 warning(s)\n", encoding="utf-8")
        sys.exit(0)
    if mode == "fail":
        print("READY:\tfalse")
        print("FAILS:\t2")
        sys.exit(0)
    print("READY:\ttrue")
    print("FAILS:\t0")
    sys.exit(0)
if "runchain" in joined or "rundam" in joined:
    print("started")
    sys.exit(0)
print("unexpected", joined)
sys.exit(9)
"""

CLI = r"""#!/usr/bin/env python3
import os, sys
from pathlib import Path
Path(os.environ["CALLS"]).open("a").write("CLI " + " ".join(sys.argv[1:]) + "\n---\n")
sys.exit(0)
"""


def _exe(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)


class FolderTests(unittest.TestCase):
    def test_writes_suite_record_and_keeps_other_tools(self):
        with tempfile.TemporaryDirectory() as d:
            folder, notes = write_dam_folder(Path(d), DAM, ANALYSIS)
            record = json.loads((folder / "dam.json").read_text(encoding="utf-8"))
            self.assertEqual(record["format"], "nd_dam_suite/dam_record")
            self.assertEqual(record["format_version"], 1)
            self.assertEqual(record["fields"]["height_ft"], 27.5)
            self.assertEqual(record["fields"]["lon"], -100.05)
            self.assertNotIn("crs", record["fields"])
            self.assertNotIn("line_xy", record["proposed_embankment"])
            self.assertEqual(record["proposed_embankment"]["phrase"], "proposed — embankment inserted from the drawn line")
            self.assertEqual(record["proposed_embankment"]["crest_m"], round(1071.09 * 0.3048, 3))
            self.assertTrue(any("line_xy" in n for n in notes))
            self.assertEqual(record["status"][-1]["step"], "screened")
            self.assertTrue(record["status"][-1]["result"]["preview"])
            for name in ("dam_line.geojson", "breach_point.geojson", "reservoir_point.geojson", "downstream_area.geojson", "hydrograph.csv"):
                self.assertTrue((folder / name).is_file(), name)
            self.assertTrue((folder / "hydrograph.csv").read_text(encoding="utf-8").startswith("time_hours,discharge_cms\n"))
            runner = folder / "inputs" / "breach_hydrograph.csv"
            self.assertTrue(runner.read_text(encoding="utf-8").startswith("time_hr,discharge_cfs\n"))
            self.assertIn("1.0000,100.0000", runner.read_text(encoding="utf-8"))
            runner.write_text("keep\n", encoding="utf-8")
            _, notes2 = write_dam_folder(Path(d), DAM, ANALYSIS)
            self.assertEqual(runner.read_text(encoding="utf-8"), "keep\n")
            self.assertTrue(any("already" in n for n in notes2))
            written = write_named_files(folder, {
                "nd00199.par": "par text",
                "flood_strip.geojson": "{}",
                "inputs/extra.csv": "a,b\n",
                "../secret.par": "no",
                "inputs/../no.csv": "no",
            })
            self.assertEqual(sorted(written), ["flood_strip.geojson", "inputs/extra.csv", "nd00199.par"])
            self.assertFalse((folder / "secret.par").exists())

            record["fields"]["height_ft"] = 99
            record["sources"]["height_ft"] = {"tool": "ndbreach", "version": "1", "when": "2020-01-01T00:00:00", "note": ""}
            record["proposed_embankment"] = {"phrase": "proposed — embankment inserted from the drawn line", "line_xy": [[1, 2], [3, 4]], "crest_m": 10}
            (folder / "dam.json").write_text(json.dumps(record), encoding="utf-8")
            write_dam_folder(Path(d), DAM, ANALYSIS)
            again = json.loads((folder / "dam.json").read_text(encoding="utf-8"))
            self.assertEqual(again["fields"]["height_ft"], 99)
            self.assertEqual(again["proposed_embankment"]["line_xy"], [[1, 2], [3, 4]])

    def test_refuses_a_foreign_dam_json(self):
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d) / "dams" / "ND00199"
            folder.mkdir(parents=True)
            (folder / "dam.json").write_text('{"format":"something-else"}', encoding="utf-8")
            with self.assertRaises(HandoffError):
                write_dam_folder(Path(d), DAM, ANALYSIS)
            self.assertEqual(json.loads((folder / "dam.json").read_text(encoding="utf-8"))["format"], "something-else")


class GateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.calls = self.root / "calls.txt"
        self.qgis = self.root / "qgis_process"
        self.cli = self.root / "ndbreach_cli.py"
        _exe(self.qgis, QGIS)
        _exe(self.cli, CLI)
        self.env = {
            "QGIS_PROCESS": os.environ.get("QGIS_PROCESS"),
            "QGIS_MODE": os.environ.get("QGIS_MODE"),
            "CALLS": os.environ.get("CALLS"),
            "CREST_NDBREACH_CLI": os.environ.get("CREST_NDBREACH_CLI"),
            "CREST_PYTHON": os.environ.get("CREST_PYTHON"),
            "CREST_DEM": os.environ.get("CREST_DEM"),
            "NDSUITE_DATA": os.environ.get("NDSUITE_DATA"),
        }
        os.environ["QGIS_PROCESS"] = str(self.qgis)
        os.environ["CALLS"] = str(self.calls)
        os.environ.pop("CREST_NDBREACH_CLI", None)
        os.environ.pop("CREST_DEM", None)
        os.environ.pop("NDSUITE_DATA", None)

    def tearDown(self):
        for key, value in self.env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmp.cleanup()

    def log(self):
        self.lines = []
        return self.lines.append

    def calls_text(self):
        return self.calls.read_text(encoding="utf-8") if self.calls.is_file() else ""

    def test_fail_does_not_start_the_chain(self):
        os.environ["QGIS_MODE"] = "fail"
        result = run_screening(DAM, ANALYSIS, str(self.root / "work"), self.log())
        self.assertFalse(result["ok"])
        text = self.calls_text()
        self.assertIn("nddam:preflight", text)
        self.assertNotIn("nddam:runchain", text)
        self.assertTrue(any("not started" in line for line in self.lines))

    def test_unparsed_report_fail_does_not_start_the_chain(self):
        os.environ["QGIS_MODE"] = "report-fail"
        run_screening(DAM, ANALYSIS, str(self.root / "work"), self.log())
        self.assertNotIn("nddam:runchain", self.calls_text())

    def test_warnings_still_start_the_chain(self):
        os.environ["QGIS_MODE"] = "report-warn"
        result = run_screening(DAM, ANALYSIS, str(self.root / "work"), self.log())
        self.assertTrue(result["ok"])
        self.assertIn("nddam:runchain", self.calls_text())

    def test_pass_starts_the_chain(self):
        os.environ["QGIS_MODE"] = "pass"
        result = run_screening(DAM, ANALYSIS, str(self.root / "work"), self.log())
        self.assertTrue(result["ok"])
        text = self.calls_text()
        self.assertIn("nddam:runchain", text)
        self.assertIn("--PREFLIGHT=true", text)
        self.assertIn("--FAIL_ON_ERROR=true", text)
        self.assertIn("--DAM_ID=ND00199", text)

    def test_model_without_dem_stops(self):
        os.environ["QGIS_MODE"] = "pass"
        os.environ["CREST_NDBREACH_CLI"] = str(self.cli)
        result = run_model(DAM, ANALYSIS, str(self.root / "work"), self.log())
        self.assertFalse(result["ok"])
        self.assertTrue(any("No DEM" in line for line in self.lines))
        self.assertEqual(self.calls_text(), "")

    def test_model_uses_rundam_not_batch_or_a_lonlat_line(self):
        os.environ["QGIS_MODE"] = "pass"
        os.environ["CREST_NDBREACH_CLI"] = str(self.cli)
        work = self.root / "work"
        (work).mkdir()
        (work / "dem.tif").write_bytes(b"not a real dem")
        result = run_model(DAM, ANALYSIS, str(work), self.log())
        self.assertTrue(result["ok"])
        text = self.calls_text()
        self.assertIn("ndbreach:rundam", text)
        self.assertNotIn("ndbreach:batch", text)
        self.assertNotIn("DAM_LINES", text)
        self.assertNotIn("CLI ", text)
        self.assertIn("Froehlich (2008)", text)
        self.assertTrue(any("ndbreach_cli.py needs the dam point" in line for line in self.lines))

    def test_model_uses_cli_only_with_a_dem_point(self):
        os.environ["QGIS_MODE"] = "pass"
        os.environ["CREST_NDBREACH_CLI"] = str(self.cli)
        work = self.root / "work"
        work.mkdir()
        (work / "dem.tif").write_bytes(b"not a real dem")
        dam = {**DAM, "lisflood": {"pointX": 500000, "pointY": 5200000}}
        result = run_model(dam, ANALYSIS, str(work), self.log())
        self.assertTrue(result["ok"])
        text = self.calls_text()
        self.assertIn("CLI run ", text)
        self.assertNotIn("ndbreach:rundam", text)
        config = json.loads((Path(result["folder"]) / "runner_config.json").read_text(encoding="utf-8"))
        self.assertEqual(config["dam"]["point"], [500000, 5200000])
        self.assertEqual(config["breach"]["method"], "piping")
        self.assertEqual(config["model"]["tier"], "lisflood")
        self.assertNotIn("lisflood_exe", config["model"])

    def test_folder_writes_exports_and_does_not_start_qgis(self):
        result = run_folder(DAM, ANALYSIS, str(self.root / "work"), self.log(), {"nd00199.par": "par text", "../no.par": "no"})
        self.assertTrue(result["ok"])
        self.assertEqual(self.calls_text(), "")
        folder = Path(result["folder"])
        self.assertEqual((folder / "nd00199.par").read_text(encoding="utf-8"), "par text")
        self.assertFalse((folder.parent / "no.par").exists())
        self.assertTrue(any("No model was started" in line for line in self.lines))

    def test_model_sends_the_chosen_tier_and_builtin(self):
        os.environ["QGIS_MODE"] = "pass"
        work = self.root / "work"
        work.mkdir()
        (work / "dem.tif").write_bytes(b"not a real dem")
        result = run_model(DAM, ANALYSIS, str(work), self.log(), model="anuga")
        self.assertTrue(result["ok"])
        text = self.calls_text()
        self.assertIn("--TIER=LISFLOOD-FP for the extent, then ANUGA (final results from ANUGA)", text)
        self.assertNotIn("LISFLOOD_EXE", text)
        self.calls.write_text("", encoding="utf-8")
        dam = {**DAM, "lisflood": {"pointX": 500000, "pointY": 5200000}}
        os.environ["CREST_NDBREACH_CLI"] = str(self.cli)
        os.environ["CREST_PYTHON"] = "python3"
        result = run_model(dam, ANALYSIS, str(work), self.log(), model="builtin")
        self.assertTrue(result["ok"])
        config = json.loads((Path(result["folder"]) / "runner_config.json").read_text(encoding="utf-8"))
        self.assertEqual(config["model"]["tier"], "lisflood")
        self.assertEqual(config["model"]["lisflood_exe"], "builtin")

    def test_pack_dem_is_used_without_a_network(self):
        pack = self.root / "pack"
        (pack / "dem").mkdir(parents=True)
        (pack / "dem" / "lidar_1m").mkdir()
        (pack / "dem" / "mosaic.vrt").write_text("<VRTDataset></VRTDataset>", encoding="utf-8")
        os.environ["NDSUITE_DATA"] = str(pack)
        os.environ["QGIS_MODE"] = "pass"
        result = run_model(DAM, ANALYSIS, str(self.root / "work"), self.log(), source="pack")
        self.assertTrue(result["ok"])
        text = self.calls_text()
        self.assertIn("mosaic.vrt", text)
        self.assertNotIn("lidar_1m", text)
        self.assertIn("--TIER=LISFLOOD-FP only", text)
        screen = run_screening(DAM, ANALYSIS, str(self.root / "work"), self.log())
        self.assertTrue(screen["ok"])
        settings = json.loads((self.root / "work" / "crest_settings.json").read_text(encoding="utf-8"))
        self.assertEqual(settings["suite_folder"], str(pack.resolve()))
        self.assertEqual(settings["terrain_source"], 1)
        self.assertTrue(settings["local_dem"].endswith("/dem"))
        self.assertIn("--SETTINGS=", self.calls_text())

    def test_download_uses_the_supplied_fetch(self):
        saved = []

        def fetch(url, dest):
            saved.append(url)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"x")

        work = self.root / "dl"
        result = download_aoi(work, DAM, self.log(), fetch=fetch)
        self.assertTrue(result["ok"])
        self.assertTrue((work / "dem.tif").is_file())
        self.assertTrue((work / "aoi" / "streams.geojson").is_file())
        self.assertTrue((work / "aoi" / "waterbodies.geojson").is_file())
        self.assertTrue(any("3DEPElevation" in url and "imageSR=5070" in url for url in saved))
        self.assertTrue(any("/6/query" in url for url in saved))
        self.assertTrue(any("/12/query" in url for url in saved))
        self.assertTrue((work / "crest_settings.json").is_file())

    def test_missing_pack_does_not_invent_a_folder(self):
        result = use_offline_pack(self.root / "work", self.log())
        self.assertFalse(result["ok"])
        self.assertTrue(any("No ND offline pack folder" in line for line in self.lines))

    def test_http_folder_and_layers(self):
        import time
        import urllib.error
        import urllib.request

        from crest.__main__ import job_snapshot, serve

        server = serve(port=0)
        try:
            port = server.server_address[1]
            body = json.dumps({
                "dam": DAM,
                "analysis": ANALYSIS,
                "work": str(self.root / "httpwork"),
                "model": "hecras",
                "source": "pack",
                "files": {"flood_strip.kml": "<kml></kml>"},
            }).encode()
            request = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/folder",
                data=body,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(request, timeout=5) as response:
                job = json.loads(response.read())
            found = None
            for _ in range(50):
                found = job_snapshot(job["id"])
                if found and found["done"]:
                    break
                time.sleep(0.05)
            self.assertTrue(found and found["done"] and found["ok"])
            self.assertTrue((Path(found["folder"]) / "flood_strip.kml").is_file())
            layers = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/layers",
                data=json.dumps({"dam": DAM, "work": str(self.root / "httpwork"), "source": "pack"}).encode(),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(layers, timeout=5) as response:
                layer_job = json.loads(response.read())
            found = None
            for _ in range(50):
                found = job_snapshot(layer_job["id"])
                if found and found["done"]:
                    break
                time.sleep(0.05)
            self.assertTrue(found and found["done"])
            self.assertFalse(found["ok"])
            self.assertIn("No ND offline pack folder", found["log"])
            missing = urllib.request.Request(f"http://127.0.0.1:{port}/api/nope", data=b"{}", method="POST")
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(missing, timeout=5)
            self.assertEqual(caught.exception.code, 404)
        finally:
            server.shutdown()


if __name__ == "__main__":
    unittest.main(verbosity=2)
