"""v3.10.17 (HEC-RAS site checks): build_model stops on a dam whose data do not fit the ground
(the DWR-3161 HEC-RAS test: 36 % volume error), raises a breach bottom that lies below the
ground it flows onto and cuts the breach opening into the terrain, keeps the dam line on the
embankment crest when no length is on record, derives the simulation window from the
hydrograph and the arrival, uses HEC's adaptive time step, says that HEC-RAS's piping breach
is not the plugin's, and reads a dam connection's time series from the right output block.

Synthetic data only (test_hecras_p0's valley), GDAL from the QGIS Python; runs in the Runner
suite (run_tests.py) and under pytest."""
import json
import os
import shutil
import tempfile

import numpy as np

from nd_breach_runner.core import hecras
from nd_breach_runner.core import hecras_model as hm
from nd_breach_runner.tests.registry import test
from nd_breach_runner.tests import test_hecras_p0 as p0

FT = 0.3048


def _build(folder, **kw):
    return hm.build_model(folder, out_folder=os.path.join(folder, "model"), **kw)


# ============================================================================ pure checks
@test
def hecras_site_breach_check_dwr3161_and_small_mismatch():
    # DWR-3161: breach bottom 1816.34 ft, ground below the dam 1840.45, top 1844.44, H 28.1
    probs, notes = hm.breach_site_check(1816.34, 1840.45, 1844.44, 28.1)
    assert len(probs) == 2, probs
    assert "24.1 ft below the ground" in probs[0] and "14 % of the 28.1 ft" in probs[1]
    # a few feet of mismatch is not a stop (build_model raises the bottom instead)
    probs, notes = hm.breach_site_check(1041.62, 1044.4, 1069.12, 27.5)
    assert probs == [] and notes == [], (probs, notes)
    # 60 % of H between top and toe: a note, not a stop
    probs, notes = hm.breach_site_check(100.0, 104.0, 110.0, 10.0)
    assert probs == [] and len(notes) == 1 and "60 %" in notes[0]
    # unknown ground: never a stop
    probs, notes = hm.breach_site_check(100.0, None, 110.0, 10.0)
    assert probs == [] and "could not be read" in notes[0]


@test
def hecras_site_storage_check_dwr3161_absaraka_and_wet_pool():
    probs, notes, st = hm.storage_site_check(1816.34, 201.0, 1840.43, 29.8, 1844.44, 28.1)
    assert len(probs) == 2, probs
    assert "never lower than 1840.4 ft" in probs[0] and "29.8 ac" in probs[1]
    assert st["depth_frac"] < 0.25 and st["area_ratio"] < 0.2
    # Absaraka (DWR-2801): LiDAR sees the 1051 ft normal pool; 189 ac outline vs 111.6 ac
    probs, notes, st = hm.storage_site_check(1041.62, 111.6, 1051.39, 189.4, 1069.12, 27.5)
    assert probs == [] and notes == [], (probs, notes)
    # a full reservoir whose LiDAR surface is 60 % of the way up the dam: a note only
    probs, notes, st = hm.storage_site_check(100.0, 50.0, 106.0, 55.0, 110.0, 10.0)
    assert probs == [] and len(notes) == 1 and "water" in notes[0]
    # an outline far larger than the curve's area: a note only
    probs, notes, st = hm.storage_site_check(100.0, 50.0, 101.0, 400.0, 110.0, 10.0)
    assert probs == [] and len(notes) == 1 and "400.0 ac" in notes[0]
    # no reservoir in the DEM: a note, not a stop
    probs, notes, st = hm.storage_site_check(100.0, 50.0, None, None, 110.0, 10.0)
    assert probs == [] and "not checked" in notes[0]


@test
def hecras_site_gate_and_override():
    assert hm.allow_bad_site({}, env={}) is False
    assert hm.allow_bad_site({"allow_bad_site": True}, env={}) is True
    assert hm.allow_bad_site({}, env={"ALLOW_BAD_SITE": "1"}) is True
    assert hm.allow_bad_site({}, env={"ALLOW_BAD_SITE": "0"}) is False
    notes = []
    hm.site_gate([], False, notes)
    assert notes == []
    try:
        hm.site_gate(["The dam is in a lake."], False, notes)
        assert False, "expected HecRasSiteError"
    except hm.HecRasSiteError as e:
        assert e.problems == ["The dam is in a lake."]
        assert "(1) The dam is in a lake." in str(e) and "ALLOW_BAD_SITE=1" in str(e)
        assert isinstance(e, hm.HecRasModelError)
    hm.site_gate(["The dam is in a lake."], True, notes)
    assert len(notes) == 1 and notes[0].startswith("SITE CHECK OVERRIDDEN")


@test
def hecras_site_crest_segment_skips_hills_and_steps_over_a_spillway():
    st = np.arange(0.0, 5001.0, 10.0)
    top = 1844.0
    z = np.full(len(st), top + 0.5)                     # crest 1500..3000
    z[st < 1500] = 1900.0 + (1500 - st[st < 1500]) * 0.1   # a hill on the left
    z[st > 3000] = 1840.0 - 5.0                         # low ground on the right
    z[(st >= 2200) & (st <= 2320)] = top - 4.0          # a 120 ft spillway, 4 ft lower
    lo, hi, ok = hm.crest_segment(st, z, 2000.0, top, 3.0, gap=300.0)
    assert ok and abs(lo - 1490.0) < 1e-9 and abs(hi - 3010.0) < 1e-9, (lo, hi)
    # a gap longer than allowed ends the crest
    lo2, hi2, ok2 = hm.crest_segment(st, z, 2000.0, top, 3.0, gap=50.0)
    assert ok2 and hi2 <= 2210.0, hi2
    # a dam point on the hill but within `gap` of the crest starts on the crest
    lo3, hi3, ok3 = hm.crest_segment(st, z, 1400.0, top, 3.0, gap=300.0)
    assert ok3 and abs(lo3 - 1490.0) < 1e-9
    # no crest near the dam point
    lo4, hi4, ok4 = hm.crest_segment(st, z, 500.0, top, 3.0, gap=300.0)
    assert not ok4 and lo4 == hi4 == 500.0
    # cap and minimum length
    A, B = np.array([0.0, 0.0]), np.array([5000.0, 0.0])
    a, b, L = hm.dam_line_part(A, B, 1490.0, 3010.0, 2000.0, 1000.0)
    assert abs(L - 1000.0) < 1e-9 and a[0] >= 1490.0 - 1e-9 and b[0] <= 3010.0 + 1e-9
    a, b, L = hm.dam_line_part(A, B, 500.0, 500.0, 500.0, 3000.0, min_length=300.0)
    assert abs(L - 300.0) < 1e-9 and abs(a[0] - 350.0) < 1e-9


@test
def hecras_site_simulation_window_rule():
    t = np.arange(0.0, 20 * 3600.0 + 1, 600.0)
    q = 1000.0 * np.exp(-((t / 3600.0 - 1.0) ** 2) / 0.5)
    q[t > 6 * 3600] = 0.0
    te = hm.hydrograph_end_hr(t, q)
    assert 2.0 < te < 3.0, te
    assert abs(hm.hydrograph_end_hr(t, q, t_ref_s=1800.0) - (te - 0.5)) < 1e-9
    # DWR-3161: hydrograph ends ~3 h, arrival 21.9 h -> 21.9 + 5.5 = 27.4 -> 28 h (was 32 h)
    sim, how = hm.simulation_hours(3.0, 21.9, 1.23)
    assert sim == 28.0 and "21.9 h" in how, (sim, how)
    # short: at least 6 h; at least 2 h to drain
    assert hm.simulation_hours(1.0, 0.5)[0] == 6.0
    assert hm.simulation_hours(8.0, 0.0)[0] == 10.0
    # capped, and says so
    sim, how = hm.simulation_hours(3.0, 60.0)
    assert sim == hm.SIM_CAP_HR and "capped" in how
    sim, how = hm.simulation_hours(3.0, 60.0, cap=96.0)
    assert sim == 75.0


# ============================================================================ build_model
@test
def hecras_site_build_stops_on_a_dam_that_is_not_there():
    """The DEM has a 10 m dam; the run says 30 m. The breach bottom is 20 m below the ground
    downstream and the top of dam only a third of the height above it: HecRasSiteError, no
    model; with the override the model is built and says the results mean nothing."""
    if p0._skip():
        return
    d = tempfile.mkdtemp()
    try:
        p0._run(d, dam=p0._dam(dam_height_ft=30.0 / FT))
        try:
            _build(d)
            assert False, "expected HecRasSiteError"
        except hm.HecRasSiteError as e:
            txt = str(e)
            assert "below the ground just downstream" in txt and "no embankment" in txt, txt
        assert not os.path.exists(os.path.join(d, "model", "SYN_1.p01"))
        r = _build(d, options={"allow_bad_site": True})
        assert sum(n.startswith("SITE CHECK OVERRIDDEN") for n in r["notes"]) >= 2
        site = r["info"]["site"]
        assert site["overridden"] and site["breach_bottom_raised_ft"] > 20.0
        readme = open(r["paths"]["readme"], encoding="utf-8").read()
        assert "FAILED, overridden" in readme
        old = os.environ.get(hm.ENV_ALLOW_BAD_SITE)
        os.environ[hm.ENV_ALLOW_BAD_SITE] = "1"
        try:
            r2 = hm.build_model(d, out_folder=os.path.join(d, "m2"))
            assert r2["info"]["site"]["overridden"]
        finally:
            if old is None:
                os.environ.pop(hm.ENV_ALLOW_BAD_SITE, None)
            else:
                os.environ[hm.ENV_ALLOW_BAD_SITE] = old
    finally:
        shutil.rmtree(d)


@test
def hecras_site_breach_bottom_raised_to_the_toe_and_cut_into_the_terrain():
    """A breach bottom 1 m below the ground just below the dam: not a stop; the bottom is
    raised to that ground (with a note giving the storage that stays behind) and the breach
    opening through the downstream half of the embankment is cut to it in the terrain."""
    if p0._skip():
        return
    from osgeo import gdal
    d = tempfile.mkdtemp()
    try:
        p0._run(d, dam=p0._dam(dam_height_ft=11.0 / FT))
        r = _build(d)
        m, info = r["model"], r["info"]
        site = info["site"]
        assert not site["problems"], site
        assert 0.5 < site["breach_bottom_raised_ft"] / 3.28084 < 1.5, site
        assert abs(m.breach["bottom_elev"] - site["toe_ground_ft"]) < 0.02, (m.breach, site)
        assert any(n.startswith("Breach bottom raised") and "acre-ft" in n for n in r["notes"])
        cut = site["breach_cut"]
        assert cut["cells"] > 0 and cut["max_cut"] > 10.0, cut
        assert any(n.startswith("Terrain: the breach opening") for n in r["notes"])
        # the terrain just downstream of the dam at the breach centre is at the breach bottom
        t = gdal.Open(os.path.join(d, "model", "Terrain", m.terrain_name + ".tif"))
        gt = t.GetGeoTransform()
        a = t.GetRasterBand(1).ReadAsArray()
        A, B = np.asarray(m.conn_line[0]), np.asarray(m.conn_line[-1])
        ab = (B - A) / np.hypot(*(B - A))
        dn = np.array([ab[1], -ab[0]])
        # downstream is south (decreasing y) in the synthetic valley
        dn = dn if dn[1] < 0 else -dn
        p = A + ab * m.breach["center_station"] + dn * 0.5 * m.cell
        c, rr = int((p[0] - gt[0]) / gt[1]), int((p[1] - gt[3]) / gt[5])
        assert a[rr, c] <= m.breach["bottom_elev"] + 0.01, (a[rr, c], m.breach["bottom_elev"])
        # the weir (the dam itself) is untouched: still at the top of dam or above
        assert min(e for _, e in m.weir) >= p0.TOP_M / FT - 0.01
        # breach_cut off: no cut
        r2 = hm.build_model(d, out_folder=os.path.join(d, "m2"), options={"breach_cut": False})
        assert r2["info"]["site"]["breach_cut"] is None
    finally:
        shutil.rmtree(d)


@test
def hecras_site_dam_line_without_a_length_stays_on_the_crest():
    """No dam length on record: the connection is the embankment crest (ground within
    max(3 ft, 10 % H) of the top of dam) instead of the whole edge across the valley."""
    if p0._skip():
        return
    d = tempfile.mkdtemp()
    try:
        p0._run(d)
        r = _build(d)
        m, info = r["model"], r["info"]
        assert "embankment crest" in info["dam_line_how"], info["dam_line_how"]
        # the embankment is 500 m long; the valley walls reach the top of dam at |x| = 500 m
        L = info["dam_line"] * FT
        assert 400.0 < L < 1300.0, L
        tol_ft = max(3.0, 0.1 * p0.H_M / FT)
        assert max(e for _, e in m.weir) <= p0.TOP_M / FT + tol_ft + 3.0, max(e for _, e in m.weir)
        assert any("no dam length on record" in n for n in r["notes"])
        # a cap
        r2 = hm.build_model(d, out_folder=os.path.join(d, "m2"), options={"max_dam_line_ft": 1000})
        assert r2["info"]["dam_line"] <= 1000.0 + 1e-6 and "capped" in r2["info"]["dam_line_how"]
    finally:
        shutil.rmtree(d)


@test
def hecras_site_plan_time_step_window_and_breach_shape_note():
    """The plan uses HEC's adaptive (Courant) time step lines (BaldEagleDamBrk.p06 layout, only
    keywords from HEC's 6.6 examples), the simulation window follows the documented rule, and
    the README / package say that HEC-RAS's piping breach is not the plugin's."""
    if p0._skip():
        return
    import datetime
    from nd_breach_runner.core import hecras_package as hp
    d = tempfile.mkdtemp()
    try:
        p0._run(d)
        r = _build(d)
        m, info = r["model"], r["info"]
        p01 = open(os.path.join(d, "model", m.name + ".p01"), encoding="ascii").read()
        lines = p01.replace("\r", "").split("\n")
        k = lines.index("Computation Time Step Use Courant=        -1")
        # HEC's BaldEagleCrkMulti2D/BaldEagleDamBrk.p06 (6.6 examples): the same eight keywords
        # in the same order; Max Courant 1 / Min 0.45 like HEC's file
        assert [ln.split("=")[0] for ln in lines[k:k + 8]] == [
            "Computation Time Step Use Courant", "Computation Time Step Use Time Series",
            "Computation Time Step Max Courant", "Computation Time Step Min Courant",
            "Computation Time Step Count To Double", "Computation Time Step Max Doubling",
            "Computation Time Step Max Halving", "Computation Time Step Residence Courant"]
        assert lines[k + 2] == "Computation Time Step Max Courant=1"
        assert lines[k + 3] == "Computation Time Step Min Courant=0.45"
        assert p0.unknown_keywords(os.path.join(d, "model", m.name + ".p01")) == set()
        # simulation window: synthetic arrival 2 h
        hy, dam, params = hecras.load_run(d)
        te = hm.hydrograph_end_hr(hy.t_s, hy.q_cms, hy.t_ref_s)
        # v3.10.22: plus the sunny-day warm-up before the breach's set start time
        assert abs(info["sim_hours"] - (hm.simulation_hours(te, 2.0, m.breach["formation_hr"])[0]
                                        + hm.BREACH_WARMUP_HR)) < 1e-9
        assert info["sim_how"] in open(r["paths"]["readme"], encoding="utf-8").read()
        assert "Breach shape" in open(r["paths"]["readme"], encoding="utf-8").read()
        # the package
        p = hp.build_package(d, when=datetime.date(2026, 10, 18), make_zip=False)
        man = json.load(open(os.path.join(p["folder"], hp.MANIFEST)))
        assert man["breach_shape_note"] == hm.BREACH_SHAPE_NOTE
        assert man["site_checks"]["problems"] == [] and man["sim_hours"] == info["sim_hours"]
        assert man["time_step"]["adaptive"] is True
        win = open(os.path.join(p["folder"], hp.README), encoding="utf-8").read()
        assert "Breach shape" in win and "(fixed)" not in win and "adjustable time step" in win
    finally:
        shutil.rmtree(d)


# ============================================================================ HDF reader
class _DS:
    def __init__(self, shape, attrs=None):
        self.shape = shape
        self.dtype = "f8"
        self.attrs = attrs or {}


@test
def hecras_site_time_series_paired_with_its_own_output_block():
    """DWR-3161 readout: a connection's DSS Profile Output table (33 rows) was read against the
    Base Output times (385). pair_time takes the Time of the series' own block."""
    B = "Results/Unsteady/Output/Output Blocks/"
    times = [(B + "Base Output/Unsteady Time Series/Time", _DS((385,))),
             (B + "DSS Hydrograph Output/Unsteady Time Series/Time", _DS((1921,))),
             (B + "DSS Profile Output/Unsteady Time Series/Time", _DS((33,)))]
    ser = (B + "DSS Profile Output/Unsteady Time Series/SA 2D Area Conn/Dam/Structure Variables",
           _DS((33, 4)))
    assert hecras.pair_time(times, ser)[0].startswith(B + "DSS Profile Output")
    ser = (B + "Base Output/Unsteady Time Series/2D Flow Areas/A/Water Surface", _DS((385, 9)))
    assert hecras.pair_time(times, ser)[0].startswith(B + "Base Output")
    assert hecras.pair_time(times, None) is times[0] and hecras.pair_time([], ser) is None


@test
def hecras_site_connection_timeseries_reads_the_time_series_not_the_profile_table():
    try:
        import h5py
    except ImportError:
        return
    d = tempfile.mkdtemp()
    try:
        path = os.path.join(d, "x.p01.hdf")
        B = "Results/Unsteady/Output/Output Blocks/"
        vu = np.array([[b"Total Flow", b"cfs"], [b"Weir Flow", b"cfs"], [b"Stage HW", b"ft"],
                       [b"Stage TW", b"ft"]], dtype="S32")
        with h5py.File(path, "w") as f:
            for blk, n in (("Base Output", 9), ("DSS Hydrograph Output", 61),
                           ("DSS Profile Output", 3)):
                g = B + blk + "/Unsteady Time Series/"
                t = f.create_dataset(g + "Time", data=np.linspace(0.0, 0.25, n))
                t.attrs["Time"] = b"days"
                v = np.zeros((n, 4))
                v[:, 0] = np.sin(np.linspace(0, np.pi, n)) * 1000.0 + (5000.0 if n == 3 else 0)
                ds = f.create_dataset(g + "SA 2D Area Conn/Dam/Structure Variables", data=v)
                ds.attrs["Variable_Unit"] = vu
        r = hecras.connection_timeseries(path, "Dam")
        assert r["block"] == "DSS Hydrograph Output" and r["values"].shape == (61, 4), r["block"]
        assert r["columns"][0] == "Total Flow" and r["units"][0] == "cfs"
        assert abs(r["t_hr"][-1] - 6.0) < 1e-9 and r["values"][:, 0].max() < 1001.0
        # without the minute block: Base Output, never the profile table
        with h5py.File(path, "a") as f:
            del f[B + "DSS Hydrograph Output"]
        r = hecras.connection_timeseries(path, "Dam")
        assert r["block"] == "Base Output" and r["values"].shape == (9, 4)
        try:
            hecras.connection_timeseries(path, "Other dam")
            assert False
        except hecras.HecRasError as e:
            assert "Other dam" in str(e)
    finally:
        shutil.rmtree(d)
