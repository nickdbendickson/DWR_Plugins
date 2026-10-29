"""3.12.5 / damsuite 1.18.2 (Nick 2026-10-28): an elevation between 0 and 300 ft is always on a
local datum (never VERTCON-shifted; the crest from the DEM at mid-crest, the others tied by the
offset, "DEM-derived"), local culvert inverts tied to the LiDAR bed, and the dam point at
mid-crest of the dam line (damsuite/midcrest.py, workflow.place_midcrest). No QGIS, no model
runs.

DWR-4031 reference: the DEM mid-crest is 1630.49 ft NAVD88 at (-407005.516, 2822706.843)
EPSG:5070; top of dam 120.7 ft (local) -> offset +1509.79 ft: streambed 100 -> 1609.79,
spillway 117.7 -> 1627.49 ft."""
import types

import numpy as np

from nd_breach_runner.tests.registry import test
from nd_breach_runner.tests.test_vertical_datum import LON, LAT, _dam, _st, _flood

FT = 0.3048


def _VD():
    from nd_breach_runner.damsuite import vdatum
    return vdatum


def _MC():
    from nd_breach_runner.damsuite import midcrest
    return midcrest


def _ridge_grid():
    """1 m grid, x -10..110, y -50..50 (top edge 50): a ridge along y = 3.5 (z 10 m, falling
    0.1 m per m) and a 50 m hill at y = 40 (beyond the +-25 m profile)."""
    xs = np.arange(-10, 110) + 0.5
    ys = 50.0 - (np.arange(100) + 0.5)
    X, Y = np.meshgrid(xs, ys)
    z = 10.0 - 0.1 * np.abs(Y - 3.5)
    z[Y > 35.0] = 50.0
    return z, (-10.0, 1.0, 0.0, 50.0, 0.0, -1.0)


@test
def test_midcrest_synthetic_line_and_dem():
    MC = _MC()
    z, gt = _ridge_grid()
    line = [(0.0, 0.0), (60.0, 0.0), (100.0, 0.0)]         # midpoint (50, 0) by length
    rec = MC.midcrest(line, MC.bilinear(z, gt), original_xy=(50.0, -10.0))
    assert rec is not None
    assert abs(rec["midpoint_xy"][0] - 50.0) < 1e-6 and abs(rec["midpoint_xy"][1]) < 1e-6, rec
    assert abs(rec["xy"][0] - 50.0) < 1e-6 and abs(rec["xy"][1] - 3.5) <= 0.05, rec
    assert abs(rec["z_m"] - 10.0) < 0.01 and abs(rec["z_ft"] - 10.0 / FT) < 0.05, rec
    assert abs(rec["moved_m"] - 13.5) <= 0.06 and abs(rec["line_length_m"] - 100.0) < 1e-6, rec
    assert rec["original_xy"] == [50.0, -10.0]
    import json
    json.dumps(MC.public(rec))
    assert "mid-crest" in MC.summary_line(rec)
    # an L-shaped line: the midpoint is measured along the line, not between the end points
    rec2 = MC.midcrest([(0.0, 0.0), (40.0, 0.0), (40.0, 40.0)], lambda x, y: 1.0)
    assert abs(rec2["midpoint_xy"][0] - 40.0) < 1e-6 and abs(rec2["midpoint_xy"][1]) < 1e-6
    # no line, no DEM
    assert MC.midcrest([(0.0, 0.0)], MC.bilinear(z, gt)) is None
    assert MC.midcrest(line, lambda x, y: float("nan")) is None


@test
def test_midcrest_place_in_the_runner():
    from nd_breach_runner.core import workflow
    from nd_breach_runner.core.grid import Grid
    z, gt = _ridge_grid()

    def read(bounds, cell):                 # the synthetic grid whatever the bounds
        return Grid(z.copy(), gt[0], gt[3], gt[1])
    dam = _dam(top=1071.15)
    dam.crest_line_xy = [(0.0, 0.0), (100.0, 0.0)]
    dam._point_original = (50.0, -10.0)
    st = _st()
    xy, rec = workflow.place_midcrest(dam, st, (50.0, 0.0), read, 1.0)
    assert rec["moved"] and abs(xy[1] - 3.5) <= 0.05 and abs(xy[0] - 50.0) < 1e-6, rec
    assert rec["original_xy"] == [50.0, -10.0] and abs(rec["moved_m"] - 13.5) <= 0.06
    # a breach position keeps the point; the crest is still the mid-crest DEM (Nick 2026-10-29
    # "Ok"): the record has the mid-crest z, so run_dam reads the crest there (MIDCREST_FROM)
    st = _st(breach_xy=(20.0, 0.0))
    xy, rec = workflow.place_midcrest(dam, st, (20.0, 0.0), read, 1.0)
    assert xy == (20.0, 0.0) and rec["moved"] is False and abs(rec["z_m"] - 10.0) < 0.01
    assert abs(rec["xy"][0] - 50.0) < 1e-6 and "_z_m" in rec and "warning" not in rec, rec
    # a batch's CREST_LINE stands in for the dam line
    dam2 = _dam(top=1071.15)
    st = _st(midcrest_line=[(0.0, 0.0), (100.0, 0.0)])
    xy, rec = workflow.place_midcrest(dam2, st, (50.0, -10.0), read, 1.0)
    assert rec["moved"] and abs(xy[1] - 3.5) <= 0.05
    # no line: the point is kept, with a warning
    xy, rec = workflow.place_midcrest(_dam(top=1071.15), _st(), (5.0, 6.0), read, 1.0)
    assert xy == (5.0, 6.0) and "no dam line" in rec["warning"]
    assert "NOT moved" in _MC().summary_line(rec)
    assert "midcrest_line" not in workflow.RunSettings().as_dict()


@test
def test_local_band_0_to_300_ft():
    VD = _VD()
    for v in (0.0, 120.7, 300.0):
        assert VD.is_local_elev(v), v
        assert VD.local_check(v)[0], v
        assert VD.local_check(v, 1630.49, 1.0)[0], v       # first test: whatever the DEM
    for v in (-1.0, 300.01, 301.0, 1071.15, None, "x", float("nan")):
        assert not VD.is_local_elev(v), v
    # 301+ follows #220: out of ND's range -> local; a plausible value is not
    assert VD.local_check(301.0)[0] and not VD.local_check(1071.15)[0]
    assert VD.local_check(301.0)[1] != VD.local_check(300.0)[1]
    # a band value is never shifted, even under an applied NGVD29 record
    rec = VD.plan(LON, LAT, "", VD.AUTO, use_pyproj=False)
    assert rec["applied"] and not rec.get("local")
    assert VD.convert_ft(95.0, rec, "x") == 95.0 and rec["fields"]["x"].get("local_elev")
    assert VD.convert_ft(301.0, rec, "y") > 301.5                # out of the band: shifted
    # plan(values=) with a band top makes the record local whatever the choice (no override
    # wording, Nick 2026-10-29); no split: the choice is not kept for any value (no other_datum)
    for override in (VD.AUTO, VD.NGVD29, VD.NAVD88):
        r = VD.plan(LON, LAT, "NGVD29", override, use_pyproj=False,
                    values={"top_of_dam_ft": 120.7})
        assert r["local"] and not r["applied"] and r.get("local_rule"), (override, r)
        assert any("between 0 and 300 ft" in w for w in r["warnings"]), r["warnings"]
        assert not any("overrid" in w for w in r["warnings"]), r["warnings"]
        assert "other_datum" not in r and "other_shift_ft" not in r, (override, r)
    r = VD.plan(LON, LAT, "", VD.AUTO, use_pyproj=False, values={"top_of_dam_ft": 1071.15})
    assert r["applied"] and not r.get("local")


@test
def test_local_band_4031_offset():
    """DWR-4031: top 120.7, streambed 100, spillway 117.7 ft (local) and the DEM mid-crest
    1630.49 ft NAVD88 -> +1509.79 ft: 1630.49 / 1609.79 / 1627.49 ft, never VERTCON-shifted,
    even with NGVD29 chosen."""
    from nd_breach_runner.core import vdatum_run as vdrun
    from nd_breach_runner.core import workflow
    VD = _VD()
    for datum in ("", "NGVD29", "NAVD88"):
        dam = _dam(top=120.7, bottom=100.0)
        fl = _flood(110.0, 118.0, [117.7, 119.0], 100.0)
        st = _st(flood=fl, elev_datum=datum or "auto")
        rec = vdrun.apply(dam, st, LON, LAT)
        assert rec["local"] and not rec["applied"], (datum, rec)
        assert dam.top_of_dam_ft == 120.7 and dam.breach_bottom_ft == 100.0     # not shifted
        assert abs(fl.spillway.elev_m[0] / FT - 117.7) < 1e-9
        rec, changed = vdrun.apply_local(dam, st, 1630.49, workflow.MIDCREST_FROM)
        assert changed, (datum, rec)
        assert abs(dam.top_of_dam_ft - 1630.49) < 1e-9
        assert abs(dam.breach_bottom_ft - 1609.79) < 1e-6, dam.breach_bottom_ft
        assert abs(fl.spillway.elev_m[0] / FT - 1627.49) < 1e-6
        assert abs(rec["local_offset_ft"] - 1509.79) < 1e-6
        f = rec["fields"]
        assert f["breach_bottom_ft"]["source"] == 100.0
        assert abs(f["breach_bottom_ft"]["navd88"] - 1609.79) < 1e-3
        assert f["breach_bottom_ft"].get("how") == "DEM-derived"
        assert f["top_of_dam_ft"].get("how") == workflow.MIDCREST_FROM
        txt = "\n".join(VD.summary_lines(VD.public(rec)))
        assert "DEM-derived" in txt and ("0 and 300" in txt or "0 - 300" in txt), txt
        # the dam point record (run_report / dam.json / Hazard summary read the same record)
        rec["dam_point"] = {"xy": [-407005.516, 2822706.843], "z_ft": 1630.49, "moved_m": 27.77,
                            "original_xy": [-407020.0, 2822683.0], "moved": True, "offset_m": 1.5,
                            "method": _MC().METHOD, "line_length_m": 200.0}
        txt = "\n".join(VD.summary_lines(VD.public(rec)))
        assert "mid-crest" in txt and "1630.49" in txt and "27.77" in txt, txt
        assert "1609.79" in "\n".join(vdrun.report_lines(VD.public(rec)))
        # Nick 2026-10-29 ("Forget overrid datum. Convert local to navd 88."): the same
        # numbers whatever datum was chosen, no override wording, no review flag
        assert not any("overrid" in w for w in rec["warnings"]), rec["warnings"]
        assert not rec.get("review") and "REVIEW" not in txt, txt
        assert abs(fl.normal_pool_ft - 1619.79) < 1e-6 and abs(fl.trigger_value - 1627.79) < 1e-6
        assert abs(fl.outlets[0].invert_m / FT - 1609.79) < 1e-6


def _tied(top=120.7, bottom=100.0, pmf=None, datum="auto", flood=None):
    from nd_breach_runner.core import vdatum_run as vdrun
    dam = _dam(top=top, bottom=bottom, pmf=pmf)
    st = _st(flood=flood, elev_datum=datum)
    vdrun.apply(dam, st, LON, LAT)
    return dam, st


@test
def test_local_band_mixed_record_ties_not_drops():
    """Mixed records (Nick 2026-10-29, no split confirmed): a band top with a PMF pool in ND's
    range - every value of the local record, the ND-range one too, is DEM-tied by the same
    offset whatever datum is chosen (no VERTCON, no chosen-datum treatment); a top outside the
    band but local (350 ft, outside ND's range) - its 0 - 300 ft values are tied by the DEM
    crest instead of dropped."""
    from nd_breach_runner.core import vdatum_run as vdrun
    from nd_breach_runner.core import workflow
    VD = _VD()
    off = 1630.49 - 120.7
    for datum in ("NAVD88", "NGVD29", "auto"):
        dam, st = _tied(pmf=1625.0, datum=datum)
        rec, changed = vdrun.apply_local(dam, st, 1630.49, workflow.MIDCREST_FROM)
        assert changed and "other_datum" not in rec, rec
        assert abs(dam.top_of_dam_ft - 1630.49) < 1e-9
        assert abs(dam.breach_bottom_ft - 1609.79) < 1e-6
        assert abs(rec["local_offset_ft"] - round(off, 3)) < 1e-9
        # the ND-range PMF pool is tied by the same offset (no split) - 1625 + 1509.79 =
        # 3134.79 ft, 1504.30 ft above the DEM crest: the 3.12.5 guard (Nick 2026-10-29)
        # flags it for review and does not use it (never kept as given / VERTCON-shifted)
        e = rec["fields"]["pmf_max_wse_ft"]
        assert not e.get("real") and e.get("implausible") and e["navd88"] is None, e
        assert abs(e["tied_ft"] - 3134.79) < 0.01 and abs(e["off_crest_ft"] - 1504.30) < 0.01, e
        assert dam.pmf_max_wse_ft is None and rec.get("review"), (datum, rec)
        txt = "\n".join(VD.summary_lines(VD.public(rec)))
        assert "not local" not in txt and "North Dakota's range" not in txt, txt
        assert not any("North Dakota's range" in w for w in rec["warnings"]), rec["warnings"]
    # top 350 ft (local: outside ND's range, not in the band), bottom 290 / spillway 297.7 (band)
    fl = _flood(None, 298.0, [297.7, 299.0], 340.0)
    dam, st = _tied(top=350.0, bottom=290.0, flood=fl)
    rec = st.vertical_datum
    assert not rec.get("local") and dam.breach_bottom_ft is None          # held, not shifted
    assert rec["fields"]["breach_bottom_ft"].get("pending_tie")
    rec, changed = vdrun.apply_local(dam, st, 1630.49, workflow.MIDCREST_FROM)
    off = 1630.49 - 350.0
    assert changed and rec["local"] and not rec.get("review"), rec
    assert abs(rec["local_offset_ft"] - off) < 1e-6
    assert abs(dam.top_of_dam_ft - 1630.49) < 1e-9
    assert abs(dam.breach_bottom_ft - (290.0 + off)) < 1e-6, dam.breach_bottom_ft
    assert abs(fl.trigger_value - (298.0 + off)) < 1e-6
    assert abs(fl.spillway.elev_m[0] / FT - (297.7 + off)) < 1e-6        # not shifted first
    assert abs(fl.outlets[0].invert_m / FT - (340.0 + off)) < 1e-6       # VERTCON undone
    assert not any(e.get("pending_tie") for e in rec["fields"].values())


@test
def test_local_band_no_local_crest_is_flagged_for_review():
    """A real top of dam (ND's range) with 0 - 300 ft values: no local crest to tie them by -
    flagged for review, left unconverted (the pools / bottom out of the model)."""
    from nd_breach_runner.core import vdatum_run as vdrun
    VD = _VD()
    fl = _flood(110.0, 1620.0, [117.7, 119.0], 1600.0)
    dam, st = _tied(top=1630.0, bottom=100.0, flood=fl)
    top_navd = dam.top_of_dam_ft
    assert top_navd > 1630.5 and dam.breach_bottom_ft is None and fl.normal_pool_ft is None
    rec, changed = vdrun.apply_local(dam, st, 1631.0)
    assert changed and rec.get("review") and "no local crest" in rec["review_reason"], rec
    f = rec["fields"]
    for k in ("breach_bottom_ft", "normal_pool_ft", "spillway_rating_first_row_ft"):
        assert f[k].get("unconverted") and f[k]["navd88"] is None, (k, f[k])
        assert "UNCONVERTED" in f[k]["note"]
    assert dam.top_of_dam_ft == top_navd and dam.breach_bottom_ft is None
    assert abs(fl.spillway.elev_m[0] / FT - 117.7) < 1e-9             # as given
    txt = "\n".join(VD.summary_lines(VD.public(rec)))
    assert "REVIEW" in txt and "UNCONVERTED" in txt, txt
    assert any(w.startswith("REVIEW") for w in rec["warnings"])
    # no top of dam at all, a band pool: no local crest either
    dam, st = _tied(top=None, bottom=None, flood=_flood(110.0, 118.0, [117.7, 119.0], 100.0))
    rec, changed = vdrun.apply_local(dam, st, 1630.49)
    assert changed and rec.get("review") and rec["local"], rec


@test
def test_local_band_no_tie_is_flagged_for_review():
    """No DEM crest, a DEM crest outside 500 - 3,600 ft, the x3.28 units guard, or no
    mid-crest (no dam line): never guessed - review flag, values unconverted."""
    from nd_breach_runner.core import vdatum_run as vdrun
    from nd_breach_runner.core import workflow
    cases = ((120.7, None, True, "", "no DEM crest"),
             (120.7, 4500.0, True, "", "outside North Dakota"),
             (250.0, 250.0 / FT, True, "", "feet / metres"),
             (120.7, 1630.49, False, "no dam line: the dam point is used as given", "no dam line"))
    for top, dem, ok, why, expect in cases:
        fl = _flood(110.0, 118.0, [117.7, 119.0], 100.0)
        dam, st = _tied(top=top, flood=fl, datum="NGVD29")
        rec, changed = vdrun.apply_local(dam, st, dem, workflow.MIDCREST_FROM, tie_ok=ok,
                                         no_tie=why)
        assert changed and rec.get("review") and expect in rec["review_reason"], (dem, rec)
        assert rec.get("local_offset_ft") is None
        for k in ("top_of_dam_ft", "breach_bottom_ft", "normal_pool_ft", "trigger_wse_ft",
                  "spillway_rating_first_row_ft", "outlet_low_invert_ft"):
            assert rec["fields"][k].get("unconverted"), (dem, k, rec["fields"][k])
        assert dam.top_of_dam_ft is None and dam.breach_bottom_ft is None
        assert fl.trigger_value == 118.0 and abs(fl.spillway.elev_m[0] / FT - 117.7) < 1e-9
        assert not vdrun.apply_local(dam, st, 1630.49)[1]          # once
    # a band record whose tie never ran (e.g. a proposed dam): finish_band flags it
    dam, st = _tied()
    assert vdrun.finish_band(dam, st, "proposed dam: no local-datum tie")
    assert st.vertical_datum["review"] and \
        st.vertical_datum["fields"]["top_of_dam_ft"].get("unconverted")
    # a tied record is left alone
    dam, st = _tied()
    vdrun.apply_local(dam, st, 1630.49)
    assert not vdrun.finish_band(dam, st) and not st.vertical_datum.get("review")


@test
def test_local_band_culvert_inverts():
    """Inverts between 0 and 300 ft: tied to the LiDAR bed (one offset when both ends agree
    within 1 ft), logged; NDDOT inverts in the normal range: no datum conversion, as before."""
    from nd_breach_runner.core import structures as structmod
    from nd_breach_runner.core.grid import Grid
    bed = 1600.0 * FT

    def read(bounds, cell):
        x0, y0, x1, y1 = bounds
        nx, ny = int(np.ceil((x1 - x0) / cell)) + 1, int(np.ceil((y1 - y0) / cell)) + 1
        return Grid(np.full((ny, nx), bed), x0, y1, cell)

    def culv(up_ft, dn_ft):
        return structmod.Structure(kind=structmod.KIND_CULVERT,
                                   line=np.array([[0.0, 0.0], [20.0, 0.0]]),
                                   invert_up_m=up_ft * FT, invert_dn_m=dn_ft * FT, name="c")
    s = culv(100.0, 99.5)
    notes = structmod.reconcile_inverts([s], read, 3.0 * FT, 1.0)
    assert s.invert_local and abs(s.invert_local["offset_ft"] - 1500.25) < 1e-6, s.invert_local
    assert abs(s.invert_up_m / FT - 1600.25) < 1e-6 and abs(s.invert_dn_m / FT - 1599.75) < 1e-6
    assert s.invert_local["source_ft"] == [100.0, 99.5] and "DEM-derived" in s.invert_local["method"]
    assert "0-300 ft" in notes[0], notes
    s = culv(100.0, 120.0)                  # ends disagree: the LiDAR bed at each end
    structmod.reconcile_inverts([s], read, 3.0 * FT, 1.0)
    assert s.invert_up_m is None and s.invert_dn_m is None and s.invert_local["offset_ft"] is None
    assert abs(s.invert_local["navd88_ft"][0] - 1600.0) < 1e-6
    s = culv(1600.5, 1600.0)                # normal range: used as given, no conversion
    structmod.reconcile_inverts([s], read, 3.0 * FT, 1.0)
    assert s.invert_local is None and abs(s.invert_up_m / FT - 1600.5) < 1e-6
    # into the datum record and its summary
    VD = _VD()
    rec = VD.plan(LON, LAT, "", VD.AUTO, use_pyproj=False)
    rec["structures_local"] = [culv(100.0, 99.5).invert_local or {
        "name": "c", "source_ft": [100.0, 99.5], "navd88_ft": [1600.25, 1599.75],
        "offset_ft": 1500.25, "method": "DEM-derived"}]
    txt = "\n".join(VD.summary_lines(VD.public(rec)))
    assert "local inverts 100.00 / 99.50 ft -> 1600.25 / 1599.75 ft NAVD88" in txt, txt


@test
def test_local_tie_guard_100ft_from_crest():
    """3.12.5 plausibility guard (Nick 2026-10-29, "change them"): a tied value more than
    TIE_MAX_FROM_CREST_FT (100 ft) above or below the DEM crest is not used - REVIEW, left
    unconverted, named in a warning with how far off it is. Kentner DWR-11011: streambed 845 ->
    2134.33 (+745 ft); Foxholm DWR-7592: bottom 100 -> 732.09 (about 966 ft below)."""
    from nd_breach_runner.core import vdatum_run as vdrun
    from nd_breach_runner.core import workflow
    VD = _VD()
    assert VD.TIE_MAX_FROM_CREST_FT == 100.0
    # Kentner-like: top 100 (band), streambed 845, DEM crest 1389.33 -> offset +1289.33
    fl = _flood(None, 845.0, [845.0, 846.0], 845.0)
    dam, st = _tied(top=100.0, bottom=845.0, flood=fl)
    rec, changed = vdrun.apply_local(dam, st, 1389.33, workflow.MIDCREST_FROM)
    assert changed and rec.get("review") and abs(rec["local_offset_ft"] - 1289.33) < 1e-6, rec
    assert abs(dam.top_of_dam_ft - 1389.33) < 1e-9                  # the crest: used
    e = rec["fields"]["breach_bottom_ft"]
    assert e.get("implausible") and e.get("unconverted") and e["navd88"] is None, e
    assert abs(e["tied_ft"] - 2134.33) < 1e-6 and abs(e["off_crest_ft"] - 745.0) < 1e-6, e
    assert "745.00 ft above" in e["note"] and "UNCONVERTED" in e["note"], e
    assert dam.breach_bottom_ft is None                              # not used
    # the trigger, the spillway rating and the outlet tied as far off: not used either
    assert fl.trigger == "peak" and fl.trigger_value is None
    assert fl.spillway is None and fl.outlets == []
    for k in ("trigger_wse_ft", "spillway_rating_first_row_ft", "outlet_low_invert_ft"):
        assert rec["fields"][k].get("implausible"), (k, rec["fields"][k])
    w = [x for x in rec["warnings"] if x.startswith("REVIEW") and "100 ft from the DEM" in x]
    assert len(w) == 1, rec["warnings"]
    assert "breach_bottom_ft 845.00 -> 2134.33 ft NAVD88 is 745.00 ft above" in w[0], w
    assert "peak pool" in w[0] and "spillway rating is left out" in w[0], w
    assert "breach_bottom_ft" in rec["review_reason"], rec["review_reason"]
    txt = "\n".join(VD.summary_lines(VD.public(rec)))
    assert "REVIEW" in txt and "not used" in txt and "no DEM tie" not in txt, txt
    assert "breach_bottom_ft: 845.00 local -> UNCONVERTED" in txt, txt
    # Foxholm-like: stated Local, top 1066 / bottom 100, DEM crest 1698.09 -> offset +632.09
    dam = _dam(top=1066.0, bottom=100.0)
    st = _st(elev_datum_stated="Local")
    vdrun.apply(dam, st, LON, LAT)
    rec, changed = vdrun.apply_local(dam, st, 1698.09, workflow.MIDCREST_FROM)
    e = rec["fields"]["breach_bottom_ft"]
    assert changed and rec.get("review") and e.get("implausible"), rec
    assert abs(e["tied_ft"] - 732.09) < 1e-6 and abs(e["off_crest_ft"] + 966.0) < 1e-6, e
    assert dam.breach_bottom_ft is None and "966.00 ft below" in e["note"], e
    assert any("966.00 ft below" in x for x in rec["warnings"]), rec["warnings"]
    # within 100 ft (above or below): tied and used, no review
    for bottom, crest in ((100.0, 199.9 + 1530.0), (100.0, 1530.0 - 0.1)):
        dam, st = _tied(top=200.0 if crest > 1600 else 0.0, bottom=bottom)
        rec, _c = vdrun.apply_local(dam, st, crest, workflow.MIDCREST_FROM)
        d = rec["fields"]["breach_bottom_ft"]
        assert not rec.get("review") and not d.get("implausible"), (bottom, crest, rec)
        assert abs(dam.breach_bottom_ft - crest) <= 100.0 + 1e-9


@test
def test_midcrest_edge_pick_flagged():
    """3.12.5 (Nick 2026-10-29): the highest point at the edge of the +-25 m profile (Foxholm
    +25.0 m) - the pick is kept (method unchanged) but flagged REVIEW."""
    MC = _MC()
    xs = np.arange(-10, 110) + 0.5
    ys = 50.0 - (np.arange(100) + 0.5)
    X, Y = np.meshgrid(xs, ys)
    gt = (-10.0, 1.0, 0.0, 50.0, 0.0, -1.0)
    z = 10.0 + 0.1 * Y                       # rising across the profile: max at +25 m
    rec = MC.midcrest([(0.0, 0.0), (100.0, 0.0)], MC.bilinear(z, gt))
    assert rec["at_edge"] and abs(rec["offset_m"] - 25.0) < 1e-6, rec
    assert "edge of the +-25 m profile" in rec["review"] and "+25.0 m" in rec["review"], rec
    line = MC.summary_line(rec)
    assert "REVIEW" in line and "may be unreliable" in line, line
    rec = MC.midcrest([(0.0, 0.0), (100.0, 0.0)], MC.bilinear(10.0 - 0.1 * Y, gt))
    assert rec["at_edge"] and rec["offset_m"] == -25.0, rec                 # the other edge
    z, gt = _ridge_grid()                    # a crest inside the profile: no flag
    rec = MC.midcrest([(0.0, 0.0), (100.0, 0.0)], MC.bilinear(z, gt))
    assert not rec["at_edge"] and "review" not in rec and "REVIEW" not in MC.summary_line(rec)
    # the Runner carries the flag (run_dam adds a REVIEW warning and report["crest_pick_review"])
    import inspect
    from nd_breach_runner.core import workflow
    src = inspect.getsource(workflow.run_dam)
    assert 'report["crest_pick_review"]' in src and '"REVIEW: Dam point: "' in src


@test
def test_local_datum_messages():
    """3.12.5 (Nick 2026-10-29) log / message fixes: no empty "(0 - 300 ft rule: )" for a
    stated Local datum; no "100.00 local -> 100.00 NAVD88 (DEM-derived)" before the tie; no
    near-duplicate local warnings; a stated Local under ELEV_DATUM NAVD88 / NGVD29 is
    "local (stated ...)", not "local (assumed) (DETECTED ... given as 'Local')"."""
    from nd_breach_runner.core import vdatum_run as vdrun
    from nd_breach_runner.core import workflow
    VD = _VD()
    for datum in ("auto", "NAVD88", "NGVD29"):
        dam = _dam(top=100.0, bottom=90.0)
        st = _st(elev_datum_stated="Local", elev_datum=datum)
        logged = []
        rec = vdrun.apply(dam, st, LON, LAT, log=logged.append)
        pre = "\n".join(VD.summary_lines(rec))
        # (2) before the tie: no "-> ... NAVD88" for the local values
        assert "100.00 local -> 100.00 NAVD88" not in pre and "(DEM-derived" not in pre, pre
        assert "waiting for the DEM tie" in pre and "to be converted to NAVD88" in pre, pre
        assert not any("local -> 100.00 NAVD88" in m for m in logged), logged
        # (4) the source line
        src = pre.splitlines()[0]
        assert src.startswith("Source datum: local (stated"), src
        assert "assumed" not in src and "DETECTED" not in src and "detected" not in src, src
        if datum != "auto":
            assert "ELEV_DATUM {} not applied".format(datum) in src, src
        run_w = list(rec["warnings"])          # what run_dam holds before the tie
        pre_tie = list(rec["warnings"])
        rec, changed = vdrun.apply_local(dam, st, DEM_CREST_T, workflow.MIDCREST_FROM)
        assert changed and not rec.get("review"), rec
        # (1) the rule text is never empty
        assert not any("rule: )" in w for w in rec["warnings"]), rec["warnings"]
        assert any("0 - 300 ft rule: the top of dam 100.00 ft is between 0 and 300" in w
                   for w in rec["warnings"]), rec["warnings"]
        # (3) one local-datum warning, in the record and in the run's list
        loc = [w for w in rec["warnings"] if "local" in w.lower()]
        assert len(loc) == 1 and loc[0].startswith("The dam elevations are on"), loc
        vdrun.merge_tie_warnings(run_w, pre_tie, rec["warnings"])
        assert [w for w in run_w if "local" in w.lower()] == loc, run_w
        post = "\n".join(VD.summary_lines(VD.public(rec)))
        assert "100.00 local -> {:.2f} NAVD88".format(DEM_CREST_T) in post, post
        assert "waiting" not in post and post.splitlines()[0] == src, post
    # detected (no stated datum): short source line, the reason on the rule line only
    dam, st = _tied(datum="NAVD88")
    txt = "\n".join(VD.summary_lines(st.vertical_datum))
    first = txt.splitlines()[0]
    assert first.startswith("Source datum: local (assumed) (detected from the values by the "
                            "0 - 300 ft rule"), first
    assert "between 0 and 300" not in first and "Local datum rule: the top of dam" in txt, txt


DEM_CREST_T = 1072.60


# --- 3.12.5 (Nick 2026-10-29, "in that case, use dem elevations"): a real top of dam with
# 0 - 300 ft values (Thompson Lake DWR-5930: NAVD 88, top 1718, streambed / outlet 100 ft)
def _toe_grid(crest_m=1718.0 * FT, pool_m=1713.7 * FT, ds_m=1700.0 * FT, thalweg_m=1695.0 * FT):
    """1 m grid, x -60..160, y -80..80: the dam line along y = 0 (x 0..100), the embankment
    |y| <= 6 m at the crest, the pool (y > 6) at the normal pool, the downstream ground (y < -6)
    with a channel at x 49..51 (the thalweg) and a deep pit at x = 150 (past the line's end)."""
    xs = np.arange(-60, 160) + 0.5
    ys = 80.0 - (np.arange(160) + 0.5)
    X, Y = np.meshgrid(xs, ys)
    z = np.where(Y > 6.0, pool_m, ds_m)
    z = np.where(np.abs(Y) <= 6.0, crest_m, z)
    z = np.where((Y < -6.0) & (np.abs(X - 50.0) <= 1.0), thalweg_m, z)
    z = np.where((Y < -6.0) & (np.abs(X - 150.0) <= 1.0), 1500.0 * FT, z)
    return z, (-60.0, 1.0, 0.0, 80.0, 0.0, -1.0)


def _toe_read(z=None):
    from nd_breach_runner.core.grid import Grid
    if z is None:
        z, _gt = _toe_grid()

    def read(bounds, cell):                 # the synthetic grid whatever the bounds
        return Grid(z.copy(), -60.0, 80.0, 1.0)
    return read


_LINE = [(0.0, 0.0), (100.0, 0.0)]


@test
def test_dem_toe_low_point():
    from nd_breach_runner.core import vdatum_run as vdrun
    dam = _dam(top=1718.0)
    toe = vdrun.toe_offset_m(dam)                 # 3H:1V x 20 ft + 10 ft = 70 ft
    assert abs(toe - 70.0 * FT) < 1e-9, toe
    v, how = vdrun.dem_toe_low(_toe_read(), (50.0, 0.0), _LINE, dam, 1.0)
    assert abs(v - 1695.0) < 1e-6 and "thalweg" in how and "downstream toe" in how, (v, how)
    # beside the line only: the pit past its end (x = 150) is not the toe
    # no line: a ring around the dam point (toe .. toe + 30 m)
    v2, how2 = vdrun.dem_toe_low(_toe_read(), (50.0, 0.0), None, dam, 1.0)
    assert abs(v2 - 1695.0) < 1e-6 and "no dam line" in how2, (v2, how2)
    # no DEM data / no point
    nan = np.full((160, 220), np.nan)
    assert vdrun.dem_toe_low(_toe_read(nan), (50.0, 0.0), _LINE, dam, 1.0)[0] is None
    assert vdrun.dem_toe_low(_toe_read(), None, None, dam, 1.0)[0] is None


@test
def test_band_values_with_real_top_use_dem_elevations():
    """Thompson Lake-like (DWR-5930): NAVD 88, top 1718, streambed 100, outlet invert 100 ft -
    no local crest to tie them by: replaced by the DEM low point at the downstream toe
    ("DEM-derived"), a warning names the value given; not used as given, no review flag."""
    from nd_breach_runner.core import vdatum_run as vdrun
    from nd_breach_runner.core import workflow
    VD = _VD()
    for datum in ("NAVD88", "auto"):
        fl = _flood(1713.7, 1716.5, [1716.5, 1718.0], 100.0)
        dam, st = _tied(top=1718.0, bottom=100.0, flood=fl, datum=datum)
        top0, pool0 = dam.top_of_dam_ft, fl.normal_pool_ft    # auto: NGVD29 +1.09 ft
        assert dam.breach_bottom_ft is None and abs(fl.outlets[0].invert_m / FT - 100.0) < 1e-9
        calls = []

        def low():
            calls.append(1)
            return vdrun.dem_toe_low(_toe_read(), (50.0, 0.0), _LINE, dam, 1.0)
        rec, changed = vdrun.apply_local(dam, st, 1716.9, workflow.MIDCREST_FROM, dem_low=low)
        assert changed and calls == [1] and not rec.get("review"), rec
        assert rec["band_dem"] and abs(rec["dem_low_ft"] - 1695.0) < 1e-3, rec
        assert dam.top_of_dam_ft == top0                       # the real top is kept
        assert abs(dam.breach_bottom_ft - 1695.0) < 1e-6, dam.breach_bottom_ft
        assert abs(fl.outlets[0].invert_m / FT - 1695.0) < 1e-6
        assert fl.normal_pool_ft == pool0 and fl.trigger == "wse"      # not in the band
        for k in ("breach_bottom_ft", "outlet_low_invert_ft"):
            e = rec["fields"][k]
            assert e["dem_derived"] and e["how"].startswith("DEM-derived") and \
                abs(e["navd88"] - 1695.0) < 1e-3 and e["source"] == 100.0, e
            assert not e.get("unconverted") and not e.get("pending_tie")
        w = [x for x in rec["warnings"] if "replaced by DEM elevations" in x]
        assert len(w) == 1 and "breach_bottom_ft 100.00 ft -> 1695.00 ft NAVD88" in w[0], w
        assert "outlet_low_invert_ft 100.00 ft" in w[0] and "NOT used as given" in w[0], w
        txt = "\n".join(VD.summary_lines(VD.public(rec)))
        assert "breach_bottom_ft: 100.00 (0-300 ft, not used) -> 1695.00 NAVD88 (DEM-derived" \
            in txt, txt
        assert "REVIEW" not in txt and "UNCONVERTED" not in txt, txt
        assert not vdrun.apply_local(dam, st, 1716.9, dem_low=low)[1] and calls == [1]  # once
        assert not vdrun.finish_band(dam, st)


@test
def test_band_pools_with_real_top_routing_or_review():
    """Pools / trigger / rating in the band with a real top: the PMF and trigger pool from the
    routing, the normal pool from a usable spillway rating; a band rating (no DEM source) and a
    normal pool without a usable rating are flagged for review and not used."""
    from nd_breach_runner.core import vdatum_run as vdrun
    low = (1695.0, "DEM low point (test)")
    # every value in the band but the top
    fl = _flood(110.0, 120.0, [117.0, 119.0], 100.0)
    dam, st = _tied(top=1718.0, bottom=100.0, pmf=150.0, flood=fl, datum="NAVD88")
    rec, changed = vdrun.apply_local(dam, st, 1716.9, dem_low=low)
    f = rec["fields"]
    assert changed and rec.get("review"), rec
    assert abs(dam.breach_bottom_ft - 1695.0) < 1e-9 and f["breach_bottom_ft"]["dem_derived"]
    assert abs(fl.outlets[0].invert_m / FT - 1695.0) < 1e-9
    assert dam.pmf_max_wse_ft is None and f["pmf_max_wse_ft"]["how"] == "routing"
    assert fl.trigger == "peak" and fl.trigger_value is None
    assert f["trigger_wse_ft"]["how"] == "routing" and not f["trigger_wse_ft"].get("unconverted")
    assert fl.spillway is None and f["spillway_rating_first_row_ft"]["unconverted"]
    assert fl.normal_pool_ft is None and f["normal_pool_ft"]["unconverted"]
    assert "spillway_rating_first_row_ft" in rec["review_reason"]
    assert "normal_pool_ft" in rec["review_reason"]
    assert any(w.startswith("REVIEW") and "spillway_rating_first_row_ft 117.00 ft" in w
               for w in rec["warnings"]), rec["warnings"]
    # a band normal pool with a usable (real) rating: the rating's crest, no review
    fl = _flood(110.0, 1716.5, [1716.5, 1718.0], 1700.0)
    dam, st = _tied(top=1718.0, bottom=100.0, flood=fl, datum="NAVD88")
    rec, changed = vdrun.apply_local(dam, st, 1716.9, dem_low=low)
    assert changed and not rec.get("review"), rec
    assert fl.normal_pool_ft is None and rec["fields"]["normal_pool_ft"]["how"] == \
        "spillway rating"
    assert "spillway rating's crest (1716.50 ft)" in "\n".join(rec["warnings"])
    assert abs(fl.outlets[0].invert_m / FT - 1700.0) < 1e-9          # not in the band
    assert abs(fl.spillway.elev_m[0] / FT - 1716.5) < 1e-9


@test
def test_band_dem_values_guarded_or_review():
    """The 100 ft guard and the below-the-top check apply to the DEM low point too; no DEM low
    point or no dem_low: flagged for review, not used (an outlet left out)."""
    from nd_breach_runner.core import vdatum_run as vdrun
    for low, expect in (((1600.0, "x"), "more than 100 ft"),
                        ((1720.0, "x"), "not below the top of dam"),
                        ((None, "no DEM data at the toe of the dam line"), "no DEM low point")):
        fl = _flood(1713.7, 1716.5, [1716.5, 1718.0], 100.0)
        dam, st = _tied(top=1718.0, bottom=100.0, flood=fl, datum="NAVD88")
        rec, changed = vdrun.apply_local(dam, st, 1716.9, dem_low=low)
        assert changed and rec.get("review"), (low, rec)
        for k in ("breach_bottom_ft", "outlet_low_invert_ft"):
            e = rec["fields"][k]
            assert e.get("unconverted") and e["navd88"] is None and expect in e["note"], (k, e)
        assert dam.breach_bottom_ft is None and fl.outlets == [], (low, fl.outlets)
        assert any(w.startswith("REVIEW") and expect in w for w in rec["warnings"])
    # no DEM crest: the old review path (never guessed)
    fl = _flood(1713.7, 1716.5, [1716.5, 1718.0], 100.0)
    dam, st = _tied(top=1718.0, bottom=100.0, flood=fl, datum="NAVD88")
    rec, changed = vdrun.apply_local(dam, st, None, dem_low=(1695.0, "x"))
    assert changed and rec.get("review") and not rec.get("band_dem"), rec
    assert rec["fields"]["breach_bottom_ft"].get("unconverted")
