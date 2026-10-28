"""v3.10.20: the reservoir bottom carved into the DEM from the storage curve
(core/reservoir_carve.py, RESERVOIR_CARVE = off | auto | on; docs/Reservoir_carve_from_storage.md).

Synthetic data: a valley reservoir held by a dam at x = 0 (downstream +x) whose true bottom is
known, seen by 'LiDAR' as a flat water surface 1.2 m below the top of dam (DWR-3161-like). The
dam's storage and surface area are the TRUE reservoir's at the top of dam, so a carve has to give
the volume back. GDAL (QGIS Python / venv) for the files; the pure parts run without it."""
import hashlib
import json
import os
import shutil
import tempfile

import numpy as np

from nd_breach_runner.core import breach as br
from nd_breach_runner.core import reservoir_carve as rc
from nd_breach_runner.core import workflow
from nd_breach_runner.core.grid import Grid
from nd_breach_runner.core.units import ACFT, ACRE, FT
from nd_breach_runner.tests.registry import test

TOP = 100.0                  # m
H = 8.0                      # dam height, m: true bottom 92 m at the dam
WATER = TOP - 1.2            # LiDAR water surface
X0, Y0 = 500000.0, 5200000.0
DX = 5.0


def _true(X, Y):
    """The real ground (metres): a valley falling towards the dam (x = X0) with parabolic
    sides, an embankment 0 <= x <= 25 m, and the valley below the dam."""
    x, y = X - X0, Y - Y0
    up = 92.0 + 0.004 * np.clip(-x, 0, None) + 10.0 * (np.abs(y) / 220.0) ** 2
    down = 91.0 - 0.001 * np.clip(x, 0, None) + 10.0 * (np.abs(y) / 220.0) ** 2
    z = np.where(x < 0, up, down)
    emb = (x >= 0) & (x <= 25.0) & (np.abs(y) <= 400.0)
    return np.where(emb, np.maximum(z, TOP + 0.5), z)


def _lidar(X, Y):
    """What the LiDAR saw: the reservoir as a flat water surface (upstream only)."""
    z = _true(X, Y)
    return np.where((X - X0 < 0) & (z < WATER), WATER, z)


def _grid(fn, half=2600.0, dx=DX):
    n = int(2 * half / dx)
    xs = X0 - half + (np.arange(n) + 0.5) * dx
    ys = Y0 + half - (np.arange(n) + 0.5) * dx
    X, Y = np.meshgrid(xs, ys)
    return Grid(fn(X, Y), X0 - half, Y0 + half, dx, crs_wkt="EPSG:26914")


def _truth():
    """Storage (acre-ft) and surface area (ac) of the TRUE reservoir at the top of dam, over the
    same footprint rule the carve uses."""
    g = _grid(_true)
    F, _ = rc.footprint(g, (X0, Y0), (1.0, 0.0), TOP, rc.RADIUS_MAX_M)
    a = g.dx ** 2
    z = g.masked()
    return rc.dem_volume(z, F, a, TOP) / ACFT, rc.dem_area(z, F, a, TOP) / ACRE


def _dam(**k):
    v, sa = _truth()
    d = dict(dam_id="CARVE-1", dam_height_ft=H / FT, storage_top_acft=v,
             top_of_dam_ft=TOP / FT, surface_area_top_ac=sa, name="Synthetic carve")
    d.update(k)
    return br.DamData(**d)


class _Terrain(object):
    z_to_m = 1.0
    z_note = "metres (synthetic)"

    def __init__(self, fn):
        self.fn = fn

    def read(self, bounds, cell):
        xmin, ymin, xmax, ymax = bounds
        nx, ny = int(round((xmax - xmin) / cell)), int(round((ymax - ymin) / cell))
        X, Y = np.meshgrid(xmin + (np.arange(nx) + 0.5) * cell, ymax - (np.arange(ny) + 0.5) * cell)
        return Grid(self.fn(X, Y), xmin, ymax, cell, crs_wkt="EPSG:26914")

    def manning(self, bounds, cell):
        return None


def _gdal():
    try:
        from osgeo import gdal, osr
        return gdal, osr
    except ImportError:
        return None, None


@test
def reservoir_carve_matches_the_storage_curve_and_keeps_the_shoreline():
    dam = _dam()
    g = _grid(_lidar)
    r = rc.plan_carve(g, dam, (X0, Y0), (1.0, 0.0), rc.MODE_ON)
    rep = r["report"]
    assert r["applied"], rep.get("why")
    # before: the water sheet holds a fraction of the storage; after: within 2 %
    assert rep["before"]["volume_ratio"] < 0.5, rep["before"]
    assert rep["flat_water"]["found"] and abs(rep["flat_water"]["surface_ft"] - WATER / FT) < 0.1
    assert abs(rep["after"]["volume_error_pct"]) <= 2.0, rep["after"]
    assert rep["volume_within_tolerance"] is True
    z0, z1 = g.masked(), r["carved"].data
    F, C = r["masks"]["footprint"], r["masks"]["carve"]
    # only lowered, nothing outside the footprint, shoreline / dry banks unchanged
    assert np.all(z1[np.isfinite(z0)] <= z0[np.isfinite(z0)] + 1e-9)
    assert not np.any((z0 - z1)[~F] > 0), "carved outside the footprint"
    assert rep["cells_lowered_outside_footprint"] == 0
    assert rep["shoreline_cells"] > 0 and rep["shoreline_cells_unchanged"] == rep["shoreline_cells"]
    edge = C & ~(np.roll(C, 1, 0) & np.roll(C, -1, 0) & np.roll(C, 1, 1) & np.roll(C, -1, 1))
    X, _Y = np.meshgrid(g.x_centers(), g.y_centers())
    shore = edge & (X - X0 < -60.0)              # the water line (not the dam face side)
    assert shore.any() and np.allclose(z1[shore], z0[shore]), "the water line moved"
    assert np.array_equal(z1[F & ~C], z0[F & ~C]), "the dry banks moved"
    # never below the floor (the curve's bottom); the area at the top of dam is the LiDAR's
    assert np.nanmin(z1[F]) >= TOP - H - 1e-6
    assert abs(rep["after"]["area_top_ac"] - rep["before"]["area_top_ac"]) < 1e-6
    # the shape: at 25 / 50 / 75 % of the height and at the water surface the carved
    # elevation-storage and -area curves follow the target (here within 3 % of the storage and
    # 10 % of the area at the top of dam)
    vt, at = rep["curve"]["storage_top_acft"], rep["curve"]["area_top_ac"]
    assert len(rep["curve_table"]) == 5
    for row in rep["curve_table"]:
        assert abs(row["carved_volume_acft"] - row["target_volume_acft"]) <= 0.03 * vt, row
        assert abs(row["carved_area_ac"] - row["target_area_ac"]) <= 0.10 * at, row
    assert rep["max_carve_depth_ft"] > 0 and rep["note"].startswith(rc.WARNING)


@test
def reservoir_carve_modes_off_auto_on():
    dam = _dam()
    wet, dry = _grid(_lidar), _grid(_true)
    off = rc.plan_carve(wet, dam, (X0, Y0), (1.0, 0.0), rc.MODE_OFF)
    assert not off["applied"] and off["carved"] is None and off["report"]["flat_water_problem"]
    auto = rc.plan_carve(wet, dam, (X0, Y0), (1.0, 0.0), rc.MODE_AUTO)
    assert auto["applied"] and "flat water" in auto["report"]["why"]
    # the true bathymetry: auto does nothing (no water sheet), on finds the DEM already holds it
    a2 = rc.plan_carve(dry, dam, (X0, Y0), (1.0, 0.0), rc.MODE_AUTO)
    assert not a2["applied"] and not a2["report"]["flat_water_problem"], a2["report"]
    o2 = rc.plan_carve(dry, dam, (X0, Y0), (1.0, 0.0), rc.MODE_ON)
    assert not o2["applied"] and "already holds" in o2["report"]["why"], o2["report"]["why"]
    # the mode text
    assert rc.normalize(None) == rc.normalize("") == rc.normalize("nonsense") == rc.MODE_OFF
    assert rc.normalize(1) == rc.normalize("Auto") == rc.normalize(rc.LABELS[1]) == rc.MODE_AUTO
    assert rc.normalize(2) == rc.normalize("ON") == rc.MODE_ON
    assert rc.env_mode({}) == rc.MODE_OFF and rc.env_mode({rc.ENV_MODE: "auto"}) == rc.MODE_AUTO


@test
def reservoir_carve_too_small_a_footprint_is_a_shortfall_not_a_pit():
    # storage four times what the valley can hold above the floor: the steepest bowl, a warning
    dam = _dam()
    dam.storage_top_acft *= 4.0
    dam.surface_area_top_ac = None
    r = rc.plan_carve(_grid(_lidar), dam, (X0, Y0), (1.0, 0.0), rc.MODE_ON)
    rep = r["report"]
    assert r["applied"] and rep["volume_within_tolerance"] is False
    assert rep["after"]["volume_error_pct"] < -2.0 and "cannot hold" in rep["note"]
    assert rep["lowest_carved_ft"] >= rep["floor_ft"] - 0.01


@test
def reservoir_carve_terrain_wrapper_keeps_the_volume_at_any_cell():
    dam = _dam()
    g = _grid(_lidar)
    r = rc.plan_carve(g, dam, (X0, Y0), (1.0, 0.0), rc.MODE_ON)
    depth = r["depth"]
    want = float(depth.data.sum()) * g.dx ** 2
    t = rc.CarvedTerrain(_Terrain(_lidar), depth)
    for cell in (10.0, 40.0, 7.5):
        b = (X0 - 2600.0, Y0 - 2600.0, X0 - 2600.0 + 5200.0, Y0 - 2600.0 + 5200.0)
        a = _Terrain(_lidar).read(b, cell)
        c = t.read(b, cell)
        got = float(np.nansum(a.data - c.data)) * cell ** 2
        assert abs(got - want) <= 0.01 * want, (cell, got, want)
        assert np.all(c.data <= a.data + 1e-9)
    # a read away from the reservoir is the source as it is
    far = (X0 + 3000, Y0 + 3000, X0 + 3400, Y0 + 3400)
    assert np.array_equal(t.read(far, 10.0).data, _Terrain(_lidar).read(far, 10.0).data)
    # anything else goes to the inner source
    assert t.z_to_m == 1.0 and t.manning(far, 10.0) is None


@test
def reservoir_carve_run_hook_writes_a_copy_and_leaves_the_source_dem():
    gdal, osr = _gdal()
    if gdal is None:
        print("    (skipped: no GDAL)")
        return
    from nd_breach_runner.core import gdal_io
    tmp = tempfile.mkdtemp(prefix="carve_")
    try:
        g = _grid(_lidar, half=3200.0)
        src = os.path.join(tmp, "source_dem.tif")
        ds = gdal.GetDriverByName("GTiff").Create(src, g.nx, g.ny, 1, gdal.GDT_Float32)
        ds.SetGeoTransform((g.x0, g.dx, 0, g.y0, 0, -g.dx))
        s = osr.SpatialReference()
        s.ImportFromEPSG(26914)
        ds.SetProjection(s.ExportToWkt())
        ds.GetRasterBand(1).SetNoDataValue(-9999.0)
        ds.GetRasterBand(1).SetUnitType("metre")
        ds.GetRasterBand(1).WriteArray(g.data.astype(np.float32))
        ds = None
        h0 = hashlib.sha256(open(src, "rb").read()).hexdigest()
        terr = gdal_io.GdalTerrain(src, "EPSG:26914", gdal_io.Z_METERS)
        dam = _dam()
        md = os.path.join(tmp, "_breach_model")
        report, warnings = {"work_crs": "EPSG:26914"}, []
        # off: nothing happens, the sources are the same objects
        st = workflow.RunSettings()
        assert st.reservoir_carve == rc.MODE_OFF and "reservoir_carve" not in st.as_dict()
        a, b = rc.run_hook(st, dam, (X0, Y0), (X0 + 60.0, Y0), terr, terr, md, report, warnings)
        assert a is terr and b is terr and rc.REPORT_KEY not in report and not warnings
        st = workflow.RunSettings(reservoir_carve="on")
        assert st.as_dict()["reservoir_carve"] == "on"
        a, b = rc.run_hook(st, dam, (X0, Y0), (X0 + 60.0, Y0), terr, terr, md, report, warnings,
                           downstream=(1.0, 0.0), crs_wkt="EPSG:26914")
        rep = report[rc.REPORT_KEY]
        assert rep["applied"], rep
        assert isinstance(a, rc.CarvedTerrain) and b is a
        assert rep["inflow_on_downstream_side"] is True
        assert warnings and warnings[0].startswith(rc.WARNING)
        for k in ("dem", "depth", "png"):
            assert os.path.exists(os.path.join(md, rep["files"][k])), rep["files"]
        assert rep["files"]["dem"] == "CARVE-1_dem_carved.tif"
        assert hashlib.sha256(open(src, "rb").read()).hexdigest() == h0, "source DEM changed"
        # the engine DEM (model_dem) is the carved one
        bnds = (X0 - 2000.0, Y0 - 1000.0, X0 - 40.0, Y0 + 1000.0)       # upstream only
        m0 = workflow.model_dem(terr, [], "lisflood", bnds, 20.0)
        m1 = workflow.model_dem(a, [], "lisflood", bnds, 20.0)
        assert np.nanmin(m0.data) >= WATER - 0.01 and np.nanmin(m1.data) < WATER - 4.0
        down = (X0 + 40.0, Y0 - 1000.0, X0 + 2000.0, Y0 + 1000.0)       # nothing downstream
        assert np.array_equal(workflow.model_dem(a, [], "lisflood", down, 20.0).data,
                              workflow.model_dem(terr, [], "lisflood", down, 20.0).data)
        assert np.all(m1.data[np.isfinite(m0.data)] <= m0.data[np.isfinite(m0.data)] + 1e-6)
        # the HEC-RAS terrain: lowered by the same depth raster (a copy in feet here)
        terr_tif = os.path.join(tmp, "terrain_ft.tif")
        gdal.Translate(terr_tif, src, scaleParams=[[0, 1, 0, 1 / FT]], outputType=gdal.GDT_Float32)
        before = gdal.Open(terr_tif).ReadAsArray().astype(float)
        note = rc.hecras_terrain_note(report, md, terr_tif, FT)
        after = gdal.Open(terr_tif).ReadAsArray().astype(float)
        assert "Reservoir bottom carved" in note and np.all(after <= before + 1e-3)
        dv = float((before - after).sum()) * FT * g.dx ** 2      # ft -> m, x cell area
        assert abs(dv - rep["carved_volume_acft"] * ACFT) <= 0.02 * rep["carved_volume_acft"] * ACFT
        assert rc.hecras_terrain_note({}, md, terr_tif, FT) == ""
        json.dumps(report)                                       # run_report.json-ready
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@test
def reservoir_carve_conditioned_terrain_keeps_its_type():
    from nd_breach_runner.core import conditioning as cond
    base = _Terrain(_lidar)
    ct = cond.ConditionedTerrain(base, cond.Edits())
    g = _grid(_lidar)
    r = rc.plan_carve(g, _dam(), (X0, Y0), (1.0, 0.0), rc.MODE_ON)
    a, b = rc.wrap_sources(ct, base, r["depth"])
    assert a is ct and isinstance(ct.base, rc.CarvedTerrain) and b is ct.base


@test
def reservoir_carve_settings_gui_and_cli():
    from nd_breach_runner import popup_logic as pl
    assert pl.DEFAULT_SETTINGS["reservoir_carve"] == "off"
    assert "RESERVOIR_CARVE" not in pl.defaults("rundam", "/d", "X", {})
    assert pl.defaults("rundam", "/d", "X", {"reservoir_carve": "auto"})["RESERVOIR_CARVE"] == 1
    assert pl.defaults("rundam", "/d", "X", {"reservoir_carve": "on"})["RESERVOIR_CARVE"] == 2
    assert tuple(pl.CARVE_CHOICES) == rc.MODES
    st = workflow.RunSettings()
    k = workflow.key_settings(st)
    assert "reservoir_carve" not in k                  # off: checkpoints of 3.10.19 still resume
    assert workflow.key_settings(workflow.RunSettings(reservoir_carve="auto"))["reservoir_carve"] \
        == "auto"
    import importlib
    cli = importlib.import_module("nd_breach_runner.scripts.ndbreach_cli")
    assert cli.MODEL_KEYS["reservoir_carve"][1] == ("enum", rc.MODES)


def _write_tif(path, g, osr, gdal):
    ds = gdal.GetDriverByName("GTiff").Create(path, g.nx, g.ny, 1, gdal.GDT_Float32)
    ds.SetGeoTransform((g.x0, g.dx, 0, g.y0, 0, -g.dx))
    s = osr.SpatialReference()
    s.ImportFromEPSG(26914)
    ds.SetProjection(s.ExportToWkt())
    ds.GetRasterBand(1).SetNoDataValue(-9999.0)
    ds.GetRasterBand(1).WriteArray(np.where(np.isfinite(g.data), g.data, -9999.0)
                                   .astype(np.float32))
    ds = None


@test
def reservoir_carve_hecras_site_checks_see_the_carved_reservoir():
    """build_model (HEC-RAS) lowers its terrain by the run's carve depth raster: the storage
    site check that stopped the flat-water reservoir (ground never 1/4 H below the top) passes."""
    gdal, osr = _gdal()
    if gdal is None:
        print("    (skipped: no GDAL)")
        return
    from nd_breach_runner.core import gdal_io, hecras
    from nd_breach_runner.core import hecras_model as hm
    tmp = tempfile.mkdtemp(prefix="carve_hr_")
    try:
        dam = _dam()
        g = _grid(_lidar, half=3200.0, dx=10.0)
        dem = os.path.join(tmp, "dem.tif")
        _write_tif(dem, g, osr, gdal)
        X, Y = np.meshgrid(g.x_centers(), g.y_centers())
        dep = np.where((X > X0 + 40) & (X < X0 + 3000) & (np.abs(Y - Y0) < 300), 1.5, np.nan)
        _write_tif(os.path.join(tmp, "Depth (Max).tif"), g.like(dep), osr, gdal)
        md = os.path.join(tmp, hecras.MODEL_FOLDER)
        os.makedirs(md)
        hy = br.make_hydrograph(dam, br.METHOD_PIPING)
        hy.to_csv(os.path.join(md, hecras.HYDROGRAPH_CSV))
        rep = {"tool": "test", "dam": br.dam_to_dict(dam), "dam_xy": [X0, Y0],
               "hydrograph": hy.summary(), "scenario": "sunny", "work_crs": "EPSG:26914",
               "dem_z_to_m": 1.0, "dem_elevation_units": "metres",
               "settings": {"cell_size_m": 20.0, "manning_n": 0.06, "input_files": {"DEM": dem}},
               "summary": {"latest_arrival_hr": 2.0, "max_velocity_fps": 10.0,
                           "max_depth_ft": 5.0},
               "inflow_point": {"xy": [X0 + 80.0, Y0], "how": "test"}}

        def save():
            with open(os.path.join(md, hecras.RUN_REPORT_JSON), "w", encoding="utf-8") as f:
                json.dump(rep, f)
        save()
        opts = {"allow_bad_site": True, "breach_cut": False}
        r0 = hm.build_model(tmp, out_folder=os.path.join(tmp, "m0"), options=opts)
        p0 = [p for p in r0["notes"] if "SITE CHECK OVERRIDDEN" in p and "never lower" in p]
        assert p0, "the flat-water reservoir should fail the storage check"
        rc.run_hook(workflow.RunSettings(reservoir_carve="auto"), dam, (X0, Y0), (X0 + 80, Y0),
                    gdal_io.GdalTerrain(dem, "EPSG:26914", gdal_io.Z_METERS), None, md, rep, [],
                    downstream=(1.0, 0.0), crs_wkt="EPSG:26914")
        assert rep[rc.REPORT_KEY]["applied"]
        save()
        r1 = hm.build_model(tmp, out_folder=os.path.join(tmp, "m1"), options=opts)
        assert any("Reservoir bottom carved" in n for n in r1["notes"]), r1["notes"]
        assert not [p for p in r1["notes"] if "SITE CHECK OVERRIDDEN" in p and "never lower" in p]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
