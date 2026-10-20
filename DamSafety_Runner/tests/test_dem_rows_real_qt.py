"""3.10.36 (#107): runs tests/realqt_dem_rows.py (real Qt, offscreen) in its own process, so the
fake Qt of this suite does not get in the way."""
import os
import subprocess
import sys
import tempfile

from nd_breach_runner.tests.registry import test

HERE = os.path.dirname(os.path.abspath(__file__))


@test
def dem_download_rows_keep_their_height_with_real_qt_offscreen():
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", HOME=tempfile.mkdtemp(prefix="demrows_home_"))
    env.pop("NDSUITE_SETTINGS", None)
    r = subprocess.run([sys.executable, os.path.join(HERE, "realqt_dem_rows.py")], env=env,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=300)
    out = r.stdout.decode("utf-8", "replace")
    if "SKIP: no real QGIS" in out:
        print("      (skipped: no real QGIS Python here)")
        return
    assert r.returncode == 0 and "all" in out and "checks passed" in out, out[-3000:]
