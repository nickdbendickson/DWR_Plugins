"""The wizard's 'DEM from dam folder' finds only the selected dam's rasters. A '/' (root,
slash-only, trailing-slash) Dams folder no longer walks every dam and takes the first DEM."""
import os
import shutil
import tempfile

from nd_breach_runner import wizard_logic as wl
from nd_breach_runner.tests.registry import test as register  # "test" would be collected by pytest


def _touch(*parts):
    p = os.path.join(*parts)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    open(p, "wb").close()
    return p


def _dams():
    d = tempfile.mkdtemp()
    # Absaraka DWR-2801 and DWR-3161 side by side; 'AAA' sorts first, as in the bug.
    other0 = _touch(d, "AAA-0001", "terrain", "AAA-0001_10m_5070.tif")
    other = _touch(d, "DWR-3161", "terrain", "DWR-3161_10m_5070.tif")
    mine = _touch(d, "DWR-2801", "terrain", "DWR-2801_10m_5070.tif")
    shared_mine = _touch(d, "terrain", "DWR-2801_1m_5070.tif")
    shared_other = _touch(d, "terrain", "DWR-3161_1m_5070.tif")
    return d, mine, shared_mine, (other0, other, shared_other)


@register
def test_dem_lookup_only_returns_the_selected_dams_rasters():
    d, mine, shared_mine, others = _dams()
    try:
        for folder in (d, d + "/", d + "//", d + os.sep + "." + os.sep):
            hits = wl.dam_dem_candidates(folder, "DWR-2801")
            assert hits and os.path.samefile(hits[0], mine), (folder, hits)
            assert any(os.path.samefile(h, shared_mine) for h in hits)
            for h in hits:
                for o in others:
                    assert not os.path.samefile(h, o), (folder, h)
        hits = wl.dam_dem_candidates(d, "DWR-3161")
        assert os.path.basename(hits[0]) == "DWR-3161_10m_5070.tif"
        assert all("2801" not in h for h in hits)
        assert wl.dam_dem_candidates(d, "DWR-9999") == []       # no folder: nothing, not a neighbour
        assert wl.dam_dem_candidates(d, "") == []
        assert wl.dam_dem_candidates(d, None) == []
    finally:
        shutil.rmtree(d, ignore_errors=True)


@register
def test_dem_lookup_rejects_root_and_slash_only_paths():
    for bad in ("/", "//", "///", "\\", "\\\\", "C:\\", "C:/", "c:", " / ", "", None):
        assert wl.bad_dams_folder(bad), bad
        assert wl.dam_dem_candidates(bad, "DWR-2801") == [], bad
    assert wl.bad_dams_folder(os.path.abspath(os.sep)) != ""
    assert wl.bad_dams_folder(tempfile.gettempdir()) == ""
    assert wl.bad_dams_folder(tempfile.gettempdir() + "/") == ""


@register
def test_terrain_page_refuses_a_root_dams_folder():
    a = wl.default_answers("/")
    a.update(dem_id="dem_1", z_units="meters")
    errors, _w = wl.validate_page("terrain", a)
    assert any("root" in e for e in errors), errors
    a["dams_folder"] = tempfile.gettempdir()
    errors, _w = wl.validate_page("terrain", a)
    assert not any("root" in e for e in errors), errors


@register
def test_wizard_uses_the_scoped_lookup():
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "wizard.py")).read()
    assert "wl.dam_dem_candidates(" in src
    assert "os.walk(folder)" not in src
