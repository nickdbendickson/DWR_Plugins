"""Design #68: TIER 2 'LISFLOOD-FP then HEC-RAS 2D' is appended; indices 0 and 1, their
labels (as on main 383ca26) and the default are unchanged, and run_dam never treats
TIER_HECRAS as ANUGA."""
import ast
import os

import numpy as np

from nd_breach_runner.core import workflow
from nd_breach_runner.tests.registry import test

HERE = os.path.dirname(os.path.abspath(__file__))
MAIN_TIERS = [("lisflood", "LISFLOOD-FP only"),
              ("lisflood+anuga", "LISFLOOD-FP for the extent, then ANUGA (final results from ANUGA)")]


@test
def hecras_tier_appended_and_defaults_unchanged():
    assert workflow.TIERS[:2] == ("lisflood", "lisflood+anuga")
    assert workflow.TIERS[2] == workflow.TIER_HECRAS == "lisflood+hecras"
    assert workflow.RunSettings().tier == workflow.TIER_LISFLOOD
    src = open(os.path.join(os.path.dirname(HERE), "algorithms.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    node = next(n for n in tree.body if isinstance(n, ast.Assign)
                and getattr(n.targets[0], "id", "") == "TIERS")
    got = []
    for el in node.value.elts:
        a, b = el.elts
        got.append((a.attr, ast.literal_eval(b)))
    names = {"TIER_LISFLOOD": "lisflood", "TIER_BOTH": "lisflood+anuga",
             "TIER_HECRAS": "lisflood+hecras"}
    got = [(names[a], b) for a, b in got]
    assert got[:2] == MAIN_TIERS, got
    assert "HEC-RAS 2D" in got[2][1]
    assert 'QgsProcessingParameterEnum("TIER", "Models",\n' \
           '                                                     [t[1] for t in TIERS], defaultValue=0))' in src
    from nd_breach_runner import wizard_logic as wl
    assert wl.TIER_IDS == workflow.TIERS and wl.TIER_LABELS[workflow.TIER_HECRAS]
    wsrc = open(os.path.join(os.path.dirname(HERE), "core", "workflow.py"), encoding="utf-8").read()
    assert "st.tier == TIER_BOTH" in wsrc and "TIER_HECRAS" not in wsrc.split("def run_dam", 1)[1]


@test
def hecras_tier_stage_passes_its_env_to_the_project_writer():
    """run_stage builds the project with its own env, so NDBREACH_HECRAS_INFLOW_MODE (and the
    base flow / tail settings) reach write_project."""
    from nd_breach_runner.core import hecras_engine as he
    src = open(he.__file__, encoding="utf-8").read().split("def run_stage", 1)[1]
    assert "write_project(dam_folder, proj, log=log, env=env)" in src
    assert '"time_step"' in src and '"inflow_series"' in src


# ===================================== TIER 2 fix: TIER 2 figures come from HEC-RAS, not LISFLOOD
def _tif_ft(path, a, x0=500000.0, ytop=5200000.0, dx=10.0):
    from osgeo import gdal, osr
    gdal.UseExceptions()
    ds = gdal.GetDriverByName("GTiff").Create(path, a.shape[1], a.shape[0], 1, gdal.GDT_Float32)
    ds.SetGeoTransform((x0, dx, 0, ytop, 0, -dx))
    s = osr.SpatialReference()
    s.ImportFromEPSG(26914)
    ds.SetProjection(s.ExportToWkt())
    b = ds.GetRasterBand(1)
    b.SetNoDataValue(-9999.0)
    b.WriteArray(np.where(np.isfinite(a), a, -9999.0).astype(np.float32))
    ds = None


@test
def hecras_tier_report_and_polygons_come_from_the_final_engine():
    """Absaraka TIER 2: run_report.json, the md/HTML summary and the flood polygons carried
    the LISFLOOD-FP figures (6,939.5 ac, 9.38 ft, 15.55 ft/s) after HEC-RAS wrote the final
    rasters. adopt_final_engine recomputes them from the final rasters and keeps the
    LISFLOOD-FP figures and polygons, labelled as the screening pass."""
    try:
        from osgeo import gdal  # noqa: F401
    except ImportError:
        return
    import json
    import shutil
    import tempfile
    d = tempfile.mkdtemp()
    try:
        m = os.path.join(d, "_breach_model")
        os.makedirs(m)
        dep = np.full((50, 50), np.nan)
        dep[10:30, 5:35] = 2.0                         # 600 cells of 100 m2 = 14.83 ac
        dep[20, 20] = 12.22
        vel = np.where(np.isfinite(dep), 3.0, np.nan)
        vel[21, 21] = 8.5
        arr = np.where(np.isfinite(dep), 1.0, np.nan)
        arr[29, 34] = 3.25
        wse = np.where(np.isfinite(dep), 1000.0, np.nan)
        for k, a in (("depth", dep), ("velocity", vel), ("arrival", arr), ("wse", wse)):
            _tif_ft(os.path.join(d, workflow.FINAL_NAMES[k]), a)
        for nm in ("Flood Extent.geojson", "Flood Polygons (Max Depth).geojson"):
            with open(os.path.join(d, nm), "w", encoding="utf-8") as f:
                json.dump({"type": "FeatureCollection", "features": [], "lisflood": True}, f)
        old = {"dam_id": "SYN-1", "scenario": "sunny", "final_model": "LISFLOOD-FP",
               "flooded_acres": 6939.5, "flooded_acres_over_arrival_depth": 6000.0,
               "max_depth_ft": 9.38, "max_velocity_fps": 15.55, "latest_arrival_hr": 9.0,
               "breach_occurs": True}
        # a run report as the real workflow writes it (tests/test_report_docx's run folder)
        from nd_breach_runner.tests import test_report_docx as trd
        with open(os.path.join(trd._run_folder(flood=False), "_breach_model",
                               "run_report.json"), encoding="utf-8") as f:
            rep = json.load(f)
        old = dict(rep["summary"], **old)
        rep.update({"dam_xy": [500250.0, 5199900.0], "summary": old, "warnings": [],
                    "outputs": {"vectors": {
                        "Flood Extent": os.path.join(d, "Flood Extent.geojson"),
                        "Flow trace": os.path.join(m, "flow_trace.csv")}}})
        with open(os.path.join(m, "run_report.json"), "w", encoding="utf-8") as f:
            json.dump(rep, f)
        s = workflow.adopt_final_engine(d, "HEC-RAS 2D")
        assert abs(s["flooded_acres"] - 14.8) < 0.05, s
        assert s["max_depth_ft"] == 12.22 and s["max_velocity_fps"] == 8.5, s
        assert s["latest_arrival_hr"] == 3.25 and s["final_model"] == "HEC-RAS 2D", s
        with open(os.path.join(m, "run_report.json"), encoding="utf-8") as f:
            r = json.load(f)
        assert r["summary"]["flooded_acres"] == s["flooded_acres"]
        sp = r["screening_pass_lisflood"]
        assert sp["summary"]["flooded_acres"] == 6939.5 and sp["summary"]["max_depth_ft"] == 9.38
        assert sp["model"] == "LISFLOOD-FP" and r["final_engine"]["model"] == "HEC-RAS 2D"
        # the LISFLOOD-FP polygons are kept under the prefix; the extent is made again
        for nm in ("Flood Extent.geojson", "Flood Polygons (Max Depth).geojson"):
            with open(os.path.join(d, workflow.SCREENING_PREFIX + nm), encoding="utf-8") as f:
                assert json.load(f).get("lisflood") is True
        with open(os.path.join(d, "Flood Extent.geojson"), encoding="utf-8") as f:
            ext = json.load(f)
        assert "lisflood" not in ext and ext["features"], ext
        txt = json.dumps(ext)
        assert "6939" not in txt
        vec = r["outputs"]["vectors"]
        assert vec.get("Flow trace", "").endswith("flow_trace.csv"), vec
        md = open(os.path.join(m, "run_report.md"), encoding="utf-8").read()
        assert "## Screening pass (LISFLOOD-FP)" in md, md
        top, scr = md.split("## Screening pass (LISFLOOD-FP)", 1)
        assert "12.22" in top and "9.38" not in top and "9.38" in scr, md
        try:
            workflow.adopt_final_engine(d, "HEC-RAS 2D")
            raise AssertionError("a second adopt must refuse")
        except ValueError:
            pass
    finally:
        shutil.rmtree(d)


@test
def hecras_tier_algorithm_adopts_the_final_engine_after_run_stage():
    src = open(os.path.join(os.path.dirname(HERE), "algorithms.py"), encoding="utf-8").read()
    i = src.index("hecras_engine.run_stage(")
    j = src.index("workflow.adopt_final_engine(", i)
    assert j > i and "res.summary = workflow.adopt_final_engine(" in src[i:j + 40]
