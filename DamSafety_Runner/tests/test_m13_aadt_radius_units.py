"""M13 regression: the ND 5.2.1.2 AADT join search radius on a feet-CRS dam folder.

algorithms_ndhazard._join_aadt starts from DEFAULT_MAX_M (30 m), or an AADT_JOIN_FT value
converted to metres, but aadt_join.match/apply measure distances in the grid's OWN coordinate
units. On a feet CRS those units are feet, so without scaling by metres-per-unit the 30 m radius
is treated as 30 feet (~9 m) and overtopped roads stop matching their traffic layer. The radius
handed down must be metres / metres-per-unit (the metres-per-unit idiom is the one used across
algorithms_tools / products / algorithms_hecras: gdal_io.linear_unit_m(crs_wkt) or 1.0)."""
import numpy as np

from nd_breach_runner.core import aadt_join as AJ
from nd_breach_runner.core import gdal_io
from nd_breach_runner.core import nd_hazard as nh
from nd_breach_runner.core.grid import Grid
from nd_breach_runner.tests.registry import test
from nd_breach_runner.tests.test_ndhazard_tool import _load_tool

FT = 0.3048


def _captured_radius(unit_m, aadt_join_ft=0):
    """Run _join_aadt with linear_unit_m stubbed to `unit_m` metres-per-unit and return the
    radius actually handed to aadt_join.apply (which measures that radius in grid units)."""
    an, fq, _ = _load_tool()
    seen = {}
    real_apply, real_read, real_unit = AJ.apply, AJ.read_layers, gdal_io.linear_unit_m
    try:
        AJ.read_layers = lambda *a, **k: [
            {"kind": "line", "parts": [[(0.0, 0.0), (400.0, 0.0)]], "aadt": 10113.0,
             "year": 2024, "name": "", "source": "HPMS"}]

        def _apply(roads, parts, traffic, max_m=AJ.DEFAULT_MAX_M, is_wet=None):
            seen["radius"] = max_m
            return []
        AJ.apply = _apply
        gdal_io.linear_unit_m = lambda wkt: unit_m
        a = an.NDHazardAlgorithm()
        a.initAlgorithm()
        grid = Grid(np.zeros((8, 8)), 0.0, 80.0, 10.0, crs_wkt="PROJCS_test")
        road = nh.Road("overtopped", 0.8)                 # aadt is None -> needs a join
        a._road_parts = {id(road): [[(0.0, 0.0), (400.0, 0.0)]]}
        a._is_wet = None
        ctx, fb = fq.QgsProcessingContext(), fq.QgsProcessingFeedback()
        params = {"AADT_JOIN_FT": aadt_join_ft, "AADT_LAYERS": "/tmp/fake_hpms.gpkg"}
        a._join_aadt(params, ctx, grid, None, [road], fb)
        return seen.get("radius")
    finally:
        AJ.apply, AJ.read_layers, gdal_io.linear_unit_m = real_apply, real_read, real_unit


@test
def m13_aadt_radius_scaled_to_grid_units_in_a_feet_crs():
    # metre CRS (1 m per unit): the 30 m default radius is 30 grid units
    rm = _captured_radius(1.0)
    assert rm is not None and abs(rm - AJ.DEFAULT_MAX_M) < 1e-6, rm
    # feet CRS (0.3048 m per unit): 30 m must become ~98.4 grid units (feet), not 30
    rf = _captured_radius(FT)
    assert rf is not None and abs(rf - AJ.DEFAULT_MAX_M / FT) < 1e-6, rf
    assert rf > 90.0, rf               # the bug left this at 30 (30 ft treated as the radius, ~9 m)
    # the physical radius is the same in both CRSs
    assert abs(rm * 1.0 - rf * FT) < 1e-6, (rm, rf)
    # an explicit AADT_JOIN_FT is likewise correct: 100 ft on a feet CRS is 100 grid units
    assert abs(_captured_radius(FT, aadt_join_ft=100.0) - 100.0) < 1e-6
