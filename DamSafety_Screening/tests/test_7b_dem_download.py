"""Tool 7b (0.17.55): 'Runner DEM source' - the tool downloads the Runner's DEM for the chosen dams
when none is loaded (Nick, Mac, QGIS 3.34.6: the 'DEM for the runner' box was empty and 7b had no
way to get one).

  1. a DEM given (default source)  -> sent unchanged, nothing downloaded, no DEM_Z
  2. no DEM + 'Download USGS 3DEP 10 m' -> the box of the CHOSEN dams' breach points + buffer,
     one EPSG:5070 GeoTIFF in <Dams folder>/_runner_dem, sent to every dam with DEM_Z = Meters;
     reused on the next run; request count / size logged; validate-only downloads nothing
  3. no DEM, no download            -> a clear error naming Download DEM and the option
  4. over 'Largest DEM download for one run (GB)' -> refused before anything is fetched (10 m and
     native 1 m); the 1 m path uses lidar_tnm's native tiles and its VRT
  5. cancel stops the 3DEP download at the next request
No network, no GDAL (tests/qgis_mock; the downloads are replaced by fakes).
Run: python3 tests/test_7b_dem_download.py
"""
import json, os, sys, tempfile

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
rdem = importlib.import_module(PKG + ".core.runner_dem")
dd = importlib.import_module(PKG + ".core.dem_download")
lt = importlib.import_module(PKG + ".core.lidar_tnm")
terrain = importlib.import_module(PKG + ".core.terrain")
RunnerHandoff = importlib.import_module(PKG + ".algorithms.runner_handoff").RunnerHandoff

W = tempfile.mkdtemp(prefix="w7b_dem_")
MI = 1609.344

# ---- 1. pure helpers
P1, P2, P3 = (-150000.0, 2800000.0), (-140000.0, 2805000.0), (-50000.0, 2900000.0)
box = rdem.dams_box_5070([P1, P2], 10)
assert box == (-166100.0, 2783900.0, -123900.0, 2821100.0), box
assert rdem.dams_box_5070([P1], 0)[2] - rdem.dams_box_5070([P1], 0)[0] == 2000.0   # never zero size
try:
    rdem.dams_box_5070([], 10)
    raise AssertionError("no points must stop")
except rdem.RunnerDemError as e:
    assert "breach point" in str(e)
assert rdem.box_key(box) == rdem.box_key(tuple(box)) and len(rdem.box_key(box)) == 8
assert rdem.box_key(box) != rdem.box_key(rdem.dams_box_5070([P1, P2, P3], 10))
pl = rdem.plan_10m(box)
assert pl["requests"] == 16 and pl["side_km"] == 12.0, pl          # 42.2 x 37.2 km in 12 km pieces, 4 x 4
assert pl["est_bytes"] == 4220 * 3720 * 4, pl
assert "Download DEM (dam, county, or state)" in rdem.NO_DEM and rdem.SOURCE_LABELS[1] in rdem.NO_DEM
for lab in rdem.SOURCE_LABELS + [rdem.NO_DEM]:
    assert "..." not in lab, lab
assert rdem.SOURCE_LABELS == ["Use the DEM above",
                              "Download USGS 3DEP 10 m covering the chosen dams",
                              "Download USGS 3DEP 1 m lidar tiles (native) covering the chosen dams"]
assert "DEM_Z" in ho.RUNDAM_KEYS and ho.DEM_Z_METERS == 3
print("OK (box of the dams + buffer, key, 10 m plan, labels)")

# ---- fakes for the downloads (no network, no GDAL)
calls = {"download": [], "write": [], "plan": [], "fetch": 0, "vrt": []}


def fake_download(extent, cell, out_dir, stem, feedback=None, scope=0):
    calls["download"].append((tuple(extent), cell))
    v = os.path.join(out_dir, stem + ".vrt")
    open(v, "w").close()
    return v


def fake_write(src, dest, cell, info, feedback=None):
    calls["write"].append((dest, cell, info.get("source", "")))
    open(dest, "w").write("tif")
    side = os.path.splitext(dest)[0] + "_dem.txt"
    open(side, "w").write("units: metres\ncrs: EPSG:5070\n")
    return dest, side


REAL = {k: getattr(dd, k) for k in ("download", "write_local_dem", "band_unit", "set_metres", "data_problem")}
dd.download, dd.write_local_dem = fake_download, fake_write
dd.band_unit, dd.set_metres, dd.data_problem = (lambda p: "m"), (lambda p: True), (lambda p: "")
lt.box_5070_to_ll = lambda b: (-97.5, 46.9, -97.0, 47.2)
PLAN = {"need": 3 * 300 * 1024 ** 2}


def fake_plan(bbox_ll, folder, http, feedback=None, keep_older=False, items=None):
    calls["plan"].append((tuple(bbox_ll), folder))
    tiles = [{"tile": "USGS_1M_14_x{}y52_ND_Test.tif".format(i), "bytes": PLAN["need"] // 3, "project": "ND_Test",
              "present": False, "partial_bytes": 0, "publication_date": "2020-01-01"} for i in range(3)]
    return {"bbox_ll": list(bbox_ll), "folder": folder, "tiles": tiles, "dropped": [], "notes": [],
            "total_bytes": PLAN["need"], "need_bytes": PLAN["need"], "free_bytes": 10 ** 15,
            "projects": ["ND_Test"]}


def fake_fetch(plan, http, feedback=None, **kw):
    calls["fetch"] += 1
    ents = []
    for t in plan["tiles"]:
        p = os.path.join(plan["folder"], t["tile"])
        os.makedirs(plan["folder"], exist_ok=True)
        open(p, "w").close()
        ents.append(dict(t, path=p))
    return ents, []


def fake_vrt(entries, vrt_path, fallback="", bounds_ll=None, meta=None, feedback=None):
    calls["vrt"].append((vrt_path, fallback, tuple(bounds_ll)))
    open(vrt_path, "w").close()
    js = os.path.splitext(vrt_path)[0] + ".json"
    json.dump({"crs": "EPSG:26914", "units": "metres", "tiles": [{"path": e["path"]} for e in entries],
               "missing_tiles": (meta or {}).get("missing_tiles", [])}, open(js, "w"))
    return vrt_path, js


lt.make_plan, lt.fetch, lt.build_vrt = fake_plan, fake_fetch, fake_vrt

# ---- the tool, against a fake rundam
sent = []
ZOPTS = ["Detect from the raster", "Feet", "US survey feet", "Meters", "Feet (the raster's unit tag is wrong)"]


class _Def:
    def __init__(self, o=None): self._o = o
    def options(self): return self._o


class FakeRunDam:
    @classmethod
    def createInstance(cls): return cls()
    def parameterDefinition(self, name):
        if name == "METHOD":
            return _Def(["ND method", "My own hydrograph"])
        if name == "DEM_Z":
            return _Def(ZOPTS)
        return _Def() if name == "WATER_MASK" else None
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
FLDS = [("dam_id", "string"), ("height_m", "double"), ("td_vw_m3", "double"), ("hz_class", "string"),
        ("inv_maxa_m2", "double")]
rows = [{"dam_id": "DWR-1", "height_m": 8.0, "td_vw_m3": 1e6, "hz_class": "High", "inv_maxa_m2": 1e5},
        {"dam_id": "DWR-2", "height_m": 8.0, "td_vw_m3": 1e6, "hz_class": "High", "inv_maxa_m2": 1e5},
        {"dam_id": "DWR-3", "height_m": 8.0, "td_vw_m3": 1e6, "hz_class": "Low", "inv_maxa_m2": 1e5}]
XY = [P1, P2, P3]
dams = make_layer(FLDS, [(pt(*XY[i]), r) for i, r in enumerate(rows)], "point", "dams")
bpts = make_layer([("dam_id", "string")], [(pt(*XY[i]), {"dam_id": r["dam_id"]}) for i, r in enumerate(rows)],
                  "point", "bpts")
ctx = QgsProcessingContext()
DF = os.path.join(W, "Dams")
TILES = os.path.join(W, "tiles")
base = dict(DAMS=ctx.add(dams), BPTS=ctx.add(bpts), DAMS_FOLDER=DF, LETHALITY=1,
            WHICH=ho.SELECT_HIGH, OUT="memory:", DEM_TILE_FOLDER=TILES)
os.environ.pop(ho.ANUGA_SETTING, None)


def run(**kw):
    fb = QgsProcessingFeedback()
    alg = RunnerHandoff()
    alg.run_mock(dict(base, **kw), ctx, fb)
    return alg, "\n".join(fb.log)


# the tool's parameters
alg = RunnerHandoff()
alg.initAlgorithm()
d = alg.parameterDefinition("DEM")
assert d.optional and "Runner DEM source" in d.description
d = alg.parameterDefinition("DEM_SOURCE")
assert d.options == rdem.SOURCE_LABELS and d.defaultValue == 0 and d.description == "Runner DEM source\u2026"
assert alg.parameterDefinition("DEM_BUFFER_MI").defaultValue == 10.0
assert alg.parameterDefinition("DEM_MAX_GB").defaultValue == 10.0
assert alg.parameterDefinition("DEM_FOLDER").optional and alg.parameterDefinition("DEM_TILE_FOLDER").optional
assert "RUNNER DEM SOURCE (0.17.55)" in alg.shortHelpString()
print("OK (7b parameters: DEM optional, DEM_SOURCE / DEM_BUFFER_MI / DEM_FOLDER / DEM_MAX_GB / DEM_TILE_FOLDER)")

# 1. a DEM given: unchanged
sent.clear()
alg, log = run(DEM="nd_dem.vrt")
assert [p["DAM_ID"] for p in sent] == ["DWR-1", "DWR-2"], sent
assert all(p["DEM"] == "nd_dem.vrt" and "DEM_Z" not in p for p in sent), sent
assert not calls["download"] and not calls["plan"] and "runner_dem" not in alg.manifest_extra
assert "Runner DEM" not in log
print("OK (a DEM given: sent unchanged, nothing downloaded)")

# 2. no DEM + 10 m download
sent.clear()
alg, log = run(DEM_SOURCE=rdem.SOURCE_10M)
assert calls["download"] == [(box, 10)], calls["download"]          # DWR-3 (Low) is not in the box
dest = os.path.join(DF, rdem.FOLDER_NAME, "7b_dem_2dams_{}_10m_5070.tif".format(rdem.box_key(box)))
assert calls["write"] == [(dest, 10, dd.product_info("usgs", 10).get("source", ""))], calls["write"]
assert os.path.isfile(dest)
assert [p["DAM_ID"] for p in sent] == ["DWR-1", "DWR-2"]
assert all(p["DEM"] == dest and p["DEM_Z"] == 3 for p in sent), sent
assert "16 ImageServer request(s)" in log and "about " in log and "EPSG:5070" in log, log
assert "DEM elevation units: Meters (DEM_Z 3)" in log, log
assert alg.manifest_extra["runner_dem"] == {"source": rdem.SOURCE_LABELS[1], "path": dest, "units": "Meters"}
assert "DEM: {} ({})".format(dest, rdem.SOURCE_LABELS[1]) in log, log
# ... the same dams again: reused, not downloaded
sent.clear()
alg, log = run(DEM_SOURCE=rdem.SOURCE_10M)
assert len(calls["download"]) == 1 and "reusing" in log and all(p["DEM"] == dest for p in sent)
# ... a DEM above is ignored (warned) when a download is chosen; a chosen DEM folder is used
sent.clear()
DEMF = os.path.join(W, "my_dem")
alg, log = run(DEM="nd_dem.vrt", DEM_SOURCE=rdem.SOURCE_10M, DEM_FOLDER=DEMF, DEM_BUFFER_MI=5)
assert "the DEM picked above is not used" in log
assert len(calls["download"]) == 2 and calls["download"][-1][0] == rdem.dams_box_5070([P1, P2], 5)
assert all(os.path.dirname(p["DEM"]) == DEMF and p["DEM_Z"] == 3 for p in sent)
# ... validate only: planned, not downloaded
sent.clear()
alg, log = run(DEM_SOURCE=rdem.SOURCE_10M, DEM_BUFFER_MI=7, DRY_RUN=True)
assert len(calls["download"]) == 2 and "validate only - not downloaded" in log and not sent
print("OK (no DEM + 10 m: chosen dams' box, EPSG:5070 file, Meters sent, reused, folder, dry run)")

# 3. no DEM, no download: a clear error, nothing sent
sent.clear()
try:
    run()
    raise AssertionError("no DEM and no download must stop")
except QgsProcessingException as e:
    msg = str(e)
    assert "No DEM for the Runner" in msg and "Download DEM" in msg and rdem.SOURCE_LABELS[1] in msg, msg
assert not sent
print("OK (no DEM, no download: clear error)")

# 4. size caps: refused before anything is fetched
n_dl = len(calls["download"])
try:
    run(DEM_SOURCE=rdem.SOURCE_10M, DEM_MAX_GB=0.1, DEM_BUFFER_MI=20)   # not on disk yet
    raise AssertionError("10 m over the cap must stop")
except QgsProcessingException as e:
    assert "over the" in str(e) and "Largest DEM download for one run (GB)" in str(e), str(e)
assert len(calls["download"]) == n_dl and not sent
PLAN["need"] = 40 * 1024 ** 3
try:
    run(DEM_SOURCE=rdem.SOURCE_1M)
    raise AssertionError("1 m over the cap must stop")
except QgsProcessingException as e:
    s = str(e)
    assert "Not downloading the 1 m lidar" in s and "over the" in s and "'Largest download'" not in s, s
    assert "Largest DEM download for one run (GB)" in s and rdem.SOURCE_LABELS[1] in s, s
assert calls["fetch"] == 0 and not sent and calls["plan"][-1][1] == TILES
print("OK (size cap: 10 m and 1 m refused before any download)")

# 1 m within the cap: native tiles, VRT in the DEM folder, Meters sent
PLAN["need"] = 3 * 300 * 1024 ** 2
sent.clear()
alg, log = run(DEM_SOURCE=rdem.SOURCE_1M)
vrt = os.path.join(DF, rdem.FOLDER_NAME, "7b_dem_2dams_{}_1m_lidar.vrt".format(rdem.box_key(box)))
assert calls["fetch"] == 1 and calls["vrt"][-1][0] == vrt, calls["vrt"]
assert calls["vrt"][-1][1] == os.path.join(DF, rdem.FOLDER_NAME, "7b_dem_2dams_{}_10m_5070.tif".format(
    rdem.box_key(box)))                                  # the 10 m file from above goes underneath
assert all(p["DEM"] == vrt and p["DEM_Z"] == 3 for p in sent), sent
assert "3 native 1 m tile(s)" in log and os.path.isfile(os.path.splitext(vrt)[0] + "_dem.txt"), log
assert not any("ImageServer" in ln for ln in log.splitlines() if "1 m" in ln and "request" in ln)
# ... again: the VRT and all its tiles are there - reused without asking TNM
n_plan = len(calls["plan"])
sent.clear()
alg, log = run(DEM_SOURCE=rdem.SOURCE_1M)
assert len(calls["plan"]) == n_plan and calls["fetch"] == 1 and "reusing" in log and sent[0]["DEM"] == vrt
print("OK (1 m: native TNM tiles, VRT, 10 m underneath, Meters sent, reused)")

# an older Runner with no DEM_Z: warned, nothing extra sent
FakeRunDam.parameterDefinition, old_def = (lambda self, n: _Def(["ND method", "My own hydrograph"])
                                           if n == "METHOD" else None), FakeRunDam.parameterDefinition
sent.clear()
alg, log = run(DEM_SOURCE=rdem.SOURCE_10M)
assert all("DEM_Z" not in p for p in sent) and "no 'DEM elevation units'" in log
FakeRunDam.parameterDefinition = old_def
print("OK (an older Runner without DEM_Z: warned)")

# 5. cancel: the real dem_download.download stops at the next 3DEP request
dd.download = REAL["download"]


class CancelFb(QgsProcessingFeedback):
    def isCanceled(self): return True


def fake_fetch3(extent, cell, out_dir, stem, log=None, progress=None, max_px=None):
    progress(0, 4)
    raise AssertionError("must not get here")


terrain.fetch_3dep = fake_fetch3
try:
    rdem.get_10m(rdem.dams_box_5070([P3], 3), os.path.join(W, "c"), 1, 0, CancelFb())
    raise AssertionError("cancel must stop")
except rdem.RunnerDemCancelled as e:
    assert "Cancelled" in str(e)
try:
    dd.download(box, 10, os.path.join(W, "c2"), "x", CancelFb())
    raise AssertionError("cancel must stop")
except dd.Cancelled:
    pass
print("OK (cancel stops the 3DEP download)")
print("ALL OK")
