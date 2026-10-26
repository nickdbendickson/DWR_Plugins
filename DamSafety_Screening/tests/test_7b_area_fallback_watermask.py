"""Tool 7b (0.17.46): (1) when the tool-3 stage-storage curve is too shallow (or missing) to give
the surface area at the top of dam, the DWR inventory's maximum-pool area (max_pool_area, acres)
is sent as the Runner's AREA_AC, and where the area came from is recorded (rn_area_src, the
dam.json note, the log, the run manifest: surface_area_source); (2) an optional water-body mask
for the Runner's report (rundam WATER_MASK), off by default.

Run: python3 tests/test_7b_area_fallback_watermask.py
"""
import csv, json, os, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(HERE)
PARENT = os.path.dirname(PLUGIN)
sys.path.insert(0, os.path.join(HERE, "qgis_mock"))
sys.path.insert(0, PARENT)
from qgis.core import (QgsGeometry, QgsPointXY, QgsProcessingContext, QgsProcessingFeedback,  # noqa: E402
                       QgsProcessingException, make_layer)
from qgis import processing  # noqa: E402
PKG = os.path.basename(PLUGIN)
import importlib  # noqa: E402
ho = importlib.import_module(PKG + ".core.handoff")
wf = importlib.import_module(PKG + ".core.workflow")
inv = importlib.import_module(PKG + ".core.inventory")
dri = importlib.import_module(PKG + ".core.damrecord_io")
RunnerHandoff = importlib.import_module(PKG + ".algorithms.runner_handoff").RunnerHandoff

W = tempfile.mkdtemp(prefix="w7b_area_")
AC = ho.ACRE_M2
FT = ho.FT


def curve(name, depth_m, area_m2=50000.0, base=100.0):
    """A prism-ish curve: storage = area * depth over depth_m metres (area constant)."""
    path = os.path.join(W, name)
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["elev_m", "storage_m3"])
        n = 20
        for i in range(n + 1):
            d = depth_m * i / n
            w.writerow([base + d, area_m2 * d])
    return path


H = 27 * FT                                    # a 27 ft dam
CREST = 100.0 + H
deep = curve("curve_deep.csv", H)              # spans the whole height
shallow = curve("curve_shallow.csv", 4 * FT)   # 4 ft of 27 ft: too shallow

# ---- 1. surface_area_choice
# normal curve: the curve's area, unchanged, whatever the inventory says
a0, _n0 = ho.area_for_runner(deep, CREST, H)
a, src, note = ho.surface_area_choice(deep, CREST, H, {"inv_maxa_m2": 999 * AC})
assert src == ho.AREA_SRC_CURVE == "stage_storage" and a == a0 and abs(a - 50000.0) < 1, (a, src)
assert note == ""
# shallow curve + inventory value: the inventory's area
a, src, note = ho.surface_area_choice(shallow, CREST, H, {"inv_maxa_m2": 12.5 * AC})
assert src == ho.AREA_SRC_INVENTORY == "inventory_max_pool_area" and abs(a - 12.5 * AC) < 1e-6, (a, src)
assert "12.5 ac from the inventory's maximum-pool area (inv_maxa_m2)" in note and "spans 4.0 ft" in note, note
# the DWR export's own fields kept on the layer are acres (gpkg and shapefile names, any case)
for fld in ("max_pool_area", "MAX_POOL_A"):
    a, src, _ = ho.surface_area_choice(shallow, CREST, H, {fld: 7})
    assert src == ho.AREA_SRC_INVENTORY and abs(a - 7 * AC) < 1e-6, (fld, a)
# tool 1's field wins over the raw one
assert ho.inventory_max_area_m2({"inv_maxa_m2": 100.0, "max_pool_area": 9})[0] == 100.0
# missing curve file: also falls back
a, src, note = ho.surface_area_choice(os.path.join(W, "nope.csv"), CREST, H, {"inv_maxa_m2": 3 * AC})
assert src == ho.AREA_SRC_INVENTORY and "no stage-storage curve" in note, note
# shallow curve, no inventory value (blank, 0, absent): None with a clear note
for attrs in ({}, {"inv_maxa_m2": None}, {"inv_maxa_m2": 0}, {"max_pool_area": 0.0}):
    a, src, note = ho.surface_area_choice(shallow, CREST, H, attrs)
    assert a is None and src == ho.AREA_SRC_NONE, (attrs, a, src)
    assert note.startswith("no surface area sent: ") and ho.NO_INV_MAXA in note, note
    assert "depth squared" in note and note.count("depth squared") == 1, note
# no curve folder at all: no area, as before
assert ho.surface_area_choice(None, CREST, H, {"inv_maxa_m2": 5 * AC}, curves_given=False) == (None, "none", "")
assert ho.has_inventory_area_field(["dam_id", "INV_MAXA_M2"]) and not ho.has_inventory_area_field(["dam_id"])
print("OK (surface_area_choice: normal curve unchanged; shallow -> inventory; none -> None + note)")

# ---- 2. rundam_params note and AREA_AC; dam.json source note
attrs = {"dam_id": "DWR-1", "height_m": H, "td_vw_m3": 1e6}
p, notes = ho.rundam_params(attrs, (1, 2), "EPSG:5070", "d.tif", W, area_top_m2=12.5 * AC,
                            area_source=ho.AREA_SRC_INVENTORY)
assert p["AREA_AC"] == 12.5 and any("from the inventory's maximum-pool area" in n for n in notes), notes
assert dri.surface_area_source(notes) == "inventory_max_pool_area"
assert dri.source_notes(attrs, notes)["surface_area_ac"].startswith("surface_area_source = inventory_max_pool_area")
p, notes = ho.rundam_params(attrs, (1, 2), "EPSG:5070", "d.tif", W, area_top_m2=50000.0)
assert dri.surface_area_source(notes) == "stage_storage"
assert dri.source_notes(attrs, notes)["surface_area_ac"].startswith("surface_area_source = stage_storage")
p, notes = ho.rundam_params(attrs, (1, 2), "EPSG:5070", "d.tif", W)
assert "AREA_AC" not in p and dri.surface_area_source(notes) is None
assert "surface_area_ac" not in dri.source_notes(attrs, notes)
_a, _s, anote = ho.surface_area_choice(shallow, CREST, H, {})
miss = dri.missing_notes({}, attrs, notes + [anote])
assert ho.NO_INV_MAXA in miss["surface_area_ac"], miss
print("OK (rundam_params note, dam.json surface_area_source note)")

# ---- 3. tool 1 reads max_pool_area (acres -> m2); 0 = not recorded
names = ["structure_", "max_pool_area", "normal_area"]
fld = inv.match_fields(names, inv.DWR_ALIASES)
assert fld["MA"] == "max_pool_area", fld
assert inv.match_fields(["max_pool_a"], inv.DWR_ALIASES)["MA"] == "max_pool_a"
row = {"structure_": "42", "max_pool_area": 10.0, "normal_area": 4.0}
s = inv.standardize(lambda n: row[n], fld)
assert abs(s["ma"] - 10 * AC) < 1e-6, s
row["max_pool_area"] = 0
assert inv.standardize(lambda n: row[n], fld)["ma"] is None
assert inv.standardize(lambda n: row[n], {"ID": "structure_"})["ma"] is None     # no field: None
assert "MA" not in inv.NID_ALIASES
pd = importlib.import_module(PKG + ".algorithms.prepare_dams")
assert pd.MAXAREA_FIELDS == [("inv_maxa_m2", pd.FT_DOUBLE)] and len(pd.OUT_FIELDS) == 12
print("OK (tool 1: max_pool_area -> inv_maxa_m2)")

# ---- 4. water-mask mapping
mfile = os.path.join(W, "water.gpkg")
open(mfile, "w").close()
assert ho.mask_path(None) is None and ho.mask_path("") is None
assert ho.mask_path(mfile) == mfile
assert ho.mask_path(mfile + "|layername=lakes|subset=x") == mfile + "|layername=lakes"
assert ho.mask_path(None, mfile + "|layername=w", "ogr") == mfile + "|layername=w"
assert ho.mask_path(None, mfile, "memory") is None                # a temporary layer
assert ho.mask_path(os.path.join(W, "gone.gpkg")) is None
assert ho.water_mask_params(0) == ({}, [], None)                  # default: nothing sent
assert ho.water_mask_params(ho.WATER_MASK_OFF, mfile, True)[0] == {"WATER_MASK": "off"}
assert ho.water_mask_params(ho.WATER_MASK_LAYER, mfile)[0] == {"WATER_MASK": mfile}
assert ho.water_mask_params(0, mfile, True)[0] == {"WATER_MASK": mfile}    # a picked layer is the choice
p_, n_, err = ho.water_mask_params(ho.WATER_MASK_LAYER, None)
assert p_ == {} and err and "missing" in err
assert "temporary" in ho.water_mask_params(0, None, True)[2]
assert "WATER_MASK" in ho.RUNDAM_KEYS and len(ho.WATER_MASK_LABELS) == 3
for lab in ho.WATER_MASK_LABELS + [ho.WATER_MASK_OLD_RUNNER, ho.NO_INV_MAXA, ho.OLD_TOOL1]:
    assert "..." not in lab, lab
print("OK (water-mask mapping)")

# ---- 5. workflow defaults and window setting
S0 = {"runner_dem": "/dem.tif", "dams_folder": "/runs/dams"}
assert wf.DEFAULT_SETTINGS["runner_water_mask"] is False and "runner_water_mask" in wf.WINDOW_KEYS
d = wf.defaults("7b", W, dict(S0, waterbodies=mfile))
assert "WATER_MASK" not in d and "WATER_MASK_MODE" not in d, d          # off by default
d = wf.defaults("7b", W, dict(S0, runner_water_mask=True, waterbodies=mfile))
assert d["WATER_MASK_MODE"] == ho.WATER_MASK_LAYER and d["WATER_MASK"] == mfile, d
d = wf.defaults("7b", W, dict(S0, runner_water_mask=True))
assert "WATER_MASK" not in d                                              # no layer: nothing


class FakeSF:
    def __init__(self): self.values = {}
    def set(self, plugin, kind, k, v):
        if v is None: self.values.pop((plugin, kind, k), None)
        else: self.values[(plugin, kind, k)] = v
    def window(self, plugin):
        return {k: v for (p_, kind, k), v in self.values.items() if p_ == plugin and kind == "window"}


back = wf.window_settings_from(wf.window_settings_to(FakeSF(), dict(S0, runner_water_mask=True)))
assert back["runner_water_mask"] is True, back
print("OK (workflow default off, persisted)")

# ---- 6. the tool, against a fake rundam
sent = []
HAS_MASK = [True]


class _Def:
    def __init__(self, o=None): self._o = o
    def options(self): return self._o


class FakeRunDam:
    @classmethod
    def createInstance(cls): return cls()
    def parameterDefinition(self, name):
        if name == "METHOD":
            return _Def(["ND method", "My own hydrograph"])
        return _Def() if name == "WATER_MASK" and HAS_MASK[0] else None
    def run_mock(self, p, context, feedback):
        for k in p:
            assert k in ho.RUNDAM_KEYS, k
        sent.append(dict(p))
        folder = os.path.join(p["DAMS_FOLDER"], ho.safe_name(p["DAM_ID"]))
        os.makedirs(folder, exist_ok=True)
        return {"FOLDER": folder}


class FakeHazard:
    @classmethod
    def createInstance(cls): return cls()
    def run_mock(self, p, context, feedback):
        js = os.path.join(p["DAM_FOLDER"], ho.RUNNER_JSON)
        json.dump({"hazard_class": "High", "borderline_raised": False, "data_gaps": [],
                   "lethality_label": "test"}, open(js, "w"))
        return {"CLASS": "High", "JSON": js}


processing.REGISTRY.update({ho.RUNNER_ALG: FakeRunDam, ho.HAZARD_ALG: FakeHazard})
pt = lambda x, y: QgsGeometry.fromPointXY(QgsPointXY(x, y))
CURVES = os.path.join(W, "curves")
os.makedirs(CURVES)
curve(os.path.join("curves", "curve_DWR-1.csv"), H)          # normal
curve(os.path.join("curves", "curve_DWR-2.csv"), 4 * FT)     # shallow, inventory area
curve(os.path.join("curves", "curve_DWR-3.csv"), 4 * FT)     # shallow, no inventory area
FLDS = [("dam_id", "string"), ("height_m", "double"), ("td_vw_m3", "double"), ("hz_class", "string"),
        ("est_crest_m", "double"), ("inv_maxa_m2", "double")]
rows = [{"dam_id": "DWR-1", "height_m": H, "td_vw_m3": 1e6, "hz_class": "High", "est_crest_m": CREST,
         "inv_maxa_m2": 99 * AC},
        {"dam_id": "DWR-2", "height_m": H, "td_vw_m3": 1e6, "hz_class": "High", "est_crest_m": CREST,
         "inv_maxa_m2": 12.5 * AC},
        {"dam_id": "DWR-3", "height_m": H, "td_vw_m3": 1e6, "hz_class": "High", "est_crest_m": CREST,
         "inv_maxa_m2": None}]
dams = make_layer(FLDS, [(pt(i, i), r) for i, r in enumerate(rows)], "point", "dams")
bpts = make_layer([("dam_id", "string")], [(pt(i, i), {"dam_id": r["dam_id"]}) for i, r in enumerate(rows)],
                  "point", "bpts")
ctx = QgsProcessingContext()
DF = os.path.join(W, "Dams")
base = dict(DAMS=ctx.add(dams), BPTS=ctx.add(bpts), DEM="nd_dem.vrt", DAMS_FOLDER=DF,
            LETHALITY=1, WHICH=ho.SELECT_ALL, CURVES=CURVES, OUT="memory:")
os.environ.pop(ho.ANUGA_SETTING, None)
fb = QgsProcessingFeedback()
alg = RunnerHandoff()
out = alg.run_mock(dict(base), ctx, fb)
by = {p["DAM_ID"]: p for p in sent}
assert set(by) == {"DWR-1", "DWR-2", "DWR-3"}, list(by)
assert abs(by["DWR-1"]["AREA_AC"] - round(50000.0 / AC, 3)) < 1e-3, by["DWR-1"]   # curve, unchanged
assert by["DWR-2"]["AREA_AC"] == 12.5, by["DWR-2"]                                 # inventory
assert "AREA_AC" not in by["DWR-3"]                                                # none
assert not any("WATER_MASK" in p for p in sent)                                    # default: no mask
log = "\n".join(fb.log)
assert "DWR-2: surface area 12.5 ac from the inventory's maximum-pool area" in log, log
assert "DWR-3: no surface area sent" in log and ho.NO_INV_MAXA in log
assert "surface area source (surface_area_source): inventory_max_pool_area 1, none 1, stage_storage 1" in log, log
assert alg.manifest_extra == {"surface_area_source": {"DWR-1": "stage_storage",
                                                      "DWR-2": "inventory_max_pool_area",
                                                      "DWR-3": "none"},
                              "water_mask": "runner default"}, alg.manifest_extra
lay = ctx.getMapLayer(out["OUT"]) if hasattr(ctx, "getMapLayer") else None
if lay is not None:
    got = {f["dam_id"]: f["rn_area_src"] for f in lay.getFeatures()}
    assert got == alg.manifest_extra["surface_area_source"], got
for d_, want in (("DWR-1", "stage_storage"), ("DWR-2", "inventory_max_pool_area")):
    js = json.load(open(os.path.join(DF, d_, "dam.json")))
    blob = json.dumps(js)
    assert "surface_area_source = " + want in blob, (d_, blob[:500])
print("OK (tool 7b: curve / inventory / none, log, manifest extra, dam.json)")

# an old tool-1 layer (no inv_maxa_m2): warned once
sent.clear()
old = make_layer(FLDS[:5], [(pt(0, 0), {k: v for k, v in rows[1].items() if k != "inv_maxa_m2"})], "point", "d")
fb = QgsProcessingFeedback()
RunnerHandoff().run_mock(dict(base, DAMS=ctx.add(old)), ctx, fb)
assert "AREA_AC" not in sent[0] and any(ho.OLD_TOOL1 in m for m in fb.log), fb.log

# water mask: off / a layer / an old Runner / a layer with no file
one = dict(base, DAMS=ctx.add(make_layer(FLDS, [(pt(0, 0), rows[0])], "point", "d1")))
sent.clear()
alg = RunnerHandoff()
alg.run_mock(dict(one, WATER_MASK_MODE=ho.WATER_MASK_OFF, WATER_MASK=mfile), ctx, QgsProcessingFeedback())
assert sent[-1]["WATER_MASK"] == "off" and alg.manifest_extra["water_mask"] == "off"
alg = RunnerHandoff()
fb = QgsProcessingFeedback()
alg.run_mock(dict(one, WATER_MASK_MODE=ho.WATER_MASK_LAYER, WATER_MASK=mfile + "|layername=lakes"), ctx, fb)
assert sent[-1]["WATER_MASK"] == mfile + "|layername=lakes" and alg.manifest_extra["water_mask"] == sent[-1]["WATER_MASK"]
assert any("water-body mask for the Runner's report" in m for m in fb.log)
HAS_MASK[0] = False
fb = QgsProcessingFeedback()
RunnerHandoff().run_mock(dict(one, WATER_MASK_MODE=ho.WATER_MASK_LAYER, WATER_MASK=mfile), ctx, fb)
assert "WATER_MASK" not in sent[-1] and any(ho.WATER_MASK_OLD_RUNNER in m for m in fb.log), fb.log
HAS_MASK[0] = True
n0 = len(sent)
try:
    RunnerHandoff().run_mock(dict(one, WATER_MASK_MODE=ho.WATER_MASK_LAYER), ctx, QgsProcessingFeedback())
    raise AssertionError("a mask asked for with no layer must stop the tool")
except QgsProcessingException as e:
    assert "missing" in str(e) and len(sent) == n0
print("OK (tool 7b: water mask off / layer / old Runner / missing)")
print("ALL OK")
