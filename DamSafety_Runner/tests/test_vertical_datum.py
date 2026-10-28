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
