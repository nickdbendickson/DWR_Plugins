"""run_results.py (heim runs): dam-breach results in a database Heim answers from exactly.

Nick asked whether the local AI could hold the run results so he can ask "what is the depth at
this structure" or "what was the peak flow". The document index cannot do that reliably: a
passage search can return the neighbour's row and a model can round or swap a number. These
tests hold the parts that make the answers exact: what is read from each kind of run folder
(DamSafety Runner, DamIT Runner, a copied folder with only run_report.md), the Hazard tables
(GeoPackage read with no GDAL, CSV with WKT, never both), the read-only query path, and the
number check on the model's sentence. The model is stubbed; no Ollama, no GDAL needed (the one
raster test runs only where GDAL is importable).
"""
import hashlib
import importlib.util
import io
import json
import os
import shutil
import sqlite3
import struct
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.join(os.path.dirname(HERE), "tools")
SRC = os.path.join(TOOLS, "run_results.py")


def load(name="run_results_test"):
    spec = importlib.util.spec_from_file_location(name, SRC)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ----------------------------------------------------------------------------- fixtures
def gp_point(x, y, srs=26914):
    """A GeoPackage point blob with no envelope (how QGIS writes points)."""
    return b"GP" + bytes([0, 0b00000001]) + struct.pack("<i", srs) + struct.pack("<BIdd", 1, 1, x, y)


def gp_polygon(ring, srs=26914):
    """A polygon blob WITH an XY envelope (how QGIS writes polygons)."""
    xs, ys = [p[0] for p in ring], [p[1] for p in ring]
    env = struct.pack("<dddd", min(xs), max(xs), min(ys), max(ys))
    wkb = struct.pack("<BII", 1, 3, 1) + struct.pack("<I", len(ring)) + b"".join(struct.pack("<dd", *p) for p in ring)
    return b"GP" + bytes([0, 0b00000011]) + struct.pack("<i", srs) + env + wkb


def write_gpkg(path, layers):
    """A minimal GeoPackage: {layer: (geom column, [(blob, {field: value})])}."""
    db = sqlite3.connect(path)
    db.executescript("""
      CREATE TABLE gpkg_spatial_ref_sys (srs_name TEXT, srs_id INTEGER PRIMARY KEY, organization TEXT,
        organization_coordsys_id INTEGER, definition TEXT, description TEXT);
      CREATE TABLE gpkg_contents (table_name TEXT PRIMARY KEY, data_type TEXT, identifier TEXT,
        description TEXT, last_change TEXT, min_x REAL, min_y REAL, max_x REAL, max_y REAL, srs_id INTEGER);
      CREATE TABLE gpkg_geometry_columns (table_name TEXT, column_name TEXT, geometry_type_name TEXT,
        srs_id INTEGER, z INTEGER, m INTEGER);
      INSERT INTO gpkg_spatial_ref_sys VALUES ('NAD83 / UTM 14N', 26914, 'EPSG', 26914, '', '');
    """)
    for layer, (gcol, feats) in layers.items():
        fields = list(feats[0][1].keys())
        db.execute('CREATE TABLE "%s" (fid INTEGER PRIMARY KEY, "%s" BLOB, %s)' % (
            layer, gcol, ", ".join('"%s"' % f for f in fields)))
        db.execute("INSERT INTO gpkg_contents (table_name, data_type, srs_id) VALUES (?, 'features', 26914)", (layer,))
        db.execute("INSERT INTO gpkg_geometry_columns VALUES (?, ?, 'GEOMETRY', 26914, 0, 0)", (layer, gcol))
        for blob, attrs in feats:
            db.execute('INSERT INTO "%s" ("%s", %s) VALUES (?, %s)' % (
                layer, gcol, ", ".join('"%s"' % f for f in fields), ", ".join("?" * len(fields))),
                [blob] + [attrs[f] for f in fields])
    db.commit()
    db.close()


DWR_REPORT = {
    "tool": "ND Dam Breach Runner 3.20.5", "run_started": "2026-11-11T16:20:36",
    "run_finished": "2026-11-11T16:39:37", "scenario": "sunny", "work_crs": "EPSG:26914",
    "dam": {"dam_id": "DWR-2801", "name": "Swan Buffalo Detention Dam 12", "dam_height_ft": 27.5,
            "storage_top_acft": 1615.5, "top_of_dam_ft": 1071.09},
    "settings": {"cell_size_m": 30.0, "manning_n": 0.06, "scenario": "sunny"},
    "hydrograph": {"starting_wse_ft": 1071.09, "breach_bottom_ft": 1043.59, "storage_at_failure_acft": 1615.5,
                   "storage_exponent_m": 2.63, "breach_parameters": "Froehlich (2008), piping",
                   "avg_breach_width_ft": 100.0, "formation_time_hr": 0.944, "peak_cfs": 28671.4,
                   "time_to_peak_hr": 0.944, "volume_acft": 1612.59},
    "cut_off": {"reason": "duration cap"},
    "summary": {"dam_id": "DWR-2801", "final_model": "LISFLOOD-FP", "peak_breach_cfs": 28671.0,
                "breach_method": "piping", "storage_at_failure_acft": 1615.5, "flooded_acres": 11655.0,
                "max_depth_ft": 10.57, "max_velocity_fps": 113.11, "velocity_p99_fps": 6.03,
                "latest_arrival_hr": 95.98, "scenario": "sunny", "flooded_acres_ge_1ft": 2123.6},
    "warnings": ["CHECK THE INFLOW POINT: 159 m from the dam point", "Coarse grid on a wide, shallow flood"],
    "outputs": {"final": {"depth": "/tmp/elsewhere/Depth (Max).tif"}},
}

DAMIT_REPORT = {
    "dam_id": "ND00199", "engine": "LISFLOOD-FP (/opt/lisflood/lisflood)", "tier": "LISFLOOD-FP only",
    "record_cell_m": 30.0, "stop_reason": "domain-run limit",
    "stats": {"flooded_acres": 10453.9, "median_depth_ft": 0.42, "max_depth_ft": 10.89},
    "hydrograph": {"method": "piping", "label": "Piping failure, Froehlich (2008) parameters, routed",
                   "duration_hr": 1.75, "starting_wse_ft": 1071.09,
                   "rule": "sunny day, no outlet or spillway flow, starting at the top of dam"},
    "warnings": ["Stopped after 6 domain runs"],
}

MD_REPORT = """# Breach model run: DWR-3034

ND Dam Breach Runner 3.26.2 · North Dakota Dam Safety Standards Policy · finished 2026-11-01T20:10:51

## Result

| | |
|---|---|
| Scenario | sunny day |
| Final rasters from | LISFLOOD-FP |
| Peak breach outflow | 8,465 cfs (overtopping) |
| Storage at failure | 268.0 ac-ft |
| Flooded area | 53.9 ac (40.1 ac deeper than the arrival depth) |
| Maximum depth / velocity | 15.9 ft / 9.81 ft/s |
| Latest arrival | 2.5 h after breach initiation |

## Check these

- The DEM cells are 10 m, too coarse to read a dam crest from.

## Breach (ND 5.1.1)

- starting wse ft: 1580.2
- breach parameters: Froehlich (2008), overtopping
- avg breach width ft: 61.5
- formation time hr: 0.41
- peak cfs: 8465.2
- time to peak hr: 0.41
"""


def hydro(path, peak_at=0.944, peak=28671.4):
    with open(path, "w") as f:
        f.write("time_hr,discharge_cfs\n0.0,0.0\n0.5,12000.5\n%s,%s\n1.75,10.0\n" % (peak_at, peak))


def build(root):
    """Three run folders the way the Runners and Hazard leave them."""
    # DamSafety: <dam>/_breach_model/run_report.json, rasters in <dam>, Hazard in <dam>/outputs
    dam = os.path.join(root, "Dams", "DWR-2801")
    bm = os.path.join(dam, "_breach_model")
    cls = os.path.join(dam, "outputs", "Classification")
    os.makedirs(bm)
    os.makedirs(cls)
    with open(os.path.join(dam, "dam.json"), "w") as f:
        json.dump({"format": "nd_dam_suite/dam_record", "fields": {"dam_id": "DWR-2801"}}, f)
    # Hazard first (older), then the run: the run is newer than the classification
    write_gpkg(os.path.join(cls, "Habitable_Structures.gpkg"), {"Habitable_Structures": ("geom", [
        (gp_point(623000.0, 5203000.0), {"Struct_ID": 17, "Address": "4512 Main St", "City": "Absaraka",
                                         "Max_Depth_ft": 3.42, "Max_Vel_fps": 1.27, "First_Arrival_hr": 2.75,
                                         "ACER_Zone_Label": "Judgment"}),
        (gp_polygon([(624000.0, 5202000.0), (624020.0, 5202000.0), (624020.0, 5202010.0),
                     (624000.0, 5202010.0), (624000.0, 5202000.0)]),
         {"Struct_ID": 18, "Address": "12 Elm Ave", "City": "Absaraka", "Max_Depth_ft": 0.0,
          "Max_Vel_fps": 0.0, "First_Arrival_hr": None, "ACER_Zone_Label": "Dry"}),
    ])})
    with open(os.path.join(cls, "Habitable_Structures.csv"), "w") as f:      # the same table again
        f.write("WKT,Struct_ID,Address,Max_Depth_ft\n\"POINT (623000 5203000)\",17,4512 Main St,3.42\n")
    with open(os.path.join(cls, "Roads_Inundated.csv"), "w") as f:
        f.write("WKT,Road_Name,Road_Class,Max_Depth_ft,Max_Vel_fps,First_Arrival_hr,ND_Class\n"
                "\"LINESTRING (622000 5203000,622200 5203400)\",County Road 7,Local,1.9,2.2,1.5,Low\n")
    with open(os.path.join(dam, "outputs", "Hazard_Classification_Summary.txt"), "w") as f:
        f.write("DamSafety Hazard v2.25.4\nND HAZARD CLASSIFICATION: HIGH\nACER TM 11 CLASSIFICATION: HIGH\n"
                "  2 habitable structure(s) inundated\n")
    old = time.time() - 3600
    for p in ("Hazard_Classification_Summary.txt",):
        os.utime(os.path.join(dam, "outputs", p), (old, old))
    with open(os.path.join(bm, "run_report.json"), "w") as f:
        json.dump(DWR_REPORT, f)
    hydro(os.path.join(bm, "breach_hydrograph.csv"))
    with open(os.path.join(bm, "receptors_flooded.csv"), "w") as f:
        f.write("id,kind,x,y,depth_ft,velocity_fps,arrival_hr\nFarm north,structure,623500,5203500,6.5,2.0,1.2\n")
    for name in ("Depth (Max).tif", "Velocity (Max).tif", "Arrival Time.tif", "WSE (Max).tif"):
        open(os.path.join(dam, name), "wb").close()
    # DamIT: <dam>/runs/<id>/run_<time>/run_report.json, outputs/ below, classify/ in the dam folder
    ddam = os.path.join(root, "Dams", "ND00199")
    run = os.path.join(ddam, "runs", "ND00199", "run_20261111_164422")
    os.makedirs(os.path.join(run, "outputs"))
    os.makedirs(os.path.join(ddam, "classify"))
    with open(os.path.join(run, "run_report.json"), "w") as f:
        json.dump(DAMIT_REPORT, f)
    with open(os.path.join(run, "run_setup.json"), "w") as f:
        json.dump({"schema": "damit.run_setup", "created_by": {"plugin": "damit_runner", "version": "0.6.1"},
                   "dam": {"dam_id": "ND00199", "height_ft": 27.5, "storage_acft": 1615.5}}, f)
    hydro(os.path.join(run, "outputs", "breach_hydrograph.csv"), peak_at=0.95, peak=28671.0)
    write_gpkg(os.path.join(ddam, "classify", "damit_hazard_receptors.gpkg"), {"structures": ("geom", [
        (gp_point(623100.0, 5203100.0), {"rid": "structures:4", "kind": "structure", "label": "High",
                                         "depth_ft": 2.81, "vel_fps": 3.3}),
    ])})
    with open(os.path.join(ddam, "classify", "damit_hazard_report.json"), "w") as f:
        json.dump({"suggested_label": "High"}, f)
    # a copied results folder with only run_report.md and the Crest record
    cdam = os.path.join(root, "copied", "DWR-3034")
    os.makedirs(cdam)
    with open(os.path.join(cdam, "run_report.md"), "w") as f:
        f.write(MD_REPORT)
    with open(os.path.join(cdam, "DWR-3034_record.json"), "w") as f:
        json.dump({"dam": {"id": "DWR-3034", "name": "Lake Example Dam", "heightFt": 22.0, "crestFt": 1583.0}}, f)
    return dam, ddam, cdam


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="heim-runs-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.db = os.path.join(self.tmp, "local-ai", "runs.db")
        os.environ["HEIM_RUNS_DB"] = self.db
        self.addCleanup(os.environ.pop, "HEIM_RUNS_DB", None)
        self.R = load()
        self.dam, self.ddam, self.cdam = build(self.tmp)
        self.out = []
        self.R._say = lambda m="": self.out.append(str(m))

    def add_all(self):
        n = self.R.add([os.path.join(self.tmp, "Dams"), os.path.join(self.tmp, "copied")], log=lambda m: None)
        self.assertEqual(n, 3)

    def q(self, sql):
        db = sqlite3.connect(self.db)
        try:
            return db.execute(sql).fetchall()
        finally:
            db.close()


class IngestTest(Base):
    def test_finds_every_kind_of_run_folder(self):
        reps = self.R.find_reports(self.tmp)
        self.assertEqual(sorted(os.path.basename(os.path.dirname(p)) for p in reps),
                         ["DWR-3034", "_breach_model", "run_20261111_164422"])

    def test_numbers_are_the_reports_own(self):
        self.add_all()
        r = self.q("SELECT dam_name, scenario, final_model, peak_breach_cfs, time_to_peak_hr, starting_wse_ft, "
                   "avg_breach_width_ft, max_depth_ft, velocity_p99_fps, latest_arrival_hr, cell_m, manning_n, "
                   "cut_off, breach_volume_acft FROM runs WHERE dam_id='DWR-2801'")[0]
        self.assertEqual(r, ("Swan Buffalo Detention Dam 12", "sunny", "LISFLOOD-FP", 28671.0, 0.944, 1071.09,
                             100.0, 10.57, 6.03, 95.98, 30.0, 0.06, "duration cap", 1612.59))
        # anything else in the report is a fact
        self.assertEqual(self.q("SELECT value_num FROM facts WHERE key='hydrograph.storage_exponent_m'"), [(2.63,)])
        self.assertEqual(self.q("SELECT value_num FROM facts WHERE key='summary.flooded_acres_ge_1ft'"), [(2123.6,)])
        self.assertEqual(self.q("SELECT COUNT(*) FROM warnings"), [(4,)])

    def test_damit_run_peak_and_time_from_its_hydrograph(self):
        self.add_all()
        r = self.q("SELECT scenario, final_model, peak_breach_cfs, time_to_peak_hr, max_depth_ft, cell_m, "
                   "cut_off, tool, storage_at_failure_acft FROM runs WHERE dam_id='ND00199'")[0]
        self.assertEqual(r, ("sunny day", "LISFLOOD-FP", 28671.0, 0.95, 10.89, 30.0, "domain-run limit",
                             "damit_runner 0.6.1", 1615.5))
        self.assertEqual(self.q("SELECT hazard_class FROM runs WHERE dam_id='ND00199'"), [("High",)])

    def test_report_md_when_there_is_no_json(self):
        self.add_all()
        r = self.q("SELECT dam_name, scenario, peak_breach_cfs, storage_at_failure_acft, flooded_acres, "
                   "max_depth_ft, max_velocity_fps, latest_arrival_hr, starting_wse_ft, avg_breach_width_ft, "
                   "dam_height_ft, top_of_dam_ft, breach_method, source FROM runs WHERE dam_id='DWR-3034'")[0]
        self.assertEqual(r, ("Lake Example Dam", "sunny day", 8465.0, 268.0, 53.9, 15.9, 9.81, 2.5, 1580.2,
                             61.5, 22.0, 1583.0, "Froehlich (2008), overtopping", "run_report.md"))

    def test_hydrograph_points(self):
        self.add_all()
        self.assertEqual(self.q("SELECT series, MAX(flow_cfs), COUNT(*) FROM hydrograph h JOIN runs r "
                                "ON r.run_id=h.run_id WHERE r.dam_id='DWR-2801'"), [("breach", 28671.4, 4)])

    def test_structures_roads_receptors(self):
        self.add_all()
        rows = self.q("SELECT layer, kind, feature_id, address, depth_ft, velocity_fps, arrival_hr, x, y, crs "
                      "FROM features f JOIN runs r ON r.run_id=f.run_id WHERE r.dam_id='DWR-2801' "
                      "ORDER BY layer, feature_id")
        self.assertIn(("Habitable_Structures", "structure", "17", "4512 Main St, Absaraka", 3.42, 1.27, 2.75,
                       623000.0, 5203000.0, "EPSG:26914"), rows)
        # the polygon's centre comes from the envelope
        self.assertIn(("Habitable_Structures", "structure", "18", "12 Elm Ave, Absaraka", 0.0, 0.0, None,
                       624010.0, 5202005.0, "EPSG:26914"), rows)
        # the .csv copy of the same table is not read a second time
        self.assertEqual(sum(1 for r in rows if r[0] == "Habitable_Structures"), 2)
        road = [r for r in rows if r[0] == "Roads_Inundated"][0]
        self.assertEqual((road[1], road[4], road[7], road[8]), ("road", 1.9, 622100.0, 5203200.0))
        self.assertEqual(self.q("SELECT name FROM features WHERE layer='Roads_Inundated'"), [("County Road 7",)])
        rec = [r for r in rows if r[0] == "runner_receptors"][0]
        self.assertEqual((rec[1], rec[2], rec[4], rec[6]), ("structure", "Farm north", 6.5, 1.2))
        # every column is kept
        self.assertEqual(self.q("SELECT json_extract(attrs, '$.ACER_Zone_Label') FROM features "
                                "WHERE feature_id='17'"), [("Judgment",)])

    def test_damit_hazard_gpkg(self):
        self.add_all()
        self.assertEqual(self.q("SELECT layer, kind, feature_id, depth_ft, velocity_fps, x FROM features f "
                                "JOIN runs r ON r.run_id=f.run_id WHERE r.dam_id='ND00199'"),
                         [("damit_structures", "structure", "structures:4", 2.81, 3.3, 623100.0)])

    def test_hazard_older_than_run_is_marked(self):
        self.add_all()
        self.assertEqual(self.q("SELECT hazard_class, tm11_class, flooded_structures, hazard_older_than_run "
                                "FROM runs WHERE dam_id='DWR-2801'"), [("High", "High", 2, 1)])

    def test_rasters_found_by_name_when_the_report_path_moved(self):
        self.add_all()
        rows = self.q("SELECT role, path FROM rasters r JOIN runs u ON u.run_id=r.run_id WHERE u.dam_id='DWR-2801'")
        self.assertEqual({r[0] for r in rows}, {"depth", "velocity", "arrival", "wse"})
        self.assertTrue(all(os.path.dirname(p) == self.dam for _, p in rows))

    def test_reading_twice_replaces(self):
        self.add_all()
        self.add_all()
        self.assertEqual(self.q("SELECT COUNT(*) FROM runs"), [(3,)])
        self.assertEqual(self.q("SELECT COUNT(*) FROM features WHERE feature_id='17'"), [(1,)])
        self.assertEqual(self.q("SELECT COUNT(*) FROM hydrograph h JOIN runs r ON r.run_id=h.run_id "
                                "WHERE r.dam_id='DWR-2801'"), [(4,)])

    def test_nothing_in_the_run_folder_changes(self):
        def digest():
            h = {}
            for root, _d, files in os.walk(os.path.join(self.tmp, "Dams")):
                for n in files:
                    p = os.path.join(root, n)
                    with open(p, "rb") as f:
                        h[p] = (hashlib.md5(f.read()).hexdigest(), os.path.getmtime(p))  # nosec B324 - test only
            return h
        before = digest()
        self.add_all()
        self.assertEqual(before, digest())

    def test_folder_with_no_run(self):
        logs = []
        self.assertEqual(self.R.add([os.path.join(self.tmp, "Dams", "DWR-2801", "outputs")], log=logs.append), 0)
        self.assertTrue(any("no run_report" in m for m in logs))


class LookupTest(Base):
    def setUp(self):
        super().setUp()
        self.add_all()
        self.con = sqlite3.connect(self.db)
        self.addCleanup(self.con.close)

    def test_match_by_id_number_nid_name(self):
        m = self.R.match_runs
        self.assertEqual([r[1] for r in m(self.con, "DWR-2801")], ["DWR-2801"])
        self.assertEqual([r[1] for r in m(self.con, "dwr_2801")], ["DWR-2801"])
        self.assertEqual([r[1] for r in m(self.con, "2801")], ["DWR-2801"])
        self.assertEqual([r[1] for r in m(self.con, "nd00199")], ["ND00199"])
        self.assertEqual([r[1] for r in m(self.con, "swan buffalo")], ["DWR-2801"])
        self.assertEqual(m(self.con, "9999"), [])

    def test_dams_named_in_a_question(self):
        f = self.R.dams_named_in
        self.assertEqual(f("what is the depth at 4512 Main St for DWR-2801?"), ["DWR-2801"])
        self.assertEqual(f("peak flow of 2801"), ["DWR-2801"])
        self.assertEqual(f("how deep at Lake Example Dam"), ["DWR-3034"])
        self.assertEqual(f("how does the edge rule work"), [])
        self.assertEqual(f("peak flow of 28"), [])

    def test_at_a_structure_by_address(self):
        self.R.cmd_at(self.con, "2801", "Main St")
        text = "\n".join(self.out)
        self.assertIn("4512 Main St, Absaraka", text)
        self.assertIn("3.42", text)
        self.assertIn("2.75", text)
        self.assertNotIn("Elm", text)

    def test_at_by_structure_id_and_road(self):
        self.R.cmd_at(self.con, "DWR-2801", "17")
        self.assertIn("3.42", "\n".join(self.out))
        self.out.clear()
        self.R.cmd_at(self.con, "DWR-2801", "county road 7")
        self.assertIn("1.9", "\n".join(self.out))

    def test_show(self):
        self.R.cmd_show(self.con, "2801")
        text = "\n".join(self.out)
        for needle in ("28,671 cfs", "0.944 h", "1,071.09 ft", "10.57 ft", "6.03 ft/s", "ND class",
                       "older than this run"):
            self.assertIn(needle, text)

    def test_hydrograph_command(self):
        self.R.cmd_hydrograph(self.con, "2801")
        self.assertIn("peak 28,671.4 cfs at 0.944 h", "\n".join(self.out))


class SqlTest(Base):
    def test_only_one_select(self):
        c = self.R.check_sql
        self.assertEqual(c("SELECT 1;"), "SELECT 1")
        self.assertEqual(c("```sql\nWITH a AS (SELECT 1) SELECT * FROM a\n```"), "WITH a AS (SELECT 1) SELECT * FROM a")
        self.assertEqual(c("SELECT * FROM features WHERE name LIKE '%a;b%' OR name = 'delete me'"),
                         "SELECT * FROM features WHERE name LIKE '%a;b%' OR name = 'delete me'")
        for bad in ("DELETE FROM runs", "SELECT 1; DROP TABLE runs", "PRAGMA table_info(runs)",
                    "ATTACH DATABASE 'x' AS y", "UPDATE runs SET dam_id='x'", "", "INSERT INTO runs VALUES (1)",
                    "WITH x AS (SELECT 1) DELETE FROM runs"):
            with self.assertRaises(ValueError, msg=bad):
                c(bad)

    def test_connection_is_read_only_anyway(self):
        self.add_all()
        db = self.R.connect(self.db)
        with self.assertRaises(sqlite3.OperationalError):
            db.execute("DELETE FROM runs")
        db.close()

    def test_run_sql_limits_rows(self):
        self.add_all()
        cols, rows, total = self.R.run_sql("SELECT * FROM facts", self.db, limit=5)
        self.assertEqual(len(rows), 5)
        self.assertGreater(total, 5)
        self.assertIn("key", cols)

    def test_no_database_yet(self):
        with self.assertRaises(ValueError):
            self.R.run_sql("SELECT 1", os.path.join(self.tmp, "none.db"))


class AskTest(Base):
    def setUp(self):
        super().setUp()
        self.add_all()

    def chat(self, replies):
        calls = []

        def fake(messages, fmt=None, timeout=None):
            calls.append((messages, fmt))
            r = replies.pop(0)
            if isinstance(r, Exception):
                raise r
            return r
        return fake, calls

    def ask(self, q, replies):
        fake, calls = self.chat(replies)
        out = []
        ans = self.R.ask(q, db_path=self.db, out=out.append, chat=fake)
        return ans, "\n".join(out), calls

    SQL = json.dumps({"sql": "SELECT r.dam_id, r.scenario, f.address, f.depth_ft, f.velocity_fps, f.arrival_hr "
                             "FROM features f JOIN runs r ON r.run_id=f.run_id WHERE r.dam_id LIKE '%2801%' "
                             "AND f.address LIKE '%4512 Main%' LIMIT 50"})

    def test_rows_are_printed_and_phrased(self):
        ans, text, calls = self.ask("depth at 4512 Main St, DWR-2801?", [
            self.SQL, "At 4512 Main St (DWR-2801, sunny) the maximum depth is 3.42 ft, velocity 1.27 ft/s, "
                      "arriving 2.75 h after the breach starts."])
        self.assertIn("4512 Main St, Absaraka", text)
        self.assertIn("3.42", text.split("-" * 70)[0])          # the number is in the table, before the model
        self.assertNotIn("CHECK:", text)
        self.assertTrue(ans.startswith("At 4512"))
        # the model is shown the schema, the catalog and the question
        sys_msg = calls[0][0][0]["content"]
        self.assertIn("depth_ft", sys_msg)
        self.assertIn("DWR-2801 | Swan Buffalo Detention Dam 12", sys_msg)
        self.assertEqual(calls[0][1], "json")

    def test_a_number_not_in_the_rows_is_called_out(self):
        _a, text, _c = self.ask("depth at 4512 Main St, DWR-2801?", [
            self.SQL, "The depth at 4512 Main St is 3.8 ft (DWR-2801)."])
        self.assertIn("CHECK: 3.8", text)

    def test_failed_query_is_retried_with_the_error(self):
        _a, text, calls = self.ask("depth at 4512 Main St, DWR-2801?", [
            json.dumps({"sql": "SELECT depth FROM structures"}), self.SQL, "3.42 ft at 4512 Main St."])
        self.assertIn("no such table", calls[1][0][-1]["content"])
        self.assertIn("4512 Main St, Absaraka", text)

    def test_write_query_from_the_model_is_refused(self):
        _a, text, _c = self.ask("x", [json.dumps({"sql": "DELETE FROM runs"}), json.dumps({"sql": "DROP TABLE runs"})])
        self.assertIn("It failed", text)
        self.assertEqual(self.q("SELECT COUNT(*) FROM runs"), [(3,)])

    def test_no_rows(self):
        empty = json.dumps({"sql": "SELECT * FROM features WHERE address LIKE '%Nowhere%'"})
        ans, text, _c = self.ask("depth at Nowhere for DWR-2801", [empty, empty])
        self.assertEqual(ans, "")
        self.assertIn("Nothing in the run results matches", text)

    def test_model_down(self):
        ans, text, _c = self.ask("peak flow DWR-2801", [OSError("connection refused")])
        self.assertEqual(ans, "")
        self.assertIn("heim runs show", text)

    def test_rounding_is_allowed(self):
        rows = [("DWR-2801", 28671.4, 0.944)]
        self.assertEqual(self.R.unsupported_numbers("peak 28,671 cfs at 0.94 h", rows), [])
        self.assertEqual(self.R.unsupported_numbers("peak 28,700 cfs", rows), ["28,700"])
        self.assertEqual(self.R.unsupported_numbers("DWR-2801 peaks once", rows), [])


class RasterTest(Base):
    def test_point_sample(self):
        try:
            from osgeo import gdal, osr
            import numpy as np
        except ImportError:
            self.skipTest("GDAL not importable here (the QGIS CI job runs this)")
        gdal.UseExceptions()
        osr.UseExceptions()
        dam = self.dam
        srs = osr.SpatialReference()
        srs.ImportFromEPSG(26914)
        for name, val in (("Depth (Max).tif", 4.25), ("Velocity (Max).tif", 2.5), ("Arrival Time.tif", 1.75),
                          ("WSE (Max).tif", 1050.5)):
            ds = gdal.GetDriverByName("GTiff").Create(os.path.join(dam, name), 10, 10, 1, gdal.GDT_Float32)
            ds.SetGeoTransform((622000.0, 30.0, 0, 5204000.0, 0, -30.0))
            ds.SetProjection(srs.ExportToWkt())
            a = np.full((10, 10), val, dtype=np.float32)
            a[0, 0] = -9999.0
            b = ds.GetRasterBand(1)
            b.SetNoDataValue(-9999.0)
            b.WriteArray(a)
            ds = None
        self.add_all()
        con = sqlite3.connect(self.db)
        self.addCleanup(con.close)
        self.R.cmd_at(con, "2801", "622100,5203900")
        text = "\n".join(self.out)
        self.assertIn("depth     4.25 ft", text)
        self.assertIn("arrival   1.75", text)
        self.out.clear()
        self.R.cmd_at(con, "2801", "622010,5203990")
        self.assertIn("dry (no data)", "\n".join(self.out))
        self.out.clear()
        self.R.cmd_at(con, "2801", "46.98,-97.39")          # lat, lon: transformed to the raster's CRS
        self.assertIn("depth", "\n".join(self.out))


class WiringTest(unittest.TestCase):
    """heim runs ..., heim add <run folder>, and the questions heim / heim-chat route to it."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="heim-runs-wire-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        os.environ["HEIM_RUNS_DB"] = os.path.join(self.tmp, "runs.db")
        self.addCleanup(os.environ.pop, "HEIM_RUNS_DB", None)
        build(self.tmp)
        spec = importlib.util.spec_from_file_location("rag_query_runs_test", os.path.join(TOOLS, "rag_query.py"))
        self.Q = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.Q)

    def test_command_words(self):
        self.assertEqual(self.Q.command_word(["runs", "list"]), ("runs", ["list"]))
        run = os.path.join(self.tmp, "Dams")
        self.assertEqual(self.Q.command_word(["add", run]), ("runs", ["add", run]))
        doc = os.path.join(self.tmp, "notes.md")
        open(doc, "w").close()
        self.assertEqual(self.Q.command_word(["add", doc]), ("index", ["--add", doc]))

    def test_questions_naming_a_dam_go_to_the_tables(self):
        self.assertEqual(self.Q.run_question("depth at 4512 Main St for DWR-2801"), [])   # nothing read in yet
        with redirect_stdout(io.StringIO()):
            self.Q.runs_module().add([os.path.join(self.tmp, "Dams")], log=lambda m: None)
        self.assertEqual(self.Q.run_question("depth at 4512 Main St for DWR-2801"), ["DWR-2801"])
        self.assertEqual(self.Q.run_question("what was the peak discharge at 2801"), ["DWR-2801"])
        self.assertEqual(self.Q.run_question("explain the DWR-2801 watcher flags"), [])
        self.assertEqual(self.Q.run_question("depth rule for structures"), [])

    def test_chat_routes_and_ingests_a_dragged_run_folder(self):
        asked = []
        mod = self.Q.runs_module()
        mod.ask = lambda q, **k: asked.append(q) or "answer"
        lines = [os.path.join(self.tmp, "Dams"), "peak flow at DWR-2801?", ""]
        self.Q.Store.search = lambda *a, **k: self.fail("a run question must not go to the passage search")
        with redirect_stdout(io.StringIO()) as buf:
            self.Q.chat("all", 5, ask=lambda _p: lines.pop(0))
        self.assertEqual(asked, ["peak flow at DWR-2801?"])
        self.assertIn("Answered from the run results (DWR-2801)", buf.getvalue())
        self.assertEqual(sqlite3.connect(os.environ["HEIM_RUNS_DB"]).execute(
            "SELECT COUNT(*) FROM runs").fetchone(), (2,))

    def test_cli(self):
        with redirect_stdout(io.StringIO()) as buf:
            self.assertEqual(load("run_results_cli").main(["--db", os.environ["HEIM_RUNS_DB"], "add",
                                                           os.path.join(self.tmp, "Dams")]), 0)
            load("run_results_cli2").main(["--db", os.environ["HEIM_RUNS_DB"], "list"])
        self.assertIn("ND00199", buf.getvalue())


class InstallTest(unittest.TestCase):
    """Every installer puts run_results.py beside rag_query.py and gives it a command."""

    def read(self, *p):
        with open(os.path.join(os.path.dirname(HERE), *p), encoding="utf-8") as f:
            return f.read()

    def test_mac(self):
        t = self.read("setup-local-heim.sh")
        self.assertRegex(t, r"for f in (run_digest heim_watch )?run_results; do")
        self.assertIn('alias heim-runs="$PYRUN \\$HOME/local-ai/run_results.py"', t)

    def test_linux_windows(self):
        root = os.path.dirname(os.path.dirname(HERE))
        for name in ("install-local-ai-linux.sh", "install-local-ai-windows.ps1"):
            p = os.path.join(root, name)
            if not os.path.exists(p):           # DWR_Plugins carries the Mac installer only
                continue
            with open(p, encoding="utf-8") as f:
                t = f.read()
            self.assertIn("run_results.py", t, name)
            self.assertIn("heim-runs", t, name)


if __name__ == "__main__":
    unittest.main()
