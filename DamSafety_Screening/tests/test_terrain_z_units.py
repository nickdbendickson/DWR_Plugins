"""H9: the local-LiDAR terrain path (algorithms/terrain_cache.py) must convert feet-tagged DEM
tiles to metres and warp tiles in a second CRS to EPSG:5070 before mosaicking. A plain
gdal.BuildVRT does neither, so a feet DEM read straight as metres is ~3.28x too high (wrong
storage / flood). These tests check the scale-decision helpers and the feet-scaling branch; the
real warp needs real GDAL (the mock has no Translate), so that branch is checked with doubles.

python3 DamSafety_Screening/tests/test_terrain_z_units.py   or   python3 -m pytest <this file>"""
import os
import sys
import tempfile

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "qgis_mock"))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))          # parent of the plugin package

import nd_dam_screening  # noqa: E402,F401
from nd_dam_screening.algorithms import terrain_cache as tc  # noqa: E402
from osgeo import gdal  # noqa: E402  (the mock double)

FT = 0.3048
US_FT = 1200.0 / 3937.0


def test_z_unit_scale_table():
    """The sibling nd_dam_screening table: metres/feet/US-survey-foot/untagged/unknown."""
    table = (("m", ("m", 1.0)), ("metre", ("m", 1.0)), ("Meters", ("m", 1.0)),
             ("", ("", 1.0)), (None, ("", 1.0)),
             ("ft", ("ft", FT)), ("foot", ("ft", FT)), ("Feet", ("ft", FT)), ("ft_intl", ("ft", FT)),
             ("us-ft", ("us-ft", US_FT)), ("ftUS", ("us-ft", US_FT)),
             ("US survey foot", ("us-ft", US_FT)), ("foot_us", ("us-ft", US_FT)), ("US_ft", ("us-ft", US_FT)),
             ("cm", ("other", 1.0)))
    for unit, (want_kind, want_scale) in table:
        scale, kind = tc.z_unit_scale(unit)
        assert kind == want_kind and abs(scale - want_scale) < 1e-12, (unit, scale, kind)


def test_feet_elevation_converts_by_0_3048():
    """A known elevation: 1000 ft -> 304.8 m; 3937 US survey ft -> 1200.0 m; metres unchanged."""
    assert abs(1000.0 * tc.z_unit_scale("ft")[0] - 304.8) < 1e-9
    assert abs(3937.0 * tc.z_unit_scale("US survey foot")[0] - 1200.0) < 1e-9
    assert tc.z_unit_scale("m")[0] == 1.0 and tc.z_unit_scale("")[0] == 1.0


class _FakeBand:
    def __init__(self, unit): self._unit = unit; self.set_unit = None
    def GetUnitType(self): return self._unit
    def SetUnitType(self, u): self.set_unit = u


class _FakeDS:
    def __init__(self, unit="", proj="EPSG:5070", gt=(0, 10, 0, 0, 0, -10)):
        self._band = _FakeBand(unit); self._proj = proj; self._gt = gt
    def GetRasterBand(self, i): return self._band
    def GetProjection(self): return self._proj
    def GetGeoTransform(self): return self._gt


def test_dem_z_scale_reads_the_band_unit(monkeypatch=None):
    """dem_z_scale reports the band's tag: a feet tile -> 0.3048, an untagged tile -> metres."""
    units = {"a.tif": "feet", "b.tif": "", "c.tif": "US survey foot"}
    old = tc.cm.gdal_open
    tc.cm.gdal_open = lambda p: _FakeDS(units[os.path.basename(p)])
    try:
        assert tc.dem_z_scale("/x/a.tif") == (FT, "ft", "feet")
        assert tc.dem_z_scale("/x/b.tif") == (1.0, "", "")
        assert tc.dem_z_scale("/x/c.tif")[:2] == (US_FT, "us-ft")
        # 1000 ft read with this scale is 304.8 m (the elevation GDAL would write)
        assert abs(1000.0 * tc.dem_z_scale("/x/a.tif")[0] - 304.8) < 1e-9
    finally:
        tc.cm.gdal_open = old


def _tile(path, arr, x0=0.0, y1=0.0, cell=10.0):
    ds = gdal.GetDriverByName("GTiff").Create(path, arr.shape[1], arr.shape[0], 1, gdal.GDT_Float32)
    ds.SetGeoTransform((x0, cell, 0, y1, 0, -cell))
    ds.SetProjection("EPSG:5070")
    ds.GetRasterBand(1).WriteArray(np.asarray(arr, "float32"))
    ds.FlushCache()
    return path


def test_untagged_single_crs_uses_the_plain_fast_path():
    """All-metres, one CRS: a plain VRT, no per-tile work dir, but the untagged WARNING is logged
    (a feet DEM with no tag would be read 3.28x too high)."""
    work = tempfile.mkdtemp(prefix="zunits_")
    t1 = _tile(os.path.join(work, "t1.tif"), np.full((20, 20), 100.0), x0=0.0, y1=200.0)
    t2 = _tile(os.path.join(work, "t2.tif"), np.full((20, 20), 110.0), x0=200.0, y1=200.0)
    vrt = os.path.join(work, "_m.vrt")
    msgs = []
    out = tc.local_mosaic([t1, t2], vrt, msgs.append)
    assert out == vrt and os.path.exists(vrt)
    assert not os.path.isdir(os.path.splitext(vrt)[0] + "_tiles")        # fast path: no conversion
    text = "\n".join(msgs)
    assert "WARNING" in text and "METRES" in text


def test_feet_tile_is_scaled_to_metres_and_retagged():
    """A feet-tagged tile goes through the scaling branch: gdal.Translate with
    scaleParams [[0, 1, 0, 0.3048]] (so 1000 ft -> 304.8 m) and the result retagged 'm'.
    The mock GDAL has no Translate, so Translate / Open(update) / BuildVRT are doubled."""
    work = tempfile.mkdtemp(prefix="zfeet_")
    vrt = os.path.join(work, "_m.vrt")
    rec = {"scale": [], "built": None, "unit_set": []}

    saved = {"open": tc.cm.gdal_open, "Translate": getattr(tc.gdal, "Translate", None),
             "Open": tc.gdal.Open, "BuildVRT": tc.gdal.BuildVRT,
             "GA_Update": getattr(tc.gdal, "GA_Update", None)}

    def fake_open(path):                                   # cm.gdal_open: a feet tile, one CRS
        return _FakeDS(unit="ft")

    def fake_translate(dst, src, format=None, outputType=None, scaleParams=None):
        rec["scale"].append(scaleParams)
        open(dst, "w").close()                            # a VRT file exists for the later Open
        return _FakeDS(unit="ft")

    def fake_open_mode(path, mode=None):                  # gdal.Open(p, GA_Update) on the VRT
        ds = _FakeDS(unit="")
        rec["unit_set"].append(ds._band)
        return ds

    def fake_buildvrt(path, parts):
        rec["built"] = list(parts)
        open(path, "w").close()
        return object()                                   # non-None: GS.checked passes

    tc.cm.gdal_open = fake_open
    tc.gdal.Translate = fake_translate
    tc.gdal.Open = fake_open_mode
    tc.gdal.BuildVRT = fake_buildvrt
    tc.gdal.GA_Update = 1
    try:
        out = tc.local_mosaic([os.path.join(work, "feet.tif")], vrt, lambda _m: None)
        assert out == vrt
        assert rec["scale"] == [[[0.0, 1.0, 0.0, FT]]], rec["scale"]      # exact 0.3048 slope
        assert abs(1000.0 * rec["scale"][0][0][3] - 304.8) < 1e-9         # 1000 ft -> 304.8 m
        assert rec["unit_set"] and rec["unit_set"][0].set_unit == "m"     # retagged metres
        assert rec["built"] and rec["built"][0].endswith("_m.vrt")        # scaled VRT in the mosaic
        assert os.path.isdir(os.path.splitext(vrt)[0] + "_tiles")
    finally:
        tc.cm.gdal_open = saved["open"]
        if saved["Translate"] is None:
            del tc.gdal.Translate
        else:
            tc.gdal.Translate = saved["Translate"]
        tc.gdal.Open = saved["Open"]
        tc.gdal.BuildVRT = saved["BuildVRT"]
        if saved["GA_Update"] is None:
            del tc.gdal.GA_Update
        else:
            tc.gdal.GA_Update = saved["GA_Update"]


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
    print("test_terrain_z_units: OK")
