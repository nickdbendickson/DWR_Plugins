"""v3.10.16 (HEC-RAS P0): the export tool builds the HEC-RAS 2D model package with
core/hecras_model.build_model; the inflow point is never put on a hill next to the dam; the dam
connection is trimmed to the dam's own length; the run report records what the builder needs.

Synthetic data only (a valley with a 10 m dam and a 40 m hill 400 m from the dam point, as at
DWR-3161), GDAL from the QGIS Python. Runs in the Runner suite (run_tests.py) and under pytest.
The keyword check compares every 'Keyword=' line written against the keyword names in HEC's own
HEC-RAS 6.6 example projects (tests/data/hecras66_example_keywords.json)."""
import json
import os
import re
import shutil
import tempfile
import zipfile

import numpy as np

from nd_breach_runner.core import breach as br
from nd_breach_runner.core import hecras
from nd_breach_runner.core import hecras_model as hm
from nd_breach_runner.core import terrain
from nd_breach_runner.core.grid import Grid
from nd_breach_runner.tests.registry import test

HERE = os.path.dirname(os.path.abspath(__file__))
KW_FILE = os.path.join(HERE, "data", "hecras66_example_keywords.json")
X0, Y0 = 500000.0, 5200000.0          # NAD83 / UTM 14N: lon -99, lat 46.9 (ND South zone)
FLOOR_M, H_M = 300.0, 10.0
TOP_M = FLOOR_M + H_M
HILL = (X0 - 280.0, Y0 - 280.0)       # 396 m from the dam point, 40 m above the floor


def _gdal():
    try:
        from osgeo import gdal, osr
        gdal.UseExceptions()
        return gdal, osr
    except ImportError:
        return None, None


def _ground(X, Y, hill=True):
    """A valley sloping south (the flow), walls 2% each side, a 10 m dam along y = Y0 for
    |x - X0| <= 250 m, and a hill to the south-west."""
    z = FLOOR_M + 0.002 * (Y - Y0) + 0.02 * np.abs(X - X0)
    dam = (np.abs(Y - Y0) <= 15.0) & (np.abs(X - X0) <= 250.0)
    z = np.where(dam, np.maximum(z, TOP_M), z)
    if hill:
        z = z + 40.0 * np.exp(-((X - HILL[0]) ** 2 + (Y - HILL[1]) ** 2) / (2 * 60.0 ** 2))
    return z


def _tif(path, a, x0, ytop, dx, epsg=26914):
    gdal, osr = _gdal()
    ds = gdal.GetDriverByName("GTiff").Create(path, a.shape[1], a.shape[0], 1, gdal.GDT_Float32)
    ds.SetGeoTransform((x0, dx, 0, ytop, 0, -dx))
    s = osr.SpatialReference()
    s.ImportFromEPSG(epsg)
    ds.SetProjection(s.ExportToWkt())
    b = ds.GetRasterBand(1)
    b.SetNoDataValue(-9999.0)
    b.WriteArray(a.astype(np.float32))
    ds = None


def _dam(**k):
    d = dict(dam_id="SYN-1", dam_height_ft=H_M / 0.3048, storage_top_acft=600.0,
             top_of_dam_ft=TOP_M / 0.3048, name="Synthetic")
    d.update(k)
    return br.DamData(**d)


def _run(folder, inflow_xy=(X0, Y0 - 60.0), dam=None, dam_json_len_ft=None):
    """A dam folder as a finished Runner run leaves it (only what build_model reads)."""
    dam = dam or _dam()
    dx = 10.0
    x0, x1, y0, y1 = X0 - 3000, X0 + 3000, Y0 - 6000, Y0 + 2500
    nx, ny = int((x1 - x0) / dx), int((y1 - y0) / dx)
    X, Y = np.meshgrid(x0 + (np.arange(nx) + 0.5) * dx, y1 - (np.arange(ny) + 0.5) * dx)
    dem = os.path.join(folder, "dem.tif")
    _tif(dem, _ground(X, Y), x0, y1, dx)
    depth = np.where((Y < Y0 - 20) & (Y > Y0 - 5000) & (np.abs(X - X0) < 400.0), 1.5, -9999.0)
    _tif(os.path.join(folder, "Depth (Max).tif"), depth / 1.0, x0, y1, dx)
    m = os.path.join(folder, hecras.MODEL_FOLDER)
    os.makedirs(m)
    hy = br.make_hydrograph(dam, br.METHOD_PIPING)
    hy.to_csv(os.path.join(m, hecras.HYDROGRAPH_CSV))
    rep = {"tool": "ND Dam Breach Runner test", "dam": br.dam_to_dict(dam), "dam_xy": [X0, Y0],
           "hydrograph": hy.summary(), "scenario": "sunny",
           "work_crs": "EPSG:26914", "dem_z_to_m": 1.0, "dem_elevation_units": "metres",
           "settings": {"cell_size_m": 20.0, "manning_n": 0.06, "input_files": {"DEM": dem}},
           "summary": {"latest_arrival_hr": 2.0, "max_velocity_fps": 10.0, "max_depth_ft": 5.0}}
    if inflow_xy is not None:
        rep["inflow_point"] = {"xy": list(inflow_xy), "how": "test"}
    with open(os.path.join(m, hecras.RUN_REPORT_JSON), "w", encoding="utf-8") as f:
        json.dump(rep, f)
    if dam_json_len_ft:
        with open(os.path.join(folder, "dam.json"), "w", encoding="utf-8") as f:
            json.dump({"format": "nd_dam_suite/dam_record", "format_version": 1,
                       "fields": {"dam_id": "SYN-1", "dam_length_ft": dam_json_len_ft}}, f)
    return dem


def _keywords():
    with open(KW_FILE, encoding="utf-8") as f:
        d = json.load(f)
    return ({e: set(v) for e, v in d["keywords"].items()},
            {e: set(v) for e, v in d["flags"].items()})


def unknown_keywords(path):
    """'Keyword=' names (and bare flag lines of .prj / .p##) in a written file that HEC's
    example projects never use."""
    kw, fl = _keywords()
    ext = re.sub(r"\d+$", "", path.rsplit(".", 1)[1].lower())
    bad = set()
    with open(path, encoding="latin-1") as f:
        for line in f.read().replace("\r", "").split("\n"):
            if "=" in line:
                k = line.split("=", 1)[0]
                if re.match(r"^[A-Za-z]", k) and k not in kw[ext]:
                    bad.add(k)
            elif ext in fl and re.match(r"^[A-Za-z][A-Za-z ]+$", line.strip()) \
                    and line.strip() not in fl[ext]:
                bad.add("flag: " + line.strip())
    return bad


def _skip():
    return _gdal()[0] is None


# ============================================================================ pure helpers
@test
def test_hecras_p0_crest_is_never_a_hill():
    """DWR-3161: the highest ground within 400 m of a dam point on the crest was a 1937.8 ft
    hill (top of dam 1844.4 ft); place_from_crest moved the crest there and put the inflow on
    the hillside. Now the crest is ground at the top of dam and the inflow is below the dam."""
    dx = 5.0
    xs = X0 - 600 + (np.arange(240) + 0.5) * dx
    ys = Y0 + 600 - (np.arange(240) + 0.5) * dx
    X, Y = np.meshgrid(xs, ys)
    g = Grid(_ground(X, Y), X0 - 600, Y0 + 600, dx)
    crest, cz, note = terrain.embankment_crest(g, (X0, Y0), H_M, TOP_M, 400.0)
    assert np.hypot(crest[0] - X0, crest[1] - Y0) < 2 * dx, (crest, note)
    assert abs(cz - TOP_M) <= terrain.crest_band_m(H_M)
    inflow, moved, why = terrain.place_from_crest(g, (X0, Y0), H_M, TOP_M, 400.0)
    zi = g.data[g.rc(*inflow)]
    assert zi <= TOP_M - 0.5 * H_M + 1e-6, (zi, why)
    assert np.hypot(inflow[0] - HILL[0], inflow[1] - HILL[1]) > 150.0, why
    assert moved is None and "top of dam" in why
    # a label point off the embankment moves to the nearest crest ground, not to the hill
    inflow2, crest2, why2 = terrain.place_from_crest(g, (X0 - 100.0, Y0 + 80.0), H_M, TOP_M, 400)
    assert crest2 is not None and abs(crest2[1] - Y0) <= 20.0, (crest2, why2)
    assert g.data[g.rc(*inflow2)] < TOP_M - 0.4 * H_M
    # all ground far above the top of dam: no crest, a clear reason (not a hillside inflow)
    hi = Grid(np.full((40, 40), TOP_M + 50.0), X0 - 100, Y0 + 100, dx)
    pt, cr, reason = terrain.place_from_crest(hi, (X0, Y0), H_M, TOP_M, 400.0)
    assert pt is None and cr is None and "above the top of dam" in reason


@test
def test_hecras_p0_inflow_check_and_dam_line_trim():
    top, h = 1844.44 * 0.3048, 28.1 * 0.3048
    assert "72.3 ft above" in hm.inflow_check(1916.74 * 0.3048, top, h)
    assert hm.inflow_check(1826.3 * 0.3048, top, h) == ""
    assert hm.inflow_check(None, top, h) == ""
    A, B = np.array([0.0, 0.0]), np.array([10000.0, 0.0])
    a, b, L, note = hm.trim_dam_line(A, B, (3000.0, 50.0), 1200.0)
    assert abs(L - 1200.0) < 1e-9 and abs(a[0] - 2400.0) < 1e-9 and abs(b[0] - 3600.0) < 1e-9
    assert "1,200 of the 10,000" in note
    a, b, L, _ = hm.trim_dam_line(A, B, (100.0, 0.0), 1200.0)        # kept on the edge
    assert abs(a[0]) < 1e-9 and abs(b[0] - 1200.0) < 1e-9
    a, b, L, note = hm.trim_dam_line(A, B, (100.0, 0.0), 20000.0)    # not shorter: unchanged
    assert note == "" and L == 10000.0
    a, b, L, _ = hm.trim_dam_line(A, B, (5000.0, 0.0), 50.0, min_length=300.0)
    assert abs(L - 300.0) < 1e-9
    ring = np.array([[0, 0], [10000, 0], [10000, -5000], [0, -5000]], float)
    r2 = hm.insert_on_ring(ring, [(2400.0, 0.0), (3600.0, 0.0), (0.0, 0.0)])
    assert len(r2) == 6 and tuple(r2[1]) == (2400.0, 0.0) and tuple(r2[2]) == (3600.0, 0.0)


@test
def test_hecras_p0_structure_block_keywords_are_hecs():
    c = {"name": "Culv 1", "desc": "test", "line": [(0.0, 0.0), (100.0, 0.0)], "weir_width": 30.0,
         "weir_coef": 2.6, "weir": [(0.0, 105.0), (50.0, 100.0), (100.0, 105.0)],
         "culvert": {"shape": 1, "rise": 4.0, "span": 4.0, "length": 60.0, "n": 0.013,
                     "entrance": 0.5, "exit": 1.0, "chart": 1, "scale": 1, "inv_up": 95.0,
                     "inv_dn": 94.5, "barrels": 2, "stations": [48.0, 52.0]}}
    import datetime
    d = tempfile.mkdtemp()
    try:
        p = os.path.join(d, "x.g01")
        with open(p, "w", newline="") as f:
            f.write("\r\n".join(hm.structure_block(c, datetime.datetime(2026, 1, 1))) + "\r\n")
        assert unknown_keywords(p) == set(), unknown_keywords(p)
    finally:
        shutil.rmtree(d)


# ============================================================================ build_model
@test
def test_hecras_p0_build_model_on_a_synthetic_run():
    if _skip():
        return
    from osgeo import gdal
    d = tempfile.mkdtemp()
    try:
        _run(d)
        out = os.path.join(d, "model")
        r = hm.build_model(d, out_folder=out)
        m, info = r["model"], r["info"]
        for ext in ("prj", "g01", "u01", "p01", "rasmap"):
            p = os.path.join(out, "{}.{}".format(m.name, ext))
            assert os.path.isfile(p), p
            raw = open(p, "rb").read()
            assert b"\r\n" in raw and raw.count(b"\n") == raw.count(b"\r\n"), ext   # CRLF only
            if ext != "rasmap":
                assert unknown_keywords(p) == set(), (ext, unknown_keywords(p))
        for rel in ("GIS/Projection.prj", "GIS/HECRAS Layout.gpkg", "README_HECRAS.md",
                    "Terrain/{}.tif".format(m.terrain_name)):
            assert os.path.isfile(os.path.join(out, rel)), rel
        # units and CRS: a dam in southern North Dakota -> State Plane South, international ft
        assert info["crs"] == "EPSG:2266" and info["units"] == "English Units"
        assert "English Units" in open(os.path.join(out, m.name + ".prj")).read()
        t = gdal.Open(os.path.join(out, "Terrain", m.terrain_name + ".tif"))
        assert "2266" in t.GetProjection() or "North Dakota South" in t.GetProjection()
        # the terrain is in feet: the valley floor is 300 m (984 ft) at the dam falling 2 m/km
        # downstream (lowest ~288 m = ~945 ft); in metres it would be under 310
        a = t.GetRasterBand(1).ReadAsArray()
        ok = a > -9000
        assert 930.0 < float(a[ok].min()) < 990.0, float(a[ok].min())
        # the dam: the weir is at the top of dam (ft) or the ground where higher
        top_ft = TOP_M / 0.3048
        assert min(e for _, e in m.weir) >= top_ft - 0.01
        assert abs(m.initial_wse - top_ft) < 0.05
        # the breach centre comes from the inflow point 60 m below the dam point
        cs = m.breach["center_station"]
        assert 0 < cs < info["dam_line"]
        g = open(os.path.join(out, m.name + ".g01")).read()
        assert "Storage Area Is2D=-1" in g and "Connection Up SA=Reservoir" in g
        assert "Breach Geom=" in open(os.path.join(out, m.name + ".p01")).read()
    finally:
        shutil.rmtree(d)


@test
def test_hecras_p0_hillside_inflow_is_walked_off_the_hill():
    """A run before 3.10.16 put the inflow on the hill (DWR-3161). build_model does not use
    it there (TIER 2 fix: it walks it down the DEM); the note says why and the breach centre is
    near the dam point, not the hill."""
    if _skip():
        return
    d = tempfile.mkdtemp()
    try:
        _run(d, inflow_xy=(HILL[0] + 25.0, HILL[1] + 20.0))
        r = hm.build_model(d, out_folder=os.path.join(d, "model"))
        # TIER 2 fix: the hill point is walked down off the hill (no flow path in this run: the
        # DEM's steepest descent) to ground a quarter of the dam height below the top of dam
        assert any("inflow point was moved" in n and "above" in n and "steepest descent" in n
                   for n in r["notes"]), r["notes"]
        stt = r["info"]["inflow_status"]
        assert stt["status"] == "moved", stt
        assert stt["ground_ft"] <= (TOP_M - 0.25 * H_M) / 0.3048 + 1e-6, stt
        assert stt["original_ground_ft"] > TOP_M / 0.3048, stt
        good = tempfile.mkdtemp()
        try:
            _run(good)
            rg = hm.build_model(good, out_folder=os.path.join(good, "model"))
        finally:
            shutil.rmtree(good)
        assert not any("inflow point was not used" in n for n in rg["notes"])
        # same dam, same edge: the hill run's breach centre lies within the dam (|x| <= 250 m)
        # of the dam point, like the good run's
        ft = 0.3048
        for res in (r, rg):
            L = res["info"]["dam_line"]
            assert abs(res["model"].breach["center_station"] - 0.5 * L) <= 250.0 / ft, \
                (res["model"].breach["center_station"], L)
    finally:
        shutil.rmtree(d)


@test
def test_hecras_p0_dam_line_trimmed_to_dam_json_length():
    if _skip():
        return
    d = tempfile.mkdtemp()
    try:
        _run(d, dam_json_len_ft=1500.0)
        r = hm.build_model(d, out_folder=os.path.join(d, "model"))
        m, info = r["model"], r["info"]
        L = float(np.hypot(*np.subtract(m.conn_line[-1], m.conn_line[0])))
        assert abs(L - 1500.0) < 1.0 and abs(info["dam_line"] - 1500.0) < 1.0, (L, info)
        assert any("dam.json dam_length_ft" in n for n in r["notes"]), r["notes"]
        assert 0.0 <= m.weir[0][0] and m.weir[-1][0] <= 1500.0 + 1e-6
        # the connection ends are perimeter vertices
        per = np.asarray(m.perimeter)
        for p in (m.conn_line[0], m.conn_line[-1]):
            assert np.hypot(*(per - np.asarray(p)).T).min() < 1e-3
        # centred on the dam point: X0 in the model CRS is the middle of the line
        mid = np.mean([m.conn_line[0], m.conn_line[-1]], axis=0)
        from osgeo import osr
        s, t = osr.SpatialReference(), osr.SpatialReference()
        s.ImportFromEPSG(26914)
        t.ImportFromEPSG(2266)
        s.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        t.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        px, py, _ = osr.CoordinateTransformation(s, t).TransformPoint(X0, Y0)
        assert np.hypot(mid[0] - px, mid[1] - py) < 60.0, (mid, px, py)
        # an option overrides dam.json; the run's crest length beats dam.json
        r2 = hm.build_model(d, out_folder=os.path.join(d, "m2"), options={"dam_length_ft": 900})
        assert abs(r2["info"]["dam_line"] - 900.0) < 1.0
        assert hm.dam_length_ft(_dam(crest_length_ft=700.0), d) == (700.0,
                                                                     "the run's crest length")
    finally:
        shutil.rmtree(d)


@test
def test_hecras_p0_package_and_zip():
    if _skip():
        return
    import datetime
    from nd_breach_runner.core import hecras_package as hp
    d = tempfile.mkdtemp()
    try:
        _run(d)
        when = datetime.date(2026, 10, 18)
        p = hp.build_package(d, when=when)
        assert p["folder"] == os.path.join(d, "_breach_model", "hecras", "SYN-1_20261018")
        assert p["zip"] == p["folder"] + ".zip" and zipfile.is_zipfile(p["zip"])
        names = zipfile.ZipFile(p["zip"]).namelist()
        top = "SYN-1_20261018/"
        for rel in ("SYN_1.prj", "SYN_1.g01", "SYN_1.u01", "SYN_1.p01", "SYN_1.rasmap",
                    "GIS/Projection.prj", "Terrain/SYN_1 Terrain.tif", hp.README, hp.MANIFEST,
                    "README_HECRAS.md", hp.REFERENCE + "/HECRAS Breach Hydrograph.csv"):
            assert top + rel in names, rel
        txt = open(os.path.join(p["folder"], hp.README), "rb").read().decode("utf-8")
        assert "\r\n" in txt and "NOT yet been opened in HEC-RAS" in txt
        assert "SYN_1.p01.hdf" in txt and "Create a New RAS Terrain" in txt
        man = json.load(open(os.path.join(p["folder"], hp.MANIFEST)))
        assert man["opened_in_hecras"] is False and man["crs"] == "EPSG:2266"
        assert all(len(f["sha256"]) == 64 for f in man["files"])
        # a second package the same day gets _2, the first is untouched
        p2 = hp.build_package(d, when=when, make_zip=False)
        assert p2["folder"].endswith("SYN-1_20261018_2") and p2["zip"] is None
        assert os.path.isfile(p["zip"])
        # a run without its depth raster: a clear error and no half package left behind
        os.remove(os.path.join(d, "Depth (Max).tif"))
        try:
            hp.build_package(d, when=datetime.date(2026, 10, 19))
            assert False, "expected HecRasModelError"
        except hm.HecRasModelError as e:
            assert "Depth (Max).tif" in str(e)
        assert not os.path.exists(os.path.join(d, "_breach_model", "hecras", "SYN-1_20261019"))
    finally:
        shutil.rmtree(d)


@test
def test_hecras_p0_old_skeleton_writer_is_retired():
    try:
        hecras.write_ras_project("x", "y", {}, None, {})
        assert False
    except hecras.HecRasError as e:
        assert "retired" in str(e)
    d = tempfile.mkdtemp()
    try:
        m = os.path.join(d, hecras.MODEL_FOLDER)
        os.makedirs(m)
        dam = _dam()
        hy = br.make_hydrograph(dam, br.METHOD_PIPING)
        hy.to_csv(os.path.join(m, hecras.HYDROGRAPH_CSV))
        with open(os.path.join(m, hecras.RUN_REPORT_JSON), "w") as f:
            json.dump({"dam": br.dam_to_dict(dam), "hydrograph": hy.summary()}, f)
        r = hecras.export_run(d)
        assert "ras_project" not in r and os.path.isfile(r["hydrograph_txt"])
        assert not os.path.exists(os.path.join(d, "_hecras", "mesh.geojson"))
    finally:
        shutil.rmtree(d)


# ============================================================ TIER 2 fix: inflow walked downstream
def _flow_line(folder, pts):
    m = os.path.join(folder, hecras.MODEL_FOLDER)
    with open(os.path.join(m, "qa_dam_inflow_and_flow_path.geojson"), "w",
              encoding="utf-8") as f:
        json.dump({"type": "FeatureCollection", "features": [
            {"type": "Feature", "properties": {"what": "flow path used to lay out the domain"},
             "geometry": {"type": "LineString", "coordinates": [list(p) for p in pts]}}]}, f)


@test
def test_hecras_p0_relocate_inflow_walks_the_path_then_the_dem():
    """Pure: the first point down the flow path low enough and off the embankment; without a
    path, the steepest descent; None (with the lowest ground seen) when there is none."""
    ground = lambda xy: 100.0 + 0.01 * np.asarray(xy, float).reshape(-1, 2)[:, 1]  # noqa: E731
    path = [(0.0, 0.0), (0.0, -1000.0)]
    diag = {}
    r = hm.relocate_inflow((0.0, -5.0), ground, 99.5, (0.0, 0.0), 30.0, 500.0, 5.0, path, diag)
    assert r["how"] == "the run's downstream flow path", r
    assert abs(r["xy"][1] + 50.0) < 1e-6 and abs(r["dist"] - 45.0) < 1e-6, r
    r = hm.relocate_inflow((0.0, -5.0), ground, 99.5, (0.0, 0.0), 80.0, 500.0, 5.0, path)
    assert abs(r["xy"][1] + 80.0) < 1e-6, r                    # off the embankment first
    r = hm.relocate_inflow((0.0, -5.0), ground, 99.5, (0.0, 0.0), 30.0, 500.0, 5.0, None)
    assert r["how"] == "the steepest descent of the DEM", r
    assert r["xy"][1] <= -50.0 + 1e-6 and r["ground_m"] <= 99.5, r
    diag = {}
    assert hm.relocate_inflow((0.0, -5.0), ground, 90.0, (0.0, 0.0), 30.0, 200.0, 5.0, path,
                              diag) is None
    assert abs(diag["lowest_m"] - 97.95) < 1e-6 and abs(diag["lowest_dist"] - 200.0) < 1e-6, diag


@test
def test_hecras_p0_inflow_on_the_dam_is_walked_down_the_flow_path():
    """DWR-3161 (TIER 2): the run's inflow point was too close to the dam and too high. It is
    walked down the run's flow line off the embankment, used, and recorded in run_report.json
    with the distance moved."""
    if _skip():
        return
    d = tempfile.mkdtemp()
    try:
        _run(d, inflow_xy=(X0, Y0 - 10.0))                  # on the 30 m wide embankment
        _flow_line(d, [(X0, Y0 + 50.0 - 10.0 * k) for k in range(400)])
        r = hm.build_model(d, out_folder=os.path.join(d, "model"))
        stt = r["info"]["inflow_status"]
        assert stt["status"] == "moved" and stt["how"] == "the run's downstream flow path", stt
        off_ft = 0.5 * 20.0 + 3.0 * H_M / 0.3048                # crest half + 3H:1V slope
        assert off_ft - 1.0 <= stt["moved_ft"] + 10.0 / 0.3048 <= off_ft + 20.0, stt
        assert stt["ground_ft"] <= (TOP_M - 0.25 * H_M) / 0.3048, stt
        assert r["info"]["inflow_xy_model"] == stt["xy_model"]
        assert any("inflow point was moved" in n and "flow path" in n for n in r["notes"])
        with open(os.path.join(d, hecras.MODEL_FOLDER, hecras.RUN_REPORT_JSON),
                  encoding="utf-8") as f:
            rep = json.load(f)
        rec = rep["hecras_inflow"]
        assert rec["status"] == "moved" and rec["moved_ft"] == stt["moved_ft"], rec
        assert abs(rec["xy"][0] - X0) < 2.0 and Y0 - 60.0 < rec["xy"][1] < Y0 - 30.0, rec
        assert rec["run_xy"] == [X0, Y0 - 10.0] and rep["inflow_point"]["xy"] == rec["run_xy"]
        from nd_breach_runner.core import hecras_engine as he
        res = he.write_project(d, os.path.join(d, "hy"), inflow_mode="hydrograph",
                               log=lambda m: None, env={})
        assert res["inflow_bc"] is not None
    finally:
        shutil.rmtree(d)


@test
def test_hecras_p0_no_valid_inflow_point_fails_with_the_real_reason():
    """No ground low enough within the search distance: the point is reported rejected (not
    missing), with the lowest ground found, and hydrograph mode says so."""
    if _skip():
        return
    from nd_breach_runner.core import hecras_engine as he
    d = tempfile.mkdtemp()
    try:
        _run(d, inflow_xy=(X0, Y0 - 10.0))
        _flow_line(d, [(X0, Y0 + 50.0 - 10.0 * k) for k in range(400)])
        opts = {"inflow_search_ft": 60.0}               # ends on the embankment's toe
        try:
            he.write_project(d, os.path.join(d, "hy"), options=opts, inflow_mode="hydrograph",
                             log=lambda m: None, env={})
            raise AssertionError("write_project should have failed")
        except he.HecRasEngineError as e:
            msg = str(e)
        assert "was rejected" in msg and "no point" in msg and "60 ft downstream" in msg, msg
        assert "has no inflow point" not in msg, msg
        with open(os.path.join(d, hecras.MODEL_FOLDER, hecras.RUN_REPORT_JSON),
                  encoding="utf-8") as f:
            assert json.load(f)["hecras_inflow"]["status"] == "rejected"
        # breach mode still builds
        he.write_project(d, os.path.join(d, "b"), options=opts, inflow_mode="breach",
                         log=lambda m: None, env={})
    finally:
        shutil.rmtree(d)
    # a run with no inflow point at all keeps the old message
    assert "has no inflow point" in he.inflow_missing_message({"status": "missing"})
