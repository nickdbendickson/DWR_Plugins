"""H8 regression: a metre DEM carried in a feet CRS must not be silently scaled as feet.

z_to_m_factor cross-checks the DEM's median elevation against North Dakota's plausible ground
range before trusting the CRS unit (core.gdal_io, ported from nd_breach_runner's 3.10.0 check).
A 3DEP metre DEM warped into an ND State Plane *feet* CRS keeps metre values; treating them as
feet multiplies elevations by 0.3048 (~3.28x too low) and produces a grossly wrong flood."""
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


@register
def z_units_metre_dem_in_a_feet_crs_is_not_scaled():
    # median ~520 m: fits ND ground as metres, but 520*0.3048 = 158 m is below it as feet.
    # Before the H8 fix this returned FT (0.3048) and under-reported elevations ~3.28x.
    with _Stub(median=520.0, linear_units=FT):
        factor, note = gdal_io.z_to_m_factor("metre_in_feet.tif", gdal_io.Z_AUTO)
    assert factor == 1.0, (factor, note)                        # metres: not scaled
    assert "metres" in note and "below North Dakota" in note, note


@register
def z_units_genuine_feet_dem_in_a_feet_crs_stays_feet():
    # median ~3000 ft (=914 m): fits ND ground only as feet, not as metres -> kept as feet.
    with _Stub(median=3000.0, linear_units=FT):
        factor, note = gdal_io.z_to_m_factor("feet.tif", gdal_io.Z_AUTO)
    assert abs(factor - FT) < 1e-12, (factor, note)
    assert "feet" in note and "only as feet" in note, note


@register
def z_units_ambiguous_elevations_fall_back_to_the_crs_unit():
    # median ~700 fits ND ground both as metres and as feet-scaled; the CRS unit (feet) decides.
    with _Stub(median=700.0, linear_units=FT):
        factor, note = gdal_io.z_to_m_factor("ambiguous.tif", gdal_io.Z_AUTO)
    assert abs(factor - FT) < 1e-12, (factor, note)
    assert "fit both feet and metres" in note, note
