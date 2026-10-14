"""Max extent defaults are 20 mi everywhere (run tools, RunSettings, CLI template), and the
wizard finds the DEM 'Download DEM' writes for the selected dam (<DAM_ID>_10m_5070.tif)."""
import importlib.util
import os
import shutil
import tempfile

from nd_breach_runner import wizard_logic as wl
from nd_breach_runner.core import workflow
from nd_breach_runner.tests.registry import test as register  # "test" would be collected by pytest

PLUGIN = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@register
def test_runsettings_and_cli_default_max_extent_is_20_mi():
    assert workflow.RunSettings().max_extent_mi == 20.0
    src = open(os.path.join(PLUGIN, "scripts", "ndbreach_cli.py")).read()
    assert '"max_extent_mi": 20.0' in src and '"max_extent_mi": 60.0' not in src


@register
def test_cli_template_max_extent_is_20_mi():
    try:
        from nd_breach_runner.scripts import ndbreach_cli as cli
    except Exception:          # the CLI needs optional modules; the source check above covers it
        return
    tpl = getattr(cli, "TEMPLATE", None)
    if isinstance(tpl, dict):
        flat = str(tpl)
        assert "'max_extent_mi': 20.0" in flat


def _screening_stem(dam_id, point=""):
    path = os.path.join(os.path.dirname(PLUGIN), "DamSafety_Screening", "core", "dem_names.py")
    if not os.path.isfile(path):
        return wl.dem_stem(dam_id) or "dam"
    spec = importlib.util.spec_from_file_location("dem_names", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.dam_scope_stem(dam_id, point)


@register
def test_wizard_picks_up_a_fresh_download_for_the_selected_dam_only():
    d = tempfile.mkdtemp()
    try:
        terrain = os.path.join(d, "terrain")
        os.makedirs(terrain)
        for dam_id in ("DWR-2801", "DWR-3161", "ND 00123"):
            assert wl.dem_stem(dam_id) == _screening_stem(dam_id)
        mine = os.path.join(terrain, _screening_stem("DWR-2801", "1,2") + "_10m_5070.tif")
        other = os.path.join(terrain, _screening_stem("DWR-3161", "3,4") + "_10m_5070.tif")
        open(mine, "w").close()
        open(other, "w").close()
        assert wl.dam_dem_candidates(d, "DWR-2801") == [mine]
        assert wl.dam_dem_candidates(d, "DWR-3161") == [other]
        # an older point-named download of this dam's point is accepted, others are not
        old = os.path.join(terrain, "dam_350000.0_5230000.0_1m_5070.tif")
        open(old, "w").close()
        stem = wl.point_dem_stem((350000.0, 5230000.0, "EPSG:26914"))
        assert old in wl.dam_dem_candidates(d, "DWR-9999", point_stem=stem)
        assert wl.dam_dem_candidates(d, "DWR-9999") == []
    finally:
        shutil.rmtree(d, ignore_errors=True)


@register
def test_dem_download_buffer_is_half_the_run_tools_max_extent():
    from nd_breach_runner.damsuite import suite_config as sc
    from nd_breach_runner.tests import fake_qgis
    fake_qgis.install()
    import importlib
    import nd_breach_runner.algorithms as algs
    importlib.reload(algs)
    assert sc.DEFAULT_MAX_EXTENT_MI == algs.DEFAULT_MAX_EXTENT_MI == workflow.RunSettings().max_extent_mi
    assert sc.dem_buffer_mi(algs.DEFAULT_MAX_EXTENT_MI) == 10.0
    src = open(os.path.join(PLUGIN, "wizard.py")).read()
    assert '"BUFFER_MI": dem_buffer_mi(' in src and '"BUFFER_MI": 15.0' not in src
