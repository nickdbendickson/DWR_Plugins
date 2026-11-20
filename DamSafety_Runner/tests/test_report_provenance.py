"""Runner 3.22.0: report provenance (H4a, H4b, A5, M2, H9). These are REPORT-TEXT-ONLY
additions - they make the submittal docx and run_report.md say what the run actually used.
The tests here assert the run-built text reflects the run, the MacDonald units sentence, and the
land-cover coverage count. No computed number is involved."""
import json
import os
import shutil
import tempfile

from nd_breach_runner.tests.registry import test
from nd_breach_runner.core import report_docx as R
from nd_breach_runner.core import breach as br
from nd_breach_runner.core import nd_data
from nd_breach_runner.core import workflow


def _rep(**over):
    """A minimal but realistic run_report dict (sunny-day, top-of-dam) to vary per test."""
    rep = {
        "tool": "ND Dam Breach Runner 3.22.0",
        "policy": "ND 5.1.1",
        "run_finished": "2026-11-19T00:00:00",
        "scenario": "sunny",
        "flood_engine_short": "LISFLOOD-FP",
        "dam": {"dam_id": "T1", "name": "Test Dam", "dam_height_ft": 25.0,
                "top_of_dam_ft": 1850.0, "surface_area_top_ac": 60.0,
                "storage_top_acft": 500.0},
        "settings": {"tier": "lisflood", "manning_n": 0.06},
        "hydrograph": {"method": "piping", "breach_parameters": "Froehlich (2008), piping",
                       "starting_wse_ft": 1850.0, "starting_wse_rule": "top of dam",
                       "avg_breach_width_ft": 68.5, "formation_time_hr": 0.578,
                       "side_slope_h": 0.7, "storage_exponent_m": 3.0, "vw_acft": 500.0,
                       "vw_source": "maximum storage (DWR), sunny day", "hb_ft": 25.0,
                       "hw_ft": 25.0, "peak_cfs": 16175.0},
        "summary": {"dam_id": "T1", "scenario": "sunny", "peak_breach_cfs": 16175.0,
                    "breach_method": "piping", "units": "US customary (ft, cfs, ac-ft)"},
        "vertical_datum": {"target": "NAVD88", "source_datum": "NGVD29", "how": "assumed",
                           "applied": False, "shift_ft": 0.0},
        "warnings": [],
    }
    for k, v in over.items():
        rep[k] = v
    return rep


def _normal_pool_rep():
    r = _rep()
    r["hydrograph"].update(starting_wse_ft=1842.0, starting_wse_rule="normal pool, your choice",
                           vw_source="storage at the stated starting pool (sunny day, your choice)")
    return r


def _user_hydro_rep():
    r = _rep()
    r["hydrograph"].update(method="user", breach_parameters=None)
    r["flood"] = {"user_hydrographs": True}
    return r


def _tr60_rep():
    r = _rep()
    r["hydrograph"].update(method="tr60")
    return r


# ---------------------------------------------------------------- H4a: start pool + routing

@test
def provenance_normal_pool_does_not_claim_top_of_dam():
    r = _normal_pool_rep()
    wse, basis = R.start_pool_basis(r)
    assert wse == "1,842 ft", wse
    assert "normal pool" in basis and "top of dam" not in basis, basis
    # the full H4a sentence pair must not claim the top of dam either
    sent = "The reservoir starts at {}, {}. {}".format(wse, basis, R.routing_sentence(r))
    assert "top of dam" not in sent, sent
    assert "routed through the reservoir" in sent and "power-law storage" in sent, sent


@test
def provenance_top_of_dam_run_still_names_top_of_dam():
    r = _rep()
    wse, basis = R.start_pool_basis(r)
    assert "top of dam" in basis and "ND 5.1.1" in basis, basis


@test
def provenance_user_hydrograph_is_not_routed():
    r = _user_hydro_rep()
    rs = R.routing_sentence(r)
    assert "NOT routed by this tool" in rs, rs
    _wse, basis = R.start_pool_basis(r)
    assert "user-supplied" in basis, basis


@test
def provenance_tr60_is_not_routed():
    r = _tr60_rep()
    rs = R.routing_sentence(r)
    assert "NOT routed through the reservoir" in rs and "TR-60" in rs, rs


# ---------------------------------------------------------------- H4a/H4b: storage exponent m

@test
def provenance_m_source_fitted_vs_default():
    fitted = R.storage_m_text(_rep())                      # surface area given
    assert "fitted to the storage and surface area given" in fitted, fitted
    r = _rep()
    r["dam"]["surface_area_top_ac"] = 0
    r["hydrograph"]["storage_exponent_m"] = 2.0
    default = R.storage_m_text(r)
    assert "m = 2" in default and "no surface area given" in default, default


# ---------------------------------------------------------------- H4b: "assumptions used" list

@test
def provenance_run_assumptions_list_reflects_the_run():
    r = _normal_pool_rep()
    pairs = dict(R.run_assumption_lines(r))
    assert "Starting reservoir pool" in pairs
    assert "normal pool" in pairs["Starting reservoir pool"], pairs["Starting reservoir pool"]
    assert "top of dam" not in pairs["Starting reservoir pool"]
    assert "fitted to the storage and surface area given" in pairs["Storage-curve exponent m"]
    assert pairs["Roughness (Manning's n)"] == "single Manning's n = 0.06", pairs
    assert pairs["Vertical datum"].startswith("NAVD88"), pairs["Vertical datum"]
    assert pairs["Guessed culverts cut into the DEM"] == "0"
    # absent values read "not recorded" rather than being invented
    bare = _rep()
    bare["hydrograph"] = {"method": "piping"}
    bare["dam"] = {}
    bare["vertical_datum"] = {}
    bp = dict(R.run_assumption_lines(bare))
    assert bp["Average breach width (ft)"] == "not recorded", bp
    assert bp["Vertical datum"] == "not recorded", bp


# ---------------------------------------------------------------- A5: headline peak inputs

@test
def provenance_headline_peak_inputs_line():
    line = R.headline_peak_inputs(_rep())
    assert line.startswith("Inputs to this peak:"), line
    for needle in ("breach height Hb 25 ft", "head on the breach 25 ft", "Vw 500 ac-ft",
                   "maximum storage (DWR)", "surface area 60 ac", "starting pool 1,850 ft",
                   "method"):
        assert needle in line, (needle, line)
    # no breach peak -> no line
    nb = _rep(scenario="flood_no_breach")
    nb["summary"]["no_breach_scenario"] = True
    assert R.headline_peak_inputs(nb) == ""


# ---------------------------------------------------------------- end to end: the docx text

@test
def provenance_docx_normal_pool_text_is_run_built():
    d = tempfile.mkdtemp(prefix="prov_docx_")
    try:
        mdl = os.path.join(d, "_breach_model")
        os.makedirs(mdl)
        r = _normal_pool_rep()
        json.dump(r, open(os.path.join(mdl, "run_report.json"), "w"))
        open(os.path.join(mdl, "run_report.md"), "w").write("# Breach model run: T1\n\n- ok\n")
        out = os.path.join(d, "prov.docx")
        res = R.build_submittal(d, "T1", out)
        assert os.path.exists(out), res.warnings
        t = R.inspect_docx(out)["text"]
        assert "Assumptions used in this run" in t
        assert "normal pool" in t
        # the old hard-coded, false-for-normal-pool claim is gone
        assert "starting reservoir level is the top of dam" not in t, \
            "the H4a bullet still hard-codes top of dam"
        # A5 line is present with the headline peak
        assert "Inputs to this peak:" in t
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------- the markdown report

@test
def provenance_markdown_has_assumptions_and_peak_inputs():
    md = workflow.render_report(_full_md_report())
    assert "## Assumptions used in this run" in md, md[:400]
    assert "Inputs to this peak:" in md
    assert "- Starting reservoir pool:" in md


def _full_md_report():
    """A report complete enough for workflow.render_report (it indexes several keys directly)."""
    r = _rep()
    s = r["summary"]
    s.update(final_model="LISFLOOD-FP", storage_at_failure_acft=500.0, flooded_acres=300.0,
             flooded_acres_over_arrival_depth=120.0, max_depth_ft=6.5, max_velocity_fps=4.9,
             latest_arrival_hr=5.0, domain_mi=[2, 1], lisflood_runs=1)
    r["inflow_point"] = {"xy": [10.0, 20.0], "how": "toe"}
    r["settings"].update(edge_tolerance_ft=1.0, expand_step_mi=3.0)
    r["lisflood_runs"] = [{"pass": 1, "run": 1, "size_mi": [2, 1], "cells": 1000,
                           "sim_time_hr": 4.0, "edge_max_depth_ft": {"N": 0, "S": 0, "E": 0,
                                                                     "W": 0},
                           "edges_over_tolerance": [], "runtime_min": 1.0,
                           "stats": {"max_volume_error_pct": 0.1, "peak_inflow_cfs": 16000.0}}]
    r["outputs"] = {"final": {"depth": "Depth.tif"}}
    return r


# ---------------------------------------------------------------- M2: MacDonald units sentence

@test
def provenance_macdonald_note_has_units_lock():
    dam = br.DamData(dam_id="M1", dam_height_ft=25.0, storage_top_acft=500.0,
                     top_of_dam_ft=1850.0, surface_area_top_ac=60.0, crest_width_ft=12,
                     us_slope_h=3, ds_slope_h=2.5)
    h = br.make_hydrograph(dam, br.METHOD_MLM)
    joined = " ".join(h.notes)
    assert "SI only: Vout in m3, hw in m, Ver in m3." in joined, h.notes
    assert "tf = 0.0179·Ver^0.364" in joined, h.notes


# ---------------------------------------------------------------- H9: Manning coverage count

@test
def provenance_nlcd_to_manning_reports_coverage():
    import numpy as np
    lut = {"default": 0.06, 11: 0.035, 41: 0.10}
    # 4 cells: one class 11 (covered), one class 41 (covered), one unknown code 99 (default),
    # one NaN (default). 2 of 4 take the default.
    codes = np.array([[11, 41], [99, np.nan]], dtype=float)
    counts = {}
    n = nd_data.nlcd_to_manning(codes, lut, counts)
    assert counts["total_cells"] == 4 and counts["default_cells"] == 2, counts
    assert counts["nodata_cells"] == 1 and counts["default_n"] == 0.06, counts
    # the n values themselves are unchanged by passing counts
    n2 = nd_data.nlcd_to_manning(codes, lut)
    assert np.array_equal(np.nan_to_num(n), np.nan_to_num(n2))
    assert n[0, 0] == 0.035 and n[0, 1] == 0.10 and n[1, 0] == 0.06 and n[1, 1] == 0.06


@test
def provenance_manning_source_text_reports_coverage():
    r = _rep(manning_coverage={"total_cells": 1600, "default_cells": 1300, "default_n": 0.06})
    r["settings"]["manning_lut"] = {"default": 0.06}
    txt = R.manning_source_text(r)
    assert "land-cover" in txt and "1,300 of 1,600" in txt, txt
