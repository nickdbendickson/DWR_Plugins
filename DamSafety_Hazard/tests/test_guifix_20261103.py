"""2026-11-03 GUI fit (QGIS Dev): short Processing labels with the detail in the help, the
OSM search distance shown to 0.1 ft with a maximum, the culvert / OSM values kept by the suite
settings file (so a reopened dialog shows the engineer's value), and the Classify tab's tick
boxes on one line at 640 x 480. Real QGIS (offscreen).

    QT_QPA_PLATFORM=offscreen python3 DamSafety_Hazard/tests/test_guifix_20261103.py
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, REPO)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.pop("NDSUITE_SETTINGS", None)
os.environ["HOME"] = tempfile.mkdtemp(prefix="guifix_home_")

fails = []


def check(cond, msg):
    print(("ok   " if cond else "FAIL ") + msg)
    if not cond:
        fails.append(msg)


try:
    from qgis.core import QgsApplication
except ImportError:
    print("skip: no QGIS")
    sys.exit(0)
app = QgsApplication([], True)
app.initQgis()
sys.path.insert(0, os.path.join(app.pkgDataPath(), "python", "plugins"))

from DamSafety_Hazard import suite_algorithms as SA  # noqa: E402
from DamSafety_Hazard import suite_settings_tools as HT  # noqa: E402
from DamSafety_Hazard.damsuite import settings as DS  # noqa: E402

MAX_LABEL = 70          # about 450 px of text: the Processing panel stays well under 800 px


_KEEP = []


def params(alg):
    alg.initAlgorithm({})
    _KEEP.append(alg)                    # the definitions live as long as their tool
    return {p.name(): p for p in alg.parameterDefinitions()}


# -- Hazard runnerculverts
rc = params(SA.RunnerCulvertsAlgorithm())
for name in ("PASS_ELEVATIONS", "ALLOW_NO_EMBANKMENT", "USE_NBI_WIDTH", "STREAM_REVERSED",
             "OSM_SEARCH_FT", "SEARCH_FT"):
    d = rc[name].description()
    check(len(d) <= MAX_LABEL, "runnerculverts {} label is short ({} chars)".format(name, len(d)))
for name in ("PASS_ELEVATIONS", "ALLOW_NO_EMBANKMENT", "OSM_SEARCH_FT"):
    check(len(rc[name].help() or "") > 30, "runnerculverts {}: the detail is in the help".format(name))
osm = rc["OSM_SEARCH_FT"]
check((osm.metadata().get("widget_wrapper") or {}).get("decimals") == 1, "OSM_SEARCH_FT: 1 decimal")
check(osm.maximum() <= 5000 and osm.maximum() >= osm.defaultValue(),
      "OSM_SEARCH_FT: a sensible maximum ({})".format(osm.maximum()))
check("3DHP" in rc["STREAM_REVERSED"].description(), "STREAM_REVERSED says 3DHP")
alg = SA.RunnerCulvertsAlgorithm()
check("ND Dam Breach Runner" not in alg.displayName() + alg.shortHelpString(),
      "runnerculverts: DamSafety naming")

# -- Runner rundam
try:
    from DamSafety_Runner import algorithms as RA  # noqa: E402
    from DamSafety_Runner import provider as prov, popup_logic as pl  # noqa: E402
    from DamSafety_Runner import suite_settings_tools as RT  # noqa: E402
except Exception as e:                                           # noqa
    RA = None
    check(False, "the Runner imports ({})".format(e))
if RA is not None:
    run = None
    for c in prov.ALGORITHMS:
        inst = RT.fresh(c)
        if RT.tool_name(inst) == "rundam":
            _KEEP.append(inst)
            run = {p.name(): p for p in inst.parameterDefinitions()}
    check(run is not None, "rundam found")
    run = run or {}
    long_bools = [n for n, p in run.items() if type(p).__name__ == "QgsProcessingParameterBoolean"
                  and len(p.description()) > MAX_LABEL]
    check(not long_bools, "rundam tick-box labels are short: {}".format(long_bools))
    for name in ("STOP_RULE", "REFINE_NEAR", "RESUME", "CULVERT_BURN_GUESSES", "S_INVERT_TOL_FT"):
        if name in run:
            check(len(run[name].help() or "") > 30, "rundam {}: the detail is in the help".format(name))
    # persistence: the suite settings file is the dialog's default, so a reopened dialog keeps it
    cat = RT.full_catalog(prov.ALGORITHMS, pl.PLUGIN_KEY, pl.settings_group, pl.TAB_ORDER)
    burn = [s for s in cat if s.tool == "rundam" and s.key == "CULVERT_BURN_GUESSES"]
    check(len(burn) == 1 and burn[0].help, "CULVERT_BURN_GUESSES is a settings-grid row with help")
    path = os.path.join(tempfile.mkdtemp(), "suite.csv")
    from DamSafety_Runner.damsuite import settings as RDS   # the Runner's own copy
    s = RDS.Settings()
    s.set("ndbreach", "rundam", "CULVERT_BURN_GUESSES", False)
    s.save(path, catalog=cat)
    RDS.set_current_path(path, remember=False)
    for c in prov.ALGORITHMS:
        inst = RT.fresh(c)
        if RT.tool_name(inst) == "rundam":
            _KEEP.append(inst)
            RT.apply_file_defaults(inst, pl.PLUGIN_KEY, pl.settings_group)
            RDS.set_current_path("", remember=False)
            v = inst.parameterDefinition("CULVERT_BURN_GUESSES").defaultValue()
            check(v is False, "CULVERT_BURN_GUESSES off in the file -> the reopened tool's default is off")

# -- Hazard catalog: OSM_SEARCH_FT is kept by the settings file
from DamSafety_Hazard.processing_provider import Provider, settings_group, SETTINGS_TABS  # noqa: E402
hcat = HT.full_catalog(Provider.original_classes(), "ndhazard", settings_group, SETTINGS_TABS)
o = [s for s in hcat if s.tool == "runnerculverts" and s.key == "OSM_SEARCH_FT"]
check(len(o) == 1 and o[0].unit == "ft", "OSM_SEARCH_FT is a settings-grid row in ft")
path = os.path.join(tempfile.mkdtemp(), "suite.csv")
s = DS.Settings()
s.set("ndhazard", "runnerculverts", "OSM_SEARCH_FT", 120.0)
s.save(path, catalog=hcat)
DS.set_current_path(path, remember=False)
inst = SA.RunnerCulvertsAlgorithm()
inst.initAlgorithm({})
HT.apply_file_defaults(inst, "ndhazard", settings_group)
check(abs(float(inst.parameterDefinition("OSM_SEARCH_FT").defaultValue()) - 120.0) < 1e-6,
      "OSM_SEARCH_FT 120 ft in the file -> the reopened tool's default is 120 ft")
DS.set_current_path("", remember=False)

# -- Classify tab tick boxes: one line each at 640 x 480
from qgis.PyQt.QtWidgets import QCheckBox  # noqa: E402
try:
    from DamSafety_Hazard import classify_tab  # noqa: E402
    import inspect
    src = inspect.getsource(classify_tab)
    for t in ("Compare with the last run", "ACER zone rasters (Hazard_*.tif)",
              "Layer of the cells read per structure", "Re-use unchanged downloads and sampling"):
        cb = QCheckBox(t)
        check(t in src and cb.sizeHint().width() <= 400,
              "Classify '{}' fits ({} px)".format(t, cb.sizeHint().width()))
except Exception as e:                                           # noqa
    check(False, "classify_tab ({})".format(e))

# -- settings grid: one parameter in several tools is not shown as duplicate rows
try:
    from DamSafety_Hazard import suite_settings as SG  # noqa: E402
    cat2 = [DS.Setting("ndbreach", t, "S_INVERT_TOL_FT", "Inventory invert tolerance (ft)", "number", 3.0,
                       unit="ft", group="Dam", tool_title=title)
            for t, title in (("rundam", "Run one dam"), ("runbatch", "Run a layer of dams"))]
    cat2.append(DS.Setting("ndbreach", "rundam", "OTHER", "Something else", "number", 1.0, group="Dam"))
    grid = SG.SettingsGrid("ndbreach", cat2)
    _KEEP.append(grid)
    tbl = grid.tables["Dam"]
    texts = [tbl.item(i, SG.C_LABEL).text() for i in range(tbl.rowCount())]
    check(len(set(texts)) == 3 and "Run one dam" in texts[0] and "Run a layer of dams" in texts[1]
          and texts[2] == "Something else", "duplicate labels name their tool: {}".format(texts))
except Exception as e:                                           # noqa
    check(False, "settings grid ({})".format(e))

# -- 3DHP: labels that mean the current stream source say 3DHP (setting keys unchanged)
try:
    _repo = os.path.dirname(os.path.dirname(HERE))

    def _src(rel):
        with open(os.path.join(_repo, rel), encoding="utf-8") as f:
            return f.read()
    check('description="USGS 3DHP flowlines' in _src("DamSafety_Hazard/suiteexport.py"),
          "suite export flowlines say 3DHP")
    if os.path.isdir(os.path.join(_repo, "shared", "damsuite")):
        sv = _src("shared/damsuite/servers.py")
        check('"name": "3DHP flowlines"' in sv and "3DHP_all/MapServer/50" in sv
              and "nhd/MapServer/6" not in sv, "servers.py flowlines are 3DHP")
        check("normaliser_for_url" in _src("DamSafety_Screening/core/offline.py"),
              "offline package adds the NHD-style fields to 3DHP flowlines")
        for rel in ("DamSafety_Screening/algorithms/data_package.py",
                    "DamSafety_Screening/algorithms/run_breach_sim.py",
                    "DamSafety_Screening/core/preflight.py",
                    "DamSafety_Screening/core/workflow.py"):
            check('"NHD flowlines' not in _src(rel) and "(NHD flowlines" not in _src(rel),
                  "{}: no NHD flowlines label".format(rel))
except Exception as e:                                           # noqa
    check(False, "3DHP labels ({})".format(e))

# -- DamSafety Data's windows fit 800 x 600 (the body scrolls, the buttons stay)
if os.path.isdir(os.path.join(REPO, "DamSafety_Data")):
    try:
        from DamSafety_Data import pack_dialog as PD, settings_dialog as SD  # noqa: E402
        from DamSafety_Data.settings import Settings as DSet  # noqa: E402

        class _Up:
            def load_manifest(self):
                return {}

        class _Plug:
            def make_updater(self):
                return _Up()

            def all_sources(self):
                return []

            def __getattr__(self, name):            # is_running, open_data_folder ...
                return lambda *a, **k: None

        for name, mk in (("Build offline pack", lambda: PD.PackDialog(settings=None)),
                         ("Data settings", lambda: SD.SettingsDialog(DSet(), _Plug()))):
            d = mk()
            _KEEP.append(d)
            m = d.minimumSizeHint()
            check(m.width() <= 800 and m.height() <= 560,
                  "{} fits 800x600 (minimum {}x{})".format(name, m.width(), m.height()))
    except Exception as e:                                       # noqa
        check(False, "Data windows ({})".format(e))

print("\n{} failed".format(len(fails)))
sys.stdout.flush()
os._exit(1 if fails else 0)
