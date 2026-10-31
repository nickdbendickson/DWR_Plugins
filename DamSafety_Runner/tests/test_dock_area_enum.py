"""3.13.4 (built as 3.13.3) / Setup 1.0.18: runs tests/realqt_dock_area.py (real Qt, offscreen) in its own
process, so the fake Qt of this suite does not get in the way. QGIS 4 / PyQt6 has no
Qt.RightDockWidgetArea; the docks use Qt.DockWidgetArea.*."""
import os
import subprocess
import sys
import tempfile

from nd_breach_runner.tests.registry import test

HERE = os.path.dirname(os.path.abspath(__file__))


@test
def results_and_status_docks_use_the_scoped_dock_area_enum():
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", HOME=tempfile.mkdtemp(prefix="dock_home_"))
    env.pop("NDSUITE_SETTINGS", None)
    r = subprocess.run([sys.executable, os.path.join(HERE, "realqt_dock_area.py")], env=env,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=300)
    out = r.stdout.decode("utf-8", "replace")
    if "SKIP: no real QGIS" in out:
        print("      (skipped: no real QGIS Python here)")
        return
    assert r.returncode == 0 and "all checks passed" in out, out[-3000:]
