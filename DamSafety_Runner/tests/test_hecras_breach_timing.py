"""v3.10.22 (Absaraka DWR-2801 HEC-RAS follow-up): the sunny-day breach starts at a set time
after a short warm-up (not a trigger level a hundredth below the starting pool), the plan uses
HEC-RAS's overtopping mode for the plugin's routed breach, a spillway can be a notch in the
dam weir with an inflow that holds the pool through the warm-up, the solver tolerances can be
tightened, the plugin's own hydrograph on the plan's breach is written for comparison, and
the HEC-RAS engine counts arrival times from the breach start.

Synthetic data only (test_hecras_p0's valley); runs in the Runner suite (run_tests.py)."""
import json
import os
import shutil
import tempfile

import numpy as np

from nd_breach_runner.core import breach as br
from nd_breach_runner.core import hecras
from nd_breach_runner.core import hecras_model as hm
from nd_breach_runner.tests.registry import test
from nd_breach_runner.tests import test_hecras_p0 as p0


def _build(folder, **opts):
    return hm.build_model(folder, out_folder=os.path.join(folder, "model"), options=opts)


def _lines(path, *prefixes):
    with open(path, encoding="ascii", newline="") as f:
        t = f.read()
    assert "\r\n" in t
    return [ln for ln in t.replace("\r", "").split("\n") if ln.startswith(prefixes)]


# ============================================================================ pure helpers
@test
def hecras_timing_breach_start_line_set_time_and_trigger():
    b = {"start_mode": "time", "start_hr": 0.25, "trigger_wse": 1069.11}
    # HEC's own set-time layout (L40_Levee_Outflow_H.p06: Breach Start=False,,<date>,<HHMM>,...)
    assert hm.breach_start_line(b) == "Breach Start=False,,01JAN2000,0015,False,,,0"
    assert hm.breach_start_line(dict(b, start_hr=1.5)) == \
        "Breach Start=False,,01JAN2000,0130,False,,,0"
    # the water-surface trigger is unchanged (BaldEagleDamBrk layout)
    assert hm.breach_start_line({"trigger_wse": 1008.0}) == "Breach Start=True,1008,,,False,,,0"
    assert hm.breach_start_line(dict(b, start_mode="wse")) == \
        "Breach Start=True,1069.11,,,False,,,0"


@test
def hecras_timing_spillway_stations_and_notch():
    # the end farther from the breach, a margin in from the end
    assert hm.spillway_stations(1700.0, 120.0, 780.0, 920.0, margin=100.0)[0] == (1480.0, 1600.0)
    assert hm.spillway_stations(1700.0, 120.0, 1100.0, 1300.0, margin=100.0)[0] == (100.0, 220.0)
    # a given centre; one that would cut into the breach is refused with the reason
    assert hm.spillway_stations(1700.0, 120.0, 780.0, 920.0, station=300.0)[0] == (240.0, 360.0)
    ss, why = hm.spillway_stations(1700.0, 120.0, 780.0, 920.0, station=800.0)
    assert ss is None and "overlap the breach" in why, why
    ss, why = hm.spillway_stations(100.0, 120.0, 40.0, 60.0)
    assert ss is None and "does not fit" in why, why
    weir = [(0.0, 1069.12), (500.0, 1069.12), (1000.0, 1070.0), (1700.0, 1069.12)]
    w = hm.add_spillway_notch(weir, 1480.0, 1600.0, 1066.0)
    st = [a for a, _ in w]
    assert st == sorted(set(st)), w                          # strictly increasing stations
    assert (1480.0, 1066.0) in w and (1600.0, 1066.0) in w, w
    assert w[0] == (0.0, 1069.12) and w[-1] == (1700.0, 1069.12)
    assert min(e for _, e in w) == 1066.0
    # the flow over it and the warm-up inflow series
    q = hm.weir_flow(2.6, 120.0, 1069.12 - 1066.0)
    assert abs(q - 2.6 * 120.0 * 3.12 ** 1.5) < 1e-9 and hm.weir_flow(2.6, 120.0, -1.0) == 0.0
    lab, vals = hm.warmup_inflow(1.0, 0.25, 100.0, base=2.0)
    assert lab == "5MIN" and len(vals) == 14
    assert vals[:4] == [102.0] * 4 and vals[4:] == [2.0] * 10, vals


@test
def hecras_timing_spillway_spec_from_options_and_dam_json():
    d = tempfile.mkdtemp()
    try:
        assert hm.spillway_spec(d, {}) is None
        assert hm.spillway_spec(d, {"spillway_crest_ft": 1066, "spillway_width_ft": 120}) == \
            (1066.0, 120.0, None, "chosen")
        with open(os.path.join(d, "dam.json"), "w", encoding="utf-8") as f:
            json.dump({"fields": {"emergency_spillway_crest_ft": 1066.0,
                                  "emergency_spillway_width_ft": 120.0}}, f)
        sp = hm.spillway_spec(d, {})
        assert sp[:3] == (1066.0, 120.0, None) and "emergency_spillway_crest_ft" in sp[3], sp
        assert hm.spillway_spec(d, {"spillway": False}) is None
    finally:
        shutil.rmtree(d)


# ============================================================================ build_model
@test
def hecras_timing_sunny_day_breach_starts_after_the_warm_up():
    if p0._skip():
        return
    d = tempfile.mkdtemp()
    try:
        p0._run(d)
        base = _build(d, breach_start="wse")         # 3.10.20's plan: trigger 0.01 below
        assert _lines(os.path.join(d, "model", base["model"].name + ".p01"),
                      "Breach Start=")[0].startswith("Breach Start=True,")
        r = _build(d)
        m, info = r["model"], r["info"]
        p01 = os.path.join(d, "model", m.name + ".p01")
        assert _lines(p01, "Breach Start=") == ["Breach Start=False,,01JAN2000,0015,False,,,0"]
        assert info["breach_start"]["mode"] == "time" and info["breach_start"]["start_hr"] == 0.25
        # the window grows by the warm-up; the pool starts where it did (top of dam)
        assert abs(info["sim_hours"] - base["info"]["sim_hours"] - 0.25) < 1e-9
        assert abs(m.initial_wse - base["model"].initial_wse) < 1e-9
        assert base["info"]["breach_start"]["mode"] == "wse"
        for k in ("g01", "u01", "p01"):
            assert p0.unknown_keywords(r["paths"][k]) == set(), k
        txt = open(r["paths"]["readme"], encoding="utf-8").read()
        assert "| Breach start | at 0.25 h (set time) | sunny day" in txt, txt
        # no warm-up: a set time of 0
        r0 = _build(d, breach_warmup_hr=0.0)
        assert _lines(os.path.join(d, "model", m.name + ".p01"), "Breach Start=") == \
            ["Breach Start=False,,01JAN2000,0000,False,,,0"]
        assert abs(r0["info"]["sim_hours"] - base["info"]["sim_hours"]) < 1e-9
        for bad in ({"breach_start": "soon"}, {"breach_warmup_hr": 7.0}):
            try:
                _build(d, **bad)
            except hm.HecRasModelError:
                pass
            else:
                raise AssertionError(bad)
    finally:
        shutil.rmtree(d)


@test
def hecras_timing_plan_breach_mode_matches_the_plugin():
    """The plugin's piping method routes a trapezoid that deepens from the crest, which is
    HEC-RAS's overtopping mode; hec_breach_mode='failure_mode' keeps HEC's piping."""
    if p0._skip():
        return
    d = tempfile.mkdtemp()
    try:
        p0._run(d)
        r = _build(d)
        g = _lines(os.path.join(d, "model", r["model"].name + ".p01"), "Breach Geom=")[0]
        assert g.split(",")[5] == "False" and r["model"].breach["hec_mode"] == "overtopping", g
        assert not any(n.startswith("Initial piping elevation") for n in r["notes"])
        r2 = _build(d, hec_breach_mode="failure_mode")
        g2 = _lines(os.path.join(d, "model", r2["model"].name + ".p01"), "Breach Geom=")[0]
        assert g2.split(",")[5] == "True" and g2.split(",")[7] != "", g2
        try:
            _build(d, hec_breach_mode="sometimes")
        except hm.HecRasModelError:
            pass
        else:
            raise AssertionError("a bad hec_breach_mode was accepted")
    finally:
        shutil.rmtree(d)


@test
def hecras_timing_spillway_notch_and_warm_up_inflow():
    if p0._skip():
        return
    d = tempfile.mkdtemp()
    try:
        p0._run(d, dam_json_len_ft=1500.0)
        top_ft = p0.TOP_M / 0.3048
        r = _build(d, spillway_crest_ft=top_ft - 3.0, spillway_width_ft=100.0)
        m, sp = r["model"], r["info"]["spillway"]
        s1, s2 = sp["stations"]
        assert abs(s2 - s1 - 100.0) < 0.2 and abs(sp["crest"] - round(top_ft - 3.0, 2)) < 0.011
        # the notch is in the dam weir, away from the breach's top width
        assert (round(s1, 2), sp["crest"]) in m.weir and (round(s2, 2), sp["crest"]) in m.weir
        b = m.breach
        half = 0.5 * b["bottom_width"] + b["left_slope"] * (top_ft - b["bottom_elev"])
        assert s2 <= b["center_station"] - half or s1 >= b["center_station"] + half, (sp, b)
        assert len(m.weir) <= hm.MAX_WEIR_POINTS + 4
        # the inflow holds the pool at its start until the breach (then none)
        hold = hm.weir_flow(2.6, 100.0, m.initial_wse - sp["crest"])
        assert abs(sp["warmup_inflow"] - round(hold, 1)) < 0.11
        u = open(r["paths"]["u01"], encoding="ascii").read().replace("\r", "")
        assert "Interval=5MIN\nLateral Inflow Hydrograph= " in u, u[:400]
        n = int(u.split("Lateral Inflow Hydrograph= ")[1].split()[0])
        assert n == int(np.ceil(r["info"]["sim_hours"] * 12)) + 2
        vals = [float(v) for v in u.split("Lateral Inflow Hydrograph= ")[1].split("\n", 1)[1]
                .split("DSS Path")[0].split()]
        assert len(vals) == n and vals[:4] == [round(hold, 2)] * 4 and set(vals[4:]) == {0.0}
        assert "| Spillway | 100 ft notch at" in open(r["paths"]["readme"], encoding="utf-8").read()
        # no hold without a warm-up, and none when asked not to
        r2 = _build(d, spillway_crest_ft=top_ft - 3.0, spillway_width_ft=100.0, warmup_hold=False)
        assert "warmup_inflow" not in r2["info"]["spillway"] and r2["model"].inflow is None
        # a crest at or above the top of dam is not a spillway
        r3 = _build(d, spillway_crest_ft=top_ft + 1.0, spillway_width_ft=100.0)
        assert r3["info"]["spillway"] is None
        assert any(n.startswith("Spillway not added") for n in r3["notes"]), r3["notes"]
        # without spillway data nothing changes in the weir
        r4 = _build(d)
        assert r4["info"]["spillway"] is None and min(e for _, e in r4["model"].weir) >= top_ft - 0.01
    finally:
        shutil.rmtree(d)


@test
def hecras_timing_tolerances_default_unchanged_and_tight():
    if p0._skip():
        return
    d = tempfile.mkdtemp()
    try:
        p0._run(d)
        keys = ("UNET ZTol=", "UNET ZSATol=", "UNET D2 Z Tol=", "UNET D2 Volume Tol=",
                "UNET D2 Max Iterations=")
        r = _build(d)
        p01 = os.path.join(d, "model", r["model"].name + ".p01")
        # 3.10.20's lines, byte for byte
        assert _lines(p01, *keys) == ["UNET ZTol= 0.02 ", "UNET ZSATol= 0.02 ",
                                      "UNET D2 Z Tol= 0.01 ", "UNET D2 Volume Tol= 0.01 ",
                                      "UNET D2 Max Iterations= 20 "] * 1 + \
            ["UNET D2 Z Tol= 0.01 ", "UNET D2 Volume Tol= 0.01 ", "UNET D2 Max Iterations= 20 "]
        r = _build(d, tolerances="tight")
        assert _lines(p01, *keys) == ["UNET ZTol= 0.01 ", "UNET ZSATol= 0.01 ",
                                      "UNET D2 Z Tol= 0.005 ", "UNET D2 Volume Tol= 0.005 ",
                                      "UNET D2 Max Iterations= 30 ",
                                      "UNET D2 Z Tol= 0.005 ", "UNET D2 Volume Tol= 0.005 ",
                                      "UNET D2 Max Iterations= 30 "]
        assert r["info"]["tolerances"]["name"] == "tight"
        r = _build(d, tolerances={"zsatol": 0.005})
        assert "UNET ZSATol= 0.005 " in _lines(p01, *keys)
        assert p0.unknown_keywords(p01) == set()
    finally:
        shutil.rmtree(d)


@test
def hecras_timing_plugin_hydrograph_on_the_hec_breach():
    """The plugin's routing with the plan's (raised) breach bottom: lower than the run's peak
    when the bottom went up, written to Runner_reference with the plan's times."""
    if p0._skip():
        return
    import datetime
    from nd_breach_runner.core import hecras_package as hp
    d = tempfile.mkdtemp()
    try:
        p0._run(d)
        r = _build(d)
        pr, b = r["info"]["plugin_reference"], r["model"].breach
        assert pr["method"] == "piping" and abs(pr["bottom_ft"] - b["bottom_elev"]) < 0.011
        assert abs(pr["formation_hr"] - b["formation_hr"]) < 1e-9
        assert abs(pr["peak_hr_in_plan"] - pr["peak_hr_after_breach"] - 0.25) < 1e-9
        assert r["info"]["site"]["breach_bottom_raised_ft"] > 0
        assert pr["peak_cfs"] < pr["run_peak_cfs"], pr
        # with the run's own bottom and width the plugin's routing gives the run's peak back
        hy, dam, params = hecras.load_run(d)
        b0 = dict(b, bottom_elev=params["breach_bottom_ft"],
                  bottom_width=params["bottom_width_ft"])
        same = hm.plugin_reference(dam, params, b0, hm.Units(False))
        assert abs(same["peak_cfs"] - params["peak_cfs"]) <= 0.02 * params["peak_cfs"], \
            (same["peak_cfs"], params["peak_cfs"])
        assert hm.plugin_reference(dam, dict(params, method="tr60"), b, hm.Units(False)) is None
        p = hp.build_package(d, when=datetime.date(2026, 10, 18), make_zip=False)
        csv = os.path.join(p["folder"], hp.REFERENCE, hp.REFERENCE_ON_HEC_BREACH)
        rows = [ln.split(",") for ln in open(csv, encoding="utf-8").read().splitlines()[2:]]
        q = [float(x[2]) for x in rows]
        assert abs(max(q) - pr["peak_cfs"]) < 0.11 and float(rows[0][1]) == 0.25
        man = json.load(open(os.path.join(p["folder"], hp.MANIFEST)))
        assert man["breach_start"]["start_hr"] == 0.25 and man["failure_mode_in_plan"] == \
            "overtopping" and man["plugin_reference"]["peak_cfs"] == pr["peak_cfs"]
        assert man["tolerances"]["name"] == "default" and man["mesh_seeds"]["used"] == "grid"
        win = open(os.path.join(p["folder"], hp.README), encoding="utf-8").read()
        assert "breaches the dam at 0.25 h (a set time" in win
    finally:
        shutil.rmtree(d)


@test
def hecras_timing_engine_arrival_counts_from_the_breach_start():
    from nd_breach_runner.core import hecras_engine as he
    assert he.results_t0_hr({"inflow_bc": {"t_ref_hr": 1.5}, "info": {}}) == 1.5
    assert he.results_t0_hr({"inflow_bc": None,
                             "info": {"breach_start": {"mode": "time", "start_hr": 0.25}}}) == 0.25
    assert he.results_t0_hr({"inflow_bc": None,
                             "info": {"breach_start": {"mode": "wse", "start_hr": None}}}) == 0.0
    assert he.results_t0_hr({"info": {}}) == 0.0
