"""Hazard 2.18.62: reproduce / data-folder 'local_data' - one shape, every file of every role.

Before 2.18.62 datafolder.open_config wrapped a role's list once more, so a role recorded as a
one-item list ([path], what plan_from_package handed over for every role) raised IndexError
and was skipped SILENTLY, and a role with several files (AADT: HPMS + county roads; bridges:
NBI + OSM) opened only its first path (the second taken as the 'category'). A reproduction
then ran without roads, rail, NSI, ... and still reported a classification.

Covered here (tiny synthetic GeoJSON layers, no rasters, an injected run):
  * datafolder.local_items: every recorded shape -> [(path, category)];
  * datafolder.open_config_all: every file of every role opens; a missing / unreadable /
    malformed / unknown entry is a note, never a silent skip; existing_roles;
  * a NEW-format package (packaged copies, [[path, cat], ...]) and an OLD-format record
    (2.18.59: one-item lists, bare strings, every traffic download in download order):
    plan_from_package gives [[path, cat], ...] for every role, the old 'aadt' is reduced to
    what the run read (newest HPMS, then the county roads), reproduce() loads every role
    (pipeline._apply_local_data on the injected run) and lists them as 'Loaded';
  * a broken package: a deleted file and a malformed entry are 'Not loaded' / missing rows
    and loud WARNING lines in the log;
  * the consumers, on a real Run with the data folder alone: _collect_inputs hands every
    NSI / address / USA Structures / outlines / AADT file to the analysis, _collect_facilities
    reads every bridges file in the order given (dedupe keeps NBI's record of an NBI / OSM
    pair), _used_inputs records traffic, crossings and lifelines as the run reads them (a
    packaged round trip keeps that order), ensemble.dam_settings puts an old record so.
(The record logic without QGIS: tests/test_reproduce_inputs_as_run.py.)

    QT_QPA_PLATFORM=offscreen python3 DamSafety_Hazard/tests/test_reproduce_local_data_shapes.py
"""
import csv
import json
import os
import shutil
import sys
import tempfile
import types

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from osgeo import gdal  # noqa: F401
except ImportError:
    print("SKIP: GDAL is not installed")
    sys.exit(0)
from qgis.core import QgsApplication  # noqa: E402
_app = QgsApplication.instance() or QgsApplication([], True)
_app.initQgis()

from DamSafety_Hazard import datafolder as DF  # noqa: E402
from DamSafety_Hazard import pipeline as PL  # noqa: E402
from DamSafety_Hazard import reproduce as R  # noqa: E402

TMP = tempfile.mkdtemp(prefix="repro_shapes_")


def gj(name, geom="Point", props=None, more=()):
    coords = {"Point": [-100.0, 47.0],
              "LineString": [[-100.0, 47.0], [-100.01, 47.01]]}[geom]
    p = os.path.join(TMP, "src", name + ".geojson")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    feats = [{"type": "Feature", "properties": props or {"id": 1, "AADT": 120},
              "geometry": {"type": geom, "coordinates": c}} for c in [coords] + list(more)]
    with open(p, "w") as fh:
        json.dump({"type": "FeatureCollection", "features": feats}, fh)
    return p


F = {
    "roads": gj("ND_roads_NG911_x", "LineString"),
    "rail": gj("NARN_rail_lines_x", "LineString"),
    "nsi": gj("NSI_structures_x"),
    "usa": gj("DWR_9999_FEMA_Structures"),
    "nddot": gj("NDDOT_traffic_counts_x", "LineString"),
    "county": gj("ND_county_roads_x", "LineString"),
    "hpms23": gj("HPMS_ND_2023_x", "LineString"),
    "hpms24": gj("HPMS_ND_2024_x", "LineString"),
    "nbi": gj("NBI_bridges_x"),
    "xosm": gj("OSM_crossings_x"),
    "hosp": gj("lifeline_nd_hospitals_x"),
    "nurse": gj("lifeline_nd_skilled_nursing_x"),
    "osml": gj("OSM_lifelines_x"),
}
BAD = os.path.join(TMP, "src", "not_a_layer.geojson")
with open(BAD, "w") as fh:
    fh.write("this is not GeoJSON")


def base(n):
    return os.path.basename(n).rsplit(".", 1)[0]


# ------------------------------------------------------------------ local_items ---
li = DF.local_items
p, q = F["roads"], F["rail"]
assert li(p) == ([(p, None)], [])
assert li([p]) == ([(p, None)], [])                         # the old one-item list
assert li([[p]]) == ([(p, None)], [])                       # the old double wrap
assert li([[p, None]]) == ([(p, None)], [])                 # the 2.18.62 shape
assert li([p, q]) == ([(p, None), (q, None)], [])           # two paths, not path + cat
assert li([p, "Hospital"]) == ([(p, "Hospital")], [])       # one unwrapped pair
assert li([[p, "Hospital"], q]) == ([(p, "Hospital"), (q, None)], [])
assert li((p, q)) == ([(p, None), (q, None)], [])
assert li([{"path": p, "category": "Hospital"}]) == ([(p, "Hospital")], [])
assert li([[[p, None]], [p, None]]) == ([(p, None)], [])    # nested + duplicate
assert li(p + "|layername=roads")[0] == [(p + "|layername=roads", None)]
assert li(None) == ([], []) and li("") == ([], []) and li([]) == ([], [])
items, probs = li([p, 123, ""])
assert items == [(p, None)] and len(probs) == 2, (items, probs)

# ------------------------------------------------------------ open_config_all ---
ld = {"roads": [p], "rail": q, "aadt": [[F["hpms24"], None], [F["county"], None]],
      "bridges": [F["nbi"], F["xosm"]],
      "lifelines": [[F["hosp"], "Hospital"], [F["nurse"], "Nursing home"], F["osml"]]}
got, notes = DF.open_config_all(ld)
assert notes == [], notes
assert {r: len(v) for r, v in got.items()} == {"roads": 1, "rail": 1, "aadt": 2,
                                               "bridges": 2, "lifelines": 3}, got
assert [lyr.name() for lyr, _ in got["aadt"]] == [base(F["hpms24"]), base(F["county"])]
assert [c for _, c in got["lifelines"]] == ["Hospital", "Nursing home", None]
assert all(lyr.isValid() for v in got.values() for lyr, _ in v)
# the old single-layer API still works on every shape (the double wrap is gone)
old, onotes = DF.open_config({"roads": [p], "rail": [[q, None]], "lifelines": [F["hosp"]]})
assert old["roads"].isValid() and old["rail"].isValid(), old
assert len(old["lifelines"]) == 1 and old["lifelines"][0][0].isValid()
assert onotes == [], onotes
assert sorted(DF.existing_roles({"roads": [p], "rail": [[q, None]], "nsi": [],
                                 "usa": [os.path.join(TMP, "gone.geojson")]})) == \
    ["rail", "roads"]
# loud notes: gone file, unreadable file, malformed entry, unknown role, empty role
got, notes = DF.open_config_all({"roads": [p, os.path.join(TMP, "gone.geojson")],
                                 "rail": [BAD], "nsi": [123], "bogus": [p]})
assert [len(v) for v in got.values()] == [1] and "roads" in got, got
txt = "\n".join(notes)
assert "gone.geojson" in txt and "no longer exists" in txt, txt
assert "not_a_layer" in txt and "could not be opened" in txt, txt
assert sum("rail (" in n for n in notes) == 1, notes       # one file, one note
assert "nsi (" in txt and "123" in txt and "'bogus'" in txt, txt

# ---------------------------------------------------------------- the packages ---
EXPECT = {"roads": 1, "rail": 1, "nsi": 1, "usa": 1, "aadt": 2, "bridges": 2,
          "lifelines": 3}


def record(inputs, version, marker=False):
    rec = {"format": R.RUN_SETTINGS_FORMAT, "format_version": R.FORMAT_VERSION,
           "plugin_version": version, "dam_name": "Synthetic", "settings": {},
           "scenarios": [], "layers": {}, "point_sources": [], "files": {},
           "fingerprints": {}, "inputs": inputs}
    if marker:                                    # 2.18.62: the inputs are as the run read them
        rec["inputs_format"] = R.INPUTS_FORMAT
    return rec


NEW = {"roads": [[F["roads"], None]], "rail": [[F["rail"], None]],
       "nsi": [[F["nsi"], None]], "usa": [[F["usa"], None]],
       "aadt": [[F["hpms24"], None], [F["county"], None]],
       "bridges": [[F["nbi"], None], [F["xosm"], None]],
       "lifelines": [[F["hosp"], "Hospital"], [F["nurse"], "Nursing home"],
                     [F["osml"], None]]}
OLD = {"roads": [F["roads"]], "rail": F["rail"], "nsi": [F["nsi"]], "usa": F["usa"],
       "aadt": [F["nddot"], F["county"], F["hpms23"], F["hpms24"]],     # download order
       "bridges": [F["nbi"], F["xosm"]],
       "lifelines": [[F["hosp"], "Hospital"], [F["nurse"], "Nursing home"], F["osml"]]}

SEEN = {}


def fake_run(cfg, progress=None, log=None):
    """pipeline._apply_local_data on a bare run, as run_classification does."""
    run = types.SimpleNamespace(local={}, local_all={}, local_notes=[])
    PL._apply_local_data(run, cfg)
    SEEN.clear()
    SEEN.update({r: [lyr.name() for lyr in v] for r, v in run.local_all.items()})
    SEEN["_cats"] = [c for _l, c in run.local.get("lifelines", [])]
    SEEN["_notes"] = list(getattr(run, "local_notes", []) or [])
    SEEN["_aadt"] = [lyr.name() for lyr in PL._local_layers(run, "aadt")]
    return {}


def plan_and_reproduce(pkg, tag):
    plan = R.plan_from_package(pkg, os.path.join(TMP, "plan_" + tag))
    ld = plan["cfg"]["local_data"]
    for role, v in ld.items():                                 # ONE shape
        assert isinstance(v, list) and all(isinstance(x, list) and len(x) == 2 for x in v), \
            (tag, role, v)
    logs = []
    res = R.reproduce(pkg, os.path.join(TMP, "work_" + tag), run=fake_run, log=logs.append)
    assert res["status"] != "FAILED", (tag, res.get("error"))
    return plan, res, logs


def rows_by(csv_path, kind):
    with open(csv_path, encoding="utf-8-sig") as fh:
        return [r for r in csv.DictReader(fh) if kind in json.dumps(r)]


# new format, packaged copies
pkg_new = os.path.join(TMP, "pkg_new")
os.makedirs(pkg_new)
R.package_inputs(record(NEW, "2.18.62", marker=True), pkg_new)
plan, res, logs = plan_and_reproduce(pkg_new, "new")
assert {r: len(v) for r, v in plan["cfg"]["local_data"].items()} == EXPECT, plan["cfg"]
assert all(os.path.join(pkg_new, "data") in x[0] for v in plan["cfg"]["local_data"].values()
           for x in v), "packaged copies are used"
assert res["loaded"] == EXPECT and res["not_loaded"] == [], (res["loaded"], res["not_loaded"])
assert {r: len(v) for r, v in SEEN.items() if not r.startswith("_")} == EXPECT, SEEN
assert SEEN["_aadt"] == [base(F["hpms24"]), base(F["county"])], SEEN
assert SEEN["lifelines"] == [base(F["hosp"]), base(F["nurse"]), base(F["osml"])]
assert SEEN["_cats"] == ["Hospital", "Nursing home", None], SEEN
assert not any("NOT" in n for n in SEEN["_notes"]), SEEN["_notes"]
assert not any("WARNING: package data" in m for m in logs), logs
assert rows_by(res["csv"], "Loaded"), "Loaded rows in the check"

# old format (2.18.59): the record in 02_Method_and_record, original files, old shapes
pkg_old = os.path.join(TMP, "pkg_old")
os.makedirs(os.path.join(pkg_old, R.RECORD_DIR))
with open(os.path.join(pkg_old, R.RECORD_DIR, R.RUN_SETTINGS_FILE), "w") as fh:
    json.dump(record(OLD, "2.18.59"), fh)
plan, res, logs = plan_and_reproduce(pkg_old, "old")
ld = plan["cfg"]["local_data"]
assert {r: len(v) for r, v in ld.items()} == EXPECT, ld
assert [x[0] for x in ld["aadt"]] == [F["hpms24"], F["county"]], ld["aadt"]   # as the run read
assert [x[1] for x in ld["lifelines"]] == ["Hospital", "Nursing home", None]
assert any("NDDOT_traffic_counts_x" in n and "HPMS_ND_2023_x" in n for n in plan["notes"]), \
    plan["notes"]
assert res["loaded"] == EXPECT and res["not_loaded"] == [], (res["loaded"], res["not_loaded"])
assert {r: len(v) for r, v in SEEN.items() if not r.startswith("_")} == EXPECT, SEEN
assert SEEN["_aadt"] == [base(F["hpms24"]), base(F["county"])], SEEN
assert SEEN["bridges"] == [base(F["nbi"]), base(F["xosm"])], SEEN

# broken: a deleted file, an unreadable file and a malformed entry - loud, never silent
broken = dict(OLD, rail=[BAD], nsi=[123], usa=[os.path.join(TMP, "src", "gone.geojson")])
pkg_bad = os.path.join(TMP, "pkg_bad")
os.makedirs(os.path.join(pkg_bad, R.RECORD_DIR))
with open(os.path.join(pkg_bad, R.RECORD_DIR, R.RUN_SETTINGS_FILE), "w") as fh:
    json.dump(record(broken, "2.18.59"), fh)
plan, res, logs = plan_and_reproduce(pkg_bad, "bad")
miss = "\n".join(res["missing"])
assert "gone.geojson" in miss and "'usa'" in miss, miss
assert "'nsi'" in miss, miss
assert "rail" in "\n".join(res["not_loaded"]) and "not_a_layer" in "\n".join(res["not_loaded"]), \
    res["not_loaded"]
assert "rail" not in res["loaded"] and "usa" not in res["loaded"] and "nsi" not in res["loaded"]
assert res["loaded"]["roads"] == 1 and res["loaded"]["aadt"] == 2, res["loaded"]
assert any("WARNING: package data NOT loaded" in m and "not_a_layer" in m for m in logs), logs
assert rows_by(res["csv"], "Not loaded"), "Not loaded rows in the check"
assert any("NOT loaded" in ln for ln in res["lines"]), res["lines"]

# ------------------------------------------- the consumers: every file is READ ---
from qgis.core import QgsCoordinateReferenceSystem, QgsRectangle, QgsVectorLayer  # noqa: E402
from DamSafety_Hazard import ensemble as E  # noqa: E402
from DamSafety_Hazard import facilities as FAC  # noqa: E402
from DamSafety_Hazard.extent import ExtentClip  # noqa: E402

G = {"nsi2": gj("NSI_structures_y"), "addr1": gj("NDGISHUB_address_points_x"),
     "addr2": gj("NDGISHUB_address_points_y"), "usa2": gj("USA_Structures_y"),
     "out1": gj("outlines_a"), "out2": gj("outlines_b"),
     # the same culvert in NBI and OSM (dedupe keeps the first read), and a second OSM one
     "nbi": gj("NBI_bridges_c", props={"structure_number_008": "B1",
                                       "structure_type_043b": "19", "adt_029": 900}),
     "xosm": gj("OSM_crossings_c", props={"category": "Culvert", "name": "osm"},
                more=[[-100.02, 47.02]])}
LD = {"nsi": [[F["nsi"], None], [G["nsi2"], None]],
      "address": [[G["addr1"], None], [G["addr2"], None]],
      "usa": [[F["usa"], None], [G["usa2"], None]],
      "outlines": [[G["out1"], None], [G["out2"], None]],
      "roads": [[F["roads"], None]], "aadt": [[F["hpms24"], None], [F["county"], None]],
      "bridges": [[G["nbi"], None], [G["xosm"], None]]}


def collect(local_data, tag):
    """pipeline._collect_inputs of a real Run on the data folder alone (no download)."""
    cfg = dict(PL.DEFAULTS)
    cfg.update({k: False for k in cfg if k.startswith("download_")})
    cfg.update({"output_folder": os.path.join(TMP, "out_" + tag),
                "dam_folder": os.path.join(TMP, "dam_" + tag), "osm_layer": None,
                "point_sources": [], "naip_flight_dates": False, "local_data": local_data})
    os.makedirs(cfg["output_folder"])
    run = PL.Run(cfg)
    PL._apply_local_data(run, cfg)
    clip = ExtentClip(None, 25.0, run.ctx, rect=QgsRectangle(-100.1, 46.9, -99.9, 47.1),
                      crs=QgsCoordinateReferenceSystem("EPSG:4326"))
    return PL._collect_inputs(run, clip)


def crossings(got):
    return [(r["source"], r["adt"], r.get("also_in")) for r in got["facilities"]
            if r["kind"] == FAC.KIND_CROSSING]


def src(key):
    return "data folder '{}'".format(base(G[key]))


got = collect(LD, "all")
assert [lyr.name() for f, lyr in got["points"] if f == "NSI"] == \
    [base(F["nsi"]), base(G["nsi2"])], got["points"]
assert [lyr.name() for f, lyr in got["points"] if f == "NDGISHUB"] == \
    [base(G["addr1"]), base(G["addr2"])], got["points"]
assert [(f, lyr.name()) for f, lyr in got["outlines"]] == [
    ("USA_STRUCTURES", base(F["usa"])), ("USA_STRUCTURES", base(G["usa2"])),
    ("OUTLINES", base(G["out1"])), ("OUTLINES", base(G["out2"]))], got["outlines"]
assert got["roads"].name() == base(F["roads"]), got["roads"]
assert [lyr.name() for lyr in got["aadt_layers"]] == [base(F["hpms24"]), base(F["county"])], \
    got["aadt_layers"]                                    # the county roads reach analyze_roads
assert crossings(got) == [(src("nbi"), 900, [src("xosm")]), (src("xosm"), None, None)], \
    crossings(got)                                        # both files; NBI's record kept
assert got["used"] == {r: [p for p, _c in v] for r, v in LD.items()}, got["used"]
got = collect(dict(LD, bridges=LD["bridges"][::-1]), "osm_first")   # the order is what counts
assert crossings(got)[0] == (src("xosm"), None, [src("nbi")]), crossings(got)

# _used_inputs: the downloads in the order the run reads them, whatever order they finished in
lyr = QgsVectorLayer(F["roads"], "x", "ogr")
done = {}
for key in ("xosm", "osml", "hpms23", "county", "nurse", "nbi", "hpms24", "nddot", "hosp"):
    done[{"osml": "life_osm", "hpms23": "hpms_2023", "hpms24": "hpms_2024",
          "nurse": "life_nd_skilled_nursing", "hosp": "life_nd_hospitals"}.get(key, key)] = \
        (lyr, 1, F[key])
used = PL._used_inputs({}, done, {})
assert used == {"aadt": [F["hpms24"], F["county"]], "bridges": [F["nbi"], F["xosm"]],
                "lifelines": [(F["hosp"], "Hospital"), (F["nurse"], "Nursing home"),
                              F["osml"]]}, used
pkg_rt = os.path.join(TMP, "pkg_written")
os.makedirs(pkg_rt)
R.package_inputs(R.run_settings_dict({"dam_name": "Synthetic"}, used, hash_files=False),
                 pkg_rt)
plan = R.plan_from_package(pkg_rt, os.path.join(TMP, "plan_written"))
assert {r: [os.path.basename(x[0]) for x in v] for r, v in plan["cfg"]["local_data"].items()} == \
    {r: [os.path.basename(x if isinstance(x, str) else x[0]) for x in v]
     for r, v in used.items()}, plan["cfg"]["local_data"]
assert not any("handed over" in n for n in plan["notes"]), plan["notes"]

# ensemble.dam_settings: the dam's old record (no marker; downloads in finish order)
dam = os.path.join(TMP, "dam_ensemble")
os.makedirs(E._qa(dam))
old = dict(OLD, bridges=[F["xosm"], F["nbi"]],
           lifelines=[F["osml"], [F["nurse"], "Nursing home"], [F["hosp"], "Hospital"]])
with open(os.path.join(E._qa(dam), R.RUN_SETTINGS_FILE), "w") as fh:
    json.dump(record(old, "2.18.59"), fh)
kw, info = E.dam_settings(dam)
ld = kw["local_data"]
assert ld["aadt"] == [[F["hpms24"], None], [F["county"], None]], ld["aadt"]
assert ld["bridges"] == [[F["nbi"], None], [F["xosm"], None]], ld["bridges"]
assert ld["lifelines"] == [[F["hosp"], "Hospital"], [F["nurse"], "Nursing home"],
                           [F["osml"], None]], ld["lifelines"]
txt = "\n".join(info["notes"])
assert "recorded but not read by the run" in txt and "NDDOT_traffic_counts_x" in txt, txt
assert "'bridges': handed over" in txt and "'lifelines': handed over" in txt, txt

shutil.rmtree(TMP, ignore_errors=True)
print("OK test_reproduce_local_data_shapes")
