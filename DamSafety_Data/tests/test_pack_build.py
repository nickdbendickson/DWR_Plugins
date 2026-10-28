"""Data 1.5.0: Build offline pack (pack_build.py) with every download mocked.

    python -m pytest -q DamSafety_Data/tests/test_pack_build.py     (from the repository root)

Needs GDAL (osgeo); skipped without it. Checks: the layer registry covers every layer of the
reference ND_Dam_Suite_Pack; one build writes format 2 AND format 1 from the same download, both
pass packcheck, open with offline_pack.open_pack and hold the same layers and counts; the
format-1 manifest names the raster it cannot hold; sha256 in the manifests; a second run
downloads nothing and rewrites nothing; the refusal rules; Setup's suite_fill finds the layers."""
import json
import os

import pytest

gdal = pytest.importorskip("osgeo.gdal")

from DamSafety_Data import build_nd_pack as BP  # noqa: E402
from DamSafety_Data import offline_pack as OP  # noqa: E402
from DamSafety_Data import pack_build as PB  # noqa: E402

REFERENCE = (
    "DWR_dam_lines", "DWR_dams", "DWR_dugout_polygons", "DWR_dugouts", "DWR_unknown_structures",
    "FRA_grade_crossings", "HIFLD_fire_ems_stations", "HIFLD_local_eoc",
    "HIFLD_local_law_enforcement", "HIFLD_wastewater_treatment_plants", "HPMS_ND_2024",
    "NARN_rail_lines", "NBI_bridges", "NDDOT_traffic_counts", "NDGISHUB_address_points",
    "ND_counties", "ND_county_roads", "ND_roads_NG911", "NHD_flowlines", "NHD_waterbodies",
    "NID_dams", "NSI_structures", "OSM_campgrounds", "OSM_lifelines", "OSM_places",
    "USA_Structures", "lifeline_eia_power_plants", "lifeline_nd_assisted_living",
    "lifeline_nd_basic_care", "lifeline_nd_hospitals", "lifeline_nd_skilled_nursing",
    "NLCD_2021_landcover")
# the fields Setup's suite_fill looks for (DWR, NSI, NID)
FIELDS = ("dam_name", "structure_id", "fd_id", "occtype", "nidid", "longitude")
BBOX = (-100.85, 46.75, -100.70, 46.85)


def _names(table):
    out = set()
    for t in table:
        out.add(t["name"])
        if t["kind"] == "first_of":
            out.update(c[0] for c in t["spec"]["choices"])
    return out


def test_registry_covers_reference_pack():
    table = PB.registry()
    missing = set(REFERENCE) - _names(table)
    assert not missing, missing
    kinds = {t["name"]: t["kind"] for t in table}
    assert kinds["DWR_dams"] == "dwr_download" and kinds["DWR_unknown_structures"] == "none"
    assert kinds["NLCD_2021_landcover"] == "nlcd"
    cats = {t["name"]: t["info"].get("category") for t in table if t["group"] == "hifld"}
    assert cats["HIFLD_local_eoc"] == "Emergency operations center"


def test_registry_local_dwr_folder(tmp_path):
    d = tmp_path / "dwr"
    d.mkdir()
    _gpkg(str(d / "DWR_Unknown_Structures.gpkg"), "DWR_Unknown_Structures", 2)
    t = {x["name"]: x for x in PB.registry(dwr_folder=str(d))}
    assert t["DWR_unknown_structures"]["kind"] == "local_gpkg"


def _gpkg(path, layer, n, geojson=False):
    from osgeo import ogr, osr
    drv = ogr.GetDriverByName("GeoJSON" if geojson else "GPKG")
    ds = drv.CreateDataSource(path)
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    lyr = ds.CreateLayer(layer, srs, ogr.wkbPoint)
    for k in FIELDS:
        lyr.CreateField(ogr.FieldDefn(k, ogr.OFTString))
    for i in range(n):
        f = ogr.Feature(lyr.GetLayerDefn())
        for k in FIELDS:
            f.SetField(k, "x%d" % i)
        f.SetGeometry(ogr.CreateGeometryFromWkt("POINT (%f 46.8)" % (-100.8 + 0.001 * i)))
        lyr.CreateFeature(f)
    # one outside the box: clipped away
    f = ogr.Feature(lyr.GetLayerDefn())
    f.SetGeometry(ogr.CreateGeometryFromWkt("POINT (-97 47)"))
    lyr.CreateFeature(f)
    ds = None
    return path


def _tif(path):
    from osgeo import osr
    ds = gdal.GetDriverByName("GTiff").Create(path, 20, 10, 1, gdal.GDT_Byte)
    ds.SetGeoTransform((-375945.0, 30.0, 0, 2666295.0, 0, -30.0))
    s = osr.SpatialReference()
    s.ImportFromEPSG(5070)
    ds.SetProjection(s.ExportToWkt())
    ds.GetRasterBand(1).Fill(41)
    ds = None
    return path


@pytest.fixture
def mocked(tmp_path, monkeypatch):
    calls = []

    def run_one(ctx, name, label, kind, spec, bbox):
        real = spec["choices"][0][0] if kind == "first_of" else name
        calls.append(real)
        w = ctx.writer
        w.begin(real, label, "https://example.test/" + real, resume=False)
        feats = [{"type": "Feature", "geometry": {"type": "Point", "coordinates": [
            -100.8 + 0.001 * i, 46.8]}, "properties": dict({k: "v" for k in FIELDS}, fid=i, CWP_NAME="Plant %d" % i)}
            for i in range(3)]
        w.add(feats, lambda f: "fid:%d" % f["properties"]["fid"])
        return w.finish("mock")

    def dwr(qlay, dest, refresh=False, log=None):
        calls.append("DWR " + qlay)
        os.makedirs(dest, exist_ok=True)
        p = os.path.join(dest, qlay + ".geojson")
        if not os.path.exists(p):
            _gpkg(p, qlay, 4, geojson=True)
        return p, "https://dwr.example.test/" + qlay
    monkeypatch.setattr(BP, "_run_one", run_one)
    monkeypatch.setattr(PB, "dwr_download", dwr)
    return calls, _tif(str(tmp_path / "nlcd.tif"))


def _build(out, nlcd, **kw):
    lines = []
    r = PB.build(str(out), bbox=BBOX, nlcd_file=nlcd, log=lines.append, **kw)
    return r, lines


def test_build_writes_both_formats(tmp_path, mocked):
    calls, nlcd = mocked
    out = tmp_path / "out"
    out.mkdir()
    r, _ = _build(out, nlcd)
    assert not r["failed"], r["failed"]
    ok, lines = PB.verify(str(out))
    assert ok, lines
    p1, p2 = out / PB.PACK1, out / PB.PACK2
    assert (p1 / "nd_offline_pack.sqlite").is_file() and (p2 / "LAYERS.txt").is_file()
    m1 = json.load(open(str(p1 / "manifest.json")))
    m2 = json.load(open(str(p2 / "manifest.json")))
    assert "NLCD_2021_landcover" in m1["rasters_not_included"]
    assert "NLCD_2021_landcover" in m2["rasters"]
    assert set(m1["datasets"]) == set(m2["datasets"])
    assert set(REFERENCE) - {"NLCD_2021_landcover", "DWR_unknown_structures"} <= set(
        m2["datasets"])
    assert "DWR_unknown_structures" in m2["left_out"]
    for e in list(m2["datasets"].values()) + list(m2["rasters"].values()):
        assert len(e["sha256"]) == 64 and e["url"] and e["fetched"]
    assert m2["datasets"]["HIFLD_fire_ems_stations"]["category"] == "Fire station"
    assert m1["datasets"]["HIFLD_fire_ems_stations"]["category"] == "Fire station"
    assert m2["datasets"]["DWR_dams"]["count"] == 4             # the point outside is clipped
    rd = OP.open_pack(str(p1))
    f = rd.query("HIFLD_wastewater_treatment_plants", BBOX)
    f = list(f)[0]
    assert f["properties"]["NAME"].startswith("Plant") and \
        f["properties"]["CATEGORY"] == "Wastewater treatment"
    # Setup's suite_fill finds the layers in the output folder
    from DamSafety_Setup import suite_fill as SF
    found = SF.scan(str(p2))[0]
    assert found["roads"].split("|")[0].endswith("ND_roads_NG911.gpkg"), found["roads"]
    for slot in ("dwr", "dugouts", "structures", "roads",  # lines / polygons: live smoke
                 "streams", "waterbodies", "nid", "rail", "bridges"):
        assert slot in found, slot

    # a second run downloads nothing and rewrites no layer file
    stamp = {p: os.path.getmtime(os.path.join(str(p2), *e["file"].split("/")))
             for p, e in m2["datasets"].items()}
    del calls[:]
    r2, lines = _build(out, None)
    assert calls == [], calls
    m2b = json.load(open(str(p2 / "manifest.json")))
    assert {p: os.path.getmtime(os.path.join(str(p2), *e["file"].split("/")))
            for p, e in m2b["datasets"].items()} == stamp
    assert PB.verify(str(out))[0]


def test_only_some_layers(tmp_path, mocked):
    calls, _nlcd = mocked
    out = tmp_path / "o"
    out.mkdir()
    r, _ = _build(out, None, only=["ND_counties", "DWR_dams"])
    assert sorted(calls) == ["DWR Dams", "ND_counties"]
    assert set(r["manifest2"]["datasets"]) == {"ND_counties", "DWR_dams"}
    assert PB.verify(str(out))[0]
    with pytest.raises(PB.PackBuildError):
        _build(out, None, only=["no_such_layer"])


def test_refusals(tmp_path):
    with pytest.raises(PB.PackBuildError):
        PB.check_target("")
    inside = tmp_path / "ND_Dam_Suite_Pack"
    inside.mkdir()
    with pytest.raises(PB.PackBuildError):                        # a pack folder itself
        PB.check_target(str(inside))
    with pytest.raises(PB.PackBuildError):                        # holds a foreign pack
        (inside / "manifest.json").write_text(json.dumps({"format": OP.PACK_FORMAT}))
        PB.check_target(str(tmp_path))
    ours = tmp_path / "ours"
    (ours / PB.PACK2).mkdir(parents=True)
    (ours / PB.PACK2 / "manifest.json").write_text(json.dumps({PB.MARK: "x"}))
    PB.check_target(str(ours))                                    # our own build: resumable
    with pytest.raises(PB.PackBuildError):                        # read by the plugins now
        PB.check_target(str(ours), active=[str(ours / PB.PACK2)])
    PB.check_target(str(tmp_path / "empty"))
