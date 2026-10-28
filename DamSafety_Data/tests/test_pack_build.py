"""Data 1.5.0: Build offline pack (pack_build.py) with every download mocked.

    python -m pytest -q DamSafety_Data/tests/test_pack_build.py     (from the repository root)

Needs GDAL (osgeo); skipped without it. Checks: the layer registry covers every layer of the
reference ND_Dam_Suite_Pack; one build writes format 2 AND format 1 from the same download, both
pass packcheck, open with offline_pack.open_pack and hold the same layers and counts; the
format-1 manifest names the raster it cannot hold; sha256 in the manifests; a second run
downloads nothing and rewrites nothing; the refusal rules; Setup's suite_fill finds the layers.
PR #221 / #223: HPMS_ND_2024 and NDDOT_traffic_counts both kept (AADT comparison); HIFLD only
from user-supplied (DataLumos) files, clipped to ND, no mirror; 3DHP under the NHD names with
the NHD service as fallback; the SARP Aquatic Barrier Inventory (ABI_*, removed / excluded
barriers kept and flagged in barrier_status); ND DEQ layers (NDDEQ_*)."""
import csv
import io
import json
import os
import zipfile

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
    "NLCD_2021_landcover", "OSM_crossings", "OSM_trailer_parks",
    "ABI_dams", "ABI_small_barriers_surveyed", "ABI_road_crossings_unsurveyed",
    "NDDEQ_landfills", "NDDEQ_source_water_protection_community",
    "NDDEQ_source_water_protection_noncommunity")
HIFLD_FILES = {"HIFLD_fire_ems_stations": "Fire_and_Emergency_Medical_Service_EMS_Stations.gpkg",
               "HIFLD_local_eoc": "Local_Emergency_Operations_Centers_EOC.gpkg",
               "HIFLD_local_law_enforcement": "Local_Law_Enforcement_Locations.gpkg",
               "HIFLD_wastewater_treatment_plants": "Wastewater_Treatment_Plants.gpkg"}
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
    assert kinds["HPMS_ND"] == "first_of" and "NDDOT_traffic_counts" in kinds  # both AADT
    t = {x["name"]: x for x in table}
    assert "NDDOT_traffic_counts" in t["HPMS_ND"]["info"]["note"]
    for n in ("NDDEQ_landfills", "NDDEQ_source_water_protection_community",
              "NDDEQ_source_water_protection_noncommunity"):
        assert t[n]["kind"] == "arcgis" and t[n]["group"] == "nddeq"
        assert t[n]["spec"]["url"].startswith("https://ndgishub.nd.gov/arcgis/rest/")
    from DamSafety_Data.damsuite import gpkgpack as GP
    assert GP.group_of("NDDEQ_landfills") == "nddeq"
    assert kinds["NHD_flowlines"] == kinds["NHD_waterbodies"] == "hydro"
    t = {x["name"]: x for x in table}
    assert "3DHP_all/MapServer/50" in t["NHD_flowlines"]["spec"]["url"]
    assert "NHDPlus_HR" in t["NHD_flowlines"]["spec"]["fallback"]["url"] or \
        "nhd" in t["NHD_flowlines"]["spec"]["fallback"]["url"].lower()
    # HIFLD: no file -> left out with the DataLumos page (no mirror any more)
    h = t["HIFLD_local_eoc"]
    assert h["kind"] == "none" and "datalumos.org" in h["info"]["why"]
    assert not hasattr(PB, "HIFLD_MIRRORS")
    assert t["HIFLD_wastewater_treatment_plants"]["info"]["why"].count("/239262")
    with pytest.raises(TypeError):
        PB.registry(hifld_mirrors=True)
    for n in ("ABI_dams", "ABI_small_barriers_surveyed", "ABI_road_crossings_unsurveyed"):
        assert t[n]["kind"] == "abi" and t[n]["group"] == "barriers"


def test_hifld_files(tmp_path):
    for fn in list(HIFLD_FILES.values()) + ["State_Emergency_Operations_Centers_EOC.gpkg",
                                            "metadata.json",
                                            "ICIS_Wastewater_Treatment_Plants_SIC_Codes.zip",
                                            "Wastewater_Treatment_Plants_NAICS_Codes.csv"]:
        (tmp_path / fn).write_text("x")
    got = PB._hifld_files(str(tmp_path))
    assert {k: os.path.basename(v) for k, v in got.items()} == HIFLD_FILES
    t = {x["name"]: x for x in PB.registry(hifld_folder=str(tmp_path))}
    assert t["HIFLD_local_eoc"]["kind"] == "ogr"
    assert t["HIFLD_local_eoc"]["info"]["source_page"].endswith("/239489")


def test_abi_features():
    head = ["sarpid", "lat", "lon", "huc8", "hasnetwork", "height", "name"]
    rows = [["d1", "46.8", "-100.8", "09030001", "True", "12", "A"],
            ["d2", "46.8", "-100.75", "09030002", "False", "", "B"],
            ["d3", "48.0", "-97.0", "09020001", "True", "3", "C"]]           # outside
    f = PB.abi_features(head, rows, BBOX)
    assert [x["properties"]["sarpid"] for x in f] == ["d1", "d2"]
    p = f[0]["properties"]
    assert p["huc8"] == "09030001" and p["height"] == 12 and p["network"] == "on-network"
    assert f[1]["properties"]["network"] == "off-network" and f[1]["properties"]["height"] is None
    assert f[0]["geometry"] == {"type": "Point", "coordinates": [-100.8, 46.8]}
    assert p["barrier_status"] == "present"


def test_barrier_status_kept_and_flagged():
    head = ["sarpid", "lat", "lon", "hasnetwork", "removed", "excluded", "yearremoved"]
    rows = [["a", "46.8", "-100.8", "True", "no", "no", ""],
            ["b", "46.8", "-100.8", "True", "yes", "no", "2019"],
            ["c", "46.8", "-100.8", "False", "no", "yes", ""],
            ["d", "46.8", "-100.8", "False", "yes", "yes", "2001"]]
    f = PB.abi_features(head, rows, BBOX)
    assert [x["properties"]["barrier_status"] for x in f] == [
        "present", "removed", "excluded", "removed+excluded"]               # none dropped
    assert f[1]["properties"]["removed"] == "yes" and f[1]["properties"]["yearremoved"] == 2019
    assert PB.barrier_status({"Removed": "Yes"}) == "removed"


def test_registry_local_dwr_folder(tmp_path):
    d = tmp_path / "dwr"
    d.mkdir()
    _gpkg(str(d / "DWR_Unknown_Structures.gpkg"), "DWR_Unknown_Structures", 2)
    t = {x["name"]: x for x in PB.registry(dwr_folder=str(d))}
    assert t["DWR_unknown_structures"]["kind"] == "local_gpkg"


def _hifld_gpkg(path, layer):
    """Four points in ND (one with a CWP_NAME) and one with STATE = MN inside the box: kept /
    dropped by the ND filter."""
    from osgeo import ogr, osr
    ds = ogr.GetDriverByName("GPKG").CreateDataSource(path)
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    lyr = ds.CreateLayer(layer, srs, ogr.wkbPoint)
    for k in ("CWP_NAME", "STATE"):
        lyr.CreateField(ogr.FieldDefn(k, ogr.OFTString))
    for i, st in enumerate(("ND", "ND", "North Dakota", "", "MN")):
        f = ogr.Feature(lyr.GetLayerDefn())
        f.SetField("CWP_NAME", "Plant %d" % i)
        f.SetField("STATE", st)
        f.SetGeometry(ogr.CreateGeometryFromWkt("POINT (%f 46.8)" % (-100.8 + 0.001 * i)))
        lyr.CreateFeature(f)
    ds = None


def _abi_csv(head, rows):
    b = io.StringIO()
    w = csv.writer(b, quoting=csv.QUOTE_ALL)
    w.writerow(head)
    w.writerows(rows)
    return b.getvalue()


def _abi_download(calls):
    """PB._download stand-in: the public API's state CSV / metadata and the national
    road-crossings zip (two ND rows - one unsurveyed - and one Minnesota row)."""
    def dl(url, dest, refresh, log, max_age=86400):
        calls.append("ABI " + url.split("/public/")[-1].split("/downloads/")[-1])
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        if url.endswith("/metadata"):
            body = json.dumps({"data_version": "4.4.0", "data_publish_date": "10/09/2026",
                               "citation": "NACC 2026", "terms_of_use": "credit NACC"})
        elif "state?id=ND" in url:
            body = _abi_csv(["sarpid", "lat", "lon", "hasnetwork", "huc8", "state", "removed",
                             "excluded"], [
                ["s%d" % i, "46.8", str(-100.8 + 0.001 * i), "True" if i else "False",
                 "09030001", "ND", "yes" if i == 2 else "no", "no"] for i in range(3)])
        else:
            head = ["SARPID", "lat", "lon", "OnNetwork", "Surveyed", "State"]
            data = _abi_csv(head, [["c1", "46.8", "-100.8", "yes", "not likely", "North Dakota"],
                                   ["c2", "46.8", "-100.79", "yes", "yes", "North Dakota"],
                                   ["c3", "46.8", "-100.78", "no", "not likely", "Minnesota"]])
            with zipfile.ZipFile(dest, "w") as z:
                z.writestr(PB.ABI_ROAD_MEMBER, data)
                z.writestr("README.txt", "Data version: 4.4.0\nData published: 10/09/2026\n")
                z.writestr("TERMS_OF_USE.txt", "By downloading these data, you agree.\n"
                                               "Citation: NACC 2026\n")
            return dest, True
        with open(dest, "w") as f:
            f.write(body)
        return dest, True
    return dl


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
    monkeypatch.setattr(PB, "_download", _abi_download(calls))
    hf = tmp_path / "hifld"
    hf.mkdir()
    for n, fn in HIFLD_FILES.items():
        _hifld_gpkg(str(hf / fn), n)
    monkeypatch.setattr(PB, "_TEST_HIFLD", str(hf), raising=False)
    return calls, _tif(str(tmp_path / "nlcd.tif"))


def _build(out, nlcd, **kw):
    lines = []
    kw.setdefault("hifld_folder", getattr(PB, "_TEST_HIFLD", None))
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
    # both AADT sources, documented in both manifests and LAYERS.txt
    for m in (m1, m2):
        assert "HPMS_ND_2024" in m["datasets"] and "NDDOT_traffic_counts" in m["datasets"]
        assert "for comparison" in m["datasets"]["HPMS_ND_2024"]["note"]
        assert "for comparison" in m["datasets"]["NDDOT_traffic_counts"]["note"]
    assert m2["datasets"]["HPMS_ND_2024"]["file"] == "roads/HPMS_ND_2024.gpkg"
    layers = (p2 / "LAYERS.txt").read_text()
    assert "for comparison" in layers and "barrier_status" in layers
    # ND DEQ layers: nddeq/ folder, URL / date / sha256 in both formats
    for n in ("NDDEQ_landfills", "NDDEQ_source_water_protection_community",
              "NDDEQ_source_water_protection_noncommunity"):
        assert m2["datasets"][n]["file"] == "nddeq/{}.gpkg".format(n)
        assert m1["datasets"][n]["count"] == m2["datasets"][n]["count"] == 3
        assert m1["datasets"][n]["url"] and m2["datasets"][n]["fetched"]
        assert len(m2["datasets"][n]["sha256"]) == 64 and m1["sha256"]
        assert "Environmental Quality" in m1["datasets"][n]["source"]
    assert "NDDEQ" in layers
    # HIFLD from the folder, the Minnesota point dropped; source page in both manifests
    for m in (m1, m2):
        e = m["datasets"]["HIFLD_local_eoc"]
        assert e["count"] == 4 and e["source_url"].endswith("/239489"), e
        assert len(e["source_sha256"]) == 64
    # 3DHP under the NHD names (the mock answers with plain features)
    assert m2["datasets"]["NHD_flowlines"]["hydro_source"] == "3DHP"
    assert m1["datasets"]["NHD_flowlines"]["hydro_3dhp_name"] == "3DHP_flowlines"
    # Aquatic Barrier Inventory: API layers and the unsurveyed ND road crossings
    d = m2["datasets"]["ABI_dams"]
    assert d["count"] == 3 and d["file"].startswith("barriers/"), d
    assert d["data_version"] == "4.4.0" and d["citation"] and d["terms_of_use"]
    assert d["counts"] == {"network=on-network": 2, "network=off-network": 1,
                           "barrier_status=present": 2, "barrier_status=removed": 1}
    assert "barrier_status" in d["flag_field"] and "barrier_status" in d["note"]
    assert sorted(f["properties"]["barrier_status"] for f in OP.open_pack(str(p1)).query(
        "ABI_dams", BBOX)) == ["present", "present", "removed"]
    rc = m1["datasets"]["ABI_road_crossings_unsurveyed"]
    assert rc["count"] == 1 and "not likely" in rc["filter"] and rc["data_version"] == "4.4.0"
    assert [f["properties"]["sarpid"] for f in OP.open_pack(str(p1)).query(
        "ABI_road_crossings_unsurveyed", BBOX)] == ["c1"]
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


def test_hydro_fallback_and_3dhp_fields(tmp_path, monkeypatch):
    """3DHP answers: NHD-style fields added and work units counted; 3DHP fails: the NHD
    service is used and the manifest says why."""
    def run_one(ctx, name, label, kind, spec, bbox):
        if "3DHP" in spec["url"] and name == "NHD_waterbodies":
            raise OSError("HTTP 504")
        w = ctx.writer
        w.begin(name, label, spec["url"], resume=False)
        p = {"id3dhp": "a1", "gnisidlabel": "Missouri River", "gnisid": "1",
             "featuretypelabel": "Stream/River", "featuretype": 460,
             "workunitid": "NHD"} if "3DHP" in spec["url"] else {"gnis_name": "Lake", "ftype": 390}
        w.add([{"type": "Feature", "geometry": {"type": "LineString", "coordinates": [
            [-100.8, 46.8], [-100.79, 46.81]]}, "properties": p}], lambda f: "x:1")
        return w.finish("mock")
    monkeypatch.setattr(BP, "_run_one", run_one)
    monkeypatch.setattr(BP, "_NET_ERRORS", (OSError,))
    out = tmp_path / "h"
    out.mkdir()
    r, lines = _build(out, None, only=["NHD_flowlines", "NHD_waterbodies"], hifld_folder=None)
    assert not r["failed"], r["failed"]
    m = r["manifest2"]["datasets"]
    assert m["NHD_flowlines"]["hydro_source"] == "3DHP"
    assert m["NHD_flowlines"]["coverage_3dhp"]["nhd_migrated"] == 1
    assert m["NHD_waterbodies"]["hydro_source"] == "NHD" and "504" in m[
        "NHD_waterbodies"]["fallback_reason"]
    f = list(OP.open_pack(str(out / PB.PACK1)).query("NHD_flowlines", BBOX))[0]["properties"]
    assert f["gnis_name"] == "Missouri River" and f["ftype"] == 460 and f["fcode"] == 46000
    assert PB.verify(str(out))[0]
