"""Real-QGIS check of the SI copies of a tool's result tables (Core 1.3.0).

    QT_QPA_PLATFORM=offscreen python3 DamSafety_Core/tests/qgis_si_copies_check.py

A Processing tool built on UnitParamsMixin writes a CSV and a report and returns them. Run
through processing.run: under SI the tables are copied to SI_units/ with SI columns beside the
US ones and the log says so; under US nothing is written; a tool that inherits its
processAlgorithm, one that calls its parent's, and one that returns nothing all still work.
"""
import csv
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)
sys.path.insert(0, "/usr/share/qgis/python/plugins")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qgis.core import (QgsApplication, QgsProcessingAlgorithm, QgsProcessingFeedback,   # noqa: E402
                       QgsProcessingOutputFile, QgsProcessingParameterString,
                       QgsProcessingProvider)

app = QgsApplication([], False)
app.initQgis()
import processing                                                   # noqa: E402
from processing.core.Processing import Processing                   # noqa: E402
Processing.initialize()

from DamSafety_Core.damsuite import display_units as DU             # noqa: E402
from DamSafety_Core.ui import unit_widgets as UW                    # noqa: E402

FAILS = []
CALLS = []


def check(label, ok, detail=""):
    print(("ok   " if ok else "FAIL ") + label + ("" if ok else " - " + str(detail)))
    if not ok:
        FAILS.append(label)


def write_outputs(folder):
    os.makedirs(os.path.join(folder, "model"), exist_ok=True)
    summary = os.path.join(folder, "Batch_Breach_Runs.csv")
    with open(summary, "w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerows([["Dam", "Peak_cfs", "Height_ft"], ["A", "28671", "27.5"]])
    with open(os.path.join(folder, "model", "receptors_flooded.csv"), "w", newline="",
              encoding="utf-8") as f:
        csv.writer(f).writerows([["id", "depth_ft"], ["1", "2.0"]])
    report = os.path.join(folder, "model", "run_report.md")
    with open(report, "w", encoding="utf-8") as f:
        f.write("# report\n")
    return {"SUMMARY": summary, "REPORT": report, "COUNTS": "1 run"}


class Tool(UW.UnitParamsMixin, QgsProcessingAlgorithm):
    def name(self):
        return "tool"

    def displayName(self):
        return "Tool"

    def createInstance(self):
        return type(self)()

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterString("FOLDER", "Folder"))
        self.addOutput(QgsProcessingOutputFile("SUMMARY", "Summary"))
        self.addOutput(QgsProcessingOutputFile("REPORT", "Report"))

    def processAlgorithm(self, parameters, context, feedback):
        CALLS.append(self.name())
        return write_outputs(self.parameterAsString(parameters, "FOLDER", context))


class Inherits(Tool):                       # no processAlgorithm of its own
    def name(self):
        return "inherits"


class CallsParent(Tool):
    def name(self):
        return "callsparent"

    def processAlgorithm(self, parameters, context, feedback):
        res = super().processAlgorithm(parameters, context, feedback)
        res["COUNTS"] = "from the child"
        return res


class Nothing(Tool):
    def name(self):
        return "nothing"

    def processAlgorithm(self, parameters, context, feedback):
        return {}


class Prov(QgsProcessingProvider):
    def id(self):
        return "sicopies"

    def name(self):
        return "sicopies"

    def loadAlgorithms(self):
        for a in (Tool, Inherits, CallsParent, Nothing):
            self.addAlgorithm(a())


class Log(QgsProcessingFeedback):
    def __init__(self):
        super().__init__()
        self.lines = []

    def pushInfo(self, info):
        self.lines.append(info)


PROV = Prov()
QgsApplication.processingRegistry().addProvider(PROV)
REAL_SYSTEM = DU.system


def units(value):
    """The unit system for the check, without touching the computer's own setting."""
    DU.system = lambda project=None: value


TMP = tempfile.mkdtemp(prefix="si_copies_")
try:
    check("each class wraps its own processAlgorithm once",
          all(getattr(c.__dict__["processAlgorithm"], "_si_copies", False)
              for c in (Tool, CallsParent, Nothing)) and "processAlgorithm" not in
          Inherits.__dict__)

    units("us")
    d = os.path.join(TMP, "us")
    res = processing.run("sicopies:tool", {"FOLDER": d}, feedback=Log())
    check("US: the tool runs and returns its outputs", res.get("COUNTS") == "1 run", res)
    check("US: no SI_units folder anywhere",
          not any(DU.SI_CSV_DIR in dirs for _b, dirs, _f in os.walk(d)))

    units("si")
    for name in ("tool", "inherits", "callsparent"):
        d = os.path.join(TMP, name)
        log = Log()
        res = processing.run("sicopies:" + name, {"FOLDER": d}, feedback=log)
        a = os.path.join(d, "SI_units", "Batch_Breach_Runs.csv")
        b = os.path.join(d, "model", "SI_units", "receptors_flooded.csv")
        check(name + ": SI copies of the summary and of the report folder's table",
              os.path.isfile(a) and os.path.isfile(b), os.listdir(d))
        if os.path.isfile(a):
            with open(a, newline="", encoding="utf-8") as f:
                rows = list(csv.reader(f))
            check(name + ": SI column beside each US one",
                  rows == [["Dam", "Peak_cfs", "Peak_cms", "Height_ft", "Height_m"],
                           ["A", "28671", "811.9", "27.5", "8.382"]], rows)
        with open(os.path.join(d, "Batch_Breach_Runs.csv"), newline="", encoding="utf-8") as f:
            check(name + ": the tool's own CSV is unchanged",
                  list(csv.reader(f)) == [["Dam", "Peak_cfs", "Height_ft"],
                                          ["A", "28671", "27.5"]])
        check(name + ": the results are the tool's own",
              res.get("SUMMARY") == os.path.join(d, "Batch_Breach_Runs.csv")
              and res.get("COUNTS") == ("from the child" if name == "callsparent" else "1 run"),
              res)
        check(name + ": the log says where the SI tables are",
              any("SI units" in ln and "SI_units" in ln for ln in log.lines), log.lines[-3:])
    res = processing.run("sicopies:nothing", {"FOLDER": os.path.join(TMP, "n")}, feedback=Log())
    check("a tool that returns nothing still runs", res == {} or "SUMMARY" not in res, res)
    check("the helper never raises on odd results",
          UW.si_copies_of(None) == [] and UW.si_copies_of({"A": 1, "B": object()}) == [])
finally:
    DU.system = REAL_SYSTEM
    shutil.rmtree(TMP, ignore_errors=True)

print("FAILED: %d" % len(FAILS) if FAILS else "all ok")
sys.stdout.flush()
os._exit(1 if FAILS else 0)
