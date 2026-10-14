"""The Setup wizard's elevation unit reaches the Runner unchanged (metres stay metres).

Runs without QGIS:  python3 -m pytest DamSafety_Setup/tests   (from the repository root)."""
import ast
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from DamSafety_Setup import runner_params as rp  # noqa: E402
from DamSafety_Runner.core import gdal_io  # noqa: E402
from DamSafety_Runner.core.units import FT, US_FT  # noqa: E402


def _runner_zunits():
    """algorithms.ZUNITS read from the source (algorithms.py needs qgis.core to import)."""
    tree = ast.parse(open(os.path.join(ROOT, "DamSafety_Runner", "algorithms.py")).read())
    for node in tree.body:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "ZUNITS":
            return [t[0] for t in ast.literal_eval(node.value)]
    raise AssertionError("ZUNITS not found in DamSafety_Runner/algorithms.py")


def _setup_page_items():
    """(label, data) of the Setup DEM page's elevation-unit combo, read from plugin.py."""
    tree = ast.parse(open(os.path.join(ROOT, "DamSafety_Setup", "plugin.py")).read())
    items = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "addItem"
                and isinstance(node.func.value, ast.Attribute)
                and node.func.value.attr == "zunit"):
            items.append((ast.literal_eval(node.args[0]), ast.literal_eval(node.args[1])))
    assert items, "no zunit.addItem calls in DamSafety_Setup/plugin.py"
    return items


def test_setup_unit_order_matches_the_runner():
    assert tuple(rp.RUNNER_Z_IDS) == tuple(gdal_io.Z_CHOICES) == tuple(_runner_zunits())


def test_metres_is_the_runners_meters_not_us_survey_feet():
    idx = rp.dem_z_index("meters")
    assert _runner_zunits()[idx] == "meters"
    assert _runner_zunits()[idx] != "us_feet"
    assert idx == 3


def test_metres_stay_metres_end_to_end():
    # Setup answer -> rundam DEM_Z -> Runner id -> factor used on the DEM
    p = rp.build_params({"dem_z": "meters", "dam_id": "DWR-2801"})
    zc = _runner_zunits()[p["DEM_Z"]]
    factor, note = gdal_io.z_to_m_factor("unused.tif", zc)
    assert factor == 1.0 and note.startswith("metres")


def test_feet_stays_international_feet():
    zc = _runner_zunits()[rp.build_params({"dem_z": "feet"})["DEM_Z"]]
    assert gdal_io.z_to_m_factor("unused.tif", zc)[0] == FT
    assert FT != US_FT


def test_every_setup_choice_maps_to_the_same_runner_unit():
    for label, data in _setup_page_items():
        zc = _runner_zunits()[rp.dem_z_index(data)]
        if label.lower().startswith("metre") or label.lower().startswith("meter"):
            assert zc == "meters", (label, data, zc)
        elif label.lower().startswith("feet"):
            assert zc == "feet", (label, data, zc)
        else:
            raise AssertionError("unexpected unit item {!r}".format(label))


def test_spellings_and_unknown_values():
    for s in ("meters", "metres", "Metres", " m ", "meter"):
        assert rp.runner_z_id(s) == "meters", s
    assert rp.runner_z_id("us_feet") == "us_feet"
    # An old build's numeric item data (2 meant 'Metres' there) is not trusted:
    # 'auto' makes the Runner ask for the unit instead of reading US survey feet.
    for s in (None, "", 2, "2", "furlongs"):
        assert rp.runner_z_id(s) == "auto", s
    assert rp.build_params({})["DEM_Z"] == 0
