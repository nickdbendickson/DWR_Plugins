"""Nothing is written into the read-only ND_Dam_Suite_Pack: the Dams folder is refused
there by the run tools and the wizard, and the default stays under the home directory.

python3 -m nd_breach_runner.tests.run_tests   or   python3 -m pytest <this file>
(with the plugin importable as nd_breach_runner, like the other tests)."""
import os
import shutil
import tempfile

from nd_breach_runner import wizard_logic as wl
from nd_breach_runner.damsuite import suite_config as sc
from nd_breach_runner.tests.registry import test as register  # "test" would be collected by pytest

PACK = os.path.join(os.sep, "data", "ND_Dam_Suite_Pack")


@register
def test_in_suite_pack_spots_the_pack_and_its_children():
    for p in (PACK, PACK + os.sep, os.path.join(PACK, "dwr"), os.path.join(PACK, "Dams"),
              "C:\\ND\\ND_Dam_Suite_Pack\\Dams", "/x/nd_dam_suite_pack/out",
              "/x/ND_Dam_Suite_Pack_work", os.path.join(PACK, "dwr", "..", "Dams")):
        assert sc.in_suite_pack(p), p
    for p in ("", None, "/home/nick/DamSafety/Dams", "/data/ND_Dams", sc.default_dams_folder()):
        assert not sc.in_suite_pack(p), p


@register
def test_in_suite_pack_follows_a_symlink_into_the_pack():
    d = tempfile.mkdtemp()
    try:
        pack = os.path.join(d, "ND_Dam_Suite_Pack")
        os.makedirs(pack)
        link = os.path.join(d, "results")
        os.symlink(pack, link)
        assert sc.in_suite_pack(link)
    finally:
        shutil.rmtree(d, ignore_errors=True)


@register
def test_safe_dams_folder_never_returns_the_pack():
    assert sc.safe_dams_folder(PACK) == sc.default_dams_folder() or \
        not sc.in_suite_pack(sc.safe_dams_folder(PACK))
    assert not sc.in_suite_pack(sc.safe_dams_folder(os.path.join(PACK, "Dams")))
    assert sc.safe_dams_folder("/home/nick/Dams") == "/home/nick/Dams"
    assert not sc.in_suite_pack(sc.safe_dams_folder(""))
    assert not sc.in_suite_pack(sc.default_dams_folder())


@register
def test_wizard_refuses_a_dams_folder_in_the_pack():
    a = wl.default_answers(os.path.join(PACK, "Dams"))
    a.update(dem_id="dem_1", z_units="meters")
    errors, _w = wl.validate_page("terrain", a)
    assert any("ND_Dam_Suite_Pack" in e and "never written to" in e for e in errors), errors
    a["dams_folder"] = tempfile.gettempdir()
    errors, _w = wl.validate_page("terrain", a)
    assert not any("ND_Dam_Suite_Pack" in e for e in errors), errors


@register
def test_run_tools_refuse_a_dams_folder_in_the_pack():
    from nd_breach_runner.tests import fake_qgis
    fake_qgis.install()
    import importlib
    import nd_breach_runner.algorithms as algs
    importlib.reload(algs)
    try:
        algs._dams_parent(os.path.join(PACK, "Dams"))
    except fake_qgis.QgsProcessingException as e:
        assert "read-only" in str(e) and "never written to" in str(e)
    else:
        raise AssertionError("a Dams folder in the pack was accepted")
    assert algs._dams_parent("/home/nick/Dams") == "/home/nick/Dams"
    alg = algs.RunDamAlgorithm() if hasattr(algs, "RunDamAlgorithm") else None
    if alg is not None:
        alg.initAlgorithm()
        ctx = fake_qgis.QgsProcessingContext()
        try:
            alg.dam_folder({"DAMS_FOLDER": PACK}, ctx, "DWR-2801")
        except fake_qgis.QgsProcessingException:
            pass
        else:
            raise AssertionError("dam_folder() accepted the pack")
        assert alg.dam_folder({"DAMS_FOLDER": "/home/nick/Dams"}, ctx, "DWR-2801") == \
            os.path.join("/home/nick/Dams", "DWR-2801")
