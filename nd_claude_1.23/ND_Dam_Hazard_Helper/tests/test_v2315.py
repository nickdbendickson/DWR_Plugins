"""2.31.5 (Lumo v1.7 / v1.8): curve line numbers, traffic upper bound, empty EAP bands, damage
flag, examples by acres, sensitivity factor checks, verdict / overview status codes.
Run: python3 -m ND_Dam_Hazard_Helper.tests.run_tests (from the folder above the plugin)."""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_DIR = os.path.dirname(HERE)
if os.path.dirname(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, os.path.dirname(PLUGIN_DIR))
from ND_Dam_Hazard_Helper.tests.registry import test  # noqa: E402


def _src(name):
    with open(os.path.join(PLUGIN_DIR, name), encoding="utf-8") as fh:
        return fh.read()


@test
def v2315_curve_warnings_name_the_file_line():
    from ND_Dam_Hazard_Helper import damage_curves as DC
    d = tempfile.mkdtemp()
    p = os.path.join(d, "c.csv")
    with open(p, "w", encoding="utf-8") as fh:
        fh.write("# comment\n\noccupancy,kind,depth_ft,percent\n# another\n"
                 "COM1,structure,0,0\nCOM1,structure,4,40\nCOM1,structure,x,1\n")
    curves, probs = DC.load(p)
    assert curves is not None
    assert any(m.startswith("line 7 skipped") for m in probs), probs


@test
def v2315_sensitivity_factor_checks():
    from ND_Dam_Hazard_Helper import pipeline_extras as PE
    errs, warns = PE.factor_problems("0,8, 0.9")
    assert errs and "0.8" in errs[0] and any("8" in w for w in warns)
    errs, warns = PE.factor_problems("0.8, 0.9 1.1 1.2")
    assert not errs and not warns
    errs, warns = PE.factor_problems("1,2")
    assert not errs and len(warns) >= 1
    errs, warns = PE.factor_problems("0.8 3")
    assert not errs and any("outside" in w for w in warns)


@test
def v2315_verdict_codes_drive_the_off_checks():
    from ND_Dam_Hazard_Helper import crosscheck as XC
    from ND_Dam_Hazard_Helper import popsource as PS
    assert XC.verdict_code(0, 5)[0] == XC.V_ONLY_PLUGIN
    assert XC.verdict_code(100, 100)[0] == XC.V_CONSISTENT
    assert XC.verdict_code(100, 300)[0] == XC.V_HIGHER
    assert XC.verdict_code(0, 0)[0] == XC.V_BOTH_ZERO
    assert XC.verdict(100, 100) == (1.0, "consistent")
    code, r, text = PS._source_verdict_code(0, 5, "polygon")
    assert code == XC.V_ONLY_PLUGIN and "population source" in text
    assert PS.is_off_ratio({"Verdict_code": code, "Verdict": "reworded"})
    assert not PS.is_off_ratio({"Verdict_code": XC.V_CONSISTENT, "Ratio_plugin_to_source": 1.0})
    assert PS._source_verdict(100, 300, "polygon")[1].startswith("plugin higher")


@test
def v2315_overviews_status_is_data():
    from ND_Dam_Hazard_Helper import overviews as OV
    st, text = OV.build_overviews_status("/nonexistent/x.tif")
    assert st == OV.ST_FAILED
    assert OV.build_overviews("/nonexistent/x.tif")[0] is False
    assert "text.endswith" not in _src("overviews.py")


@test
def v2315_source_level_fixes():
    con = _src("consequences.py")
    assert 'b.startswith("EGM")' not in con and "curve_applied = True" in con
    assert "key=lambda c: -(c.get(\"acres\")" in _src("devscreen.py")
    eap = _src("eap.py")
    assert "no bands" in eap and "return None\n        return path" in eap
    assert "upper bound" in _src("estimate.py")
    pe = _src("pipeline_extras.py")
    assert "Data vintage records not read" in pe and "earlier-download rows not read" in pe
    assert "Suite version check skipped" in _src("plugin.py")
    assert "result[\"n_strips_unread\"] = result.get" in _src("popsource.py")
