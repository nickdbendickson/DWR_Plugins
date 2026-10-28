"""Tool 7b (0.17.55), with real GDAL: what 'Runner DEM source' writes is what the Runner reads.

  10 m: several 3DEP pieces (a multi-tile VRT, as dem_download.download leaves them) become one
        EPSG:5070 GeoTIFF with band unit 'm' and its facts file; the Runner's unit check
        (dem_zunit.recorded) reads metres.
  1 m:  native UTM tiles -> the lidar_tnm VRT (the 10 m file underneath), band unit 'm', reused
        when every tile is still there, rebuilt when one is missing.
Needs GDAL (the QGIS Python); prints SKIP otherwise. No network: the download itself is a fake
that writes small rasters."""
import json, os, shutil, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
sys.path.insert(0, HERE)

import _optional  # noqa: E402
_optional.need("osgeo", "writes and reads small GeoTIFFs")
import numpy as np  # noqa: E402
from osgeo import gdal, osr  # noqa: E402
from nd_dam_screening.core import dem_download as dd, dem_zunit as ZU, lidar_tnm as lt  # noqa: E402
from nd_dam_screening.core import runner_dem as rdem  # noqa: E402

gdal.UseExceptions()


def srs(epsg):
    s = osr.SpatialReference()
    s.ImportFromEPSG(epsg)
    s.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    return s


def tif(path, epsg, x0, y1, nx, ny, cell, z):
    ds = gdal.GetDriverByName("GTiff").Create(path, nx, ny, 1, gdal.GDT_Float32)
    ds.SetProjection(srs(epsg).ExportToWkt())
    ds.SetGeoTransform((x0, cell, 0.0, y1, 0.0, -cell))
    ds.GetRasterBand(1).WriteArray(np.full((ny, nx), z, dtype="float32"))
    ds = None
    return path


def unit(p):
    ds = gdal.Open(p)
    try:
        return ds.GetRasterBand(1).GetUnitType(), ds.GetSpatialRef().GetAuthorityCode(None)
    finally:
        ds = None


def main(work):
    ll = srs(4326)
    a = osr.CoordinateTransformation(ll, srs(5070))
    dams = [a.TransformPoint(-97.50, 47.00)[:2], a.TransformPoint(-97.48, 47.01)[:2]]
    box = rdem.dams_box_5070(dams, 0.5)
    got = []

    def fake_download(extent, cell, out_dir, stem, feedback=None, scope=0):
        got.append(tuple(extent))
        x0, y0, x1, y1 = extent
        mid = round((x0 + x1) / 2 / cell) * cell
        parts = [tif(os.path.join(out_dir, "{}_{}.tif".format(stem, i)), 5070, xa, y1,
                     int(round((xb - xa) / cell)), int(round((y1 - y0) / cell)), cell, 300.0)
                 for i, (xa, xb) in enumerate(((x0, mid), (mid, x1)))]
        v = os.path.join(out_dir, stem + ".vrt")
        gdal.BuildVRT(v, parts)                       # untagged, as the ImageServer pieces are
        return v

    real = dd.download
    dd.download = fake_download
    try:
        folder = os.path.join(work, "Dams", rdem.FOLDER_NAME)
        p10 = rdem.get_10m(box, folder, 2, 10 * 1024 ** 3, None)
    finally:
        dd.download = real
    assert got == [box], got
    assert unit(p10) == ("m", "5070"), unit(p10)
    ds = gdal.Open(p10)
    gt = ds.GetGeoTransform()
    assert abs(gt[1] - 10) < 1e-9 and gt[0] <= box[0] + 1e-6 and gt[3] >= box[3] - 1e-6, gt
    ds = None
    assert os.path.isfile(os.path.splitext(p10)[0] + "_dem.txt")
    ds = gdal.Open(p10)
    try:
        r = ZU.recorded(ds, p10)
    finally:
        ds = None
    assert r and r[0] == ZU.METERS, r
    # again: reused, the fake is not called
    dd.download = fake_download
    try:
        assert rdem.get_10m(box, folder, 2, 0, None) == p10 and len(got) == 1
    finally:
        dd.download = real
    print("OK 10 m: one EPSG:5070 GeoTIFF, band unit m, Runner reads metres, reused")

    # 1 m: two native tiles in UTM 14N, the 10 m underneath
    u = osr.CoordinateTransformation(ll, srs(26914))
    tiles = os.path.join(work, "tiles")
    os.makedirs(tiles)
    ents = []
    for i, (lon, lat) in enumerate(((-97.50, 47.00), (-97.48, 47.01))):
        x, y = u.TransformPoint(lon, lat)[:2]
        name = "USGS_1M_14_t{}.tif".format(i)
        p = tif(os.path.join(tiles, name), 26914, round(x) - 100, round(y) + 100, 200, 200, 1.0, 301.0 + i)
        ents.append({"tile": name, "path": p, "project": "ND_Test", "bytes": os.path.getsize(p),
                     "collection_begin": "2019-01-01", "collection_end": "2019-12-31",
                     "publication_date": "2020-01-01"})
    calls = {"plan": 0, "fetch": 0}

    def fake_plan(bbox_ll, f, http, feedback=None, keep_older=False, items=None):
        calls["plan"] += 1
        return {"bbox_ll": list(bbox_ll), "folder": f, "tiles": [dict(e, present=True, partial_bytes=0) for e in ents],
                "dropped": [], "notes": [], "total_bytes": 2, "need_bytes": 0, "free_bytes": 10 ** 15,
                "projects": ["ND_Test"]}

    def fake_fetch(plan, http, feedback=None, **kw):
        calls["fetch"] += 1
        return [dict(e) for e in ents], []

    keep = lt.make_plan, lt.fetch
    lt.make_plan, lt.fetch = fake_plan, fake_fetch
    try:
        vrt = rdem.get_1m(box, folder, 2, 10 * 1024 ** 3, None, tile_folder=tiles)
        assert vrt.endswith("_1m_lidar.vrt") and unit(vrt) == ("m", "26914"), unit(vrt)
        info = json.load(open(os.path.splitext(vrt)[0] + ".json"))
        assert info["fallback"] == p10 and [t["path"] for t in info["tiles"]] == [e["path"] for e in ents], info
        ds = gdal.Open(vrt)
        # 300 m below the top, in the middle: no tile there, so the 10 m underneath shows
        arr = ds.GetRasterBand(1).ReadAsArray(ds.RasterXSize // 2, 300, 1, 1)
        x, y = ds.GetGeoTransform()[0], ds.GetGeoTransform()[3]
        tile_at = [ds.GetRasterBand(1).ReadAsArray(int(round(gdal.Open(e["path"]).GetGeoTransform()[0] - x)) + 100,
                                                   int(round(y - gdal.Open(e["path"]).GetGeoTransform()[3])) + 100,
                                                   1, 1)[0][0] for e in ents]
        ds = None
        assert abs(float(arr[0][0]) - 300.0) < 1e-3, arr
        assert [round(float(v), 3) for v in tile_at] == [301.0, 302.0], tile_at     # the 1 m on top
        assert os.path.isfile(os.path.splitext(vrt)[0] + "_dem.txt")
        assert rdem.get_1m(box, folder, 2, 0, None, tile_folder=tiles) == vrt and calls["plan"] == 1
        os.remove(ents[1]["path"])                                     # a tile gone: not reused
        assert not rdem._reusable_vrt(vrt)
    finally:
        lt.make_plan, lt.fetch = keep
    print("OK 1 m: native tiles VRT, band unit m, 10 m underneath, reused only when complete")


if __name__ == "__main__":
    w = tempfile.mkdtemp(prefix="w7b_dem_gdal_")
    try:
        main(w)
    finally:
        shutil.rmtree(w, ignore_errors=True)
    print("ALL OK")
