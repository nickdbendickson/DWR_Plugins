"""The wizard's DEM page refuses a missing file or a folder (it used to accept any text and the
Runner failed after it started). Runs without QGIS."""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from DamSafety_Setup import runner_params as rp  # noqa: E402


def test_dem_problem():
    d = tempfile.mkdtemp(prefix="demchk_")
    f = os.path.join(d, "dem.tif")
    open(f, "wb").close()
    assert rp.dem_problem("") and rp.dem_problem("   ")
    assert "does not exist" in rp.dem_problem(os.path.join(d, "missing.tif"))
    assert "folder" in rp.dem_problem(d)
    assert rp.dem_problem(f) == ""
    assert rp.dem_problem(f + "|layername=dem") == ""


def test_page_uses_it():
    src = open(os.path.join(ROOT, "DamSafety_Setup", "plugin.py"), encoding="utf-8").read()
    assert "why = dem_problem(self.dem.text())" in src


if __name__ == "__main__":
    test_dem_problem(); test_page_uses_it(); print("OK")
