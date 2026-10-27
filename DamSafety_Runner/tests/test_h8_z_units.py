"""H8 regression: a metre DEM carried in a feet CRS must not be silently scaled as feet.

3.10.0 made z_to_m_factor cross-check the DEM's median elevation against North Dakota's ground
range before trusting a feet CRS unit; 3.10.89 goes further: 'auto' is the run's rule
(gdal_io.resolve_dem_z), so a DEM with no unit recorded is refused, naming the setting, whatever
its CRS and median - the culvert pre-check, the CLI and the HEC-RAS terrain read guessed here
while the run itself refused. A 3DEP metre DEM warped into an ND State Plane *feet* CRS keeps
metre values; treating them as feet multiplies elevations by 0.3048 (~3.28x too low)."""
import types

import numpy as np

from nd_breach_runner.core import gdal_io
from nd_breach_runner.core.units import FT
from nd_breach_runner.tests.registry import test as register   # "test" would be collected by pytest


def _fake_osr(linear_units):
    """An osr stand-in whose SpatialReference() reports a projected CRS whose linear unit is
    `linear_units` metres-per-unit (FT for a feet CRS); never compound."""
    class _SRS:
        def ImportFromWkt(self, wkt):
            return 0

        def IsCompound(self):
            return False

        def IsProjected(self):
            return True

        def GetLinearUnits(self):
            return linear_units

        def GetTargetLinearUnits(self, key):
            return linear_units

    return types.SimpleNamespace(SpatialReference=_SRS)


def _fake_dem(median, nx=300, ny=300, nodata=-9999.0, wkt="PROJCS[\"feet\"]"):
    """A raster stand-in: a band of constant `median`, a projection WKT, and no band unit type
    (so the decision falls through to the CRS + elevation cross-check)."""
    class _Band:
        def GetUnitType(self):
            return ""

        def GetNoDataValue(self):
            return nodata

        def ReadAsArray(self, x0, y0, w, h, buf_xsize=None, buf_ysize=None):
            return np.full((buf_ysize or h, buf_xsize or w), float(median), dtype=np.float64)

    band = _Band()
    return types.SimpleNamespace(GetRasterBand=lambda i: band, RasterXSize=nx,
                                 RasterYSize=ny, GetProjection=lambda: wkt)


class _Stub:
    """z_to_m_factor against the fakes above for a with-block (GDAL is not installed)."""
    def __init__(self, median, linear_units=FT):
        self.median = median
        self.linear_units = linear_units

    def __enter__(self):
        self.saved = (gdal_io._gdal, gdal_io.GS.open_raster)
        ds = _fake_dem(self.median)
        gdal_io._gdal = lambda: (None, _fake_osr(self.linear_units))
        gdal_io.GS.open_raster = lambda p: ds
        return self

    def __exit__(self, *a):
        gdal_io._gdal, gdal_io.GS.open_raster = self.saved


def _refused(path, median):
    with _Stub(median=median, linear_units=FT):
        try:
            got = gdal_io.z_to_m_factor(path, gdal_io.Z_AUTO)
        except ValueError as e:
            msg = str(e)
            assert "'DEM elevation units'" in msg and "no elevation unit is recorded" in msg, msg
            return msg
    raise AssertionError("{}: no unit recorded, but auto gave {}".format(path, got))


@register
def z_units_metre_dem_in_a_feet_crs_is_not_scaled():
    # median ~520 m: fits ND ground as metres, but 520*0.3048 = 158 m is below it as feet.
    # Before the H8 fix this returned FT (0.3048); 3.10.0-3.10.88 metres with a 'check' note.
    _refused("metre_in_feet.tif", 520.0)


@register
def z_units_genuine_feet_dem_in_a_feet_crs_is_not_guessed_either():
    # median ~3000 ft (=914 m): 3.10.0-3.10.88 kept it as feet from the CRS; now the unit must
    # be recorded (band unit, vertical CRS, Download DEM record) or chosen
    _refused("feet.tif", 3000.0)


@register
def z_units_ambiguous_elevations_are_refused():
    # median ~700 fits ND ground both ways; the CRS unit (feet) decided before
    _refused("ambiguous.tif", 700.0)


@register
def z_units_explicit_choice_needs_no_file():
    # an explicit unit is used as given (the run checks it against the record in resolve_dem_z)
    assert gdal_io.z_to_m_factor("missing.tif", gdal_io.Z_FEET) == (FT, "feet (chosen)")
    assert gdal_io.z_to_m_factor("missing.tif", gdal_io.Z_METERS)[0] == 1.0
