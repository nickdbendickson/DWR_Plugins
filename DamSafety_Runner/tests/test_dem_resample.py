"""DEM resampling onto the model grid: explicit parameter > NDBREACH_DEM_RESAMPLE > default,
validated against GDAL's warp names, offered as a tool parameter and a CLI key, and recorded
in run_manifest.json (extra.dem_resample)."""
import os
import types

from nd_breach_runner.core import gdal_io, workflow
from nd_breach_runner.tests.registry import test as register  # "test" would be collected by pytest
from nd_breach_runner.tests.test_cli import cli


class _Env:
    """NDBREACH_DEM_RESAMPLE set (or removed, value None) for a with-block."""
    def __init__(self, value):
        self.value = value

    def __enter__(self):
        self.saved = os.environ.get(gdal_io.ENV_RESAMPLE)
        os.environ.pop(gdal_io.ENV_RESAMPLE, None)
        if self.value is not None:
            os.environ[gdal_io.ENV_RESAMPLE] = self.value

    def __exit__(self, *a):
        os.environ.pop(gdal_io.ENV_RESAMPLE, None)
        if self.saved is not None:
            os.environ[gdal_io.ENV_RESAMPLE] = self.saved


def _terrain(resample=None):
    """A GdalTerrain without GDAL: the raster open and z-units lookup are stubbed."""
    saved = (gdal_io._gdal, gdal_io.GS.open_raster, gdal_io.z_to_m_factor)
    band = types.SimpleNamespace(GetNoDataValue=lambda: None)
    gdal_io._gdal = lambda: (None, None)
    gdal_io.GS.open_raster = lambda p: types.SimpleNamespace(GetRasterBand=lambda i: band)
    gdal_io.z_to_m_factor = lambda p, c: (1.0, "metres")
    try:
        return gdal_io.GdalTerrain("dem.tif", "EPSG:26914", resample=resample)
    finally:
        gdal_io._gdal, gdal_io.GS.open_raster, gdal_io.z_to_m_factor = saved


@register
def dem_resample_precedence_param_over_env_over_default():
    with _Env(None):
        assert gdal_io.resolve_resample() == ("bilinear", "default")
        t = _terrain()
        assert (t.resample, t.resample_source) == ("bilinear", "default")
    with _Env("average"):
        assert gdal_io.resolve_resample() == ("average", "environment")
        assert gdal_io.resolve_resample("") == ("average", "environment")     # blank = not set
        t = _terrain()
        assert (t.resample, t.resample_source) == ("average", "environment")
        t = _terrain("near")                                                  # explicit wins
        assert (t.resample, t.resample_source) == ("near", "parameter")
    with _Env("  "):
        assert gdal_io.resolve_resample() == ("bilinear", "default")
    assert gdal_io.resolve_resample(None, {"NDBREACH_DEM_RESAMPLE": "Average"}) == ("average", "environment")


@register
def dem_resample_rejects_names_gdal_does_not_have():
    for bad in ("bicubic", "avg", "nearest neighbour"):
        try:
            gdal_io.check_resample(bad)
        except ValueError as e:
            assert "average" in str(e) and repr(bad) in str(e)
        else:
            raise AssertionError(bad)
    with _Env("smooth"):
        try:
            _terrain()
        except ValueError as e:
            assert gdal_io.ENV_RESAMPLE in str(e)
        else:
            raise AssertionError("a bad environment value must not be ignored")
    assert gdal_io.check_resample(" Average ") == "average"
    assert gdal_io.DEFAULT_RESAMPLE in gdal_io.RESAMPLE_METHODS and "average" in gdal_io.RESAMPLE_METHODS


@register
def dem_resample_recorded_in_the_manifest_extra():
    t = _terrain("average")
    rs = workflow._dem_resample_of(t)
    assert rs == {"method": "average", "source": "parameter"}
    wrapped = types.SimpleNamespace(base=t)                    # e.g. ConditionedTerrain
    assert workflow._dem_resample_of(wrapped) == rs
    assert workflow._dem_resample_of(types.SimpleNamespace()) is None   # ASCII / fake terrain
    report = {"settings": {}, "dem_resample": rs}
    extra = workflow._manifest_extra(report, {"final_model": "lisflood"}, [])
    assert extra["dem_resample"] == {"method": "average", "source": "parameter"}
    assert "dem_resample" not in workflow._manifest_extra({"settings": {}}, {"final_model": "x"}, [])


@register
def dem_resample_tool_parameter_maps_to_gdal_names():
    from nd_breach_runner import algorithms as algs
    assert algs.dem_resample_param(0) is None and algs.dem_resample_param(None) is None
    for i, name in enumerate(gdal_io.RESAMPLE_METHODS, start=1):
        assert algs.dem_resample_param(i) == name
    assert algs.dem_resample_param(len(gdal_io.RESAMPLE_METHODS) + 1) is None
    src = open(algs.__file__, encoding="utf-8").read()
    assert '"DEM_RESAMPLE"' in src and "resample_source" in src


@register
def dem_resample_cli_key_is_validated_and_in_the_template():
    c = cli()
    assert c.SECTIONS["dem"]["resample"] == ("enum", gdal_io.RESAMPLE_METHODS)
    assert "resample" in c.TEMPLATE["dem"] and c.TEMPLATE["dem"]["resample"] is None
    assert c._check_value("dem.resample", "average", c.SECTIONS["dem"]["resample"]) == "average"
    try:
        c._check_value("dem.resample", "bicubic", c.SECTIONS["dem"]["resample"])
    except c.CliError as e:
        assert "average" in str(e)
    else:
        raise AssertionError("bicubic accepted")
