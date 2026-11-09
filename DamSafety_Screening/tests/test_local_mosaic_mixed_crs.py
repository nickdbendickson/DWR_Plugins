"""Grok audit 2026-11-07 finding 3.2 (Screening 0.18.16): the three tools that mosaic a local
DEM folder themselves - TerrainLoader (run_breach_sim, local folder), propose_dam._source and
find_ponds - used a bare gdal.BuildVRT, which silently drops every tile in another CRS than the
first one's and mosaics feet beside metres. They now go through terrain_cache.local_mosaic (tool
2's repair: feet scaled, other CRSs warped to EPSG:5070) and name the mosaic by
_common.mosaic_key(tiles), so a changed folder never reuses a stale VRT. Direction: on a mixed
folder storage can only go up (the missing tiles are terrain again).

The mock GDAL (tests/qgis_mock) has no reprojecting Warp, so the mixed-CRS branch is checked
with doubles that record what local_mosaic asks GDAL to do; the real warp needs GDAL (not
available here - the real-GDAL check is listed in the PR).

python3 DamSafety_Screening/tests/test_local_mosaic_mixed_crs.py   or   python3 -m pytest <this file>"""
import os
import sys
import tempfile
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "qgis_mock"))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))          # parent of the plugin package

import nd_dam_screening  # noqa: E402,F401
from nd_dam_screening.algorithms import _common as cm  # noqa: E402
from nd_dam_screening.algorithms import terrain_cache as tc  # noqa: E402
from osgeo import gdal  # noqa: E402  (the mock double)


def _tile(path, arr, x0=0.0, y1=0.0, cell=10.0, proj="EPSG:5070"):
    ds = gdal.GetDriverByName("GTiff").Create(path, arr.shape[1], arr.shape[0], 1, gdal.GDT_Float32)
    ds.SetGeoTransform((x0, cell, 0, y1, 0, -cell))
    ds.SetProjection(proj)
    ds.GetRasterBand(1).WriteArray(np.asarray(arr, "float32"))
    ds.FlushCache()
    return path


def _mixed_folder(work):
    """Two tiles: one in EPSG:5070 (metres), one in EPSG:26914 (UTM 14N) - the case a bare
    BuildVRT silently reduced to the first tile."""
    a = _tile(os.path.join(work, "a_5070.tif"), np.full((20, 20), 500.0), x0=0.0, y1=200.0)
    b = _tile(os.path.join(work, "b_utm.tif"), np.full((20, 20), 510.0), x0=200.0, y1=200.0,
              proj="EPSG:26914")
    return [a, b]


PROJ = {"a_5070.tif": "EPSG:5070", "b_utm.tif": "EPSG:26914", "only.tif": "EPSG:5070"}


class _Rec:
    """Doubles for the GDAL calls of local_mosaic's mixed branch (the mock GDAL stores one
    CRS and its Warp cannot reproject): cm.gdal_open answers each tile's own CRS (PROJ), and
    Warp / BuildVRT record which tiles were warped to EPSG:5070 and what the mosaic holds."""

    def __init__(self):
        self.warped, self.built = [], None
        self.saved = (tc.gdal.Warp, tc.gdal.BuildVRT, tc.cm.gdal_open)

    def __enter__(self):
        rec = self
        real_open = self.saved[2]

        def gdal_open(path):
            ds = real_open(path)
            ds.SetProjection(PROJ.get(os.path.basename(path), ""))
            return ds

        def warp(dst, src, **kw):
            assert kw.get("dstSRS") == "EPSG:5070", kw
            rec.warped.append(os.path.basename(src))
            open(dst, "w").close()
            return object()

        def buildvrt(path, parts):
            rec.built = list(parts)
            open(path, "w").close()
            return object()
        tc.gdal.Warp, tc.gdal.BuildVRT, tc.cm.gdal_open = warp, buildvrt, gdal_open
        return self

    def __exit__(self, *a):
        tc.gdal.Warp, tc.gdal.BuildVRT, tc.cm.gdal_open = self.saved


def test_mosaic_key_follows_the_tile_list():
    work = tempfile.mkdtemp(prefix="mkey_")
    tiles = _mixed_folder(work)
    k1 = cm.mosaic_key(tiles)
    assert len(k1) == 12 and k1 == cm.mosaic_key(list(reversed(tiles)))     # order-free
    assert k1 != cm.mosaic_key(tiles[:1])                                    # a tile removed
    time.sleep(1.1)
    with open(tiles[0], "ab") as f:
        f.write(b"x")                                                        # a tile changed
    assert cm.mosaic_key(tiles) != k1
    # the ND copy's rule: a missing file still hashes (no OSError)
    assert len(cm.mosaic_key([os.path.join(work, "gone.tif")])) == 12


def test_local_mosaic_warps_every_tile_of_a_mixed_folder():
    """Both tiles reach the mosaic, the UTM one through a warp to EPSG:5070; the log names
    the two CRSs. A bare BuildVRT kept only the first tile's CRS."""
    work = tempfile.mkdtemp(prefix="mixed_")
    tiles = _mixed_folder(work)
    vrt = os.path.join(work, "_m.vrt")
    msgs = []
    with _Rec() as rec:
        out = tc.local_mosaic(tiles, vrt, msgs.append)
    assert out == vrt and os.path.exists(vrt)
    assert sorted(rec.warped) == ["a_5070.tif", "b_utm.tif"], rec.warped   # every tile, one grid
    assert rec.built and len(rec.built) == 2 and all(p.endswith("_5070.vrt") for p in rec.built)
    text = "\n".join(msgs)
    assert "tiles in 2 CRSs" in text and "warped to EPSG:5070" in text, text


def test_terrain_loader_uses_local_mosaic_with_a_keyed_name():
    """TerrainLoader(source='local'): the mosaic comes from local_mosaic (not a bare BuildVRT),
    is named by mosaic_key, and a second folder with other tiles gets its own VRT."""
    work = tempfile.mkdtemp(prefix="loader_")
    tiles = _mixed_folder(work)
    tmp = tempfile.mkdtemp(prefix="loader_tmp_")        # outside the folder: a .vrt is a tile
    msgs = []
    with _Rec() as rec:
        loader = cm.TerrainLoader(os.path.join(work, "dem"), "local", work, tmp, log=msgs.append)
    assert loader.vrt == os.path.join(tmp, "_terrain_mosaic_{}.vrt".format(cm.mosaic_key(tiles)))
    assert os.path.exists(loader.vrt) and sorted(rec.warped) == ["a_5070.tif", "b_utm.tif"]
    assert any("2 CRSs" in m for m in msgs), msgs
    # the same folder again: the mosaic is reused, nothing rebuilt
    with _Rec() as rec2:
        again = cm.TerrainLoader(os.path.join(work, "dem"), "local", work, tmp, log=msgs.append)
    assert again.vrt == loader.vrt and rec2.warped == [] and rec2.built is None
    # another folder (one metre tile, one CRS): a different name, the plain fast path
    work2 = tempfile.mkdtemp(prefix="loader2_")
    _tile(os.path.join(work2, "only.tif"), np.full((20, 20), 100.0), x0=0.0, y1=200.0)
    with _Rec() as rec3:
        other = cm.TerrainLoader(os.path.join(work2, "dem"), "local", work2, tmp, log=msgs.append)
    assert rec3.warped == [] and rec3.built == [os.path.join(work2, "only.tif")]  # plain path
    assert other.vrt != loader.vrt and other.vrt.startswith(os.path.join(tmp, "_terrain_mosaic_"))
    assert os.path.exists(other.vrt)


def test_mixed_folder_without_a_crs_on_a_tile_is_refused_in_words():
    work = tempfile.mkdtemp(prefix="nocrs_")
    a = _tile(os.path.join(work, "a.tif"), np.full((20, 20), 500.0), x0=0.0, y1=200.0)
    b = _tile(os.path.join(work, "b.tif"), np.full((20, 20), 510.0), x0=200.0, y1=200.0, proj="")
    real = tc.cm.gdal_open

    def gdal_open(path):
        ds = real(path)
        ds.SetProjection("EPSG:5070" if os.path.basename(path) == "a.tif" else "")
        return ds
    tc.cm.gdal_open = gdal_open
    try:
        tc.local_mosaic([a, b], os.path.join(work, "_m.vrt"), lambda _m: None)
    except RuntimeError as e:
        assert "mixes tiles with and without a CRS" in str(e) and "b.tif" in str(e), e
    else:
        raise AssertionError("a mixed folder with a CRS-less tile must be refused")
    finally:
        tc.cm.gdal_open = real


def test_tools_no_longer_call_a_bare_buildvrt_on_local_folders():
    """The source text of the three tools: the local-folder mosaic goes through local_mosaic
    and mosaic_key; the 3DEP mosaic in TerrainLoader.load (one CRS, metres) is left as it is."""
    root = os.path.dirname(HERE)
    for rel, n_bare in (("algorithms/_common.py", 1), ("algorithms/propose_dam.py", 0),
                        ("algorithms/find_ponds.py", 0)):
        src = open(os.path.join(root, rel), encoding="utf-8").read()
        assert src.count("gdal.BuildVRT(") == n_bare, (rel, src.count("gdal.BuildVRT("))
        assert "local_mosaic(" in src and "mosaic_key(" in src, rel
    src = open(os.path.join(root, "algorithms/_common.py"), encoding="utf-8").read()
    assert 'gdal.BuildVRT("", src_list)' in src                       # the 3DEP one stays


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
    print("test_local_mosaic_mixed_crs: OK")
