"""v3.10.22: the HEC-RAS dam line along the embankment crest (core/hecras_crest.py,
build_model option dam_axis='crest', the default): the crest traced from the DEM on synthetic
straight and bent embankments and on Absaraka (DWR-2801), the fallback to the straight line
when there is no crest, the breach pick and override, and the polyline breach frame."""
import os
import shutil
import tempfile

import numpy as np

from nd_breach_runner.core import hecras_crest as hc
from nd_breach_runner.tests.registry import test

HERE = os.path.dirname(os.path.abspath(__file__))


def _grid_sampler(z, x0, y0, c):
    """Bilinear sampler of a north-up grid (row 0 at y0, top edge), cell centres at +c/2."""
    z = np.asarray(z, float)

    def f(xy):
        xy = np.atleast_2d(np.asarray(xy, float))
        fx = (xy[:, 0] - x0) / c - 0.5
        fy = (y0 - xy[:, 1]) / c - 0.5
        i0 = np.clip(np.floor(fy).astype(int), 0, z.shape[0] - 2)
        j0 = np.clip(np.floor(fx).astype(int), 0, z.shape[1] - 2)
        ty = np.clip(fy - i0, 0.0, 1.0)
        tx = np.clip(fx - j0, 0.0, 1.0)
        v = (z[i0, j0] * (1 - tx) * (1 - ty) + z[i0, j0 + 1] * tx * (1 - ty) +
             z[i0 + 1, j0] * (1 - tx) * ty + z[i0 + 1, j0 + 1] * tx * ty)
        out = (fx < -0.5) | (fy < -0.5) | (fx > z.shape[1] - 0.5) | (fy > z.shape[0] - 0.5)
        v[out] = np.nan
        return v
    return f


def _seg_dist(X, Y, line):
    line = np.asarray(line, float)
    d = np.full(X.shape, np.inf)
    for a, b in zip(line[:-1], line[1:]):
        ab = b - a
        t = np.clip(((X - a[0]) * ab[0] + (Y - a[1]) * ab[1]) / (ab @ ab), 0.0, 1.0)
        d = np.minimum(d, np.hypot(X - a[0] - t * ab[0], Y - a[1] - t * ab[1]))
    return d


TOP, H, CW = 130.0, 30.0, 20.0           # ft: top of dam, height, crest width (3:1 slopes)


def _valley_dem(crest, c=10.0, half=900.0, embankment=True):
    """A V-shaped valley across x (floor 100 ft at x = 0, the top of dam reached at
    |x| ~ 495 ft, falling 0.5 % to the south = downstream) with an embankment along `crest`
    (3:1 slopes, 20 ft crest at TOP). Returns (z, x0, y0, c)."""
    xs = np.arange(-half, half, c) + 0.5 * c
    ys = np.arange(half, -half, -c) - 0.5 * c
    X, Y = np.meshgrid(xs, ys)
    g = 100.0 + 40.0 * (np.abs(X) / 600.0) ** 1.5 + 0.005 * Y
    z = g
    if embankment:
        dd = _seg_dist(X, Y, crest)
        emb = TOP - np.maximum(dd - 0.5 * CW, 0.0) / 3.0
        z = np.maximum(g, emb)
    return z, -half, half, c


def _trace(z, x0, y0, c, P, axis, cell=40.0, max_len=1500.0):
    f = _grid_sampler(z, x0, y0, c)
    toe = 3.0 * H + 0.5 * CW
    tr = hc.trace(f, P, axis, TOP, H, c, toe, max_len, max(2 * cell, 300.0))
    return f, tr, hc.tidy(tr["line"], c, cell, keep_len=max_len, centre=tr["p0"])


@test
def hecras_crest_traces_a_straight_embankment():
    crest = [(-700.0, 0.0), (700.0, 0.0)]
    z, x0, y0, c = _valley_dem(crest)
    # the dam point 15 ft off the crest, the axis guess 20 degrees off
    a = np.radians(20.0)
    f, tr, line = _trace(z, x0, y0, c, (30.0, 15.0), (-np.cos(a), -np.sin(a)))
    assert tr["snap"] < 25.0 and abs(tr["p0"][1]) < 6.0, tr["p0"]
    assert np.abs(line[:, 1]).max() < 8.0, line
    L = hc.cumlen(line)[-1]
    # the embankment stands above the valley where the ground is below the top: |x| < ~495
    assert 850.0 < L < 1100.0, L
    for k in ("A", "B"):
        assert tr["ends"][k][0] != "the dam length", tr["ends"]
    # A -> B runs along the axis (westwards; downstream, to the left of it, is south)
    assert line[0][0] > line[-1][0]
    assert hc.angle_deg(hc.tangent_at(line, 0.5 * L), (1.0, 0.0)) < 3.0
    assert np.abs(line[:, 1]).max() < 2.0 and len(line) <= 4, line   # straight: few vertices


@test
def hecras_crest_follows_a_bent_embankment():
    # 30 degree bend at the valley floor: the west arm along x, the east arm turned north
    b = np.radians(30.0)
    crest = [(-800.0, 0.0), (0.0, 0.0), (800.0 * np.cos(b), 800.0 * np.sin(b))]
    z, x0, y0, c = _valley_dem(crest)
    f, tr, line = _trace(z, x0, y0, c, (-200.0, 10.0), (-1.0, 0.0))
    d = _seg_dist(line[:, 0], line[:, 1], crest)
    assert d.max() < 12.0, (d.max(), line)
    # both arms are followed, through the bend
    assert line[:, 0].min() < -350.0 and line[:, 0].max() > 300.0, line
    assert line[:, 1].max() > 150.0, line
    s_w, s_e = hc.station_of(line, (-300.0, 0.0)), hc.station_of(line, (350.0, 202.0))
    assert hc.angle_deg(hc.tangent_at(line, s_w), (-1.0, 0.0)) < 5.0
    assert abs(hc.angle_deg(hc.tangent_at(line, s_e), (-1.0, 0.0)) - 30.0) < 6.0
    # a straight span across a breach on the east arm, the frame at right angles to the crest
    s = hc.station_of(line, (300.0, 173.0))
    l2 = hc.straighten(line, s - 60.0, s + 60.0)
    O, along, down = hc.frame_at(l2, hc.station_of(l2, hc.point_at(line, s)))
    assert abs(along @ down) < 1e-9 and abs(hc.angle_deg(along, (-np.cos(b), -np.sin(b)))) < 6.0
    # downstream is to the south-east, at right angles to the east arm (left of A -> B)
    assert abs(hc.angle_deg(down, (np.sin(b), -np.cos(b)))) < 6.0, down
    assert hc.monotonic(l2, (-1.0, 0.0))


@test
def hecras_crest_raises_without_an_embankment():
    z, x0, y0, c = _valley_dem([(-700.0, 0.0), (700.0, 0.0)], embankment=False)
    try:
        _trace(z, x0, y0, c, (0.0, 0.0), (-1.0, 0.0))
    except hc.CrestTraceError as e:
        assert "crest" in str(e), e
    else:
        raise AssertionError("no CrestTraceError on a valley without a dam")
    # flat ground at the top of dam: no prominence
    zf = np.full((60, 60), TOP)
    try:
        hc.trace(_grid_sampler(zf, 0.0, 600.0, 10.0), (300.0, 300.0), (1.0, 0.0), TOP, H, 10.0,
                 100.0, 1000.0, 300.0)
    except hc.CrestTraceError:
        pass
    else:
        raise AssertionError("no CrestTraceError on flat ground")


@test
def hecras_crest_breach_pick_and_line_helpers():
    crest = [(-700.0, 0.0), (700.0, 0.0)]
    z, x0, y0, c = _valley_dem(crest)
    f, tr, line = _trace(z, x0, y0, c, (0.0, 0.0), (-1.0, 0.0))
    toe = 3.0 * H + 0.5 * CW
    sc, how, det = hc.pick_breach(line, f, toe, 40.0, 25.0, 60.0, TOP, 3.0, "auto", c)
    # the deepest section is the valley floor, x = 0
    assert abs(hc.point_at(line, sc)[0]) < 30.0, (sc, hc.point_at(line, sc))
    assert "deepest" in how and det["toe_ground"] < 103.0, (how, det)
    s2, how2, _ = hc.pick_breach(line, f, toe, 40.0, 25.0, 60.0, TOP, 3.0, "lowest_crest", c)
    assert "lowest crest" in how2 and 0.0 <= s2 <= hc.cumlen(line)[-1]
    # helpers
    pl = np.array([(0.0, 0.0), (100.0, 0.0), (200.0, 100.0)])
    L = hc.cumlen(pl)[-1]
    assert abs(L - (100.0 + 100.0 * np.sqrt(2))) < 1e-9
    assert np.allclose(hc.point_at(pl, 50.0), (50.0, 0.0))
    st = hc.straighten(pl, 80.0, 140.0)
    q0, q1 = hc.point_at(pl, 80.0), hc.point_at(pl, 140.0)
    i = int(np.argmin(np.hypot(*(st - q0).T)))
    assert np.allclose(st[i], q0) and np.allclose(st[i + 1], q1), st
    assert hc.monotonic(pl, (1.0, 0.0))
    assert not hc.monotonic(np.array([(0.0, 0.0), (100.0, 0.0), (50.0, 50.0)]), (1.0, 0.0))
    ext = hc.extend(pl, 500.0, (1.0, 0.0))
    assert hc.monotonic(ext, (1.0, 0.0)) and hc.cumlen(ext)[-1] > L + 900.0
    assert abs(hc.bearing_deg((0.0, 1.0))) < 1e-9 and abs(hc.bearing_deg((1.0, 0.0)) - 90.0) < 1e-9


def _absaraka():
    d = np.load(os.path.join(HERE, "data", "abs2801_crest_dem.npz"))
    return d["z"] * float(d["scale"]), float(d["x0"]), float(d["y0"]), float(d["cell"])


@test
def hecras_crest_absaraka_bend():
    """DWR-2801: the straight line (bearing 191, across the flow to the inflow point) runs
    about 27 degrees off the crest at the breach; the trace follows the bent embankment."""
    z, x0, y0, c = _absaraka()
    f = _grid_sampler(z, x0, y0, c)
    P = (2744159.0, 495413.2)
    d = np.array([2744423.5 - P[0], 495361.6 - P[1]])
    d /= np.hypot(*d)                               # downstream, bearing ~101
    axis = np.array([d[1], -d[0]])                  # A -> B: the right of downstream
    assert abs(hc.bearing_deg(axis) - 191.0) < 1.0
    top, Hd, cell = 1069.117, 27.5, 100.0
    toe = 3.0 * Hd + 10.0
    tr = hc.trace(f, P, axis, top, Hd, c, toe, 1700.0, max(2 * cell, 300.0))
    line = hc.tidy(tr["line"], c, cell, keep_len=1700.0, centre=tr["p0"])
    L = hc.cumlen(line)[-1]
    assert 1000.0 < L < 1300.0, L
    assert "abutment" in tr["ends"]["A"][0], tr["ends"]
    # north arm runs about N-S, the SW arm about NE-SW: a bend of 30+ degrees
    t0, t1 = hc.tangent_at(line, 0.15 * L), hc.tangent_at(line, 0.85 * L)
    assert hc.angle_deg(t0, t1) > 25.0, (t0, t1)
    pb = breach_half = 40.4
    sc, how, det = hc.pick_breach(line, f, toe, cell, breach_half, breach_half + 0.7 * 25.5,
                                  top, 3.0, "auto", c)
    bxy = hc.point_at(line, sc)
    # v2e: (2744349.6, 495646.7), the deepest toe section on the NE arm near the bend
    assert np.hypot(*(bxy - (2744349.6, 495646.7))) < 80.0, (bxy, how, det)
    skew = hc.angle_deg(hc.tangent_at(line, sc), axis)
    assert 18.0 < skew < 40.0, skew
    assert hc.monotonic(line, axis)
    assert pb == 40.4


@test
def hecras_crest_build_model_options_and_fallback():
    """build_model: dam_axis crest (default) records the trace in run_report.json; a bad
    dam_axis is refused; a trace that fails falls back to the straight line with a WARNING; a
    user's dam line wins; breach_station_ft overrides the pick; straight is the old line."""
    import json
    from nd_breach_runner.core import hecras_model as hm
    from nd_breach_runner.tests import test_hecras_p0 as p0
    if p0._skip():
        return
    d = tempfile.mkdtemp()
    try:
        p0._run(d)
        r = hm.build_model(d, out_folder=os.path.join(d, "crest"))
        ax = r["info"]["dam_axis"]
        assert ax["used"] == "crest" and ax["source"] == "traced from the DEM", ax
        for k in ("line", "skew_deg", "breach_station", "breach_how", "breach_method",
                  "final_breach_station", "connection_length"):
            assert k in ax, (k, ax)
        rep = json.load(open(os.path.join(d, "_breach_model", "run_report.json")))
        assert rep["hecras_dam_line"]["used"] == "crest", rep.get("hecras_dam_line")
        cl = np.asarray(r["model"].conn_line, float)
        assert abs(float(np.hypot(*np.diff(cl, axis=0).T).sum()) - r["info"]["dam_line"]) < 1.0
        try:
            hm.build_model(d, out_folder=os.path.join(d, "bad"), options={"dam_axis": "curvy"})
        except hm.HecRasModelError as e:
            assert "dam_axis" in str(e)
        else:
            raise AssertionError("dam_axis='curvy' accepted")
        # fallback
        old = hc.trace

        def boom(*a, **k):
            raise hc.CrestTraceError("test: no crest")
        hc.trace = boom
        try:
            rf = hm.build_model(d, out_folder=os.path.join(d, "fb"))
        finally:
            hc.trace = old
        assert rf["info"]["dam_axis"]["used"] == "straight" and rf["info"]["dam_axis"]["fallback"]
        assert any(n.startswith("WARNING: the dam line could not be traced") and "test: no crest"
                   in n for n in rf["notes"]), rf["notes"]
        cf = np.asarray(rf["model"].conn_line, float)
        u = (cf[-1] - cf[0]) / np.hypot(*(cf[-1] - cf[0]))
        assert np.abs((cf[:, 0] - cf[0, 0]) * u[1] - (cf[:, 1] - cf[0, 1]) * u[0]).max() < 1e-3      # one straight line
        # a user's dam line wins (here: the traced one, shifted 3 ft along itself, given back)
        assert ax["units"] == "ft", ax                 # the synthetic model is in EPSG:2266 ft
        want = ax["final_breach_station"] + 40.0
        rs = hm.build_model(d, out_folder=os.path.join(d, "user"),
                            options={"dam_line_xy": ax["line"], "breach_station_ft": want})
        au = rs["info"]["dam_axis"]
        assert au["source"] == "your dam line" and au["breach_method"] == "station", au
        assert abs(au["breach_station"] - want) < 1.0, (au["breach_station"], want)
        assert abs(rs["model"].breach["center_station"] - want) < 0.5 * rs["model"].cell, \
            (rs["model"].breach["center_station"], want)
        # straight: no trace
        r2 = hm.build_model(d, out_folder=os.path.join(d, "straight"),
                            options={"dam_axis": "straight"})
        assert r2["info"]["dam_axis"]["used"] == "straight" and \
            "fallback" not in r2["info"]["dam_axis"]
    finally:
        shutil.rmtree(d)


@test
def hecras_crest_breaklines_and_seeds_follow_a_bent_dam_line():
    """The dam row runs half a spacing inside the bent dam line along its whole length, the
    breach-side lines are at right angles to the crest at the breach (not to the old
    straight line), and the gmsh dam refinement line follows the polyline."""
    try:
        import scipy  # noqa: F401
    except ImportError:
        print("  (skipped: no scipy)")
        return
    from nd_breach_runner.core import hecras_breaklines as hbl
    from nd_breach_runner.core import hecras_seeds as hs
    dam = [(3000.0, 3600.0), (1500.0, 4000.0), (0.0, 3600.0)]   # A (east) .. B, bent 30 deg
    per = [(0.0, 0.0), (3000.0, 0.0), (3000.0, 3600.0), (1500.0, 4000.0), (0.0, 3600.0)]
    xs = np.arange(50.0, 3000.0, 100.0)
    ys = np.arange(50.0, 4000.0, 100.0)
    X, Y = np.meshgrid(xs, ys)
    pts = np.column_stack([X.ravel(), Y.ravel()])
    keep = hbl.poly_dist(pts[:, 0], pts[:, 1], dam) > 1.0
    up = pts[:, 1] > 3600.0 + (400.0 / 1500.0) * (1500.0 - np.abs(pts[:, 0] - 1500.0))
    pts = pts[keep & ~up]
    bxy = np.array([2250.0, 3800.0])
    along = np.array([-1500.0, 400.0]) / np.hypot(1500.0, 400.0)    # A -> B on the east arm
    down = np.array([-along[1], along[0]])                  # left of A -> B: downstream
    assert down[1] < 0
    plan = hbl.plan(per, 100.0, dam, down, breach=(tuple(bxy), 40.0, 250.0))
    out, st = hbl.enforce(pts, per, plan)
    names = [b["name"] for b in plan["lines"]]
    assert names[:2] == ["Breach left", "Breach right"], names
    for b in plan["lines"][:2]:
        ln = np.asarray(b["line"], float)
        v = (ln[-1] - ln[0]) / np.hypot(*(ln[-1] - ln[0]))
        assert abs(v @ along) < 0.02, (b["name"], v, along)      # across the crest
    # the dam row: half the dam spacing inside the polyline on both arms
    dd = hbl.poly_dist(out[:, 0], out[:, 1], dam)
    row = out[(dd > 5.0) & (dd < 35.0)]
    assert (row[:, 0] < 1400.0).sum() >= 10 and (row[:, 0] > 1600.0).sum() >= 10, len(row)
    rd = hbl.poly_dist(row[:, 0], row[:, 1], dam)
    assert np.median(rd) > 15.0 and np.median(rd) < 30.0, np.median(rd)
    # gmsh: the dam refinement line follows the polyline, two line steps inside it
    spec, sizes, _ = hs.spec_for(per, 100.0, dam_line=dam, downstream=down, breach_xy=bxy,
                                 breach_dir=down)
    ln = np.asarray(spec["lines"][0], float)
    assert len(ln) >= 3, ln
    off = hbl.poly_dist(ln[:, 0], ln[:, 1], dam)
    h = min(sizes["dam_spacing"], sizes["breach_spacing"])
    assert np.allclose(off[1:-1], 2.0 * h, atol=0.3 * h), (off, h)
    bl = np.asarray(spec["lines"][1], float)
    v = (bl[-1] - bl[0]) / np.hypot(*(bl[-1] - bl[0]))
    assert v @ down > 0.999


@test
def hecras_crest_runner_checkbox_is_on_by_default():
    """The Runner's Advanced 'HEC-RAS dam line follows the crest (recommended)': on by default
    (no parameter filled, the run settings say crest, checkpoints unchanged); off fills
    HECRAS_CREST=False and the HEC-RAS stage asks build_model for dam_axis='straight'."""
    from nd_breach_runner import popup_logic as pl
    from nd_breach_runner.core import workflow
    from nd_breach_runner.core import hecras_engine as he
    from nd_breach_runner.core import hecras_model as hm
    assert pl.DEFAULT_SETTINGS["hecras_dam_axis"] == "crest"
    assert hm.DEFAULTS["dam_axis"] == "crest"
    assert "HECRAS_CREST" not in pl.defaults("rundam", "/d", "X", {})
    assert pl.defaults("rundam", "/d", "X", {"hecras_dam_axis": "straight"})["HECRAS_CREST"] is False
    tip = pl.HECRAS_CREST_TIP
    assert "On by default" in tip and "straight line" in tip and "log says so" in tip
    assert "..." not in tip
    st = workflow.RunSettings()
    assert st.hecras_dam_axis == "crest" and "hecras_dam_axis" not in st.as_dict()
    off = workflow.RunSettings(hecras_dam_axis="straight")
    assert off.as_dict()["hecras_dam_axis"] == "straight"
    assert workflow.key_settings(off) == workflow.key_settings(st)   # the flood is the same
    src = open(he.__file__, encoding="utf-8").read()
    assert 'getattr(st, "hecras_dam_axis", None) == "straight"' in src
    assert hm.dam_axis_of({}) == "crest" and hm.dam_axis_of({"dam_axis": "Straight"}) == "straight"
