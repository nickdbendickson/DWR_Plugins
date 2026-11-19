"""H12: tailwater disclosure (a report limitation sentence and a run warning) and a cited,
conditionally-recorded crest weir coefficient. Report / record / warning only - no computed
number changes."""
import json
import os
import shutil
import tempfile

import numpy as np

from nd_breach_runner.tests.registry import test
from nd_breach_runner.core import workflow
from nd_breach_runner.core import flood
from nd_breach_runner.core import breach as br
from nd_breach_runner.core import conditioning as cond
from nd_breach_runner.core.units import FT


def _dam():
    return br.DamData(dam_id="T1", dam_height_ft=25.0, storage_top_acft=500.0,
                      top_of_dam_ft=1850.0, surface_area_top_ac=60.0, breach_bottom_ft=1825.0)


@test
def h12_tailwater_over_breach_point_warns():
    dam = _dam()
    poly = np.array([[-50.0, -50.0], [50.0, -50.0], [50.0, 50.0], [-50.0, 50.0]])
    over = cond.Edits(stages=[cond.TailwaterStage(poly, 1830.0 * FT, None, "tailwater 1")])
    w = workflow.tailwater_breach_warning((0.0, 0.0), dam, over)
    assert w and "Tailwater disclosure" in w and "breach point" in w, w
    assert "no tailwater" in w and "1830" in w, w
    # a stage below the breach invert (1825 ft) does not warn
    low = cond.Edits(stages=[cond.TailwaterStage(poly, 1820.0 * FT, None, "tailwater 1")])
    assert workflow.tailwater_breach_warning((0.0, 0.0), dam, low) is None
    # a polygon that does not cover the breach point does not warn
    away = np.array([[500.0, 500.0], [600.0, 500.0], [600.0, 600.0], [500.0, 600.0]])
    off = cond.Edits(stages=[cond.TailwaterStage(away, 1830.0 * FT, None, "tailwater 1")])
    assert workflow.tailwater_breach_warning((0.0, 0.0), dam, off) is None
    # a cascade downstream reservoir pool (PondLower with an explicit stage) is covered too
    pool = cond.Edits(ponds=[cond.PondLower(poly, 1.5, wse_m=1830.0 * FT, name="pond 1")])
    assert workflow.tailwater_breach_warning((0.0, 0.0), dam, pool) is not None
    # no edits -> no warning
    assert workflow.tailwater_breach_warning((0.0, 0.0), dam, None) is None


@test
def h12_crest_coefficient_recorded_only_when_not_default():
    default = workflow.RunSettings(
        flood=flood.FloodInputs.triangle(3000.0, 4.0, 20.0, normal_pool_ft=1846.0))
    fd = default.as_dict()["flood"]
    assert "crest_c_us" not in fd, fd                       # default -> not recorded
    assert "crest_c_us" not in workflow.key_settings(default).get("flood", {})
    nondef = workflow.RunSettings(
        flood=flood.FloodInputs.triangle(3000.0, 4.0, 20.0, normal_pool_ft=1846.0,
                                         crest_c_us=2.8))
    fd2 = nondef.as_dict()["flood"]
    assert fd2.get("crest_c_us") == 2.8, fd2
    assert workflow.key_settings(nondef)["flood"]["crest_c_us"] == 2.8


@test
def h12_limitation_sentence_in_submittal_docx():
    from nd_breach_runner.core import report_docx as R
    from nd_breach_runner.tests.test_report_provenance import _rep
    d = tempfile.mkdtemp(prefix="h12_docx_")
    try:
        mdl = os.path.join(d, "_breach_model")
        os.makedirs(mdl)
        json.dump(_rep(), open(os.path.join(mdl, "run_report.json"), "w"))
        open(os.path.join(mdl, "run_report.md"), "w").write("# Breach model run: T1\n\n- ok\n")
        out = os.path.join(d, "h12.docx")
        res = R.build_submittal(d, "T1", out)
        assert os.path.exists(out), res.warnings
        t = R.inspect_docx(out)["text"]
        assert "free weir or orifice discharge with no tailwater" in t, \
            "H12 tailwater limitation sentence missing from the assumptions"
    finally:
        shutil.rmtree(d, ignore_errors=True)
