"""Data 1.5.0: Download imagery for these dams (imagery.py) with the NAIP service mocked.

    python -m pytest -q DamSafety_Data/tests/test_imagery.py     (from the repository root)

Checks: the per-dam plan (UTM zone, 2 x radius box on the pixel grid, pieces within the server
limit); the dams file reader (ids, ONLY_IDS, centroids, reprojection); one JPEG YCbCr COG per
dam named <dam_id>_naip_<year>.tif with overviews, georeferenced on the box; the newest year is
locked in the exportImage mosaicRule; the manifest (url, year, resolution, sha256); a second
run downloads nothing; an interrupted run resumes from the pieces; pack folders are refused."""
import json
import os
import urllib.parse

import pytest

gdal = pytest.importorskip("osgeo.gdal")
from osgeo import ogr, osr  # noqa: E402

from DamSafety_Data import imagery as IM  # noqa: E402

BISMARCK = [("ND00001", -100.78, 46.81), ("ND00002", -100.70, 46.85)]


def _tiff_bytes(w, h):
    p = "/vsimem/naip_{}x{}.tif".format(w, h)
    ds = gdal.GetDriverByName("GTiff").Create(p, w, h, 3, gdal.GDT_Byte)
    for b in (1, 2, 3):
        ds.GetRasterBand(b).Fill(40 * b)
    ds = None
    f = gdal.VSIFOpenL(p, "rb")
    gdal.VSIFSeekL(f, 0, 2)
    n = gdal.VSIFTellL(f)
    gdal.VSIFSeekL(f, 0, 0)
    data = gdal.VSIFReadL(1, n, f)
    gdal.VSIFCloseL(f)
    gdal.Unlink(p)
    return data


class Fake:
    def __init__(self, years=(2021, 2023), fail_after=None):
        self.calls = []
        self.years = years
        self.fail_after = fail_after

    def __call__(self, url, params, timeout=None, cancel=None, log=None):
        self.calls.append((url, dict(params)))
        if url.endswith("/query"):
            return json.dumps({"features": [{"attributes": {"Year": y, "Name": "m_{}".format(y)}}
                                            for y in self.years]}).encode()
        assert url.endswith("/exportImage")
        n = sum(1 for u, _p in self.calls if u.endswith("/exportImage"))
        if self.fail_after is not None and n > self.fail_after:
            raise IM.ImageryError("HTTP 504")
        w, h = (int(v) for v in params["size"].split(","))
        assert w <= IM.TILE_PX and h <= IM.TILE_PX
        return _tiff_bytes(w, h)

    def exports(self):
        return [p for u, p in self.calls if u.endswith("/exportImage")]


def test_plan_tiles_and_estimate():
    p = IM.dam_plan("A", -100.78, 46.81, 2500, 0.6)
    assert p["epsg"] == 26914 and p["pixels"] == 8334
    x0, y0, x1, y1 = p["bbox"]
    assert abs((x1 - x0) - 8334 * 0.6) < 1e-6 and abs((y1 - y0) - 8334 * 0.6) < 1e-6
    assert len(p["tiles"]) == 25 and all(t["w"] <= 2000 and t["h"] <= 2000 for t in p["tiles"])
    assert sum(t["w"] for t in p["tiles"] if t["row"] == 0) == 8334
    for t in p["tiles"]:                       # each piece is w x h pixels of res_m
        b = t["bbox"]
        assert abs((b[2] - b[0]) / t["w"] - 0.6) < 1e-6 and abs((b[3] - b[1]) / t["h"] - 0.6) < 1e-6
    assert IM.dam_plan("A", -103.5, 47.0, 2500, 2.0)["epsg"] == 26913
    q = IM.dam_plan("A", -100.78, 46.81, 2500, 2.0)
    assert q["pixels"] == 2500 and len(q["tiles"]) == 4
    dl, out = IM.estimate([p, p])
    assert 100e6 < dl < 140e6 and 40e6 < out < 60e6
    assert IM.safe_id("ND 0001/a") == "ND_0001_a"


def test_read_dams(tmp_path):
    path = str(tmp_path / "dams.gpkg")
    ds = ogr.GetDriverByName("GPKG").CreateDataSource(path)
    sr = osr.SpatialReference()
    sr.ImportFromEPSG(26914)
    lyr = ds.CreateLayer("dams", sr, ogr.wkbPolygon)
    lyr.CreateField(ogr.FieldDefn("NIDID", ogr.OFTString))
    x, y = IM.to_utm(-100.78, 46.81, 26914)
    for did, dx in (("ND1", 0), ("ND2", 1000)):
        f = ogr.Feature(lyr.GetLayerDefn())
        f["NIDID"] = did
        f.SetGeometry(ogr.CreateGeometryFromWkt("POLYGON(({0} {1},{2} {1},{2} {3},{0} {3},{0} "
                                                "{1}))".format(x + dx - 50, y - 50, x + dx + 50,
                                                               y + 50)))
        lyr.CreateFeature(f)
    ds = None
    dams = IM.read_dams(path)
    assert [d[0] for d in dams] == ["ND1", "ND2"]
    assert abs(dams[0][1] + 100.78) < 1e-6 and abs(dams[0][2] - 46.81) < 1e-6
    assert [d[0] for d in IM.read_dams(path, only_ids=["ND2"])] == ["ND2"]
    with pytest.raises(IM.ImageryError):
        IM.read_dams(path, id_field="nope")


def test_download_cog_manifest_and_rerun(tmp_path, monkeypatch):
    fake = Fake()
    monkeypatch.setattr(IM, "_get", fake)
    r = IM.download(BISMARCK, str(tmp_path), radius_m=500, res_m=1.0)
    folder = str(tmp_path / "imagery")
    assert r["folder"] == folder and not r["failed"] and set(r["done"]) == {"ND00001", "ND00002"}
    for did, _x, _y in BISMARCK:
        e = r["done"][did]
        path = os.path.join(folder, "{}_naip_2023.tif".format(did))
        assert e["file"] == os.path.basename(path) and os.path.isfile(path)
        assert e["year"] == 2023 and e["years_on_box"] == [2021, 2023]
        assert e["resolution_m"] == 1.0 and e["sha256"] == IM.sha256(path)
        assert e["url"] == IM.SERVICE + "/exportImage" and e["crs"] == "EPSG:26914"
        ds = gdal.Open(path)
        assert ds.RasterXSize == ds.RasterYSize == 1000 and ds.RasterCount == 3
        gt = ds.GetGeoTransform()
        assert gt[1] == 1.0 and abs(gt[0] - e["bbox"][0]) < 1e-6 and abs(gt[3] - e["bbox"][3]) < 1e-6
        md = ds.GetMetadata("IMAGE_STRUCTURE")
        assert md.get("COMPRESSION") == "YCbCr JPEG" or (md.get("COMPRESSION") == "JPEG" and
                                                         md.get("SOURCE_COLOR_SPACE") == "YCbCr")
        assert md.get("LAYOUT") == "COG" and ds.GetRasterBand(1).GetOverviewCount() >= 1
        ds = None
    for p in fake.exports():
        assert json.loads(p["mosaicRule"]) == {"where": "Year = 2023"}
        assert p["bandIds"] == "0,1,2" and p["format"] == "tiff" and p["imageSR"] == 26914
    man = json.load(open(os.path.join(folder, IM.MANIFEST)))
    assert man["service"] == IM.SERVICE and set(man["dams"]) == {"ND00001", "ND00002"}
    assert not os.path.exists(os.path.join(folder, IM.TILES))          # pieces cleaned up
    n = len(fake.calls)
    r2 = IM.download(BISMARCK, str(tmp_path), radius_m=500, res_m=1.0)
    assert len(fake.calls) == n and sorted(r2["skipped"]) == ["ND00001", "ND00002"]
    r3 = IM.download(BISMARCK, str(tmp_path), radius_m=500, res_m=2.0)   # other settings
    assert len(r3["done"]) == 2 and len(fake.calls) > n


def test_resume_from_pieces(tmp_path, monkeypatch):
    fake = Fake(years=(2023,), fail_after=2)
    monkeypatch.setattr(IM, "_get", fake)
    dams = BISMARCK[:1]
    r = IM.download(dams, str(tmp_path), radius_m=1500, res_m=0.6)   # 5000 px: 9 pieces
    assert "ND00001" in r["failed"] and not r["done"]
    assert len(fake.exports()) == 3
    fake2 = Fake(years=(2023,))
    monkeypatch.setattr(IM, "_get", fake2)
    r = IM.download(dams, str(tmp_path), radius_m=1500, res_m=0.6)
    assert set(r["done"]) == {"ND00001"} and len(fake2.exports()) == 7    # 2 pieces kept
    assert r["done"]["ND00001"]["pieces"] == 9


def test_no_imagery_and_refusals(tmp_path, monkeypatch):
    monkeypatch.setattr(IM, "_get", Fake(years=()))
    r = IM.download(BISMARCK[:1], str(tmp_path), radius_m=300, res_m=2.0)
    assert "no NAIP" in r["failed"]["ND00001"]
    for bad in ("ND_Dam_Suite_Pack", "x/nd_offline_pack_v2"):
        with pytest.raises(IM.ImageryError):
            IM.download(BISMARCK[:1], str(tmp_path / bad), radius_m=300, res_m=2.0)
    with pytest.raises(IM.ImageryError):
        IM.download(BISMARCK[:1], str(tmp_path), res_m=0.5)
    with pytest.raises(IM.ImageryError):
        IM.download([("a b", -100.7, 46.8), ("a_b", -100.6, 46.8)], str(tmp_path))


def test_export_params_urlencode():
    p = IM.dam_plan("A", -100.78, 46.81, 2500, 0.6)
    q = IM.export_params(p, p["tiles"][0], 2023)
    assert q["compression"] == "JPEG" and q["compressionQuality"] == 90
    assert "LZ77" == IM.export_params(p, p["tiles"][0], 2023, "lossless")["compression"]
    assert "Year+%3D+2023" in urllib.parse.urlencode(q)
