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


@test
def road_crown_depth_leaves_out_the_structure_cut_trench():
    from nd_breach_runner.core import road_profile as rp
    cut = {"kind": "bridge", "line": [[100.0, -15.0], [100.0, 15.0]], "width_m": 10.0,
           "barrels": 1, "name": "NBI"}
    cuts = rp.cut_corridors([cut], 30.0, 15.0)
    assert abs(cuts[0][1] - 30.0) < 1e-9                       # 0.5 * max(30, 11) + 15
    # the model's water stands 1 m over the road surface only in the cut's cells
    w = _wse(ROAD_M + 1.0, lambda xy: np.abs(xy[:, 0] - 100.0) <= 15.0)
    got = rp.road_crown_depth(_line_x(), w, _ground(), cuts=cuts)
    assert got["depth_ft"] == 0.0 and got["n_cut"] > 0, got
    assert "structure cuts" in got["how"]
    raw = rp.road_crown_depth(_line_x(), w, _ground(), cuts=())
    assert abs(raw["depth_ft"] - 1.0 / FT) < 0.01, raw          # without the mask: 3.28 ft


@test
def road_crown_depth_reads_water_surface_minus_road_surface():
    from nd_breach_runner.core import road_profile as rp
    # below the road everywhere: not overtopped, freeboard reported
    got = rp.road_crown_depth(_line_x(), _wse(ROAD_M - 2.0), _ground())
    assert got["depth_ft"] == 0.0 and abs(got["freeboard_ft"] - 2.0 / FT) < 0.01, got
    # real overtopping is kept: 0.5 m over the crown -> 1.64 ft
    got = rp.road_crown_depth(_line_x(), _wse(ROAD_M + 0.5), _ground())
    assert abs(got["depth_ft"] - 0.5 / FT) < 0.01 and got["x"] is not None, got
    # an overtopping next to (not in) a cut corridor still counts
    cuts = rp.cut_corridors([{"line": [[20.0, -5.0], [20.0, 5.0]], "width_m": 2.0}], 1.0, 2.0)
    got = rp.road_crown_depth(_line_x(), _wse(ROAD_M + 0.5,
                              lambda xy: xy[:, 0] > 150.0), _ground(), cuts=cuts)
    assert abs(got["depth_ft"] - 0.5 / FT) < 0.01 and got["x"] > 150.0, got


@test
def road_crown_depth_leaves_out_a_bare_earth_opening_under_a_bridge():
    from nd_breach_runner.core import road_profile as rp
    w = _wse(ROAD_M - 3.0, lambda xy: np.abs(xy[:, 0] - 100.0) <= 10.0)
    got = rp.road_crown_depth(_line_x(), w, _ground(notch_x=100.0))
    assert got["depth_ft"] == 0.0 and got["n_opening"] > 0, got
    raw = rp.road_crown_depth(_line_x(), w, _ground(notch_x=100.0), mask_openings=False)
    assert abs(raw["depth_ft"] - 2.0 / FT) < 0.01, raw


@test
def road_crown_depth_without_a_road_surface_falls_back_to_the_raster():
    from nd_breach_runner.core import road_profile as rp
    got = rp.road_crown_depth(_line_x(), _wse(ROAD_M + 1.0),
                              lambda xy: np.full(len(xy), np.nan))
    assert got is None


def _tiny_dem(path, value=ROAD_M):
    from osgeo import gdal, osr
    ds = gdal.GetDriverByName("GTiff").Create(path, 80, 80, 1, gdal.GDT_Float32)
    assert ds is not None, path
    ds.SetGeoTransform((0.0, 1.0, 0.0, 80.0, 0.0, -1.0))
    sr = osr.SpatialReference()
    sr.ImportFromEPSG(26914)
    ds.SetProjection(sr.ExportToWkt())
    ds.GetRasterBand(1).WriteArray(np.full((80, 80), value, dtype=np.float32))
    ds.GetRasterBand(1).SetNoDataValue(-9999.0)
    ds = None


@test
def ndhazard_tool_road_overtopping_on_the_centreline_not_in_the_cut_trench():
    try:
        from osgeo import gdal  # noqa: F401
    except ImportError:
        print("      (skipped: no GDAL)")
        return
    from nd_breach_runner.core import workflow
    from nd_breach_runner.tests import test_ndhazard_tool as tt
    an, fq, _ = tt._load_tool()
    h = tt._Harness()
    tmp = tempfile.mkdtemp()
    try:
        dem = os.path.join(tmp, "dem_1m.tif")
        _tiny_dem(dem)
        bm = os.path.join(h.folder, "_breach_model")
        os.makedirs(bm)
        json.dump({"settings": {"input_files": {"DEM": dem}, "cell_size_m": 10.0},
                   "dem_z_to_m": 1.0}, open(os.path.join(bm, "run_report.json"), "w"))
        # a bridge cut across Hwy 1 (y 55) at x 25: the trench is Hwy 1's only wet cell
        json.dump([{"kind": "bridge", "line": [[25.0, 50.0], [25.0, 60.0]], "width_m": 2.0,
                    "barrels": 1, "name": "NBI 1"}], open(os.path.join(bm, "structures.json"), "w"))
        d = h.arrays[os.path.join(h.folder, workflow.FINAL_NAMES["depth"])]
        wse = np.where(d > 0, ROAD_M / FT + np.nan_to_num(d), np.nan)
        wp = os.path.join(h.folder, workflow.FINAL_NAMES["wse"])
        open(wp, "w").write("x")
        h.arrays[wp] = wse
        s, r, t, l, dm = tt._layers(fq)
        base = {"DAM_FOLDER": h.folder, "STRUCTURES": s, "ROADS": r, "RAIL": t,
                "LIFELINES": l, "DOWNSTREAM_DAMS": dm, "CHART_FILE": h.chart_file(),
                "ROAD_CUT_BUFFER_FT": 0.0}
        res, ctx, fb = tt._run(an, fq, base)
        js = json.load(open(res["JSON"]))
        rd = js["road_check"]["road_depth"] if "road_check" in js else js["road_depth"]
        assert rd["basis"] == "centreline" and rd["structure_cuts"] == 1, rd
        hwy = [f for f in js["findings"] if f["kind"] == "road" and f["id"] == "Hwy 1"]
        assert not hwy or hwy[0]["depth_ft"] == 0.0, hwy          # was 0.5 ft in the trench
        i94 = tt._find(js, "road", "I- 94")                       # real overtopping stays
        assert abs(i94["depth_ft"] - 0.2) < 0.02, i94
        md = open(res["REPORT"], encoding="utf-8").read()
        assert "Road overtopping depth" in md and "centreline" in md
        # the old basis on request
        res, ctx, fb = tt._run(an, fq, dict(base, ROAD_DEPTH_BASIS=1))
        js = json.load(open(res["JSON"]))
        assert tt._find(js, "road", "Hwy 1")["depth_ft"] == 0.5
        # no WSE raster: the depth raster, with a note
        os.remove(wp)
        res, ctx, fb = tt._run(an, fq, base)
        js = json.load(open(res["JSON"]))
        assert tt._find(js, "road", "Hwy 1")["depth_ft"] == 0.5
    finally:
        h.close()
        shutil.rmtree(tmp, ignore_errors=True)
