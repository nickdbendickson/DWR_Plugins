"""3.12.11 / Setup 1.0.18 (QGIS 4 fulltest 2026-10-30): the docks are added with the scoped
Qt.DockWidgetArea enum. QGIS 4 (PyQt6) has no Qt.RightDockWidgetArea / Qt.LeftDockWidgetArea,
so the Runner's results dock and Setup's status dock raised AttributeError / TypeError there.

Real Qt, offscreen, in the QGIS python (QGIS 3.34 and 4):
    QT_QPA_PLATFORM=offscreen python3 DamSafety_Runner/tests/realqt_dock_area.py
The fast suite runs it in its own process through tests/test_dock_area_enum.py.
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

FAILS = []


def check(what, ok, got=""):
    print("{:4} {}{}".format("ok" if ok else "FAIL", what, "" if ok else "  -> {!r}".format(got)))
    if not ok:
        FAILS.append(what)


try:
    from qgis.PyQt.QtCore import Qt
    from qgis.PyQt.QtWidgets import QApplication, QDockWidget, QMainWindow
except ImportError:                                    # pragma: no cover
    print("SKIP: no real QGIS")
    sys.exit(0)

app = QApplication.instance() or QApplication([])
area = getattr(Qt, "DockWidgetArea", None)

from DamSafety_Runner import results_dock as rd        # noqa: E402

check("Runner RIGHT_DOCK is Qt.DockWidgetArea.RightDockWidgetArea",
      area is None or rd.RIGHT_DOCK == area.RightDockWidgetArea, rd.RIGHT_DOCK)
win = QMainWindow()
d = QDockWidget("results")
d.setAllowedAreas(rd.LEFT_DOCK | rd.RIGHT_DOCK)
win.addDockWidget(rd.RIGHT_DOCK, d)
check("Runner: addDockWidget(RIGHT_DOCK) puts the dock on the right",
      win.dockWidgetArea(d) == rd.RIGHT_DOCK, win.dockWidgetArea(d))

# Setup's plugin.py imports qgis.utils / iface pieces; read its LEFT_DOCK without the GUI
src = open(os.path.join(ROOT, "DamSafety_Setup", "plugin.py"), encoding="utf-8").read()
check("Setup: addDockWidget is given LEFT_DOCK", "addDockWidget(LEFT_DOCK" in src)
ns = {"Qt": Qt}
exec(re.search(r"try:\n    LEFT_DOCK = .*?\n(?:.*\n){2}", src).group(0), ns)
s = QDockWidget("status")
win.addDockWidget(ns["LEFT_DOCK"], s)
check("Setup: addDockWidget(LEFT_DOCK) puts the dock on the left",
      win.dockWidgetArea(s) == ns["LEFT_DOCK"], win.dockWidgetArea(s))

# no bare Qt.<X>DockWidgetArea left in either plugin
bare = []
for plug in ("DamSafety_Runner", "DamSafety_Setup", "DamSafety_Hazard", "DamSafety_Screening",
             "DamSafety_Data"):
    for dp, _dn, fn in os.walk(os.path.join(ROOT, plug)):
        if "tests" in dp.split(os.sep):
            continue
        for f in fn:
            if f.endswith(".py"):
                t = open(os.path.join(dp, f), encoding="utf-8").read()
                for m in re.finditer(r"Qt\.(Left|Right|Top|Bottom)DockWidgetArea", t):
                    line = t[:m.start()].count("\n") + 1
                    ctx = t.splitlines()[line - 1]
                    if ctx.lstrip().startswith("#"):
                        continue
                    if "except AttributeError" not in t[max(0, m.start() - 120):m.start()]:
                        bare.append("{}:{}: {}".format(os.path.relpath(os.path.join(dp, f), ROOT),
                                                      line, ctx.strip()))
check("no bare Qt.*DockWidgetArea outside a fallback", not bare, bare)

print("\n{} failure(s)".format(len(FAILS)) if FAILS else "\nall checks passed")
sys.exit(1 if FAILS else 0)
