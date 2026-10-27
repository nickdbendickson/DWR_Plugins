"""3.10.90 (DWR-3034 review): road overtopping depth on the road centreline at the road
surface, structure cuts and bare-earth openings left out (core/road_profile), and inventory
bridges moved to the road's opening / channel crossing (core/bridge_snap). Tiny synthetic
data only."""
import json
import os
import shutil
import tempfile

import numpy as np

from nd_breach_runner.tests.registry import test

FT = 0.3048
ROAD_M = 100.0


# ------------------------------------------------------------------ synthetic pieces
class _Terrain:
    """read(bounds, cell) -> Grid of fn(X, Y) (as tests/synthetic.SyntheticTerrain)."""

    def __init__(self, fn):
        self.fn, self.reads = fn, []

    def read(self, bounds, cell):
        from nd_breach_runner.core.grid import Grid
        xmin, ymin, xmax, ymax = bounds
        nx = max(1, int(round((xmax - xmin) / cell)))
        ny = max(1, int(round((ymax - ymin) / cell)))
        xs = xmin + (np.arange(nx) + 0.5) * cell
        ys = ymax - (np.arange(ny) + 0.5) * cell
        X, Y = np.meshgrid(xs, ys)
        self.reads.append((bounds, cell))
        return Grid(self.fn(X, Y), xmin, ymax, cell)


def _road_with_creek(X, Y, creek_x=155.0, half=15.0, depth=6.0, grade=0.0):
    """An east-west road embankment (y = 0) at ROAD_M (+ grade * x) over 90 m ground, with a
    creek opening `depth` m deep, 2 * half m wide at x = creek_x."""
    z = np.full(np.shape(X), 90.0)
    on = np.abs(Y) <= 10.0
    z = np.where(on, ROAD_M + grade * X, z)
    if depth:
        z = np.where(on & (np.abs(X - creek_x) <= half), ROAD_M - depth, z)
    return z


ROAD = np.array([[-400.0, 0.0], [400.0, 0.0]])


def _bridge(x, name, y0=-15.0, y1=15.0):
    from nd_breach_runner.core import structures as sm
    return sm.Structure(kind=sm.KIND_BRIDGE, line=np.array([[x, y0], [x, y1]]), width_m=30.0,
                        height_m=5.0, name=name, invert_up_m=95.0, invert_dn_m=95.0)


# ------------------------------------------------------------------ (b) bridge snap
@test
def bridge_snap_moves_a_misplaced_nbi_cut_to_the_road_opening_and_logs_the_distance():
    from nd_breach_runner.core import bridge_snap as bs
    t = _Terrain(_road_with_creek)
    b = _bridge(-35.0, "0025008224")              # 190 m from the creek, as on DWR-3034
    rows, notes, warns = bs.snap_bridges([b], [ROAD], t.read, [], radius_m=300.0)
    assert not warns, warns
    r = rows[0]
    assert r["method"] == "dem_opening", r
    assert abs(r["moved_m"] - 190.0) < 6.0, r
    mid = b.line.mean(axis=0)
    assert abs(mid[0] - 155.0) < 6.0 and abs(mid[1]) < 2.0, b.line
    assert abs(b.line[0][0] - b.line[-1][0]) < 1.0           # still across the road
    assert b.line[0][1] < b.line[-1][1]                       # same direction as given
    assert b.invert_up_m is None and b.invert_dn_m is None    # re-read at the new place
    assert any("0025008224" in n and "moved" in n and str(int(round(r["moved_m"]))) in n
               for n in notes), notes
    assert t.reads                                            # read the DEM, nothing else


@test
def bridge_snap_keeps_a_cut_already_at_the_opening():
    from nd_breach_runner.core import bridge_snap as bs
    t = _Terrain(_road_with_creek)
    b = _bridge(158.0, "on the creek")
    before = b.line.copy()
    rows, notes, warns = bs.snap_bridges([b], [ROAD], t.read, [], radius_m=300.0)
    assert rows[0]["method"] == "kept" and rows[0]["moved_m"] == 0.0, rows
    assert np.allclose(b.line, before) and b.invert_up_m == 95.0
    assert not warns


@test
def bridge_snap_follows_a_channel_crossing_when_the_dem_shows_no_opening():
    from nd_breach_runner.core import bridge_snap as bs
    t = _Terrain(lambda X, Y: _road_with_creek(X, Y, depth=0.0, grade=0.01))
    stream = np.array([[100.0, -200.0], [100.0, 200.0]])
    b = _bridge(0.0, "channel")
    rows, notes, warns = bs.snap_bridges([b], [ROAD], t.read, [stream], radius_m=300.0)
    r = rows[0]
    assert r["method"] == "channel_crossing", r
    assert abs(r["moved_m"] - 100.0) < 3.0, r
    assert abs(b.line.mean(axis=0)[0] - 100.0) < 3.0, b.line


@test
def bridge_snap_falls_back_with_a_warning_when_no_crossing_is_found():
    from nd_breach_runner.core import bridge_snap as bs
    # graded road, no opening, no stream: nothing to snap to
    t = _Terrain(lambda X, Y: _road_with_creek(X, Y, depth=0.0, grade=0.01))
    b = _bridge(0.0, "nowhere")
    before = b.line.copy()
    rows, notes, warns = bs.snap_bridges([b], [ROAD], t.read, [], radius_m=300.0)
    assert rows[0]["method"] == "none" and np.allclose(b.line, before), rows
    assert len(warns) == 1 and "nowhere" in warns[0] and "inventory location" in warns[0]
    # a bridge far from any road; and a run without road lines
    far = _bridge(0.0, "far away", 500.0, 530.0)
    rows, notes, warns = bs.snap_bridges([far], [ROAD], t.read, [], radius_m=300.0)
    assert rows[0]["method"] == "none" and "far away" in warns[0]
    rows, notes, warns = bs.snap_bridges([_bridge(0.0, "x")], [], t.read, [], radius_m=300.0)
    assert not rows and len(warns) == 1 and "no road or rail lines" in warns[0]


@test
def bridge_snap_leaves_culverts_and_guesses_alone_and_can_be_turned_off():
    from nd_breach_runner.core import bridge_snap as bs
    from nd_breach_runner.core import structures as sm
    t = _Terrain(_road_with_creek)
    c = sm.Structure(kind=sm.KIND_CULVERT if hasattr(sm, "KIND_CULVERT") else "culvert",
                     line=np.array([[-35.0, -15.0], [-35.0, 15.0]]), name="cv")
    g = _bridge(-35.0, "guess")
    g.source = "guess"
    rows, notes, warns = bs.snap_bridges([c, g], [ROAD], t.read, [], radius_m=300.0)
    assert rows == [] and not warns
    old = os.environ.get("NDBREACH_BRIDGE_SNAP_M")
    os.environ["NDBREACH_BRIDGE_SNAP_M"] = "0"
    try:
        b = _bridge(-35.0, "off")
        rows, notes, warns = bs.snap_bridges([b], [ROAD], t.read, [])
        assert rows == [] and b.line[0][0] == -35.0 and "off" in notes[0]
    finally:
        if old is None:
            os.environ.pop("NDBREACH_BRIDGE_SNAP_M", None)
        else:
            os.environ["NDBREACH_BRIDGE_SNAP_M"] = old


@test
def bridge_snap_runs_in_the_runner_before_the_inverts_are_read():
    src = open(os.path.join(os.path.dirname(os.path.dirname(__file__)), "core",
                            "workflow.py"), encoding="utf-8").read()
    i = src.index("bridge_snap.snap_bridges(")
    assert i < src.index("reconcile_inverts(st.structures") < src.index("key_settings(st),")
    assert 'report["bridge_snap"]' in src


# ------------------------------------------------------------------ (a) road crown depth
def _line_x(x0=0.0, x1=200.0):
    return [np.array([[x0, 0.0], [x1, 0.0]])]


def _ground(trench_x=None, notch_x=None):
    def g(xy):
        xy = np.asarray(xy, dtype=float)
        z = np.full(len(xy), ROAD_M)
        if notch_x is not None:                      # bare earth under a bridge (no deck)
            z[np.abs(xy[:, 0] - notch_x) <= 10.0] = ROAD_M - 5.0
        return z
    return g


def _wse(level_m, where=None):
    def w(xy):
        xy = np.asarray(xy, dtype=float)
        out = np.full(len(xy), level_m / FT)
        if where is not None:
            out[~where(xy)] = np.nan
        return out
    return w


SAG_ROAD = [np.array([[-300.0, 15.0], [300.0, 15.0]])]


def _sag(grade=0.03, x0=15.0, opening_half=None, bed=494.0):
    """A road crown 500 m + grade * |x - x0| (a sag at x0), with a bare-earth opening (no deck)
    down to `bed` within opening_half of x0."""
    def g(xy):
        x = np.asarray(xy, dtype=float)[:, 0] - x0
        z = 500.0 + grade * np.abs(x)
        if opening_half:
            z = np.where(np.abs(x) <= opening_half, bed, z)
        return z
    return g


@test
def road_crown_culverts_never_mask_and_bridge_cuts_read_the_deck_stand_in():
    """3.10.90 review (roads F1): a culvert is buried, so the fine DEM is the road over it and
    its cut never masks; a bridge / notch cut reads the road on a deck stand-in from the
    approaches (no extra 50 ft buffer), never below what that stand-in gives."""
    from nd_breach_runner.core import road_profile as rp
    culv = [{"kind": "culvert", "line": [[15.0, 0.0], [15.0, 30.0]], "width_m": 1.5,
             "barrels": 1, "name": "culvert 1"}]
    assert rp.cut_corridors(culv, 10.0) == []
    # a 3 % sag with the culvert at its low point, 0.5 m (1.64 ft) of real overtopping
    got = rp.road_crown_depth(SAG_ROAD, _wse(500.5), _sag(), cuts=rp.cut_corridors(culv, 10.0))
    assert abs(got["depth_ft"] - 0.5 / FT) < 0.02 and got["n_cut"] == 0, got
    br = [{"kind": "bridge", "line": [[15.0, 0.0], [15.0, 30.0]], "width_m": 24.0,
           "barrels": 1, "name": "NBI"}]
    cuts = rp.cut_corridors(br, 30.0)
    assert len(cuts) == 1 and abs(cuts[0][1] - 15.0) < 1e-9, cuts     # 0.5 * max(30, 26.4)
    # the bridge at the sag: the deck and approaches 0.6 m (1.97 ft) under water, the bare earth
    # under the removed deck 6 m down: read on the stand-in (the approaches' grade carried in)
    got = rp.road_crown_depth(SAG_ROAD, _wse(500.6), _sag(opening_half=12.0), cuts=cuts)
    assert got["n_cut"] > 0 and got["depth_ft"] >= 0.6 / FT - 0.05, got
    assert got["depth_ft"] < 2.5, got                                # not the 6 m bare earth
    # DWR-3034: the water stands 3 m below the road in the cut's trench only -> dry
    trench = _wse(497.0, lambda xy: np.abs(xy[:, 0] - 15.0) <= 15.0)
    for ground in (_sag(opening_half=12.0), _sag()):     # at the opening, or on solid fill
        got = rp.road_crown_depth(SAG_ROAD, trench, ground, cuts=cuts)
        assert got["depth_ft"] == 0.0 and got["freeboard_ft"] > 5.0, got
    # the stand-in never hides water: a deck in the LiDAR lower than both approaches reads on
    # its own surface
    deck = _sag(grade=0.0)
    got = rp.road_crown_depth(SAG_ROAD, _wse(500.3), lambda xy: deck(xy) - np.where(
        np.abs(np.asarray(xy)[:, 0] - 15.0) <= 10.0, 0.2, 0.0), cuts=cuts)
    assert abs(got["depth_ft"] - 0.5 / FT) < 0.02, got


@test
def road_crown_openings_need_a_mapped_bridge_and_a_multi_metre_baseline():
    """3.10.90 review (roads F6): a bare-earth opening takes the deck stand-in only where it
    lines up with a mapped bridge / notch, and its steep side is measured over several metres
    (pixel steps, LiDAR noise and seams do not make a road sag an opening)."""
    from nd_breach_runner.core import road_profile as rp
    wet = lambda xy: np.abs(np.asarray(xy)[:, 0]) < 60.0                 # noqa: E731
    # a 0.25 m collection seam 8 m from the bottom of a 5 % sag, 0.4 m of real overtopping
    seam = lambda xy: 500.0 + 0.05 * np.abs(xy[:, 0]) + np.where(xy[:, 0] > 8.0, 0.25, 0.0)  # noqa: E731,E501
    got = rp.road_crown_depth(SAG_ROAD, _wse(500.4), seam, wet=wet)
    assert abs(got["depth_ft"] - 0.4 / FT) < 0.02 and got["n_opening"] == 0, got
    # noisy LiDAR (sigma 5 cm) on an 8 % sag: never hidden
    for seed in range(10):
        nz = np.random.default_rng(seed).normal(0.0, 0.05, 1000)
        g = lambda xy, nz=nz: 500.0 + 0.08 * np.abs(xy[:, 0]) + nz[np.floor(xy[:, 0]).astype(int) + 500]  # noqa: E731,E501
        got = rp.road_crown_depth(SAG_ROAD, _wse(500.3), g, wet=wet)
        assert got["depth_ft"] > 0.5, (seed, got)
    # a 10 m DEM (pixel steps of 0.6 m) on a 6 % sag: no opening
    class Coarse:
        pixel_m = 10.0

        def __call__(self, xy):
            return 500.0 + 0.06 * np.abs((np.floor(xy[:, 0] / 10.0) + 0.5) * 10.0)
    got = rp.road_crown_depth(SAG_ROAD, _wse(500.6), Coarse(), wet=wet)
    raw = rp.road_crown_depth(SAG_ROAD, _wse(500.6), Coarse(), wet=wet, mask_openings=False)
    assert got["n_opening"] == 0 and got["depth_ft"] == raw["depth_ft"] > 0.5, (got, raw)
    # a real bare-earth opening with no mapped structure: read as it is (counted)
    w = _wse(ROAD_M - 3.0, lambda xy: np.abs(xy[:, 0] - 100.0) <= 10.0)
    got = rp.road_crown_depth(_line_x(), w, _ground(notch_x=100.0))
    assert abs(got["depth_ft"] - 2.0 / FT) < 0.01 and got["n_opening_unmatched"] == 1, got
    # the same opening at a mapped bridge: the deck stand-in from the approaches
    cuts = rp.cut_corridors([{"kind": "bridge", "line": [[100.0, -5.0], [100.0, 5.0]],
                              "width_m": 4.0}], 1.0)
    got = rp.road_crown_depth(_line_x(), w, _ground(notch_x=100.0), cuts=cuts)
    assert got["depth_ft"] == 0.0 and got["n_opening"] > 0 and got["n_cut"] > 0, got
    assert abs(got["freeboard_ft"] - 3.0 / FT) < 0.05, got
    # a culvert is no evidence of an opening (a sag over a culvert is the usual crossing)
    cv = rp.cut_corridors([{"kind": "culvert", "line": [[100.0, -5.0], [100.0, 5.0]]}], 1.0)
    got = rp.road_crown_depth(_line_x(), w, _ground(notch_x=100.0), cuts=cv)
    assert abs(got["depth_ft"] - 2.0 / FT) < 0.01, got


@test
def road_crown_wet_points_with_no_road_surface_take_the_depth_raster():
    """3.10.90 review (roads F4): a wet centreline point with no road surface (DEM no-data,
    outside the DEM, a refilled area) takes the depth raster there; a road whose wet points
    have no water surface falls back to the raster (None), not 0."""
    from nd_breach_runner.core import road_profile as rp
    road = [np.array([[-100.0, 0.0], [100.0, 0.0]])]
    part_dem = lambda xy: np.where(xy[:, 0] < 0.0, np.nan, 501.0)       # noqa: E731
    raster = lambda xy: np.where(xy[:, 0] < 0.0, 1.0 / FT, 0.5 / FT)    # noqa: E731
    wet = lambda xy: np.ones(len(xy), bool)                              # noqa: E731
    got = rp.road_crown_depth(road, _wse(500.0), part_dem, wet=wet, depth_ft=raster)
    assert abs(got["depth_ft"] - 1.0 / FT) < 0.01 and got["n_nodem"] > 90, got
    assert got["x"] < 0.0 and abs(got["freeboard_ft"] - 1.0 / FT) < 0.01, got
    assert abs(got["raster_points_ft"] - 1.0 / FT) < 0.01, got
    # no depth raster to fall back on: the caller keeps the raster depth (None), never 0
    assert rp.road_crown_depth(road, _wse(500.0), part_dem, wet=wet) is None
    # wet by the depth raster, no water surface anywhere: None (the raster), not 0
    nan_w = lambda xy: np.full(len(xy), np.nan)                          # noqa: E731
    assert rp.road_crown_depth(road, nan_w, _ground(), wet=wet, depth_ft=raster) is None
    # no road surface anywhere: the raster for the road
    assert rp.road_crown_depth(_line_x(), _wse(ROAD_M + 1.0),
                               lambda xy: np.full(len(xy), np.nan)) is None
    # a road layer split at the bridge (an OSM bridge way): no approach on the line, so no
    # deck stand-in - those points take the depth raster (the whole way: None, the raster)
    cuts = rp.cut_corridors([{"kind": "bridge", "line": [[0.0, -15.0], [0.0, 15.0]],
                              "width_m": 40.0}], 10.0)
    bed = lambda xy: np.full(len(xy), 494.0)                             # noqa: E731
    assert rp.road_crown_depth([np.array([[-15.0, 0.0], [15.0, 0.0]])], _wse(497.0), bed,
                               wet=wet, cuts=cuts, depth_ft=raster) is None
    got = rp.road_crown_depth([np.array([[-15.0, 0.0], [60.0, 0.0]])], _wse(497.0),
                              lambda xy: np.where(xy[:, 0] > 22.0, 500.0, 494.0), wet=wet,
                              cuts=cuts, depth_ft=raster)
    assert got["n_no_approach"] == 0 and got["n_cut"] > 0, got     # one approach is enough
    assert got["depth_ft"] == 0.0, got          # the stand-in from the east approach: dry
    nan_east = lambda xy: np.where(xy[:, 0] > 22.0, np.nan, 494.0)       # noqa: E731
    got = rp.road_crown_depth([np.array([[-15.0, 0.0], [60.0, 0.0]])], _wse(497.0), nan_east,
                              wet=wet, cuts=cuts, depth_ft=raster)
    assert got is None                           # no approach surface and no DEM: the raster


def _tiny_dem(path, value=ROAD_M, unit=None):
    from osgeo import gdal, osr
    ds = gdal.GetDriverByName("GTiff").Create(path, 80, 80, 1, gdal.GDT_Float32)
    assert ds is not None, path
    ds.SetGeoTransform((0.0, 1.0, 0.0, 80.0, 0.0, -1.0))
    sr = osr.SpatialReference()
    sr.ImportFromEPSG(26914)
    ds.SetProjection(sr.ExportToWkt())
    a = np.full((80, 80), value, dtype=np.float32) if np.isscalar(value) else \
        np.asarray(value, dtype=np.float32)
    ds.GetRasterBand(1).WriteArray(a)
    ds.GetRasterBand(1).SetNoDataValue(-9999.0)
    if unit:
        ds.GetRasterBand(1).SetUnitType(unit)
    ds = None


def _have_gdal():
    try:
        from osgeo import gdal  # noqa: F401
        return True
    except ImportError:
        print("      (skipped: no GDAL)")
        return False


@test
def ndhazard_tool_road_overtopping_on_the_centreline_not_in_the_cut_trench():
    if not _have_gdal():
        return
    from nd_breach_runner.core import workflow
    from nd_breach_runner.tests import test_ndhazard_tool as tt
    an, fq, _ = tt._load_tool()
    h = tt._Harness()
    tmp = tempfile.mkdtemp()
    try:
        dem = os.path.join(tmp, "dem_1m.tif")
        z = np.full((80, 80), ROAD_M)
        z[20:30, 22:29] = ROAD_M - 5.0                   # bare earth under the bridge on Hwy 1
        _tiny_dem(dem, z)
        bm = os.path.join(h.folder, "_breach_model")
        os.makedirs(bm)
        json.dump({"settings": {"input_files": {"DEM": dem}, "cell_size_m": 10.0},
                   "dem_z_to_m": 1.0}, open(os.path.join(bm, "run_report.json"), "w"))
        # a bridge cut across Hwy 1 (y 55) at x 25: its trench is Hwy 1's only wet cell
        json.dump([{"kind": "bridge", "line": [[25.0, 50.0], [25.0, 60.0]], "width_m": 6.0,
                    "barrels": 1, "name": "NBI 1"}], open(os.path.join(bm, "structures.json"), "w"))
        d = h.arrays[os.path.join(h.folder, workflow.FINAL_NAMES["depth"])]
        wse = np.where(d > 0, ROAD_M / FT + np.nan_to_num(d), np.nan)
        wse[2, 2] = (ROAD_M - 3.0) / FT                  # the trench water: 3 m below the road
        wp = os.path.join(h.folder, workflow.FINAL_NAMES["wse"])
        open(wp, "w").write("x")
        h.arrays[wp] = wse
        s, r, t, l, dm = tt._layers(fq)
        base = {"DAM_FOLDER": h.folder, "STRUCTURES": s, "ROADS": r, "RAIL": t,
                "LIFELINES": l, "DOWNSTREAM_DAMS": dm, "CHART_FILE": h.chart_file()}
        res, ctx, fb = tt._run(an, fq, base)
        js = json.load(open(res["JSON"]))
        rd = js["road_depth"]
        assert rd["basis"] == "centreline" and rd["bridge_notch_cuts"] == 1, rd
        assert not [f for f in js["findings"] if f["kind"] == "road" and f["id"] == "Hwy 1"]
        drop = [x for x in rd["dropped"] if x["id"] == "Hwy 1"]
        assert drop and drop[0]["raster_depth_ft"] == 0.5 and drop[0]["n_cut"] > 0, rd
        assert any("Hwy 1" in e and "not counted as overtopped" in e for e in fb.errors), \
            fb.errors
        i94 = tt._find(js, "road", "I- 94")                       # real overtopping stays
        assert abs(i94["depth_ft"] - 0.2) < 0.02, i94
        md = open(res["REPORT"], encoding="utf-8").read()
        assert "Road overtopping depth" in md and "Not counted as overtopped: Hwy 1" in md, md
        # the old basis on request
        res, ctx, fb = tt._run(an, fq, dict(base, ROAD_DEPTH_BASIS=1))
        js = json.load(open(res["JSON"]))
        assert tt._find(js, "road", "Hwy 1")["depth_ft"] == 0.5
        # no WSE raster: the depth raster, with a note
        os.remove(wp)
        res, ctx, fb = tt._run(an, fq, base)
        js = json.load(open(res["JSON"]))
        assert tt._find(js, "road", "Hwy 1")["depth_ft"] == 0.5
        assert any("depth raster for every road" in e for e in fb.errors), fb.errors
    finally:
        h.close()
        shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------------- the ND 5.2 tool on real rasters
class _Site:
    """A dam folder of real GDAL rasters for the ND 5.2 tool (fake QGIS): a 10 m model grid of
    40 x 3 cells (local x 0..400, y 0..30 m), the run's 1 m DEM (400 x 30 pixels), County 12
    along y = 15. `u` metres per CRS unit and `origin` the lower-left corner in the CRS: the
    same site in State Plane feet (EPSG:2265) or UTM. fine (30, 400) and cell_ground (3, 40)
    are metres; the DEM is written as fine / dem_z (dem_z metres per DEM unit)."""

    def __init__(self, fine, cell_ground, wse_m, structures=None, report=None, epsg=26914,
                 u=1.0, origin=(0.0, 0.0), dem_z=1.0, dem_unit=None, work_epsg=None):
        self.tmp = tempfile.mkdtemp()
        self.epsg, self.u, self.origin = epsg, float(u), origin
        self.folder = os.path.join(self.tmp, "ND00999")
        bm = os.path.join(self.folder, "_breach_model")
        os.makedirs(bm)
        self.dem = self.tif(os.path.join(self.tmp, "dem_1m.tif"), np.asarray(fine) / dem_z, 1.0,
                            unit=dem_unit)
        wse = np.broadcast_to(np.asarray(wse_m, dtype=float), np.shape(cell_ground))
        depth = wse - np.asarray(cell_ground, dtype=float)
        wet = depth > 0.003
        self.tif(os.path.join(self.folder, "Depth (Max).tif"), np.where(wet, depth / FT, np.nan),
                 10.0)
        self.tif(os.path.join(self.folder, "Velocity (Max).tif"), np.where(wet, 1.0, np.nan),
                 10.0)
        self.tif(os.path.join(self.folder, "WSE (Max).tif"), np.where(wet, wse / FT, np.nan),
                 10.0)
        rep = {"settings": {"input_files": {"DEM": self.dem}, "cell_size_m": 10.0},
               "dem_z_to_m": dem_z}
        if work_epsg:
            rep["work_crs"] = "EPSG:{}".format(work_epsg)
        for k, v in (report or {}).items():
            if v is None:
                rep.pop(k, None)
            else:
                rep[k] = v
        json.dump(rep, open(os.path.join(bm, "run_report.json"), "w"))
        if structures is not None:
            json.dump(structures, open(os.path.join(bm, "structures.json"), "w"))

    def xy(self, x, y):
        return (self.origin[0] + x / self.u, self.origin[1] + y / self.u)

    def tif(self, path, a, px_m, unit=None, values=None):
        from osgeo import gdal, osr
        a = np.asarray(a, dtype=float)
        ds = gdal.GetDriverByName("GTiff").Create(path, a.shape[1], a.shape[0], 1,
                                                  gdal.GDT_Float32)
        x0, y1 = self.xy(0.0, 30.0)
        ds.SetGeoTransform((x0, px_m / self.u, 0.0, y1, 0.0, -px_m / self.u))
        sr = osr.SpatialReference()
        sr.ImportFromEPSG(self.epsg)
        ds.SetProjection(sr.ExportToWkt())
        b = ds.GetRasterBand(1)
        b.SetNoDataValue(-9999.0)
        b.WriteArray(np.where(np.isfinite(a), a, -9999.0).astype(np.float32))
        if unit:
            b.SetUnitType(unit)
        ds = None
        return path

    def run(self, roads=None, **params):
        from nd_breach_runner.core import nd_hazard as nh
        from nd_breach_runner.tests import test_ndhazard_tool as tt
        an, fq, _ = tt._load_tool()
        roads = roads or [("County 12", [(0.5, 15.0), (399.5, 15.0)], 600.0)]
        src = fq.FakeSource([fq.FakeFeature(fq.FakeGeometry(
            "line", [[self.xy(x, y) for x, y in pts]]), NAME=name, AADT=aadt, RTTYP="C",
            private="") for name, pts, aadt in roads], source_name="roads")
        p = {"DAM_FOLDER": self.folder, "ROADS": src,
             "LETHALITY": nh.LETHALITY_METHODS.index(nh.LETHALITY_ALL_WET)}
        p.update(params)
        res, ctx, fb = tt._run(an, fq, p)
        js = json.load(open(res["JSON"], encoding="utf-8"))
        md = open(res["REPORT"], encoding="utf-8").read()
        return js, md, fb

    def close(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


_X = np.arange(400) + 0.5
_Y = 30.0 - (np.arange(30) + 0.5)
_XX, _YY = np.meshgrid(_X, _Y)
_ON_ROAD = np.abs(_YY - 15.0) <= 5.0


def _cells(fine):
    """The model's cell ground: the 1 m DEM averaged over each 10 m cell (the default)."""
    return np.asarray(fine, dtype=float).reshape(3, 10, 40, 10).mean(axis=(1, 3))


def _roads_of(js):
    return {f["id"]: f["depth_ft"] for f in js["findings"] if f["kind"] == "road"}


def _flat_site(**kw):
    fine = np.where(_ON_ROAD, 500.0, 497.0)
    return _Site(fine, _cells(fine), 500.5, **kw), fine


@test
def ndhazard_tool_culvert_at_a_sag_keeps_its_overtopping_and_lists_the_lower_depth():
    """3.10.90 review (roads F1, F7): a culvert at a road sag no longer hides the overtopping;
    a road read lower than the depth raster gets a Check and a road_depth.lowered row."""
    if not _have_gdal():
        return
    fine = np.where(_ON_ROAD, 500.0 + 0.03 * np.abs(_XX - 205.0), 497.0)
    cg = _cells(fine)
    cg[:, 20] = 497.0                                              # the culvert's trench cell
    site = _Site(fine, cg, 500.5, structures=[{
        "kind": "culvert", "line": [[205.0, 0.0], [205.0, 30.0]], "width_m": 1.5,
        "barrels": 1, "name": "culvert 1"}])
    try:
        js, md, fb = site.run()
        got = _roads_of(js)
        assert abs(got.get("County 12", 0.0) - 0.5 / FT) < 0.1, (got, fb.errors)
        assert js["hazard_class"] == "Medium", js["hazard_class"]
        low = js["road_depth"]["lowered"]
        assert low and low[0]["id"] == "County 12" and low[0]["raster_depth_ft"] > 10.0, low
        assert any("County 12" in e and "shallower than the depth raster" in e
                   for e in fb.errors), fb.errors
        assert "Overtopped, shallower than the depth raster: County 12" in md
        assert js["road_depth"]["culverts_not_masked"] == 1
    finally:
        site.close()


def _dwr3034_site(misplaced, **kw):
    """DWR-3034-like: Hwy 25's embankment at 500 m over 494 m ground; Square Butte Creek's
    bare-earth opening (the deck is not in the LiDAR) at x 190-220 down to 493 m; the NBI
    bridge cut at the creek (after the snap) or 100 m south of it on solid fill (misplaced).
    The flood stands at 494 m: 6 m below the road, wet in the creek and in the cut's trench."""
    fine = np.where(_ON_ROAD, 500.0, 494.0)
    fine = np.where(_ON_ROAD & (np.abs(_XX - 205.0) <= 15.0), 493.0, fine)
    cg = _cells(fine)
    xc = 105.0 if misplaced else 205.0
    cg[:, int(xc // 10)] = 493.0                                    # the cut's trench cell
    st = [{"kind": "bridge", "line": [[xc, 0.0], [xc, 30.0]], "width_m": 30.0, "barrels": 1,
           "name": "NBI 0025008224"}]
    if misplaced:      # the creek's own (correct) bridge record, e.g. OSM
        st.append({"kind": "bridge", "line": [[205.0, 0.0], [205.0, 30.0]], "width_m": 30.0,
                   "barrels": 1, "name": "OSM bridge"})
    return _Site(fine, cg, 494.0, structures=st, **kw)


@test
def ndhazard_tool_dwr3034_bridge_trench_reads_dry_and_is_listed():
    """DWR-3034: the trench of a bridge cut (snapped onto the creek, or misplaced on solid
    fill) is not road overtopping; the road the depth raster had overtopped is listed with
    the raster depth and a Check (no silent drop)."""
    if not _have_gdal():
        return
    for misplaced in (False, True):
        site = _dwr3034_site(misplaced)
        try:
            js, md, fb = site.run(roads=[("Hwy 25", [(0.5, 15.0), (399.5, 15.0)], 1500.0)])
            assert "Hwy 25" not in _roads_of(js), (misplaced, _roads_of(js), fb.errors)
            rd = js["road_depth"]
            drop = rd["dropped"]
            assert drop and drop[0]["id"] == "Hwy 25" and drop[0]["raster_depth_ft"] > 3.0, rd
            assert drop[0]["n_cut"] > 0 and drop[0]["freeboard_ft"] > 15.0, drop
            assert any("Hwy 25" in e and "not counted as overtopped" in e for e in fb.errors)
            assert "Not counted as overtopped: Hwy 25" in md
            # the old basis still reads the trench
            js, md, fb = site.run(roads=[("Hwy 25", [(0.5, 15.0), (399.5, 15.0)], 1500.0)],
                                  ROAD_DEPTH_BASIS=1)
            assert _roads_of(js).get("Hwy 25", 0.0) > 3.0, _roads_of(js)
        finally:
            site.close()


@test
def ndhazard_tool_road_surface_dem_has_its_own_units_and_is_checked_against_the_run():
    """3.10.90 review (roads F2): the 'Road surface DEM' is read in its own vertical unit (band
    unit or vertical CRS, or the units chosen), and is used only when its median offset from
    the run's own ground is within 1 m; otherwise every road reads the depth raster, with a
    Check."""
    if not _have_gdal():
        return
    site, fine = _flat_site()
    try:
        ref = _roads_of(site.run()[0])
        assert abs(ref["County 12"] - 0.5 / FT) < 0.02, ref
        ft_tag = site.tif(os.path.join(site.tmp, "road_ft_tagged.tif"), fine / FT, 1.0, unit="ft")
        js, md, fb = site.run(ROAD_DEM=ft_tag)
        assert js["road_depth"]["basis"] == "centreline", js["road_depth"]
        assert abs(js["road_depth"]["datum_check"]["median_offset_m"]) < 0.01
        assert _roads_of(js) == ref, (_roads_of(js), ref)
        ft_raw = site.tif(os.path.join(site.tmp, "road_ft.tif"), fine / FT, 1.0)
        js, md, fb = site.run(ROAD_DEM=ft_raw)                   # no unit recorded: the raster
        assert js["road_depth"]["basis"] == "raster", js["road_depth"]
        assert any("Road surface DEM elevation units" in e for e in fb.errors), fb.errors
        assert "County 12" in _roads_of(js)
        js, md, fb = site.run(ROAD_DEM=ft_raw, ROAD_DEM_UNITS=2)   # feet, chosen
        assert js["road_depth"]["basis"] == "centreline" and _roads_of(js) == ref
        shifted = site.tif(os.path.join(site.tmp, "road_ngvd.tif"), fine + 2.0, 1.0, unit="m")
        js, md, fb = site.run(ROAD_DEM=shifted)                  # 2 m off the run's datum
        assert js["road_depth"]["basis"] == "raster", js["road_depth"]
        assert any("vertical datum" in e for e in fb.errors), fb.errors
        # without the run's DEM: checked against the run's model ground (WSE - depth)
        os.remove(site.dem)
        js, md, fb = site.run(ROAD_DEM=ft_tag)
        assert js["road_depth"]["basis"] == "centreline", (js["road_depth"], fb.errors)
        assert "model ground" in js["road_depth"]["datum_check"]["against"]
        assert _roads_of(js) == ref
    finally:
        site.close()


@test
def ndhazard_tool_old_run_reports_keep_their_dem_units():
    """3.10.90 review (roads F3): a run_report.json from before 3.10.16 has no dem_z_to_m; the
    unit comes from dem_elevation_units (as core/hecras_model._z_factor), then the raster's own
    unit, never a metres default (unknown -> the depth raster, with a Check)."""
    if not _have_gdal():
        return
    fine = np.where(_ON_ROAD, 500.0, 497.0)
    for note, unit, want in (("feet (chosen)", None, "centreline"),
                             (None, "ft", "centreline"), (None, None, "raster")):
        rep = {"dem_z_to_m": None}
        if note:
            rep["dem_elevation_units"] = note
        site = _Site(fine, _cells(fine), 500.5, report=rep, dem_z=FT, dem_unit=unit)
        try:
            js, md, fb = site.run()
            assert js["road_depth"]["basis"] == want, (note, unit, js["road_depth"])
            assert abs(_roads_of(js).get("County 12", 0.0) - 0.5 / FT) < 0.02, _roads_of(js)
            if want == "raster":
                assert any("records no unit" in e for e in fb.errors), fb.errors
        finally:
            site.close()


@test
def ndhazard_tool_road_dem_read_error_keeps_the_raster_for_that_road():
    """3.10.90 review (roads F5): a road surface DEM that opens but cannot be read (a VRT tile
    gone since the run) no longer stops the tool: that road keeps the depth raster, with a
    Check naming it, as on main."""
    if not _have_gdal():
        return
    from osgeo import gdal
    site, fine = _flat_site()
    try:
        t0 = site.tif(os.path.join(site.tmp, "t0.tif"), fine[:, :200], 1.0)
        site.origin = (site.origin[0] + 200.0, site.origin[1])
        t1 = site.tif(os.path.join(site.tmp, "t1.tif"), fine[:, 200:], 1.0)
        site.origin = (site.origin[0] - 200.0, site.origin[1])
        vrt = os.path.join(site.tmp, "dem.vrt")
        gdal.BuildVRT(vrt, [t0, t1])
        os.remove(t1)
        rp = os.path.join(site.folder, "_breach_model", "run_report.json")
        rep = json.load(open(rp))
        rep["settings"]["input_files"]["DEM"] = vrt
        json.dump(rep, open(rp, "w"))
        js, md, fb = site.run()
        assert abs(_roads_of(js).get("County 12", 0.0) - 0.5 / FT) < 0.02, _roads_of(js)
        rr = js["road_depth"]["raster_roads"]
        assert rr and rr[0]["id"] == "County 12" and "could not be read" in rr[0]["reason"], rr
        assert any("County 12" in e and "could not be read" in e for e in fb.errors), fb.errors
    finally:
        site.close()


@test
def ndhazard_tool_feet_crs_folder_moves_the_structure_lines_and_works_in_metres():
    """3.10.90 review (roads F8): with the DEM in State Plane feet the final rasters are in that
    CRS while structures.json is in the run's work CRS (UTM m); the cut lines are moved into the
    rasters' CRS and the profile's metre constants are applied in metres, so the bridge trench
    reads dry there too, and the report says what matched."""
    if not _have_gdal():
        return
    from osgeo import osr
    sp, utm = osr.SpatialReference(), osr.SpatialReference()
    sp.ImportFromEPSG(2265)                       # NAD83 / North Dakota North (ft)
    utm.ImportFromEPSG(26914)
    for s in (sp, utm):
        s.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    tf = osr.CoordinateTransformation(sp, utm)
    unit = sp.GetLinearUnits()
    origin = (1968500.0, 300000.0)
    fine = np.where(_ON_ROAD, 500.0, 494.0)
    fine = np.where(_ON_ROAD & (np.abs(_XX - 205.0) <= 15.0), 493.0, fine)
    cg = _cells(fine)
    cg[:, 20] = 493.0
    line = [tf.TransformPoint(origin[0] + 205.0 / unit, origin[1] + y / unit)[:2]
            for y in (0.0, 30.0)]
    site = _Site(fine, cg, 494.0, epsg=2265, u=unit, origin=origin, dem_z=unit,
                 work_epsg=26914, structures=[{"kind": "bridge", "line": [list(p) for p in line],
                                               "width_m": 30.0, "barrels": 1, "name": "NBI"}])
    try:
        js, md, fb = site.run(roads=[("Hwy 25", [(0.5, 15.0), (399.5, 15.0)], 1500.0)])
        rd = js["road_depth"]
        assert "Hwy 25" not in _roads_of(js), (_roads_of(js), rd)
        assert rd["points_on_cut_stand_in"] > 0 and rd["roads_on_a_stand_in"] == 1, rd
        assert abs(rd["unit_m"] - unit) < 1e-9 and "work CRS" in rd["structures_note"], rd
        assert rd["dropped"] and rd["dropped"][0]["freeboard_ft"] > 15.0, rd
    finally:
        site.close()
    # a cut that matches no road: the report says so, and the real overtopping stays
    site, _f = _flat_site(structures=[{"kind": "bridge", "line": [[50.0, -100.0], [50.0, -80.0]],
                                       "width_m": 10.0, "barrels": 1, "name": "far away"}])
    try:
        js, md, fb = site.run()
        assert js["road_depth"]["roads_on_a_stand_in"] == 0, js["road_depth"]
        assert "1 bridge / notch cut(s) in structures.json, 0 road(s) with wet points in one" \
            in js["road_depth"]["text"], js["road_depth"]["text"]
        assert abs(_roads_of(js)["County 12"] - 0.5 / FT) < 0.02
    finally:
        site.close()


@test
def cli_runs_record_road_and_rail_lines_and_the_dem():
    """3.10.90 review (bookkeeping F4): the command-line runner fills st.road_lines /
    st.rail_lines from the kept receptor roads / rail (as the QGIS tools' apply_features) and
    records input_files['DEM'], so the bridge snap and the ND tool's centreline basis work."""
    import sys
    from nd_breach_runner.tests import test_cli as tc
    c = tc.cli()
    with tc.Tmp() as d:
        p, _cfg = tc.base_config(d)
        tc.dump(os.path.join(d, "road.geojson"), tc.fc([tc.line([[400, -200], [400, 200]]),
                                                        tc.line([[9e6, 0], [9e6, 5]])]))
        tc.dump(os.path.join(d, "rail.geojson"), tc.fc([tc.line([[600, -200], [600, 200]])]))
        cfg = json.load(open(p))
        cfg["layers"] = {"receptor_roads": "road.geojson", "receptor_rail": "rail.geojson"}
        tc.dump(p, cfg)
        opts = c.build_parser().parse_args(["run", p, "--lisflood", sys.executable])
        st = c.prepare(c.load_config(p), opts).st
        assert len(st.road_lines) == 1 and np.allclose(st.road_lines[0][:, 0], 400.0), \
            st.road_lines
        assert len(st.rail_lines) == 1 and np.allclose(st.rail_lines[0][:, 0], 600.0)
        assert os.path.samefile(st.input_files["DEM"], os.path.join(d, "dem.asc")), \
            st.input_files
        assert "DEM" in st.as_dict()["input_files"]


@test
def readme_documents_the_bridge_snap_radius_and_the_road_depth_basis():
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    text = open(os.path.join(here, "README.md"), encoding="utf-8").read()
    assert "NDBREACH_BRIDGE_SNAP_M" in text
    assert "Road surface DEM elevation units" in text and "deck stand-in" in text
