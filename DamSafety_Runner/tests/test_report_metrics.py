"""Run-report metrics (core/report_metrics.py, v3.10.22, issue #91): depth-threshold areas,
P95 / P99 velocity over wet cells, the max-velocity cell's depth and position, velocity at
receptors, and the reporting-only water-body mask (masked and unmasked side by side)."""
import hashlib
import json
import os
import shutil
import tempfile
import types

import numpy as np

from nd_breach_runner.core import impacts as impmod
from nd_breach_runner.core import report_metrics as RM
from nd_breach_runner.core import workflow
from nd_breach_runner.core.grid import Grid
from nd_breach_runner.tests.registry import test

FT = 0.3048
CELL = 10.0
CELL_AC = CELL * CELL / 4046.8564224


def _flood(n=40):
    """A 40 x 40 grid (10 m cells, x0 = 0, ytop = 400): a 0.05 ft fringe (rows 0-4), a 0.5 ft
    film over a flat 'lake' (rows 5-24), a 1.5 ft overbank (rows 25-29) and a 3 ft channel
    (rows 30-34); rows 35-39 dry. Velocity 1 ft/s on the film, 2 on the overbank, 5 in the
    channel, 0.2 on the fringe."""
    d = np.full((n, n), np.nan)
    v = np.full((n, n), np.nan)
    d[0:5] = 0.05 * FT
    v[0:5] = 0.2 * FT
    d[5:25] = 0.5 * FT
    v[5:25] = 1.0 * FT
    d[25:30] = 1.5 * FT
    v[25:30] = 2.0 * FT
    d[30:35] = 3.0 * FT
    v[30:35] = 5.0 * FT
    return Grid(np.zeros((n, n)), 0.0, 400.0, CELL), {"depth": d, "velocity": v,
                                                      "arrival": np.where(np.isfinite(d), 1.0,
                                                                          np.nan)}


def _ac(cells):
    return round(cells * CELL_AC, 1)


@test
def report_metrics_depth_threshold_areas_split_the_film_from_the_channel():
    g, fin = _flood()
    s = RM.flood_metrics(fin, g, CELL_AC, 1.0 * FT)
    # any depth = 35 rows x 40; >= 0.1 ft leaves out the fringe; >= 1 ft = overbank + channel
    assert RM.depth_areas(fin["depth"], CELL_AC)["flooded_acres"] == _ac(35 * 40)
    assert s["flooded_acres_ge_0_1ft"] == _ac(30 * 40), s
    assert s["flooded_acres_ge_1ft"] == _ac(10 * 40), s
    assert s["flooded_acres_ge_2ft"] == _ac(5 * 40), s
    assert RM.area_key(0.1) == "flooded_acres_ge_0_1ft" and RM.area_key(2.0) == "flooded_acres_ge_2ft"
    # a cell exactly at a threshold counts (at or above)
    d = np.full((2, 2), 1.0 * FT)
    assert RM.depth_areas(d, 1.0)["flooded_acres_ge_1ft"] == 4.0


@test
def report_metrics_one_fast_shallow_cell_moves_the_max_not_the_p99():
    g, fin = _flood()
    base = RM.flood_metrics(fin, g, CELL_AC, 1.0 * FT)
    fin2 = {k: a.copy() for k, a in fin.items()}
    fin2["depth"][10, 7] = 0.15 * FT           # wet (>= 0.1 ft) but shallow
    fin2["velocity"][10, 7] = 19.36 * FT
    s = RM.flood_metrics(fin2, g, CELL_AC, 1.0 * FT)
    assert s["velocity_p99_fps"] == base["velocity_p99_fps"] == 5.0, (s, base)
    assert s["velocity_p95_fps"] == base["velocity_p95_fps"], (s, base)
    vmax, dmax, xy = RM.max_velocity_cell(g, fin2["depth"], fin2["velocity"])
    assert vmax == 19.36 and s["max_velocity_depth_ft"] == 0.15 == dmax, s
    assert s["max_velocity_xy"] == [75.0, 295.0] == xy, s        # centre of row 10, col 7
    assert s["velocity_wet_min_depth_ft"] == 0.1
    # the 0.05 ft fringe is not "wet": its velocities are left out of the percentiles
    pct, n = RM.velocity_percentiles(fin["depth"], fin["velocity"])
    assert n == 30 * 40 and pct[95] is not None
    # without velocity: None, not an error
    s3 = RM.flood_metrics({"depth": fin["depth"], "velocity": None}, g, CELL_AC, FT)
    assert s3["velocity_p99_fps"] is None and s3["max_velocity_xy"] is None


@test
def report_metrics_velocity_at_flooded_structures_and_overtopped_roads():
    g, fin = _flood()
    rec = impmod.Receptors()
    # structures: one in the channel (5 ft/s, 3 ft), one on the overbank (2 ft/s), one on the
    # 0.5 ft film (below the 1 ft arrival depth: not flooded), one dry
    rec.add_points([(55.0, 400 - 32.5 * CELL), (155.0, 400 - 27.5 * CELL),
                    (255.0, 400 - 10.5 * CELL), (255.0, 400 - 37.5 * CELL)])
    # a road across the channel and the overbank (sampled every 30 m)
    rec.add_lines([np.array([[205.0, 400 - 25.5 * CELL], [205.0, 400 - 34.5 * CELL]])],
                  impmod.KIND_ROAD)
    s = RM.flood_metrics(fin, g, CELL_AC, 1.0 * FT, receptors=rec)
    assert s["velocity_at_structures_max_fps"] == 5.0, s
    assert 2.0 <= s["velocity_at_structures_p95_fps"] <= 5.0, s
    assert s["velocity_at_roads_max_fps"] == 5.0, s
    assert "velocity_at_rail_max_fps" not in s
    # agrees with what impacts.summarize lists in receptors_flooded.csv
    _summ, rows = impmod.summarize(rec, g, fin, 1.0 * FT)
    assert max(r["velocity_fps"] for r in rows if r["kind"] == "structure") == 5.0
    assert RM.receptor_velocities(None, g, fin, FT) == {}


@test
def report_metrics_mask_removes_the_lake_from_the_masked_figures_only():
    g, fin = _flood()
    before = {k: a.copy() for k, a in fin.items()}
    lake = [[(0.0, 400 - 5 * CELL), (400.0, 400 - 5 * CELL), (400.0, 400 - 25 * CELL),
             (0.0, 400 - 25 * CELL)]]                     # exactly the 0.5 ft film rows 5-24
    m = RM.rasterize_polygons(g, lake)
    assert m.sum() == 20 * 40 and m[5:25].all() and not m[:5].any() and not m[25:].any()
    plain = RM.flood_metrics(fin, g, CELL_AC, 1.0 * FT)
    s = RM.flood_metrics(fin, g, CELL_AC, 1.0 * FT, mask=m, mask_source="lake")
    # the model arrays are untouched
    for k in fin:
        assert np.array_equal(fin[k], before[k], equal_nan=True), k
    # unmasked figures are the same with or without the mask
    for k, v in plain.items():
        assert s[k] == v, (k, s[k], v)
    assert s["water_mask_source"] == "lake"
    assert s["water_mask_acres"] == _ac(800) == s["water_mask_flooded_acres"]
    assert s["flooded_acres_masked"] == _ac(35 * 40 - 800), s
    assert s["flooded_acres_ge_0_1ft_masked"] == _ac(10 * 40), s
    assert s["flooded_acres_ge_1ft_masked"] == s["flooded_acres_ge_1ft"] == _ac(400), s
    assert s["flooded_acres_over_arrival_depth_masked"] == _ac(400)
    # with the 1 ft/s film masked, the percentiles are over the overbank and channel only
    rest = [2.0] * 200 + [5.0] * 200
    assert s["velocity_p95_fps_masked"] == round(float(np.percentile(rest, 95)), 2), s
    pm, n = RM.velocity_percentiles(fin["depth"], fin["velocity"], ~m, q=(50,))
    assert n == 400 and pm[50] == 3.5 and RM.velocity_percentiles(
        fin["depth"], fin["velocity"], q=(50,))[0][50] == 1.0
    assert s["max_velocity_fps_masked"] == 5.0
    # a hole in the polygon stays unmasked
    holed = [[lake[0], [(100.0, 300.0), (200.0, 300.0), (200.0, 200.0), (100.0, 200.0)]]]
    mh = RM.rasterize_polygons(g, holed)
    assert mh.sum() == 800 - 100, mh.sum()


@test
def report_metrics_mask_sources_user_then_dam_then_suite_and_off():
    d = tempfile.mkdtemp()
    try:
        env = {}
        assert RM.mask_sources(None, env) == []
        env = {RM.ENV_MASK: "/x/a.gpkg ;; /x/b.geojson|layername=lakes"}
        assert RM.mask_sources(None, env) == ["/x/a.gpkg", "/x/b.geojson|layername=lakes"]
        assert RM.mask_sources("off", env) == [] and RM.mask_sources(None, {RM.ENV_MASK: "OFF"}) == []
        assert RM.mask_sources("/u/mine.shp", env) == ["/u/mine.shp"]
        # the suite pack's hydro waterbodies when the file exists (read only)
        hydro = os.path.join(d, RM.PACK_DIR, "hydro")
        os.makedirs(hydro)
        env = {"NDSUITE_DATA": d}
        assert RM.mask_sources(None, env) == []                        # not there yet
        p = os.path.join(hydro, "NHD_waterbodies.gpkg")
        open(p, "wb").close()
        listing = sorted(os.listdir(hydro))
        assert RM.mask_sources(None, env) == [p]
        assert RM.mask_sources("/u/mine.shp", env) == ["/u/mine.shp", p]
        assert sorted(os.listdir(hydro)) == listing                    # nothing written
        # the 1.19 3DHP name wins over the NHD one in the same folder
        p3 = os.path.join(hydro, "3DHP_waterbodies.gpkg")
        open(p3, "wb").close()
        assert RM.mask_sources(None, env) == [p3]
        polys = [[(0, 0), (1, 0), (1, 1)]]
        assert RM.mask_sources(polys, env) == [polys]
        # an unreadable file is a note, never an error
        g, _fin = _flood()
        m, info = RM.build_mask(g, os.path.join(d, "missing.gpkg"), "EPSG:26914", environ={})
        assert m is None and info["problems"] and not info["sources"], info
        m, info = RM.build_mask(g, None, None, environ={})
        assert m is None and info is None
    finally:
        shutil.rmtree(d)


@test
def report_metrics_mask_file_is_reprojected_onto_the_grid():
    try:
        from osgeo import gdal, osr  # noqa: F401
    except ImportError:
        return
    gdal.UseExceptions()
    d = tempfile.mkdtemp()
    try:
        # grid in UTM 14N; the lake polygon in lon / lat (EPSG:4326, GeoJSON)
        x0, ytop = 500000.0, 5200000.0
        g = Grid(np.zeros((40, 40)), x0, ytop, CELL)
        s = osr.SpatialReference()
        s.ImportFromEPSG(26914)
        t = osr.SpatialReference()
        t.ImportFromEPSG(4326)
        for x in (s, t):
            try:
                x.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
            except AttributeError:
                pass
        ct = osr.CoordinateTransformation(s, t)
        ring = [(x0 + 100, ytop - 100), (x0 + 300, ytop - 100), (x0 + 300, ytop - 300),
                (x0 + 100, ytop - 300), (x0 + 100, ytop - 100)]
        ll = [list(ct.TransformPoint(x, y)[:2]) for x, y in ring]
        p = os.path.join(d, "lake.geojson")
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"type": "FeatureCollection", "features": [{
                "type": "Feature", "properties": {"name": "lake"},
                "geometry": {"type": "Polygon", "coordinates": [ll]}}]}, f)
        m = RM.rasterize_file(g, p, s.ExportToWkt())
        assert abs(int(m.sum()) - 400) <= 30, m.sum()                # 20 x 20 cells
        assert m[15, 15] and not m[5, 5] and not m[35, 35]
        mm, info = RM.build_mask(g, p, s.ExportToWkt(), environ={})
        assert mm is not None and info["sources"] == [p] and not info["problems"]
    finally:
        shutil.rmtree(d)


@test
def report_metrics_hecras_adopt_final_engine_reports_them_from_the_final_rasters():
    """TIER 2: adopt_final_engine recomputes the new fields from the HEC-RAS rasters, with
    the mask, and leaves the rasters as they were."""
    try:
        from osgeo import gdal  # noqa: F401
    except ImportError:
        return
    from nd_breach_runner.tests import test_hecras_tier as tht
    d = tempfile.mkdtemp()
    try:
        m = tht._tier2_folder(d)
        rp = os.path.join(m, "run_report.json")
        with open(rp, encoding="utf-8") as f:
            rep = json.load(f)
        rep["summary"]["flooded_acres_ge_1ft"] = 999999.0        # a stale screening value
        rep["summary"]["velocity_p99_fps"] = 99.0
        with open(rp, "w", encoding="utf-8") as f:
            json.dump(rep, f)

        def digest():
            return {k: hashlib.sha256(open(os.path.join(d, n), "rb").read()).hexdigest()
                    for k, n in workflow.FINAL_NAMES.items()}
        h0 = digest()
        # the 2 ft block covers rows 10-29, cols 5-34 of a 10 m grid at (500000, 5200000);
        # mask the west half of it (cols 5-19)
        lake = [[(500050.0, 5199900.0), (500200.0, 5199900.0), (500200.0, 5199700.0),
                 (500050.0, 5199700.0)]]
        st = types.SimpleNamespace(water_mask=lake, receptors=None, arrival_depth_ft=1.0)
        s = workflow.adopt_final_engine(d, "HEC-RAS 2D", st=st)
        assert digest() == h0                                     # reporting only
        assert s["flooded_acres_ge_1ft"] == round(600 * CELL_AC, 1) == s["flooded_acres_ge_0_1ft"]
        assert s["flooded_acres_ge_2ft"] == round(600 * CELL_AC, 1), s
        assert s["velocity_p99_fps"] == 3.0 and s["max_velocity_fps"] == 3.0, s
        assert s["max_velocity_depth_ft"] == 2.0 and s["max_velocity_xy_crs"] == "EPSG:26914"
        assert s["water_mask_acres"] == round(300 * CELL_AC, 1), s
        assert s["flooded_acres_masked"] == round(300 * CELL_AC, 1), s
        with open(rp, encoding="utf-8") as f:
            r = json.load(f)
        assert r["summary"]["flooded_acres_ge_1ft"] == s["flooded_acres_ge_1ft"]
        assert r["screening_pass_lisflood"]["summary"]["flooded_acres_ge_1ft"] == 999999.0
        assert r["water_mask"]["sources"] and os.path.exists(r["water_mask"]["polygons"])
        md = open(os.path.join(m, "run_report.md"), encoding="utf-8").read()
        assert "Flooded area at or above 0.1 / 1 / 2 ft" in md and "Water-body mask" in md, md
        assert "999,999" not in md.split("## Screening pass")[0]
    finally:
        shutil.rmtree(d)


@test
def report_metrics_old_reports_render_as_before_and_fields_are_added_only():
    s = {"dam_id": "X", "flooded_acres": 10.0, "flooded_acres_over_arrival_depth": 5.0,
         "max_depth_ft": 3.0, "max_velocity_fps": 4.0}
    assert RM.report_lines(s) == [] and RM.qa_title_lines(s) == []
    g, fin = _flood()
    new = RM.flood_metrics(fin, g, CELL_AC, FT)
    for k in ("flooded_acres", "flooded_acres_over_arrival_depth", "max_depth_ft",
              "max_velocity_fps", "latest_arrival_hr", "receptors_flooded"):
        assert k not in new, k                    # existing names are never written here
    assert all(RM.is_metric_key(k) for k in new), [k for k in new if not RM.is_metric_key(k)]
    # the summary from final_summary keeps the old fields and adds the new ones
    fs = workflow.final_summary(fin, CELL_AC, FT, base={"dam_id": "X"}, grid=g)
    assert fs["flooded_acres"] == _ac(35 * 40) and fs["flooded_acres_ge_1ft"] == _ac(400)
    assert "flooded_acres_ge_1ft" not in workflow.final_summary(fin, CELL_AC, FT)
    lines = RM.report_lines(fs)
    assert any("0.1 / 1 / 2 ft" in x for x in lines) and any("P95 / P99" in x for x in lines)
    t = RM.qa_title_lines(fs)
    assert t[0].startswith("any ") and "P99" in t[1] and "of water" in t[1]
    # CSV / table columns: the old ones keep their places, the new ones come after them
    from nd_breach_runner.core import sensitivity, probabilistic
    assert sensitivity.CSV_FIELDS[-len(RM.TABLE_FIELDS):] == list(RM.TABLE_FIELDS)
    assert sensitivity.CSV_FIELDS[:3] == ["case", "group", "method"]
    assert probabilistic.RUN_FIELDS[-len(RM.TABLE_FIELDS):] == list(RM.TABLE_FIELDS)
    assert probabilistic.RUN_FIELDS[probabilistic.RUN_FIELDS.index("minutes") + 1] == \
        RM.TABLE_FIELDS[0]
    tv = RM.table_values(fs)
    assert tv["flooded_ac_ge_1ft"] == fs["flooded_acres_ge_1ft"] and tv["flooded_ac_masked"] is None


@test
def report_metrics_per_pass_stats_and_settings_keys():
    g, fin = _flood()
    d0 = np.where(np.isfinite(fin["depth"]), fin["depth"], 0.0)      # LISFLOOD-FP: dry = 0
    ps = RM.pass_stats(g, d0, fin["velocity"])
    assert ps["flooded_acres_any"] == _ac(35 * 40) and ps["flooded_acres_ge_1ft"] == _ac(400)
    assert ps["velocity_p99_fps"] == 5.0 and ps["max_velocity_depth_ft"] == 3.0
    json.dumps(ps)
    # water_mask never changes a checkpoint key and is left out of the report while unset
    st = workflow.RunSettings()
    assert st.water_mask is None and "water_mask" not in st.as_dict()
    k0 = workflow.key_settings(st)
    st.water_mask = "/u/lakes.gpkg"
    assert st.as_dict()["water_mask"] == "/u/lakes.gpkg"
    assert workflow.key_settings(st) == k0
    st.water_mask = [[(0, 0), (1, 0), (1, 1)]]
    assert st.as_dict()["water_mask"] == "polygons (1)"


@test
def report_metrics_qa_depth_map_title_and_mask_overlay():
    from nd_breach_runner.core import qa_maps as QA
    g, fin = _flood()
    fs = workflow.final_summary(fin, CELL_AC, FT, grid=g, mask=np.zeros((40, 40), bool))
    t = QA.depth_title("T1", 34.6, 3.0, fs)
    first = t.split("\n")[0]
    assert first == "T1: max depth (ft), 35 ac over 0.1 ft, max 3.00 ft", first
    assert "water-body mask" in t and ">=2 ft" in t
    assert QA.depth_title("T1", 34.6, 3.0) == first
    try:
        import matplotlib  # noqa: F401
    except Exception:  # noqa: BLE001
        return
    d = tempfile.mkdtemp()
    try:
        m = np.zeros((40, 40), bool)
        m[5:25] = True
        p = QA.draw_depth_map(os.path.join(d, "q.png"), g, fin["depth"], "T1", (200.0, 50.0),
                              metrics=fs, water_mask=m)
        assert open(p, "rb").read(8) == b"\x89PNG\r\n\x1a\n"
    finally:
        shutil.rmtree(d)
