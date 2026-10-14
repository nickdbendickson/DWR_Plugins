"""Preflight, 3DEP tiles, pond storage check, and culvert guesses."""
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "nd_breach_runner"))

from nd_dam_screening.core import solve_gate as sg
from nd_dam_screening.core import terrain
from nd_breach_runner.core import culvert_guess as cg


def test_ten_mile_tile_splits():
    # 10 mile buffer is a 20 mile box. At 10 m that is about 3,219 pixels.
    side = 20 * 1609.344
    tiles = list(terrain.tile_extents(0, 0, side, side, 10))
    assert len(tiles) > 1, len(tiles)
    for ext in tiles:
        w = (ext[2] - ext[0]) / 10
        h = (ext[3] - ext[1]) / 10
        assert w <= sg.TILE_PX + 1e-6
        assert h <= sg.TILE_PX + 1e-6


def test_one_crs_and_hydrograph():
    assert sg.one_crs(["EPSG:26914", "EPSG:26914"])["status"] == "ok"
    assert sg.one_crs(["EPSG:26914", "EPSG:5070"])["status"] == "fail"
    bad = sg.hydro_stats([0, 100], [0, 0])
    assert bad["status"] == "fail"
    line = sg.hydro_stats([0, 3600], [0, 10])
    assert line["status"] == "warn" and "straight line" in line["detail"]
    curve = sg.hydro_stats([0, 1800, 3600], [0, 20, 1])
    assert curve["status"] == "ok" and "cfs" in curve["detail"]


def test_structures_extent_is_not_called_a_crs_problem():
    hit = sg.structures_in_extent([(0, 0), (10, 10)], (100, 100, 200, 200))
    assert hit["status"] == "fail"
    assert "extent mismatch" in hit["detail"]
    assert "missing CRS" in hit["detail"]


def test_storage_check_keeps_the_dem_curve():
    note = sg.storage_check(1000 * sg.ACFT, 100 * sg.ACFT)
    assert "differs" in note and "not a line" in note
    agree = sg.storage_check(100 * sg.ACFT, 110 * sg.ACFT)
    assert "agrees" in agree


def test_culvert_guess_is_labeled():
    road = [(0, 0), (10, 0)]
    channel = [(5, -5), (5, 5)]
    rows = cg.guess_crossings([road], [channel])
    assert len(rows) == 1
    assert rows[0]["source"] == "guess"
    assert rows[0]["surveyed"] is False
    assert rows[0]["name"].startswith("guess:")
    inv = cg.from_inventory([{"name": "C-1", "line": [(0, 0), (1, 0)]}])
    assert inv[0]["surveyed"] is True and inv[0]["source"] == "inventory"


def test_lisflood_one_second():
    exe = "/usr/local/bin/lisflood"
    if not os.path.isfile(exe):
        return
    hit = sg.lisflood_smoke(exe)
    assert hit["status"] == "ok", hit


def test_guess_is_not_surveyed_on_a_structure():
    import numpy as np
    from nd_breach_runner.core.structures import Structure
    s = Structure("culvert", np.array([[0.0, 0.0], [1.0, 0.0]]), name="guess: road 1",
                  source="guess", surveyed=False)
    d = s.to_dict()
    assert d["surveyed"] is False and d["source"] == "guess"
    assert d["name"].startswith("guess:")


def test_lisflood_skip_and_quarters_and_candidates():
    os.environ["ND_DAM_PRE_RUN_SMOKE"] = "0"
    try:
        hit = sg.lisflood_smoke("/no/such/lisflood")
        assert hit["status"] == "ok" and "skipped" in hit["detail"]
    finally:
        os.environ.pop("ND_DAM_PRE_RUN_SMOKE", None)
    qs = terrain._quarters((0, 0, 1000, 800))
    assert len(qs) == 4 and qs[0] == (0, 0, 500, 400)
    import tempfile
    rows = cg.guess_crossings([[(0, 0), (10, 0)]], [[(5, -5), (5, 5)]])
    folder = tempfile.mkdtemp()
    path = cg.write_candidates_csv(os.path.join(folder, "culvert_candidates.csv"), rows)
    text = open(path, encoding="utf-8").read()
    assert text.startswith("name,kind") and "guess:" in text and ",no," in text


if __name__ == "__main__":
    test_ten_mile_tile_splits()
    test_one_crs_and_hydrograph()
    test_structures_extent_is_not_called_a_crs_problem()
    test_storage_check_keeps_the_dem_curve()
    test_culvert_guess_is_labeled()
    test_lisflood_one_second()
    test_guess_is_not_surveyed_on_a_structure()
    test_lisflood_skip_and_quarters_and_candidates()
    print("ok")
