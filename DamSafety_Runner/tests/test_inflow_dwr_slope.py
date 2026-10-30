"""v3.13.0 (Nick: "downstream slope data is in dwr data"): the inflow toe band's downstream
slope comes from the dam's DWR record (DWR_dams downstream_slope, text such as "3", "2.5",
"2:1", "2.5;3") when DS_SLOPE / F_DS is not given; else 3:1 with a warning (core/dwr_slope.py,
workflow._toe_slope). Synthetic records only; no DWR data."""
import math
import os
import shutil
import sqlite3
import tempfile

from nd_breach_runner.core import dwr_slope, terrain, workflow
from nd_breach_runner.tests.registry import test as register  # "test" would be collected by pytest
from nd_breach_runner.tests.synthetic import TOP_M, H_M


def _gpkg(path, rows, layer="DWR_dams", id_col="structure_id", slope_col="downstream_slope"):
    """A GeoPackage-shaped SQLite file with a DWR dams table (no geometry is needed)."""
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE gpkg_contents (table_name TEXT, data_type TEXT)")
    con.execute("INSERT INTO gpkg_contents VALUES (?, 'features')", (layer,))
    con.execute('CREATE TABLE "{}" (fid INTEGER PRIMARY KEY, "{}", dam_name TEXT, "{}" TEXT, '
                'upstream_slope TEXT)'.format(layer, id_col, slope_col))
    for sid, ds in rows:
        con.execute('INSERT INTO "{}" ("{}", dam_name, "{}", upstream_slope) VALUES '
                    '(?, ?, ?, ?)'.format(layer, id_col, slope_col), (sid, "dam", ds, "9"))
    con.commit()
    con.close()
    return path


@register
def dwr_slope_reads_the_ways_the_dwr_types_it():
    p = dwr_slope.parse_ds_slope
    for raw, want in (("3", 3.0), ("2", 2.0), ("3.0", 3.0), ("2.5", 2.5), (" 1.5 ", 1.5),
                      ("10", 10.0), (3.0, 3.0), (2, 2.0), ("2:1", 2.0), ("3`", 3.0),
                      ("4 STEP", 4.0), ("2.5;3", 3.0), ("3,3.5,", 3.5), ("2.5 - 3", 3.0)):
        z, note = p(raw)
        assert z is not None and abs(z - want) < 1e-9, (raw, z, note)
        assert "{:g}:1".format(want) in note, (raw, note)
    for raw in (None, "", "  ", "0", "0.0", "varies", "N/A", ".6", "3:5", "37.5", "80",
                float("nan"), -3.0):
        z, note = p(raw)
        assert z is None and note, (raw, z, note)


@register
def dwr_slope_finds_the_dams_record_by_its_dwr_number():
    d = tempfile.mkdtemp(prefix="dwr_slope_")
    try:
        g = _gpkg(os.path.join(d, "DWR_dams.gpkg"),
                  [(4031, "2"), ("02801", "2.5"), (77, "varies"), (78, None), (79, "2.5;3")])
        z, note = dwr_slope.ds_slope_of("DWR-4031", paths=[g])
        assert z == 2.0 and "'2'" in note and "DWR record" in note, note
        z, _n = dwr_slope.ds_slope_of("DWR-2801", paths=[g])          # leading zero in the table
        assert z == 2.5, z
        z, _n = dwr_slope.ds_slope_of("ND99999", {"dwr_structure_id": "79"}, paths=[g])
        assert z == 3.0, z                                          # dam.json's DWR number
        for dam_id in ("DWR-77", "DWR-78"):
            z, note = dwr_slope.ds_slope_of(dam_id, paths=[g])
            assert z is None and "downstream_slope" in note, (dam_id, note)
        z, note = dwr_slope.ds_slope_of("DWR-5", paths=[g])
        assert z is None and "not in" in note, note
        z, note = dwr_slope.ds_slope_of("ND00123", paths=[g])        # no DWR number
        assert z is None and "no DWR structure number" in note, note
        z, note = dwr_slope.ds_slope_of("DWR-4031", paths=[os.path.join(d, "none.gpkg")])
        assert z is None and "no DWR dams layer" in note, note
        # a shapefile-cut field name and |layername= (the dam folder's data/dwr_dams.gpkg)
        g2 = _gpkg(os.path.join(d, "dwr_dams.gpkg"), [("4031", "3:1")], layer="dwr_dams",
                   id_col="structure_", slope_col="downstream")
        z, _n = dwr_slope.ds_slope_of("DWR-4031", paths=[g2 + "|layername=dwr_dams", g])
        assert z == 3.0, z                                          # the first layer wins
    finally:
        shutil.rmtree(d, ignore_errors=True)


@register
def dwr_slope_only_when_ds_slope_is_not_given():
    d = tempfile.mkdtemp(prefix="dwr_slope_")
    try:
        g = _gpkg(os.path.join(d, "DWR_dams.gpkg"), [(4031, "2")])

        class Dam(object):
            dam_id = "DWR-4031"
            ds_slope_h = 2.5
        dam = Dam()
        assert dwr_slope.apply_to_dam(dam, paths=[g]) == ""
        assert not hasattr(dam, "dwr_ds_slope_h")
        dam.ds_slope_h = None
        line = dwr_slope.apply_to_dam(dam, paths=[g])
        assert dam.dwr_ds_slope_h == 2.0 and "2:1" in line, line
        dam.dam_id = "DWR-1"
        line = dwr_slope.apply_to_dam(dam, paths=[g])
        assert dam.dwr_ds_slope_h is None and "3:1 is used" in line, line
    finally:
        shutil.rmtree(d, ignore_errors=True)


@register
def toe_slope_order_setting_then_ds_slope_then_dwr_then_none():
    class Dam(object):
        ds_slope_h = None
        dwr_ds_slope_h = None
        dwr_ds_slope_note = None

    class St(object):
        inflow_toe_ds_slope_h = None
    dam, st = Dam(), St()
    got = workflow._toe_slope(dam, st)
    assert got["toe_ds_slope_h"] is None and "no DS_SLOPE / F_DS" in got["toe_ds_slope_src"]
    dam.dwr_ds_slope_h, dam.dwr_ds_slope_note = 2.0, "DWR record downstream_slope '2' = 2:1"
    got = workflow._toe_slope(dam, st)
    assert got == {"toe_ds_slope_h": 2.0, "toe_ds_slope_src": dam.dwr_ds_slope_note}, got
    dam.ds_slope_h = 2.5
    assert workflow._toe_slope(dam, st) == {"toe_ds_slope_h": 2.5,
                                            "toe_ds_slope_src": "DS_SLOPE / F_DS"}
    st.inflow_toe_ds_slope_h = 4.0
    assert workflow._toe_slope(dam, st)["toe_ds_slope_h"] == 4.0
    # a bad value is skipped, not used
    st.inflow_toe_ds_slope_h, dam.ds_slope_h = float("nan"), 0.0
    assert workflow._toe_slope(dam, st)["toe_ds_slope_h"] == 2.0
    assert workflow._toe_slope(object(), object())["toe_ds_slope_h"] is None


@register
def workflow_toe_band_uses_the_dwr_slope_and_warns_without_one():
    from nd_breach_runner.tests.test_inflow_toe_band import LINE, _Src

    class St(object):
        midcrest_line = None
        inflow_toe_band_m = None
        inflow_toe_cell_m = None
        inflow_toe_ds_slope_h = None
        inflow_toe_min_outer_m = None
    src = _Src()
    local = src.read((-430.0, -430.0, 430.0, 430.0), 20.0)

    def pick(dam):
        warnings, info = [], {}
        pt, why = workflow._downstream_inflow(src, local, dam, (0.0, 0.0), H_M, TOP_M, 90.0,
                                              20.0, warnings, lambda *_a: None, out=info,
                                              **workflow._toe_kwargs(dam, St()))
        return pt, why, warnings, info

    class Dwr(object):
        crest_line_xy = LINE
        ds_slope_h = None
        dwr_ds_slope_h = 2.0
        dwr_ds_slope_note = "DWR record downstream_slope '2' = 2:1 (DWR_dams.gpkg)"
    pt, why, warnings, info = pick(Dwr())
    assert abs(info["toe_band_m"][1] - 3.0 * H_M) < 1e-9, info           # H x (2 + 1)
    assert "DWR record downstream_slope '2'" in info["toe_band_how"], info
    assert pt is not None and not warnings, (pt, why, warnings)

    class Nothing(object):
        crest_line_xy = LINE
        ds_slope_h = None
        dwr_ds_slope_h = None
        dwr_ds_slope_note = "DWR record downstream_slope 'varies' is not a number (x.gpkg)"
    pt, why, warnings, info = pick(Nothing())
    assert abs(info["toe_band_m"][1] - (terrain.INFLOW_TOE_DS_SLOPE_DEFAULT + 1) * H_M) < 1e-9
    assert pt is not None and len(warnings) == 1, warnings
    assert "downstream slope is not known" in warnings[0] and "3:1" in warnings[0] \
        and "'varies'" in warnings[0], warnings
    assert math.isfinite(pt[0])
