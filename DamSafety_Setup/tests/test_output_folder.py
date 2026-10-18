"""The Setup wizard never sends results (or a DEM download) into the read-only
ND_Dam_Suite_Pack. Runs without QGIS:  python3 -m pytest DamSafety_Setup/tests"""
import ast
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from DamSafety_Setup import runner_params as rp  # noqa: E402
from DamSafety_Setup.damsuite import suite_config as sc  # noqa: E402

PACK = os.path.join(os.sep, "data", "ND_Dam_Suite_Pack")


def test_suite_data_folder_is_not_the_default_dams_folder(monkeypatch):
    monkeypatch.delenv("NDSUITE_DAMS_FOLDER", raising=False)
    p = rp.build_params({"folder": PACK, "dam_id": "DWR-2801"})
    assert not sc.in_suite_pack(p["DAMS_FOLDER"])
    assert p["DAMS_FOLDER"] == sc.default_dams_folder()
    assert p["DAMS_FOLDER"].startswith(os.path.expanduser("~"))


def test_a_dams_folder_in_the_pack_falls_back_to_a_safe_one(monkeypatch):
    monkeypatch.setenv("NDSUITE_DAMS_FOLDER", os.path.join(PACK, "Dams"))
    p = rp.build_params({"dams_folder": os.path.join(PACK, "Dams"), "folder": PACK})
    assert not sc.in_suite_pack(p["DAMS_FOLDER"])


def test_a_chosen_dams_folder_outside_the_pack_is_kept(monkeypatch):
    monkeypatch.delenv("NDSUITE_DAMS_FOLDER", raising=False)
    assert rp.build_params({"dams_folder": "/home/nick/Dams", "folder": PACK})[
        "DAMS_FOLDER"] == "/home/nick/Dams"
    monkeypatch.setenv("NDSUITE_DAMS_FOLDER", "/home/nick/SavedDams")
    assert rp.build_params({"folder": PACK})["DAMS_FOLDER"] == "/home/nick/SavedDams"


def test_the_damsuite_copies_are_identical():
    """suite_config.py is shared: every plugin's copy carries the same pack guard."""
    paths = [os.path.join(ROOT, p, "damsuite", "suite_config.py")
             for p in ("DamSafety_Setup", "DamSafety_Runner", "DamSafety_Screening",
                       "DamSafety_Hazard")]
    paths.append(os.path.join(ROOT, "DamSafety_Setup", "suite_config.py"))
    texts = {open(p).read() for p in paths if os.path.isfile(p)}
    assert len(texts) == 1 and "def in_suite_pack" in texts.pop()


def test_dem_download_does_not_write_into_the_data_folder():
    src = open(os.path.join(ROOT, "DamSafety_Setup", "plugin.py")).read()
    fn = [n for n in ast.walk(ast.parse(src))
          if isinstance(n, ast.FunctionDef) and n.name == "_download"][0]
    body = ast.get_source_segment(src, fn)
    # 1.0.8: the folder is DemPage._dem_folder() (the page also shows it before the download)
    assert "out = self._dem_folder()" in body
    fn = [n for n in ast.walk(ast.parse(src))
          if isinstance(n, ast.FunctionDef) and n.name == "_dem_folder"][0]
    folder = ast.get_source_segment(src, fn)
    assert 'os.path.join(safe_dams_folder(), "terrain")' in folder
    for part in (body, folder):
        assert "page(0).path" not in part and "FOLDER_PAGE).path" not in part
