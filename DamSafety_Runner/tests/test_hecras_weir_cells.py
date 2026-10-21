"""Runner 3.10.54 (run queue 2026-10-21, c4_hecras_v2e_gmsh_r): HEC-RAS 6.6 refused the dam
connection because the spillway notch (1066.00 ft, stations 100-220) was lower than two of the
2D cells it connects to (64353 at 1066.61 ft, 64354 at 1066.25 ft). The builder now checks that
before HEC-RAS (check only: it never changes the terrain or the notch)."""
import json
import os
import shutil
import tempfile

import numpy as np

from nd_breach_runner.core import hecras_model as hm
from nd_breach_runner.tests.registry import test
from nd_breach_runner.tests import test_hecras_p0 as p0

C4_WEIR = [(0.0, 1069.12), (99.75, 1069.12), (100.0, 1066.0), (220.0, 1066.0), (220.25, 1069.12),
           (1132.33, 1069.12)]
# HEC-RAS's own cell minima and face stations on the connection (the failed run's g01.hdf)
C4_RUNS = [(64352, 0.0, 51.86), (64353, 51.86, 103.73), (64354, 103.73, 155.59),
           (64355, 155.59, 207.45), (64356, 207.45, 259.32), (64357, 259.32, 311.18)]
C4_ZMIN = {64352: 1066.54, 64353: 1066.607, 64354: 1066.252, 64355: 1065.966, 64356: 1065.47,
           64357: 1064.851}


@test
def hecras_weir_cells_c4_notch_cells_are_flagged():
    rows, bad = hm.weir_cell_rows(C4_RUNS, C4_ZMIN, C4_WEIR, tol=0.01)
    assert [r["cell"] for r in bad] == [64353, 64354], bad
    assert abs(bad[0]["above"] - 0.607) < 1e-6 and abs(bad[1]["above"] - 0.252) < 1e-6, bad
    by = {r["cell"]: r for r in rows}
    # 64352 fronts 0-51.86 where the weir is the 1069.12 crest; 64353 reaches the notch
    assert by[64352]["weir_min"] == 1069.12 and by[64353]["weir_min"] == 1066.0, by
    # QGIS Dev's notchfix terrain (2x2 pixels at 1065.90 ft in each cell): nothing above
    fixed = dict(C4_ZMIN)
    fixed.update({64353: 1065.9, 64354: 1065.9, 64355: 1065.944})
    assert hm.weir_cell_rows(C4_RUNS, fixed, C4_WEIR, tol=0.01)[1] == []
    # within the tolerance is not flagged; a missing or NaN cell is skipped
    assert hm.weir_cell_rows([(1, 100.0, 150.0)], {1: 1066.005}, C4_WEIR, tol=0.01)[1] == []
    assert hm.weir_cell_rows([(1, 100.0, 150.0)], {1: float("nan")}, C4_WEIR)[0] == []


class _U:
    len_name = "ft"

    @staticmethod
    def from_ft(v):
        return float(v)


def _precheck(zcell, mode="warn", weir=None, **opts):
    """A 1,000 ft straight dam line on y=0 with the 2D area below it (y<0), a row of cell
    centres 50 ft apart at y=-25, and a flat terrain at `zcell` (no GDAL: _tif_window faked)."""
    per = np.array([[0.0, 0.0], [1000.0, 0.0], [1000.0, -500.0], [0.0, -500.0]])
    xs = np.arange(25.0, 1000.0, 50.0)
    pts = np.vstack([np.column_stack([xs, np.full_like(xs, -25.0)]),
                     np.column_stack([xs, np.full_like(xs, -75.0)])])
    conn = [(0.0, 0.0), (1000.0, 0.0)]
    weir = weir or [(0.0, 1070.0), (99.75, 1070.0), (100.0, 1066.0), (220.0, 1066.0),
                    (220.25, 1070.0), (1000.0, 1070.0)]
    real = hm._tif_window
    hm._tif_window = lambda path, bounds, srs, max_cells=2000: (
        np.full((80, 140), float(zcell)), -100.0, 100.0, 10.0)
    try:
        return hm.weir_cells_precheck(pts, per, conn, weir, 1070.0, "terrain.tif", None, 50.0,
                                      dict({"weir_cell_check": mode}, **opts), _U())
    finally:
        hm._tif_window = real


@test
def hecras_weir_cells_precheck_warns_and_never_edits():
    try:
        import scipy  # noqa: F401
    except ImportError:
        print("    (skipped: no scipy)")
        return
    weir = [(0.0, 1070.0), (99.75, 1070.0), (100.0, 1066.0), (220.0, 1066.0), (220.25, 1070.0),
            (1000.0, 1070.0)]
    before = list(weir)
    r = _precheck(1066.4, weir=weir)
    assert weir == before                               # check only
    assert r["action"] == "warn" and not r["ok"], r
    # only the cells fronting the notch (stations 50-250 for 50 ft cells) are checked
    assert {c["cell"] for c in r["cells"]} == {1, 2, 3, 4}, r["cells"]
    # cell 1's face (50-100) ends just before the notch: weir 1066.8 there, not flagged
    assert [c["cell"] for c in r["cells"] if c["above"] > 0] == [2, 3, 4], r["cells"]
    assert r["bad"] == 3 and "WARNING" in r["note"] and "weir elevation lower" in r["note"], r
    assert "does not change" in r["note"]
    ok = _precheck(1065.5)
    assert ok["action"] == "ok" and ok["bad"] == 0 and ok["note"].startswith(
        "Weir-cell pre-check: OK"), ok
    assert _precheck(1066.4, mode="fail")["action"] == "fail"
    assert _precheck(1066.4, mode="off") is None
    flat = [(0.0, 1070.0), (1000.0, 1070.0)]            # no notch: nothing to check
    assert _precheck(1069.0, weir=flat) is None
    try:
        _precheck(1066.4, mode="maybe")
    except hm.HecRasModelError as e:
        assert "weir_cell_check" in str(e)
    else:
        raise AssertionError("a bad weir_cell_check option was accepted")


@test
def hecras_weir_cells_auto_mode_fails_only_clearly_above():
    """'auto' fails only when a cell is more than weir_cell_fail_ft (default 0.1 ft) above the
    weir (c4: +0.25 / +0.61 ft), warns below; 'warn' stays the default."""
    act = hm.weir_cell_action
    assert act("auto", [], 0.1) == "ok"
    assert act("auto", [{"above": 0.05}], 0.1) == "warn"
    assert act("auto", [{"above": 0.05}, {"above": 0.252}], 0.1) == "fail"
    assert act("warn", [{"above": 0.607}], 0.1) == "warn"
    assert act("fail", [{"above": 0.02}], 0.1) == "fail"
    assert hm.weir_cell_check_mode({}) == "warn" and hm.WEIR_CELL_AUTO_FAIL_FT == 0.1
    assert hm.weir_cell_fail_ft({}) == 0.1 and hm.weir_cell_fail_ft({"weir_cell_fail_ft": "0.5"}) == 0.5
    for bad in ("x", -1, float("nan")):
        try:
            hm.weir_cell_fail_ft({"weir_cell_fail_ft": bad})
        except hm.HecRasModelError as e:
            assert "weir_cell_fail_ft" in str(e)
        else:
            raise AssertionError("weir_cell_fail_ft {!r} was accepted".format(bad))
    try:
        import scipy  # noqa: F401
    except ImportError:
        print("    (skipped the geometry part: no scipy)")
        return
    r = _precheck(1066.4, mode="auto")                  # 0.4 ft above the 1066.0 notch
    assert r["action"] == "fail" and r["fail_above"] == 0.1 and "FAILED" in r["note"], r
    r = _precheck(1066.05, mode="auto")                 # 0.05 ft: HEC-RAS noise level, warn
    assert r["action"] == "warn" and "WARNING" in r["note"], r
    assert _precheck(1066.4, mode="auto", weir_cell_fail_ft=0.5)["action"] == "warn"
    assert _precheck(1066.4)["action"] == "warn"        # default warn never fails


@test
def hecras_weir_cells_check_error_never_aborts_warn_or_auto():
    """An exception inside the pre-check is a note and run_report 'not run' in warn / auto mode;
    only weir_cell_check='fail' lets it through."""
    if p0._skip():
        print("    (skipped: no GDAL)")
        return
    real = hm.weir_cells_precheck

    def boom(*a, **k):
        raise RuntimeError("odd terrain")
    d = tempfile.mkdtemp()
    hm.weir_cells_precheck = boom
    try:
        p0._run(d, dam_json_len_ft=1500.0)
        top_ft = p0.TOP_M / 0.3048
        opts = {"spillway_crest_ft": top_ft - 3.0, "spillway_width_ft": 100.0}
        for mode in (None, "warn", "auto"):
            o = dict(opts, **({"weir_cell_check": mode} if mode else {}))
            r = hm.build_model(d, out_folder=os.path.join(d, "m_{}".format(mode)), options=o)
            wc = r["info"]["site"]["weir_cells"]
            assert wc["action"] == "not run" and "odd terrain" in wc["error"], wc
            assert any(n.startswith("Weir-cell pre-check not run") for n in r["notes"]), r["notes"]
            rep = json.load(open(os.path.join(d, hm.hecras.MODEL_FOLDER,
                                              hm.hecras.RUN_REPORT_JSON)))
            assert rep["hecras_weir_cells"]["action"] == "not run", rep["hecras_weir_cells"]
        try:
            hm.build_model(d, out_folder=os.path.join(d, "m_fail"),
                           options=dict(opts, weir_cell_check="fail"))
        except RuntimeError as e:
            assert "odd terrain" in str(e)
        else:
            raise AssertionError("weir_cell_check='fail' swallowed the check's own error")
        # review 2: a bad weir_cell_fail_ft is a settings error, not "pre-check not run"
        for bad in ("0.1ft", -1):
            try:
                hm.build_model(d, out_folder=os.path.join(d, "m_bad"),
                               options=dict(opts, weir_cell_check="auto", weir_cell_fail_ft=bad))
            except hm.HecRasModelError as e:
                assert "weir_cell_fail_ft must be" in str(e), e
            else:
                raise AssertionError("bad weir_cell_fail_ft {!r} was not a settings error".format(bad))
    finally:
        hm.weir_cells_precheck = real
        shutil.rmtree(d, ignore_errors=True)


@test
def hecras_weir_cells_precheck_in_the_build_and_run_report():
    """build_model with a spillway notch records the check in the site checks and
    run_report.json ('hecras_weir_cells'); without a notch there is none."""
    if p0._skip():
        print("    (skipped: no GDAL)")
        return
    try:
        import scipy  # noqa: F401
    except ImportError:
        print("    (skipped: no scipy)")
        return
    d = tempfile.mkdtemp()
    try:
        p0._run(d, dam_json_len_ft=1500.0)
        top_ft = p0.TOP_M / 0.3048
        r = hm.build_model(d, out_folder=os.path.join(d, "model"),
                           options={"spillway_crest_ft": top_ft - 3.0, "spillway_width_ft": 100.0})
        wc = r["info"]["site"].get("weir_cells")
        assert wc is not None and wc["action"] in ("ok", "warn"), wc
        assert any(n.startswith("Weir-cell pre-check") for n in r["notes"]), r["notes"]
        rep = json.load(open(os.path.join(d, hm.hecras.MODEL_FOLDER, hm.hecras.RUN_REPORT_JSON)))
        assert rep["hecras_weir_cells"]["cells"], rep.get("hecras_weir_cells")
        sp = r["info"]["spillway"]
        assert (round(sp["stations"][0], 2), sp["crest"]) in r["model"].weir   # notch unchanged
        r2 = hm.build_model(d, out_folder=os.path.join(d, "model2"))
        assert "weir_cells" not in r2["info"]["site"]
    finally:
        shutil.rmtree(d, ignore_errors=True)
