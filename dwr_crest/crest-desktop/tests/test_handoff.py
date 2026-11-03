"""Dam folder, preflight gate, and the one-dam flood button. No QGIS required."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import io  # noqa: E402

import crest.__main__ as m  # noqa: E402
from crest.handoff import HandoffError, write_dam_folder, write_named_files  # noqa: E402
from crest.layers import download_aoi, use_offline_pack  # noqa: E402
from crest.qgis_run import (  # noqa: E402
    _collect_bounded,
    _taskkill_tree,
    chain_names,
    chain_results,
    run_folder,
    run_model,
    run_screening,
)

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
    "datum": {"crestNavdFt": 1071.09, "poolNavdFt": 1060.0, "streambedNavdFt": 1043.59},
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
    print("started", flush=True)
    if mode == "slow":
        import time
        time.sleep(60)
    if mode == "outputs" and "runchain" in joined:
        import csv, sqlite3
        work = Path(next(a.split("=", 1)[1] for a in args if a.startswith("--WORK=")))
        (work / "1_dams.gpkg").unlink(missing_ok=True)
        con = sqlite3.connect(work / "1_dams.gpkg")
        con.execute("CREATE TABLE gpkg_contents (table_name TEXT, data_type TEXT)")
        con.execute("INSERT INTO gpkg_contents VALUES ('dams', 'features')")
        con.execute("CREATE TABLE dams (fid INTEGER PRIMARY KEY, dam_id TEXT, nid_id TEXT)")
        con.execute("INSERT INTO dams (dam_id, nid_id) VALUES ('DWR-2801', 'ND00199'), ('DWR-1', 'ND00001')")
        con.commit()
        con.close()
        (work / "10_reports").mkdir(exist_ok=True)
        with (work / "10_reports" / "summary.csv").open("w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["dam_id", "name", "hz_class", "haz_existing", "hz_vs_existing", "hz_conf", "review"])
            w.writerow(["DWR-2801", "Test dam", "Significant", "Significant", "same", "medium", ""])
            w.writerow(["DWR-1", "Other", "High", "Low", "higher", "low", "check"])
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
    def test_writes_streambed_with_datum_and_source_like_top_of_dam(self):
        with tempfile.TemporaryDirectory() as d:
            folder, _ = write_dam_folder(Path(d), DAM, ANALYSIS)
            record = json.loads((folder / "dam.json").read_text(encoding="utf-8"))
            fields, sources = record["fields"], record["sources"]
            self.assertEqual(fields["streambed_ft"], 1043.59)
            self.assertEqual(fields["streambed_datum"], fields["top_of_dam_datum"])
            for key in ("streambed_ft", "streambed_datum"):
                self.assertEqual(sources[key]["tool"], sources["top_of_dam_ft"]["tool"])
                self.assertIn("when", sources[key])
            no_bed = {**ANALYSIS, "datum": {"crestNavdFt": 1071.09}}
            folder2, _ = write_dam_folder(Path(d) / "b", DAM, no_bed)
            fields2 = json.loads((folder2 / "dam.json").read_text(encoding="utf-8"))["fields"]
            self.assertNotIn("streambed_ft", fields2)
            self.assertNotIn("streambed_datum", fields2)

    def test_labels_tied_elevations_navd88_whatever_the_stated_datum(self):
        cases = [
            # Local 0-300 ft crest tied to the DEM: the written values are NAVD88, not "local".
            ({"statedDatum": "Local"}, {"offsetFt": 1509.76, "warnings": []}, "NAVD88 ft"),
            # NGVD29 shifted by VERTCON: NAVD88.
            ({"statedDatum": "NGVD29"}, {"offsetFt": 0.4, "warnings": []}, "NAVD88 ft"),
            # NGVD29 with no shift available: still NGVD29.
            ({"statedDatum": "NGVD29"}, {"offsetFt": None, "warnings": ["NGVD29 offset unknown"]}, "NGVD29 ft"),
            # Local crest with no DEM tie: left as stated.
            ({"statedDatum": "Local"}, {"offsetFt": None, "warnings": ["No DEM tie. Crest left unconverted."]}, "local"),
        ]
        with tempfile.TemporaryDirectory() as d:
            for i, (dam_patch, datum_patch, want) in enumerate(cases):
                analysis = {**ANALYSIS, "datum": {**ANALYSIS["datum"], **datum_patch}}
                folder, _ = write_dam_folder(Path(d) / str(i), {**DAM, **dam_patch}, analysis)
                fields = json.loads((folder / "dam.json").read_text(encoding="utf-8"))["fields"]
                self.assertEqual(fields["top_of_dam_datum"], want, dam_patch)
                self.assertEqual(fields["streambed_datum"], want, dam_patch)

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
            "CREST_SCREENING_TIMEOUT_S": os.environ.get("CREST_SCREENING_TIMEOUT_S"),
        }
        os.environ.pop("CREST_SCREENING_TIMEOUT_S", None)
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

    def test_chain_settings_name_the_dam(self):
        # DamSafety_Screening's nddam:runchain has no DAM_ID parameter: it screens the dams in
        # settings["only_ids"], so the handoff writes the dam there even with no pack or DEM.
        os.environ["QGIS_MODE"] = "pass"
        result = run_screening(DAM, ANALYSIS, str(self.root / "work"), self.log())
        self.assertTrue(result["ok"])
        path = self.root / "work" / "crest_settings.json"
        settings = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(settings["only_ids"], "ND00199")
        text = self.calls_text()
        self.assertIn(f"--SETTINGS={path}", text)
        self.assertIn("--DAM_ID=ND00199", text)
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
        self.assertEqual(settings["only_ids"], "ND00199")
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

        from crest.__main__ import API_TOKEN, TOKEN_HEADER, job_snapshot, serve

        # jobs sent over the API may only write under the allowed roots (crest.qgis_run.resolve_work)
        os.environ["CREST_WORK_ROOTS"] = str(self.root)
        self.addCleanup(os.environ.pop, "CREST_WORK_ROOTS", None)
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
                headers={"Content-Type": "application/json", TOKEN_HEADER: API_TOKEN},
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
                headers={"Content-Type": "application/json", TOKEN_HEADER: API_TOKEN},
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
            missing = urllib.request.Request(f"http://127.0.0.1:{port}/api/nope", data=b"{}", method="POST", headers={TOKEN_HEADER: API_TOKEN}
            )
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(missing, timeout=5)
            self.assertEqual(caught.exception.code, 404)
        finally:
            server.shutdown()


    def test_timeout_stops_the_chain(self):
        os.environ["QGIS_MODE"] = "slow"
        started = time.monotonic()
        result = run_screening(DAM, ANALYSIS, str(self.root / "work"), self.log(), timeout=1.5)
        self.assertLess(time.monotonic() - started, 30)
        self.assertFalse(result["ok"])
        self.assertTrue(result["timedOut"])
        self.assertTrue(any("Stopped" in line for line in self.lines))

    def test_timeout_from_environment(self):
        os.environ["QGIS_MODE"] = "slow"
        os.environ["CREST_SCREENING_TIMEOUT_S"] = "1"
        started = time.monotonic()
        result = run_screening(DAM, ANALYSIS, str(self.root / "work"), self.log())
        self.assertLess(time.monotonic() - started, 30)
        self.assertTrue(result["timedOut"])

    def test_cancel_stops_the_chain(self):
        os.environ["QGIS_MODE"] = "slow"
        cancel = threading.Event()
        threading.Timer(1.5, cancel.set).start()
        started = time.monotonic()
        result = run_screening(DAM, ANALYSIS, str(self.root / "work"), self.log(), cancel=cancel)
        self.assertLess(time.monotonic() - started, 30)
        self.assertFalse(result["ok"])
        self.assertTrue(result["cancelled"])

    def test_chain_name_and_result_are_read_back(self):
        os.environ["QGIS_MODE"] = "outputs"
        work = self.root / "work"
        result = run_screening(DAM, ANALYSIS, str(work), self.log())
        self.assertTrue(result["ok"])
        self.assertEqual(result["chainIds"], ["DWR-2801"])
        self.assertEqual([r["dam_id"] for r in result["chainResult"]], ["DWR-2801"])
        text = "\n".join(self.lines)
        self.assertIn("name this dam DWR-2801 (Crest: ND00199)", text)
        self.assertIn("Screening result for DWR-2801: Significant", text)
        self.assertIn("not an official class", text)
        # A second run in the same work folder sends both names, so later tools keep the dam.
        run_screening(DAM, ANALYSIS, str(work), self.log())
        self.assertIn("--DAM_ID=ND00199,DWR-2801", self.calls_text())
        settings = json.loads((work / "crest_settings.json").read_text(encoding="utf-8"))
        self.assertEqual(settings["only_ids"], "ND00199,DWR-2801")

    def test_read_back_helpers_without_outputs(self):
        self.assertEqual(chain_names(self.root, "ND00199"), [])
        self.assertEqual(chain_results(self.root, ["DWR-2801"]), [])


class RobustnessTests(unittest.TestCase):
    """Audit H1 (Windows tree-kill), H2 (bounded output + job eviction), H3 (per-folder lock + status gating)."""

    # ---- H3: status append is gated, and a plain folder export does not log a screening -------------------
    def test_append_status_false_writes_no_screened_entry(self):
        with tempfile.TemporaryDirectory() as d:
            folder, _ = write_dam_folder(Path(d), DAM, ANALYSIS, append_status=False)
            record = json.loads((folder / "dam.json").read_text(encoding="utf-8"))
            self.assertEqual(record["status"], [])
            # The merged fields are still written — only the screening log entry is withheld.
            self.assertEqual(record["fields"]["height_ft"], 27.5)

    def test_plain_folder_export_does_not_grow_the_status_list(self):
        with tempfile.TemporaryDirectory() as d:
            work = Path(d) / "work"
            log = lambda line: None
            for _ in range(3):
                result = run_folder(DAM, ANALYSIS, str(work), log)
            record = json.loads((Path(result["folder"]) / "dam.json").read_text(encoding="utf-8"))
            self.assertEqual(record["status"], [], "a plain folder export must not append a screened status")

    def test_screening_still_appends_one_status_per_run(self):
        with tempfile.TemporaryDirectory() as d:
            folder, _ = write_dam_folder(Path(d), DAM, ANALYSIS)  # default append_status=True
            record = json.loads((folder / "dam.json").read_text(encoding="utf-8"))
            self.assertEqual(len(record["status"]), 1)
            self.assertEqual(record["status"][-1]["step"], "screened")

    def test_concurrent_writes_to_one_folder_lose_no_status_entry(self):
        # Without the per-folder lock, concurrent read-merge-write on the same dam.json would drop appends.
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            n = 12
            start = threading.Event()
            errors: list[Exception] = []

            def writer():
                start.wait()
                try:
                    write_dam_folder(root, DAM, ANALYSIS)  # each appends one screened status
                except Exception as exc:  # noqa: BLE001 — surface any error to the assertion
                    errors.append(exc)

            threads = [threading.Thread(target=writer) for _ in range(n)]
            for t in threads:
                t.start()
            start.set()
            for t in threads:
                t.join(timeout=20)
            self.assertEqual(errors, [])
            folder = root / "dams" / "ND00199"
            record = json.loads((folder / "dam.json").read_text(encoding="utf-8"))
            self.assertEqual(len(record["status"]), n, "every concurrent write must land its status entry")

    # ---- H2: run output is kept head + tail only -------------------------------------------------------------
    def test_collect_bounded_keeps_head_and_tail_and_notes_the_drop(self):
        block = 8192
        data = "A" * block + "B" * (block * 5) + "C" * block
        text = _collect_bounded(io.StringIO(data), head_cap=block, tail_cap=block)
        self.assertTrue(text.startswith("A" * 100))
        self.assertTrue(text.rstrip().endswith("C" * 100))
        self.assertNotIn("B", text, "the middle must be dropped, not retained")
        self.assertIn("omitted", text)
        self.assertLess(len(text), len(data))

    def test_collect_bounded_keeps_short_output_whole(self):
        text = _collect_bounded(io.StringIO("short output\n"), head_cap=8192, tail_cap=8192)
        self.assertEqual(text, "short output\n")

    # ---- H2: finished jobs are evicted past the cap, running jobs never are ----------------------------------
    def test_finished_jobs_evicted_past_cap_running_kept(self):
        with m._jobs_lock:
            saved_jobs = dict(m._jobs)
            saved_cancels = dict(m._cancels)
            m._jobs.clear()
            m._cancels.clear()
            m._jobs["run"] = {"id": "run", "done": False}  # a running job, inserted first (oldest)
            for i in range(m.MAX_JOBS + 5):
                m._jobs[f"d{i}"] = {"id": f"d{i}", "done": True}
            m._evict_finished_locked()
            ids = list(m._jobs)
            # restore
            m._jobs.clear()
            m._jobs.update(saved_jobs)
            m._cancels.clear()
            m._cancels.update(saved_cancels)
        self.assertEqual(len(ids), m.MAX_JOBS)
        self.assertIn("run", ids, "a running job is never evicted")
        self.assertNotIn("d0", ids, "the oldest finished job is evicted first")
        self.assertIn(f"d{m.MAX_JOBS + 4}", ids, "the newest finished job is kept")

    # ---- H1: Windows tree-kill helper degrades gracefully when taskkill is absent ----------------------------
    def test_taskkill_tree_returns_bool_and_never_raises(self):
        # On this POSIX CI host taskkill is absent, so the helper returns False (the caller then falls back to
        # terminate()/kill()); on Windows it runs taskkill and returns True. Either way it must not raise.
        self.assertIsInstance(_taskkill_tree(2_000_000_000), bool)


if __name__ == "__main__":
    unittest.main(verbosity=2)
