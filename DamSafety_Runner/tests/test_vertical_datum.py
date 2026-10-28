"""3.12.0: NGVD29 -> NAVD88 (VERTCON) of the dam elevations before they meet the NAVD88 DEM
(damsuite/vdatum.py, core/vdatum_run.py). No QGIS, no model runs.

Reference (DWR-2801, lat 46.983031 lon -97.391489): NOAA NCAT (VERTCON) gives +0.332 m
(+1.09 ft): 1071.150 ft NGVD29 -> 1072.24 ft NAVD88. The bundled grid (NOAA vertconc.gtx,
the grid PROJ ships as us_noaa_vertconc.tif) gives 0.3318 m there: within 0.02 ft."""
import copy
import os
import tempfile
import types

from nd_breach_runner.tests.registry import test

LON, LAT = -97.391489, 46.983031


def _VD():
    from nd_breach_runner.damsuite import vdatum
    return vdatum


def _dam(top=1071.15, pmf=None, bottom=None):
    from nd_breach_runner.core import breach as brmod
    return brmod.DamData(dam_id="DWR-2801", dam_height_ft=20.0, storage_top_acft=500.0,
                         top_of_dam_ft=top, surface_area_top_ac=60.0, pmf_max_wse_ft=pmf,
                         breach_bottom_ft=bottom)


def _st(**kw):
    from nd_breach_runner.core import workflow
    st = workflow.RunSettings()
    for k, v in kw.items():
        setattr(st, k, v)
    return st


@test
def test_vdatum_dwr2801_reference():
    VD = _VD()
    sm, method = VD.shift_m(LON, LAT, use_pyproj=False)
    assert sm is not None and "bundled" in method, method
    assert abs(sm - 0.332) < 0.002, sm
    rec = VD.plan(LON, LAT, "", VD.AUTO, use_pyproj=False)
    assert rec["source_datum"] == VD.NGVD29 and rec["how"] == "assumed" and rec["applied"]
    out = VD.convert_ft(1071.15, rec, "top_of_dam_ft")
    assert abs(out - 1072.24) <= 0.02, out
    assert any("assumed" in w for w in rec["warnings"]), rec["warnings"]
    # stated NGVD29: converted, no "assumed" warning
    rec = VD.plan(LON, LAT, "NGVD 1929 ft", VD.AUTO, use_pyproj=False)
    assert rec["how"] == "stated" and not any("assumed" in w for w in rec["warnings"])
    assert abs(VD.convert_ft(1071.15, rec) - 1072.24) <= 0.02


@test
def test_vdatum_navd88_unchanged():
    VD = _VD()
    for stated, override in (("NAVD88", VD.AUTO), ("NAVD 1988 ft", VD.AUTO), ("DEM", VD.AUTO),
                             ("", VD.NAVD88), ("NGVD29", VD.NAVD88)):
        rec = VD.plan(LON, LAT, stated, override, use_pyproj=False)
        assert rec["source_datum"] == VD.NAVD88 and not rec["applied"], (stated, rec)
        assert VD.convert_ft(1071.15, rec) == 1071.15
    rec = VD.plan(LON, LAT, "NAVD88", VD.NGVD29, use_pyproj=False)     # the override wins
    assert rec["applied"] and rec["how"] == "override"


@test
def test_vdatum_outside_grid_warns():
    VD = _VD()
    rec = VD.plan(-80.0, 30.0, "", VD.AUTO, use_pyproj=False)
    assert not rec["applied"] and rec["warnings"]
    assert VD.convert_ft(100.0, rec) == 100.0


@test
def test_vdatum_apply_once_through_copies():
    from nd_breach_runner.core import vdatum_run as vdrun
    dam = _dam(top=1071.15, pmf=1070.0, bottom=1052.0)
    st = _st()
    rec = vdrun.apply(dam, st, LON, LAT)
    assert abs(dam.top_of_dam_ft - 1072.24) <= 0.02 and abs(dam.pmf_max_wse_ft - 1071.09) <= 0.02
    assert st.vertical_datum is rec and set(rec["fields"]) == {"top_of_dam_ft", "pmf_max_wse_ft",
                                                               "breach_bottom_ft"}
    top = dam.top_of_dam_ft
    # the same dam again, a shallow copy of the settings (sensitivity / refine runs): no change
    st2 = copy.copy(st)
    vdrun.apply(dam, st2, LON, LAT)
    vdrun.apply(copy.copy(dam), st2, LON, LAT)
    assert dam.top_of_dam_ft == top
    # a fresh DamData with the source value under the same record: the recorded result
    d2 = _dam(top=1071.15)
    vdrun.apply(d2, st2, LON, LAT)
    assert abs(d2.top_of_dam_ft - top) < 1e-9
    # NAVD88 stated: unchanged
    d3 = _dam(top=1071.15)
    st3 = _st(elev_datum_stated="NAVD88 ft")
    r3 = vdrun.apply(d3, st3, LON, LAT)
    assert d3.top_of_dam_ft == 1071.15 and not r3["applied"]


@test
def test_vdatum_dem_top_no_warning():
    from nd_breach_runner.core import vdatum_run as vdrun
    dam = _dam(top=None)
    rec = vdrun.apply(dam, _st(), LON, LAT, top_is_dem=True)
    assert not rec["applied"] and not rec["warnings"] and dam.top_of_dam_ft is None


@test
def test_vdatum_flood_inputs_shifted():
    import numpy as np
    from nd_breach_runner.core import vdatum_run as vdrun
    fl = types.SimpleNamespace(normal_pool_ft=1065.0, trigger="wse", trigger_value=1070.0,
                               spillway=types.SimpleNamespace(elev_m=np.array([1065.0, 1070.0]) * 0.3048),
                               outlets=[])
    st = _st(flood=fl)
    rec = vdrun.apply(_dam(), st, LON, LAT)
    assert abs(fl.normal_pool_ft - 1066.09) <= 0.02 and abs(fl.trigger_value - 1071.09) <= 0.02
    assert abs(fl.spillway.elev_m[0] / 0.3048 - 1066.09) <= 0.02
    pool = fl.normal_pool_ft
    vdrun.apply(_dam(), copy.copy(st), LON, LAT)               # shared flood inputs: once
    assert fl.normal_pool_ft == pool and "normal_pool_ft" in rec["fields"]


@test
def test_vdatum_cascade_dam_stated():
    from nd_breach_runner.core import vdatum_run as vdrun
    dam = _dam(top=1071.15)
    dam.elev_datum_stated = "NAVD88"
    rec = vdrun.apply(dam, _st(elev_datum_stated="NGVD29"), LON, LAT)
    assert dam.top_of_dam_ft == 1071.15 and rec["source_datum"] == "NAVD88"


@test
def test_vdatum_record_and_report():
    from nd_breach_runner.core import vdatum_run as vdrun
    from nd_breach_runner.damsuite import dam_record as DR
    st = _st()
    rec = vdrun.apply(_dam(), st, LON, LAT)
    pub = vdrun.VD.public(rec)
    assert pub["source_datum"] == "NGVD29" and abs(pub["shift_ft"] - 1.09) <= 0.01
    assert not any(k.startswith("_") for k in pub)
    text = "\n".join(vdrun.report_lines(pub))
    assert "NAVD88" in text and "assumed" in text.lower() and "VERTCON" in text
    vals, note = vdrun.damjson_values(rec, {"top_of_dam_ft": 1072.24}, None)
    assert vals.get("top_of_dam_datum") == "NAVD88 ft" and not note
    # dam.json already holds a top: the converted value is not written over it
    d = tempfile.mkdtemp(prefix="ndbr_vd_")
    r = DR.DamRecord(d)
    r.set("top_of_dam_ft", 1071.15, "dwr", "1")
    vals, note = vdrun.damjson_values(rec, {"top_of_dam_ft": 1072.24}, r)
    assert "top_of_dam_ft" not in vals and note
    # an earlier Runner top without a datum is read as the DEM's
    r2 = DR.DamRecord(d)
    r2.set("top_of_dam_ft", 1071.15, "ndbreach", "3.11")
    stated, n2 = vdrun.stated_from_record(r2)
    assert stated == "DEM" and n2
    r.set("top_of_dam_datum", "NGVD29 ft", "dwr", "1")
    assert vdrun.stated_from_record(r)[0] == "NGVD29 ft"


@test
def test_vdatum_window_line():
    from nd_breach_runner import popup_logic as pl
    line = pl.datum_info("", "auto")
    assert "NGVD29 assumed" in line, line
    assert "NAVD88" in pl.datum_info("", "NAVD88")
    p = pl.defaults("rundam", "/tmp/x", "D1", {"elev_datum": "NGVD29"})
    assert p.get("ELEV_DATUM") == 2
    assert "ELEV_DATUM" not in pl.defaults("rundam", "/tmp/x", "D1", {})


# --- 3.12.1 / damsuite 1.18.1: local (assumed) datums: elevations taken from the DEM ---------------
# Nick 2026-10-28: "be assumed ngvd29, if elevation is 100 or similar, they would be local and
# would have to take actual elevations from dem"
DEM_CREST = 1072.60          # ft NAVD88, a DEM crest near DWR-2801


def _flood(pool, trig, spill_ft, inv_ft):
    import numpy as np
    from nd_breach_runner.core import outlets as outmod
    return types.SimpleNamespace(
        normal_pool_ft=pool, trigger="wse", trigger_value=trig,
        spillway=types.SimpleNamespace(elev_m=np.array(spill_ft) * 0.3048),
        outlets=[outmod.Conduit("low", inv_ft * 0.3048, 0.6, 0.6)])


@test
def test_vdatum_local_check_thresholds():
    VD = _VD()
    assert VD.local_check(100.0)[0]                          # outside ND's range, no DEM needed
    assert VD.local_check(4000.0)[0]
    assert not VD.local_check(1071.15)[0]                    # plausible, no DEM to compare
    assert not VD.local_check(1071.15, DEM_CREST, 1.09)[0]   # NGVD29 value next to the DEM
    assert VD.local_check(1000.0, DEM_CREST, 1.09)[0]        # round, 71 ft off
    assert VD.local_check(1100.0, DEM_CREST, 1.09)[0]        # round, 28 ft off (> 10)
    assert not VD.local_check(1070.0, DEM_CREST, 1.09)[0]    # a 10 ft multiple is not "round"
    hit, why = VD.local_check(1140.0, DEM_CREST, 1.09)       # 68.5 ft from the DEM
    assert hit and "DEM crest" in why, why
    assert VD.mode("local") == VD.LOCAL and VD.mode(3) == VD.LOCAL
    assert VD.resolve("", "local") == (VD.LOCAL, "override")


@test
def test_vdatum_local_top_100_from_dem_relative_kept():
    """top 100 ft (local): not converted; the top becomes the DEM crest and the pool,
    trigger, spillway and outlet keep their height below the local top."""
    from nd_breach_runner.core import vdatum_run as vdrun
    dam = _dam(top=100.0, pmf=99.0, bottom=82.0)
    fl = _flood(95.0, 98.0, [95.0, 97.5], 84.0)
    st = _st(flood=fl)
    vdrun.apply(dam, st, LON, LAT)                     # auto: NGVD29 assumed, shifted first
    rec, changed = vdrun.apply_local(dam, st, DEM_CREST, "DEM near the dam point")
    assert changed and rec["local"] and rec["source_datum"] == "local (assumed)", rec
    assert rec["how"] == "detected" and rec["method"] == "DEM" and not rec["applied"]
    assert abs(dam.top_of_dam_ft - DEM_CREST) < 1e-9
    off = DEM_CREST - 100.0
    assert abs(dam.pmf_max_wse_ft - (99.0 + off)) < 1e-6
    assert abs(dam.breach_bottom_ft - (82.0 + off)) < 1e-6
    assert abs(fl.normal_pool_ft - (DEM_CREST - 5.0)) < 1e-6
    assert abs(fl.trigger_value - (DEM_CREST - 2.0)) < 1e-6
    assert abs(fl.spillway.elev_m[0] / 0.3048 - (DEM_CREST - 5.0)) < 1e-6
    assert abs(fl.spillway.elev_m[1] / 0.3048 - (DEM_CREST - 2.5)) < 1e-6
    assert abs(fl.outlets[0].invert_m / 0.3048 - (DEM_CREST - 16.0)) < 1e-6
    f = rec["fields"]
    assert f["top_of_dam_ft"]["source"] == 100.0 and f["normal_pool_ft"]["source"] == 95.0
    assert abs(f["top_of_dam_ft"]["navd88"] - DEM_CREST) < 1e-3
    assert not any("NGVD29 was assumed" in w for w in rec["warnings"]), rec["warnings"]
    assert any("local datum" in w for w in rec["warnings"]), rec["warnings"]
    # once: a second call changes nothing
    assert not vdrun.apply_local(dam, st, DEM_CREST + 5.0)[1]
    assert abs(dam.top_of_dam_ft - DEM_CREST) < 1e-9
    # the report / Hazard summary lines and the JSON record
    VD = _VD()
    txt = "\n".join(VD.summary_lines(VD.public(rec)))
    assert "local (assumed)" in txt and "100.00 local -> 1072.60 NAVD88 (DEM)" in txt, txt
    assert "WARNING" in txt
    import json
    json.dumps(VD.public(rec))
    out, _n = vdrun.damjson_values(rec, {"top_of_dam_ft": dam.top_of_dam_ft})
    assert "top_of_dam_datum" not in out          # dam.json keeps the plans' local values


@test
def test_vdatum_local_normal_value_still_converted():
    from nd_breach_runner.core import vdatum_run as vdrun
    dam = _dam(top=1071.15)
    st = _st()
    vdrun.apply(dam, st, LON, LAT)
    rec, changed = vdrun.apply_local(dam, st, DEM_CREST)
    assert not changed and rec["applied"] and not rec.get("local")
    assert abs(dam.top_of_dam_ft - 1072.24) <= 0.02, dam.top_of_dam_ft


@test
def test_vdatum_local_not_applied_on_a_dem_in_the_wrong_units():
    """A DEM in feet read as metres (x 3.28) or outside ND's ground is not taken as the
    top of dam: the local step does nothing and the run's unit check reports it
    (run_tests.wrong_dem_units_are_caught)."""
    from nd_breach_runner.core import vdatum_run as vdrun
    for top, dem_ft in ((1850.0, 1850.0 / 0.3048), (900.0, 900.0 / 0.3048), (100.0, 4500.0)):
        dam = _dam(top=top)
        st = _st()
        vdrun.apply(dam, st, LON, LAT)
        before = dam.top_of_dam_ft
        rec, changed = vdrun.apply_local(dam, st, dem_ft)
        assert not changed and not rec.get("local"), (top, dem_ft, rec)
        assert dam.top_of_dam_ft == before


@test
def test_vdatum_local_override_and_explicit_datum():
    from nd_breach_runner.core import vdatum_run as vdrun
    # "Local (use DEM)" chosen: a plausible-looking value is still taken from the DEM
    dam = _dam(top=1060.0, pmf=1058.0)
    st = _st(elev_datum="local")
    rec = vdrun.apply(dam, st, LON, LAT)
    assert rec["local"] and not rec["applied"] and dam.top_of_dam_ft == 1060.0
    rec, changed = vdrun.apply_local(dam, st, DEM_CREST)
    assert changed and rec["source_datum"] == "local" and rec["how"] == "override"
    assert abs(dam.top_of_dam_ft - DEM_CREST) < 1e-9
    assert abs(dam.pmf_max_wse_ft - (DEM_CREST - 2.0)) < 1e-6
    # NGVD29 chosen explicitly: no detection, even for 100 ft
    dam = _dam(top=100.0)
    st = _st(elev_datum="NGVD29")
    vdrun.apply(dam, st, LON, LAT)
    assert not vdrun.apply_local(dam, st, DEM_CREST)[1]
    # stated "local" in dam.json / the inventory
    dam = _dam(top=100.0)
    st = _st(elev_datum_stated="Local (assumed)")
    rec = vdrun.apply(dam, st, LON, LAT)
    rec, changed = vdrun.apply_local(dam, st, DEM_CREST)
    assert changed and rec["source_datum"] == "local" and rec["how"] == "stated"


@test
def test_vdatum_local_pool_without_top():
    """No top given (the DEM's) and a local pool of 95 ft: detected; the pool cannot be placed,
    so it is dropped (the run takes it from the spillway rating / DEM)."""
    from nd_breach_runner.core import vdatum_run as vdrun
    dam = _dam(top=None)
    fl = types.SimpleNamespace(normal_pool_ft=95.0, trigger="peak", trigger_value=None,
                               spillway=None, outlets=[])
    st = _st(flood=fl)
    vdrun.apply(dam, st, LON, LAT, top_is_dem=True)
    rec, changed = vdrun.apply_local(dam, st, DEM_CREST)
    assert changed and fl.normal_pool_ft is None and rec["local_offset_ft"] is None
    assert rec["fields"]["normal_pool_ft"]["navd88"] is None
    assert "not used" in "\n".join(_VD().summary_lines(rec))


@test
def test_vdatum_dem_crest_on_line():
    import numpy as np
    from nd_breach_runner.core import vdatum_run as vdrun

    def read(bounds, cell):
        x0, y0, x1, y1 = bounds
        xs = np.arange(x0 + cell / 2, x1, cell)
        ys = np.arange(y1 - cell / 2, y0, -cell)
        X, Y = np.meshgrid(xs, ys)
        z = np.where(np.abs(Y) <= 2.0, DEM_CREST * 0.3048, 1060.0 * 0.3048)   # crest band y=0
        z = np.where(np.abs(X) > 45.0, 1080.0 * 0.3048, z)                    # high abutments
        return types.SimpleNamespace(masked=lambda: np.ma.masked_invalid(z),
                                     x_centers=lambda: xs, y_centers=lambda: ys)
    c, how = vdrun.dem_crest_on_line(read, [(-50.0, 0.0), (50.0, 0.0)], 1.0)
    assert abs(c - DEM_CREST) < 0.01 and "dam line" in how, (c, how)
    assert vdrun.dem_crest_on_line(read, [], 1.0)[0] is None


@test
def test_vdatum_local_window_line_and_choice():
    from nd_breach_runner import popup_logic as pl
    assert pl.ELEV_DATUM_CHOICES[3] == "local"
    d = tempfile.mkdtemp(prefix="vd_local_")
    with open(os.path.join(d, "dam.json"), "w") as fh:
        fh.write('{"format": "nd_dam_suite/dam_record", "format_version": 1, "fields": '
                 '{"dam_id": "X-1", "top_of_dam_ft": 100.0, "lat": 46.983031, '
                 '"lon": -97.391489}}')
    line = pl.datum_info(d, "auto")
    assert "Looks local" in line, line
    assert "taken from the DEM" in pl.datum_info(d, "local")
    from nd_breach_runner import algorithms as A
    assert [k for k, _l in A.ELEV_DATUMS] == list(pl.ELEV_DATUM_CHOICES)
