"""Tool 7b: the Runner's engine choice (TIER / FORCE_ANUGA / near-dam FV1 refine) and a culvert /
bridge layer (CULVERTS, with its field mapping) reach 'ndbreach:rundam' as TIER, FORCE_ANUGA,
REFINE_NEAR, STRUCTURES and S_KIND ... S_NAME. The defaults send nothing extra (the run is the same
as before). The reference is the Absaraka ANUGA-with-structures run (rundam_params_used.json).

Run: python3 tests/test_7b_engine_culverts.py
"""
import json, os, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(HERE)
PARENT = os.path.dirname(PLUGIN)
sys.path.insert(0, os.path.join(HERE, "qgis_mock"))
sys.path.insert(0, PARENT)
from qgis.core import QgsGeometry, QgsPointXY, QgsProcessingContext, QgsProcessingFeedback, make_layer  # noqa: E402
from qgis import processing  # noqa: E402
PKG = os.path.basename(PLUGIN)
import importlib  # noqa: E402
ho = importlib.import_module(PKG + ".core.handoff")
wf = importlib.import_module(PKG + ".core.workflow")
RunnerHandoff = importlib.import_module(PKG + ".algorithms.runner_handoff").RunnerHandoff

NEW = ("TIER", "FORCE_ANUGA", "REFINE_NEAR", "STRUCTURES", "S_KIND", "S_WIDTH", "S_HEIGHT",
       "S_BARRELS", "S_SHAPE", "S_NAME")
CUL = "/runs/structures.gpkg|layername=structures"
REF_FIELDS = {"S_KIND": "kind", "S_WIDTH": "width_ft", "S_HEIGHT": "height_ft",
              "S_BARRELS": "barrels", "S_SHAPE": "shape", "S_NAME": "name"}

# ---- 1. pure mapping: defaults add nothing; the reference choice gives the reference keys
assert ho.engine_params() == ({}, [])
assert ho.engine_params(0, False, False, None, {"C_KIND": "kind"}) == ({}, [])   # fields need a layer
p, notes = ho.engine_params(1, True, False, CUL, {"C_KIND": "kind", "C_WIDTH": "width_ft",
                                                  "C_HEIGHT": "height_ft", "C_BARRELS": "barrels",
                                                  "C_SHAPE": "shape", "C_NAME": "name"})
assert p == dict({"TIER": 1, "STRUCTURES": CUL}, **REF_FIELDS), p    # 0.17.21: FORCE folded into TIER
assert set(p) <= set(ho.RUNDAM_KEYS) and len(notes) == 2 and ho.CULVERTS_IGNORED not in notes
p, _ = ho.engine_params(0, False, True, CUL, {"C_KIND": "", "C_NAME": "label"})
assert p == {"REFINE_NEAR": True, "STRUCTURES": CUL, "S_NAME": "label"}, p   # empty = runner synonyms
assert "STRUCTURES" not in ho.CULVERT_FIELDS and all(k.startswith("C_") for k, _r, _d in ho.CULVERT_FIELDS)
print("OK (mapping)")

# ---- 2. the Runner really has these parameters (its own fake QGIS, own process)
runner = os.path.join(os.path.dirname(os.path.realpath(PLUGIN)), "DamSafety_Runner")
if os.path.exists(os.path.join(runner, "tests", "fake_qgis.py")):
    tmp = tempfile.mkdtemp(prefix="rd7b_")
    os.symlink(runner, os.path.join(tmp, "nd_breach_runner"))
    code = r'''
import json
from nd_breach_runner.tests import fake_qgis; fake_qgis.install()
from nd_breach_runner import algorithms as A
a = A.RunDamAlgorithm(); a.initAlgorithm()
d = {p.name(): [type(p).__name__, [str(x) for x in (p.args[0] if p.args and isinstance(p.args[0], (list, tuple)) else [])]]
     for p in a.parameterDefinitions()}
print(json.dumps(d))
'''
    env = dict(os.environ, PYTHONPATH=tmp, PYTHONDONTWRITEBYTECODE="1")
    env.pop("NDSUITE_SETTINGS", None)
    r = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=300, cwd=tmp)
    assert r.returncode == 0, r.stderr[-2000:]
    rd = json.loads(r.stdout.strip().splitlines()[-1])
    missing = [k for k in NEW if k not in rd]
    assert not missing, missing
    assert rd["TIER"][0] == "QgsProcessingParameterEnum" and len(rd["TIER"][1]) == len(ho.ENGINE_LABELS)
    assert "ANUGA" in rd["TIER"][1][1] and "ANUGA" in ho.ENGINE_LABELS[1]
    assert rd["FORCE_ANUGA"][0] == rd["REFINE_NEAR"][0] == "QgsProcessingParameterBoolean"
    assert rd["STRUCTURES"][0] == "QgsProcessingParameterFeatureSource"
    for k in REF_FIELDS:
        assert rd[k][0] == "QgsProcessingParameterField", k
    print("OK (runner parameters)")
else:
    print("SKIP runner parameter check: no runner at", runner)

# ---- 3. step defaults and window settings
W = tempfile.mkdtemp(prefix="w7b_")
S0 = {"runner_dem": "/dem.tif", "dams_folder": "/runs/dams"}
d0 = wf.defaults("7b", W, S0)
assert not {"ENGINE", "FORCE_ANUGA", "REFINE_NEAR", "CULVERTS"} & set(d0), d0
assert not [k for k in d0 if k.startswith("C_")]
S1 = dict(S0, runner_tier=1, runner_force_anuga=True, culverts=CUL, culvert_width="width_ft",
          culvert_name="name")
d1 = wf.defaults("7b", W, S1)
assert d1["ENGINE"] == 1 and "FORCE_ANUGA" not in d1 and "REFINE_NEAR" not in d1
assert d1["CULVERTS"] == CUL and d1["C_WIDTH"] == "width_ft" and d1["C_NAME"] == "name" and "C_KIND" not in d1
assert wf.DEFAULT_SETTINGS["runner_tier"] == 0 and wf.DEFAULT_SETTINGS["runner_force_anuga"] is False
assert "culverts" in [k for k, _l, _p in wf.LAYER_SLOTS]
for k in ("runner_tier", "runner_refine", "culvert_kind", "culvert_name"):
    assert k in wf.WINDOW_KEYS, k
assert "runner_force_anuga" not in wf.WINDOW_KEYS          # 0.17.21: not persisted on its own
# an old saved force (tier 0) still gives engine 1
assert wf.defaults("7b", W, dict(S0, runner_force_anuga=True))["ENGINE"] == 1


class FakeSF:
    def __init__(self): self.values = {}
    def set(self, plugin, kind, k, v):
        if v is None: self.values.pop((plugin, kind, k), None)
        else: self.values[(plugin, kind, k)] = v
    def window(self, plugin):
        return {k: v for (p_, kind, k), v in self.values.items() if p_ == plugin and kind == "window"}


sf = wf.window_settings_to(FakeSF(), S1)
back = wf.window_settings_from(sf)
assert back["runner_tier"] == 1 and "runner_force_anuga" not in back and back["culvert_width"] == "width_ft"
old = FakeSF()                                                   # a settings file from 0.17.20
old.values[(wf.PLUGIN_KEY, "window", "runner_tier")] = "0"
old.values[(wf.PLUGIN_KEY, "window", "runner_force_anuga")] = "True"
mig = wf.window_settings_from(old)
assert mig["runner_tier"] == 1 and "runner_force_anuga" not in mig, mig
sf = wf.window_settings_to(FakeSF(), dict(wf.DEFAULT_SETTINGS), defaults=False)
assert not [k for (_p, _k, k) in sf.values if k.startswith(("runner_", "culvert"))]
print("OK (step defaults and window settings)")

# ---- 4. the tool: default run sends nothing new; the reference choice reaches rundam
sent = []


class _Def:
    def __init__(self, o): self._o = o
    def options(self): return self._o


class FakeRunDam:
    @classmethod
    def createInstance(cls): return cls()
    def parameterDefinition(self, name):
        return _Def(["ND method", "My own hydrograph"]) if name == "METHOD" else None
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
dams = make_layer([("dam_id", "string"), ("height_m", "double"), ("td_vw_m3", "double"), ("hz_class", "string")],
                  [(pt(0, 0), {"dam_id": "DWR-2801", "height_m": 8.38, "td_vw_m3": 1615.5 * 1233.48,
                               "hz_class": "High"})], "point", "dams")
bpts = make_layer([("dam_id", "string")], [(pt(11, 22), {"dam_id": "DWR-2801"})], "point", "bpts")
cul = make_layer([("kind", "string"), ("width_ft", "double")], [], "line", "culverts")
ctx = QgsProcessingContext()
base = dict(DAMS=ctx.add(dams), BPTS=ctx.add(bpts), DEM="nd_dem.vrt", DAMS_FOLDER=os.path.join(W, "Dams"),
            LETHALITY=1, WHICH=ho.SELECT_ALL)
os.environ.pop(ho.ANUGA_SETTING, None)
RunnerHandoff().run_mock(dict(base), ctx, QgsProcessingFeedback())
assert len(sent) == 1 and not set(NEW) & set(sent[0]), sent[0]
FAKE_PY = os.path.join(W, "anuga_python")
open(FAKE_PY, "w").close()
os.environ[ho.ANUGA_SETTING] = FAKE_PY                   # an ANUGA environment "installed"
CL = ctx.add(cul)
fb = QgsProcessingFeedback()
RunnerHandoff().run_mock(dict(base, ENGINE=1, FORCE_ANUGA=True, CULVERTS=CL, C_KIND="kind",
                              C_WIDTH="width_ft", C_HEIGHT="height_ft", C_BARRELS="barrels",
                              C_SHAPE="shape", C_NAME="name"), ctx, fb)
got = {k: sent[1][k] for k in NEW if k in sent[1]}
assert got == dict({"TIER": 1, "STRUCTURES": CL}, **REF_FIELDS), got
assert any("Runner engine (7b): LISFLOOD-FP \u2192 ANUGA" in m for m in fb.log), fb.log
assert any("FORCE_ANUGA (before 0.17.21)" in m for m in fb.log)
print("OK (7b sends the engine and culverts to rundam; defaults send nothing new)")

# culverts with LISFLOOD-FP only and no force: said to be unused (the Runner uses them in ANUGA only)
_p, _n = ho.engine_params(0, False, False, CUL, {})
assert ho.CULVERTS_IGNORED in _n and _p.get("STRUCTURES") == CUL, _n
assert ho.CULVERTS_IGNORED not in ho.engine_params(0, True, False, CUL, {})[1]
print("OK (culverts-ignored note)")

# ---- 5. 0.17.21 follow-ups (QGIS Dev's #22 review)
# legacy FORCE_ANUGA alone = engine 1
n0 = len(sent)
RunnerHandoff().run_mock(dict(base, FORCE_ANUGA=True), ctx, QgsProcessingFeedback())
assert sent[n0].get("TIER") == 1 and "FORCE_ANUGA" not in sent[n0], sent[n0]
# culverts on a LISFLOOD-only run: warned, still passed (harmless), engine recorded
fb = QgsProcessingFeedback()
RunnerHandoff().run_mock(dict(base, CULVERTS=CL), ctx, fb)
assert any(ho.CULVERTS_IGNORED in m for m in fb.log), fb.log
assert "TIER" not in sent[-1]
# ANUGA requested, no environment: stops before any dam is sent
os.environ[ho.ANUGA_SETTING] = os.path.join(W, "no_such_python")
RH = importlib.import_module(PKG + ".algorithms.runner_handoff")
_det = RH._runner_anuga_detect
RH._runner_anuga_detect = lambda: None               # no Runner auto-detect on this machine
n0 = len(sent)
from qgis.core import QgsProcessingException  # noqa: E402
try:
    RunnerHandoff().run_mock(dict(base, ENGINE=1), ctx, QgsProcessingFeedback())
    raise AssertionError("ANUGA missing must stop the tool")
except QgsProcessingException as e:
    assert "no ANUGA environment" in str(e) and len(sent) == n0
RH._runner_anuga_detect = _det
os.environ[ho.ANUGA_SETTING] = FAKE_PY
# pure helpers
assert ho.effective_engine(0, True) == 1 and ho.effective_engine(1) == 1 and ho.effective_engine(None) == 0
assert "IGNORED" in ho.engine_status(0, CUL) and "used by ANUGA" in ho.engine_status(1, CUL)
assert ho.find_anuga_python(lambda: "", {}, lambda: "/x/py", isfile=lambda p: p == "/x/py") == "/x/py"
assert ho.find_anuga_python(lambda: "/s/py", {}, None, isfile=lambda p: p == "/s/py") == "/s/py"
assert ho.find_anuga_python(lambda: "/gone", {}, lambda: "", isfile=lambda p: False) == ""
# parameters: one engine enum, FORCE_ANUGA hidden, numeric culvert fields, advanced field pickers
alg = RunnerHandoff(); alg.initAlgorithm()
defs = alg._params
assert len(defs["ENGINE"].options) == 2 and "force ANUGA" in defs["ENGINE"].options[1]
assert "upstream end first" in str(defs["CULVERTS"].description)
assert defs["FORCE_ANUGA"].defaultValue is False
help_ = alg.shortHelpString()
for needle in ("RUNNER ENGINE", "NEAR-DAM REFINE", "UPSTREAM end", "span or diameter (ft)"):
    assert needle in help_, needle
assert ("rn_engine" in [n for n, _t in importlib.import_module(PKG + ".algorithms.runner_handoff").NEW])
print("OK (0.17.21: one engine choice, legacy force, culvert warning, early ANUGA check, help)")

os.environ.pop(ho.ANUGA_SETTING, None)
