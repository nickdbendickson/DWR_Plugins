"""Setup follow-ups: the Run page states the real grid default (10 m, not '100 m'), and a
fresh 'Download DEM' (<DAM_ID>_10m_5070.tif) is picked up for the selected dam only."""
import ast
import os
import sys
import importlib.util

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from DamSafety_Setup import runner_params as rp  # noqa: E402


def _screening_names():
    path = os.path.join(ROOT, "DamSafety_Screening", "core", "dem_names.py")
    spec = importlib.util.spec_from_file_location("dem_names", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run_page():
    src = open(os.path.join(ROOT, "DamSafety_Setup", "plugin.py")).read()
    cls = [n for n in ast.walk(ast.parse(src)) if isinstance(n, ast.ClassDef) and n.name == "RunPage"][0]
    return ast.get_source_segment(src, cls)


def test_run_page_text_matches_the_grid_default():
    body = _run_page()
    assert "100 m" not in body
    assert "self.cell.setValue(10)" in body
    assert "{:g} m grid" in body and "self.cell.value()" in body


def test_download_and_lookup_use_the_same_name():
    dn = _screening_names()
    for dam_id in ("DWR-2801", "DWR-3161", "ND 00123", "A/B"):
        assert rp.dem_stem(dam_id) == dn.dem_stem(dam_id)
    assert dn.dam_scope_stem("DWR-2801", "-97.4,46.9") == "DWR-2801"
    assert dn.dam_scope_stem("", "-97.4,46.9") == "dam_-97.4_46.9"    # unchanged without an ID


def test_fresh_download_is_found_for_this_dam_only(tmp_path):
    out = str(tmp_path)
    dn = _screening_names()
    other = os.path.join(out, dn.dam_scope_stem("DWR-3161", "") + "_10m_5070.tif")
    open(other, "w").close()
    assert rp.downloaded_dem(out, "DWR-2801", "-97.4,46.9") == ""
    mine = os.path.join(out, dn.dam_scope_stem("DWR-2801", "-97.4,46.9") + "_10m_5070.tif")
    open(mine, "w").close()
    assert rp.downloaded_dem(out, "DWR-2801", "-97.4,46.9") == mine
    # an older download named by this dam's own point is also accepted
    old = os.path.join(out, "dam_-98.1_47.2_1m_5070.tif")
    open(old, "w").close()
    assert rp.downloaded_dem(out, "DWR-9999", "-98.1, 47.2") == old
    assert rp.downloaded_dem(out, "", "") == ""


def test_both_wizards_pass_the_dam_id_to_the_download():
    setup = open(os.path.join(ROOT, "DamSafety_Setup", "plugin.py")).read()
    runner = open(os.path.join(ROOT, "DamSafety_Runner", "wizard.py")).read()
    assert '"DAM_ID": str(dam.get("id")' in setup
    assert '"DAM_ID": str((a.get("dam") or {}).get("DAM_ID")' in runner
    shot = open(os.path.join(ROOT, "DamSafety_Screening", "algorithms", "download_dem.py")).read()
    assert '"DAM_ID"' in shot and "dn.dam_scope_stem" in shot


# ---- DEM download buffer = half the max extent (one helper in damsuite/suite_config)
from DamSafety_Setup.damsuite import suite_config as sc  # noqa: E402


def test_dem_buffer_is_half_the_max_extent():
    assert sc.DEFAULT_MAX_EXTENT_MI == rp.DEFAULT_MAX_EXTENT_MI == 20.0
    assert sc.dem_buffer_mi() == 10.0
    assert sc.dem_buffer_mi(20.0) == 10.0
    assert sc.dem_buffer_mi(35) == 17.5
    for bad in (None, "", "x", 0, -5):
        assert sc.dem_buffer_mi(bad) == 10.0, bad


def test_both_wizards_use_the_shared_buffer_helper():
    setup = open(os.path.join(ROOT, "DamSafety_Setup", "plugin.py")).read()
    runner = open(os.path.join(ROOT, "DamSafety_Runner", "wizard.py")).read()
    assert '"BUFFER_MI": dem_buffer_mi(self.wiz.page(RUN_PAGE).max_extent.value())' in setup
    assert '"BUFFER_MI": dem_buffer_mi(a.get("max_extent_mi"))' in runner
    assert '"BUFFER_MI": 15.0' not in setup and '"BUFFER_MI": 15.0' not in runner
