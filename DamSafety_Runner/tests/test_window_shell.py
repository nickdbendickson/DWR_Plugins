"""3.10.15 window shell: runs tests/realqt_window_shell.py (real Qt, offscreen) in its own
process, so the fake Qt of this suite does not get in the way."""
import os
import subprocess
import sys

from nd_breach_runner.tests.registry import test

HERE = os.path.dirname(os.path.abspath(__file__))


@test
def window_shell_with_real_qt_offscreen():
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    env.pop("NDSUITE_SETTINGS", None)
    r = subprocess.run([sys.executable, os.path.join(HERE, "realqt_window_shell.py")], env=env,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=600)
    out = r.stdout.decode("utf-8", "replace")
    if "No module named 'qgis'" in out:
        print("      (skipped: no real QGIS Python here)")
        return
    assert r.returncode == 0 and "all checks passed" in out, out[-3000:]
