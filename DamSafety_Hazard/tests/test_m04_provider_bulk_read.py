"""Hazard 2.24.4, audit M04 (SAFETY-NUM): RasterSampler._read's QGIS-provider branch reads the
block buffer in one go (helpers._block_bulk) instead of the per-cell loop (helpers._block_loop)
- ONLY for Byte / Int8 / UInt16 / Int16 / UInt32 / Int32 / Float32 / Float64, keyed on
int(block.dataType()). Everything else (ARGB32 image blocks, complex types), a no-data bitmap,
an empty / short buffer or any error falls back to the loop. Proven here BIT-IDENTICAL
(float64 bit patterns compared, NaN payloads included):

  1. stub blocks that mirror QgsRasterBlock::isNoData / value (no QGIS needed for these);
  2. real QgsRasterBlock objects of every whitelisted type, with and without a no-data value,
     NaN / inf / extremes / values within qgsDoubleNear of the no-data value;
  3. fallbacks: ARGB32, ARGB32_Premultiplied, CInt16, CInt32, CFloat32, CFloat64, unknown,
     empty block, no-data bitmap, a buffer of the wrong length;
  4. a real provider-backed raster GDAL cannot open (a 'virtualraster' layer over local
     GTiffs, no network): every strip and window, valid_extent, window_values and the
     Population-tab path (popsource.estimate_population_at_risk) give the same numbers with
     the bulk path and with the loop forced.

    QT_QPA_PLATFORM=offscreen python3 DamSafety_Hazard/tests/test_m04_provider_bulk_read.py
"""
import os
import shutil
import sys
import tempfile
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qgis.core import QgsApplication  # noqa: E402
_app = QgsApplication.instance() or QgsApplication([], False)
_app.setPrefixPath(os.environ.get("QGIS_PREFIX_PATH", "/home/box/miniforge3/envs/qgis334"), True)
_app.initQgis()

import numpy as np  # noqa: E402
from osgeo import gdal, osr  # noqa: E402
from qgis.core import (  # noqa: E402
    Qgis, QgsCoordinateTransformContext, QgsRasterBlock, QgsRasterLayer,
)
from qgis.PyQt.QtCore import QByteArray  # noqa: E402

from DamSafety_Hazard import helpers as H  # noqa: E402
from DamSafety_Hazard import popsource as PS  # noqa: E402

EPS = 4 * np.finfo(np.float64).eps
WL = {"Byte": 1, "Int8": 14, "UInt16": 2, "Int16": 3, "UInt32": 4, "Int32": 5,
      "Float32": 6, "Float64": 7}
NOT_WL = {"UnknownDataType": 0, "CInt16": 8, "CInt32": 9, "CFloat32": 10, "CFloat64": 11,
          "ARGB32": 12, "ARGB32_Premultiplied": 13}
NP = {1: "=u1", 14: "=i1", 2: "=u2", 3: "=i2", 4: "=u4", 5: "=i4", 6: "=f4", 7: "=f8"}

FAILS = []


def check(name, cond, detail=""):
    print("{:4} {}{}".format("ok" if cond else "FAIL", name, (" - " + detail) if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def bits_equal(a, b):
    a, b = np.ascontiguousarray(a, np.float64), np.ascontiguousarray(b, np.float64)
    return a.shape == b.shape and np.array_equal(a.view(np.uint64), b.view(np.uint64))


def delta(a, b):
    d = a.view(np.uint64) != b.view(np.uint64)
    return "{} of {} cells differ".format(int(d.sum()), d.size)


def sample_values(code, h, w, nd, rng):
    """Values of the type: random, its extremes, the no-data value, and for floats NaN,
    +-inf, -0.0 and values within / just outside qgsDoubleNear of the no-data value."""
    dt = np.dtype(NP[code])
    if dt.kind == "f":
        a = rng.normal(0, 1000, size=h * w).astype(dt)
        special = [np.nan, np.inf, -np.inf, -0.0, 0.0, np.finfo(dt).max, np.finfo(dt).min,
                   np.finfo(dt).tiny, -9999.0]
    else:
        info = np.iinfo(dt)
        a = rng.integers(info.min, int(info.max) + 1, size=h * w, dtype=np.int64).astype(dt)
        special = [info.min, info.max, 0, 1]
    if nd is not None:
        special += [nd] * 5
        if dt.kind == "f" and np.isfinite(nd):
            for k in (1, 2, 3, 4, 5, 8):
                special.append(np.nextafter(dt.type(nd), dt.type(np.inf)) if k == 1 else
                               dt.type(nd) + dt.type(k * EPS))
    special = np.array(special, dtype=np.float64)
    with np.errstate(over="ignore", invalid="ignore"):
        sp = special.astype(dt)
    k = min(sp.size, a.size)
    a[:k] = sp[:k]
    rng.shuffle(a)
    return a.reshape(h, w)


class StubBlock(object):
    """Mirrors QgsRasterBlock: isNoData = (hasNoDataValue and (isnan(v) or qgsDoubleNear(v,
    nd))) or the bitmap; value(r, c) = the stored value as a double (NaN for image types)."""

    def __init__(self, code, arr=None, nd=None, bitmap=None, buf=None):
        self.code, self.nd, self.bitmap = code, nd, bitmap
        self.arr = arr
        self.buf = buf if buf is not None else (arr.tobytes() if arr is not None else b"")

    def dataType(self):
        return self.code

    def hasNoData(self):
        return self.nd is not None or self.bitmap is not None

    def hasNoDataValue(self):
        return self.nd is not None

    def noDataValue(self):
        return float(self.nd) if self.nd is not None else float("nan")

    def data(self):
        return self.buf

    def value(self, r, c):
        if self.arr is None or self.code in NOT_WL.values():
            return float("nan")
        return float(self.arr[r, c])

    def isNoData(self, r, c):
        if self.nd is not None:
            if self.arr is None:
                return True
            v = float(self.arr[r, c])
            d = v - float(self.nd)
            return v != v or (d > -EPS and d <= EPS)
        if self.bitmap is not None:
            return bool(self.bitmap[r, c])
        return False


def nd_choices(code):
    dt = np.dtype(NP[code])
    if dt.kind == "f":
        return [None, -9999.0, 0.0, float(np.finfo(dt).min), float("nan"), float("inf")]
    info = np.iinfo(dt)
    return [None, 0, int(info.max), int(info.min), 255 if info.max >= 255 else 127]


def test_stub_blocks():
    rng = np.random.default_rng(20261110)
    for name, code in WL.items():
        for nd in nd_choices(code):
            for (h, w) in ((1, 1), (7, 13), (64, 50)):
                arr = sample_values(code, h, w, nd, rng)
                b = StubBlock(code, arr, nd)
                bulk = H._block_bulk(b, w, h)
                loop = H._block_loop(b, w, h)
                ok = bulk is not None and bits_equal(bulk, loop)
                check("stub {} nd={} {}x{}: bulk == loop bit-identical".format(name, nd, h, w),
                      ok, "bulk None" if bulk is None else delta(bulk, loop))


def test_stub_fallbacks():
    h, w = 5, 6
    raw = np.arange(h * w, dtype="=u4").reshape(h, w)
    for name, code in NOT_WL.items():
        b = StubBlock(code, raw, None)
        check("stub {}: refused by the whitelist (loop, all NaN)".format(name),
              H._block_bulk(b, w, h) is None and np.isnan(H._block_loop(b, w, h)).all())
    for name, code in WL.items():
        e = StubBlock(code, None, -9999.0, buf=b"")
        check("stub {} empty block: falls back (loop gives all NaN)".format(name),
              H._block_bulk(e, w, h) is None and np.isnan(H._block_loop(e, w, h)).all())
        short = StubBlock(code, np.zeros((h, w), NP[code]), 0, buf=b"\0" * (np.dtype(NP[code]).itemsize * h * w - 1))
        check("stub {} short buffer: falls back".format(name), H._block_bulk(short, w, h) is None)
        bm = np.zeros((h, w), bool)
        bm[1, 2] = True
        bb = StubBlock(code, np.ones((h, w), NP[code]), None, bitmap=bm)
        check("stub {} no-data bitmap: keeps the loop".format(name),
              H._block_bulk(bb, w, h) is None and np.isnan(H._block_loop(bb, w, h)[1, 2]))

    class Boom(StubBlock):
        def data(self):
            raise RuntimeError("provider hiccup")
    check("stub error in data(): falls back, no raise",
          H._block_bulk(Boom(6, np.zeros((h, w), "=f4"), None), w, h) is None)


def real_block(code, arr, nd):
    h, w = arr.shape
    b = QgsRasterBlock(Qgis.DataType(code), w, h)
    b.setData(QByteArray(arr.tobytes()))
    if nd is not None:
        b.setNoDataValue(float(nd))
    return b


def test_real_qgsrasterblocks():
    rng = np.random.default_rng(3161)
    for name, code in WL.items():
        for nd in nd_choices(code):
            for (h, w) in ((1, 1), (9, 11), (40, 33)):
                arr = sample_values(code, h, w, nd, rng)
                b = real_block(code, arr, nd)
                if not b.isValid() or int(b.dataType()) != code:
                    check("real {} block could be made".format(name), False)
                    continue
                bulk = H._block_bulk(b, w, h)
                loop = H._block_loop(b, w, h)
                ok = bulk is not None and bits_equal(bulk, loop)
                check("real QgsRasterBlock {} nd={} {}x{}: bulk == loop bit-identical".format(
                    name, nd, h, w), ok, "bulk None" if bulk is None else delta(bulk, loop))
    # real blocks that must fall back
    for name in ("ARGB32", "ARGB32_Premultiplied", "CInt16", "CFloat32", "CFloat64"):
        code = NOT_WL[name]
        b = QgsRasterBlock(Qgis.DataType(code), 4, 3)
        check("real QgsRasterBlock {}: refused by the whitelist".format(name),
              H._block_bulk(b, 4, 3) is None)
    b = QgsRasterBlock(Qgis.DataType.Float32, 4, 3)
    b.setNoDataValue(-9999.0)
    b.setIsNoData()                                       # whole block no-data
    loop = H._block_loop(b, 4, 3)
    bulk = H._block_bulk(b, 4, 3)
    check("real whole-block-nodata Float32: bulk None or identical, all NaN",
          np.isnan(loop).all() and (bulk is None or bits_equal(bulk, loop)))
    e = QgsRasterBlock()
    check("real empty QgsRasterBlock(): falls back", H._block_bulk(e, 4, 3) is None)


def _tif(path, arr, gdt, nd):
    h, w = arr.shape
    ds = gdal.GetDriverByName("GTiff").Create(path, w, h, 1, gdt)
    ds.SetGeoTransform((100000.0, 30.0, 0.0, 2600000.0 + 30.0 * h, 0.0, -30.0))
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(5070)
    ds.SetProjection(srs.ExportToWkt())
    band = ds.GetRasterBand(1)
    if nd is not None:
        band.SetNoDataValue(nd)
    band.WriteArray(arr)
    ds = None


def _virtual(path, h, w, name="v"):
    ext = "100000,2600000,{},{}".format(100000 + 30 * w, 2600000 + 30 * h)
    q = ("crs=EPSG:5070&extent={}&width={}&height={}&formula={}&r:uri={}&r:provider=gdal"
         .format(ext, w, h, urllib.parse.quote('"r@1"'), urllib.parse.quote(path)))
    return QgsRasterLayer("?" + q, name, "virtualraster")


def test_real_provider_layer():
    d = tempfile.mkdtemp(prefix="m04_")
    old = H._block_bulk
    try:
        rng = np.random.default_rng(7)
        h, w = 120, 97
        pop = np.round(rng.gamma(0.6, 8.0, size=(h, w)), 2).astype(np.float32)
        pop[:5, :] = -9999.0
        pop[50:60, 40:45] = np.nan
        ppath = os.path.join(d, "pop.tif")
        _tif(ppath, pop, gdal.GDT_Float32, -9999.0)
        dep = np.where(rng.random((h, w)) > 0.35, rng.gamma(1.5, 3.0, (h, w)), -9999.0)
        dpath = os.path.join(d, "depth.tif")
        _tif(dpath, dep.astype(np.float32), gdal.GDT_Float32, -9999.0)
        lyr = _virtual(ppath, h, w, "pop (virtual)")
        check("virtualraster layer is valid", lyr.isValid())
        s = H.RasterSampler(lyr)
        check("GDAL cannot open it: provider backend", s.ok and s.backend == "provider",
              str(s.backend))
        blk = s._provider.block(1, lyr.extent(), w, h)
        check("provider block type is whitelisted ({})".format(int(blk.dataType())),
              int(blk.dataType()) in WL.values())
        wins = [(0, 0, w, h), (3, 7, 50, 40), (0, 100, w, 20), (96, 119, 1, 1), (10, 0, 1, h)]
        bulk_reads = [s._read(*win) for win in wins]
        bulk_ext = s.valid_extent(positive_only=True)
        bulk_wv = s.window_values(5, 5, 60, 70)
        depth = H.RasterSampler(QgsRasterLayer(dpath, "depth"))
        ctx = QgsCoordinateTransformContext()
        bulk_par = PS.estimate_population_at_risk(lyr, depth, ctx, raster_units="per_cell")
        # ground truth from GDAL on the source file
        g = gdal.Open(ppath).ReadAsArray().astype(np.float64)
        H._block_bulk = lambda block, w, h: None                # the pre-fix loop
        loop_reads = [s._read(*win) for win in wins]
        loop_ext = s.valid_extent(positive_only=True)
        loop_wv = s.window_values(5, 5, 60, 70)
        loop_par = PS.estimate_population_at_risk(lyr, depth, ctx, raster_units="per_cell")
        H._block_bulk = old
        for win, a, b in zip(wins, bulk_reads, loop_reads):
            check("provider _read{} bulk == loop bit-identical".format(win),
                  a is not None and bits_equal(a, b), "" if a is None else delta(a, b))
        full = bulk_reads[0]
        # (the virtualraster provider writes its own no-data - -FLT_MAX, no declared no-data
        # value - for the source's -9999 / NaN cells, through either path)
        m = np.isfinite(g) & (g != -9999.0)
        check("provider values match the GTiff on its valid cells",
              np.array_equal(full[m], g[m]), delta(full[m].copy(), g[m].copy()))
        check("valid_extent identical", bulk_ext == loop_ext, "{} vs {}".format(bulk_ext, loop_ext))
        check("window_values identical", bits_equal(np.asarray(bulk_wv, float), np.asarray(loop_wv, float)))
        keys = ("ok", "error", "par", "pop_in_model", "par_by_class")
        same = all(repr(bulk_par.get(k)) == repr(loop_par.get(k)) for k in keys)
        check("Population tab (estimate_population_at_risk) identical: par={} in_model={}".format(
            bulk_par.get("par"), bulk_par.get("pop_in_model")), same and bulk_par.get("ok"),
            "{} vs {}".format({k: bulk_par.get(k) for k in keys}, {k: loop_par.get(k) for k in keys}))
        # integer population raster through the provider too
        ipop = rng.integers(0, 300, size=(h, w)).astype(np.int16)
        ipop[0, :] = -1
        ipath = os.path.join(d, "ipop.tif")
        _tif(ipath, ipop, gdal.GDT_Int16, -1)
        il = _virtual(ipath, h, w, "ipop (virtual)")
        si = H.RasterSampler(il)
        a = si._read(0, 0, w, h)
        H._block_bulk = lambda block, w, h: None
        b = si._read(0, 0, w, h)
        H._block_bulk = old
        check("provider (Int16 source) _read bulk == loop bit-identical",
              si.backend == "provider" and a is not None and bits_equal(a, b))
        s.close()
        si.close()
        depth.close()
    finally:
        H._block_bulk = old
        shutil.rmtree(d, ignore_errors=True)


def test_speed_note():
    import time
    h, w = 400, 500
    arr = np.random.default_rng(1).normal(size=(h, w)).astype("=f4")
    b = real_block(6, arr, -9999.0)
    t0 = time.time()
    H._block_bulk(b, w, h)
    t1 = time.time()
    H._block_loop(b, w, h)
    t2 = time.time()
    print("info 200,000 cells: bulk {:.4f} s, loop {:.3f} s".format(t1 - t0, t2 - t1))


if __name__ == "__main__":
    test_stub_blocks()
    test_stub_fallbacks()
    test_real_qgsrasterblocks()
    test_real_provider_layer()
    test_speed_note()
    print("\n{} failed".format(len(FAILS)))
    sys.exit(1 if FAILS else 0)
