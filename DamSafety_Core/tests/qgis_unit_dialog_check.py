"""Real-QGIS check of the SI display units in a Processing tool dialog (Core 1.1.0).

    QT_QPA_PLATFORM=offscreen python3 DamSafety_Core/tests/qgis_unit_dialog_check.py

Builds a tool with two foot parameters, an acre parameter, a metre parameter and an integer,
opens QGIS's own tool dialog under SI and under US, and checks: the labels and spin boxes show
m / ha under SI, an untouched default reaches the tool exactly in feet, a typed metre value
reaches it converted to feet, the metre parameter is left alone, the batch dialog keeps QGIS's
own widget, and under US nothing is tagged.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)
sys.path.insert(0, "/usr/share/qgis/python/plugins")

from qgis.core import (QgsApplication, QgsProcessingAlgorithm, QgsProcessingParameterNumber,
                       QgsProcessingContext, QgsProcessingFeedback, QgsProcessingProvider)

app = QgsApplication([], True)
app.initQgis()
from processing.core.Processing import Processing                 # noqa: E402
Processing.initialize()
from qgis.gui import QgsGui                                         # noqa: E402
QgsGui.instance()

from DamSafety_Core.damsuite import display_units as DU             # noqa: E402
from DamSafety_Core.ui import unit_widgets as UW                    # noqa: E402

FAILS = []


def check(label, ok, detail=""):
    print(("ok   " if ok else "FAIL ") + label + ("" if ok else " - " + str(detail)))
    if not ok:
        FAILS.append(label)


class Alg(UW.UnitParamsMixin, QgsProcessingAlgorithm):
    def name(self):
        return "unitcheck"

    def displayName(self):
        return "Unit check"

    def createInstance(self):
        return Alg()

    def initAlgorithm(self, config=None):
        D = QgsProcessingParameterNumber.Double
        self.addParameter(QgsProcessingParameterNumber(
            "CREST_WIDTH", "Crest width (ft; starting value 15 is the default)", D, 15.0, False, 0.0))
        self.addParameter(QgsProcessingParameterNumber(
            "NORMAL_POOL_FT", "Normal pool elevation (ft, DEM datum)", D, 1234.56, True))
        self.addParameter(QgsProcessingParameterNumber("AREA", "Largest pond (acres; YOUR value)", D, 10.0))
        self.addParameter(QgsProcessingParameterNumber("CELL", "Cell size (m)", D, 10.0))
        self.addParameter(QgsProcessingParameterNumber(
            "COUNT_FT", "Search distance (ft)", QgsProcessingParameterNumber.Integer, 500))

    def processAlgorithm(self, parameters, context, feedback):
        return {}


class Prov(QgsProcessingProvider):
    def id(self):
        return "unitcheck"

    def name(self):
        return "unitcheck"

    def loadAlgorithms(self):
        self.addAlgorithm(Alg())


PROV = Prov()
QgsApplication.processingRegistry().addProvider(PROV)
from processing.gui.AlgorithmDialog import AlgorithmDialog          # noqa: E402

os.environ.pop("NDSUITE_UNITS", None)
os.environ["NDSUITE_UNITS"] = "si"
from qgis.core import QgsSettings                                   # noqa: E402
QgsSettings().remove("ndsuite/unit_system")
check("system from env", DU.system() == "si", DU.system())

alg = QgsApplication.processingRegistry().createAlgorithmById("unitcheck:unitcheck")
md = {p.name(): bool(p.metadata().get("widget_wrapper")) for p in alg.parameterDefinitions()}
check("ft/acre params tagged, metre param not",
      md == {"CREST_WIDTH": True, "NORMAL_POOL_FT": True, "AREA": True, "CELL": False,
             "COUNT_FT": True}, md)

dlg = AlgorithmDialog(alg)
panel = dlg.mainWidget()
wr = panel.wrappers
w = wr["CREST_WIDTH"]
check("crest box in m", w.widget.suffix() == " m" and abs(w.widget.value() - 4.572) < 1e-9,
      (w.widget.suffix(), w.widget.value()))
lab = w.createLabel().text()
check("crest label relabelled", lab.startswith("Crest width (m; starting value 15 ft"), lab)
ctx = QgsProcessingContext()
params = dlg.createProcessingParameters()
check("untouched defaults reach the tool exactly",
      params["CREST_WIDTH"] == 15.0 and params["NORMAL_POOL_FT"] == 1234.56
      and params["AREA"] == 10.0 and params["CELL"] == 10.0 and params["COUNT_FT"] == 500,
      params)
w.widget.setValue(10.0)
wr["AREA"].widget.setValue(1.0)
params = dlg.createProcessingParameters()
check("10 m typed -> 32.808 ft", abs(params["CREST_WIDTH"] - 10.0 / 0.3048) < 1e-9, params["CREST_WIDTH"])
check("1 ha typed -> 2.471 acres", abs(params["AREA"] - 2.4710538146716536) < 1e-9, params["AREA"])
check("integer param stays int", isinstance(params["COUNT_FT"], int), type(params["COUNT_FT"]))
wr["CREST_WIDTH"].setParameterValue(20.0, ctx)
check("setValue(stored ft) shows m", abs(wr["CREST_WIDTH"].widget.value() - 6.096) < 1e-9,
      wr["CREST_WIDTH"].widget.value())
check("and returns 20 ft exactly", dlg.createProcessingParameters()["CREST_WIDTH"] == 20.0)

# batch dialog: QGIS's own widget, stored unit
from processing.gui.BatchAlgorithmDialog import BatchAlgorithmDialog  # noqa: E402
try:
    bd = BatchAlgorithmDialog(alg.create())
    tw = bd.mainWidget()
    wrapper = tw.wrappers[0][0] if hasattr(tw, "wrappers") else None
    ok = all(not isinstance(x, UW.wrapper_class()) for row in tw.wrappers for x in row if x)
    n = sum(1 for row in tw.wrappers for x in row if x)
    check("batch dialog keeps QGIS's own widgets (%d)" % n, ok and n > 0)
except Exception as e:                                              # noqa
    check("batch dialog opens", False, repr(e))

# model designer: QGIS's own widget (model inputs), stored unit
try:
    from qgis.core import QgsProcessingModelAlgorithm, QgsProcessingModelChildAlgorithm
    from processing.modeler.ModelerParametersDialog import ModelerParametersDialog
    model = QgsProcessingModelAlgorithm("m", "g")
    child = QgsProcessingModelChildAlgorithm("unitcheck:unitcheck")
    model.addChildAlgorithm(child)
    md_dlg = ModelerParametersDialog(alg.create(), model, child.childId())
    ws = getattr(md_dlg.widget, "widget", md_dlg.widget)
    wrappers = getattr(ws, "wrappers", {})
    ok = bool(wrappers) and all(not isinstance(x, UW.wrapper_class()) for x in wrappers.values())
    check("model designer keeps QGIS's own widgets (%d)" % len(wrappers), ok, wrappers)
except Exception as e:                                              # noqa
    import traceback
    traceback.print_exc()
    check("model designer opens", False, repr(e))

os.environ["NDSUITE_UNITS"] = "us"
alg2 = QgsApplication.processingRegistry().createAlgorithmById("unitcheck:unitcheck")
check("US: nothing tagged",
      not any(p.metadata().get("widget_wrapper") for p in alg2.parameterDefinitions()))

print("FAILED: %d" % len(FAILS) if FAILS else "all ok")
sys.stdout.flush()
os._exit(1 if FAILS else 0)
