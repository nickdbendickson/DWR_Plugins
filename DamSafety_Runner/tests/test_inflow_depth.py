"""The breach inflow depth check (a warning only, the pick is kept): with no dam line the
inflow is the lowest ground near the dam point on the downstream side, with no lower limit.
DWR-3034 (30.7 ft high) got ground 53.3 ft below the top (1.74 H), maybe a deeper coulee
beside the dam. Ground more than terrain.INFLOW_DEEP_FACTOR x H below the top is flagged."""
import inspect
import os
import shutil
import tempfile

import numpy as np

from nd_breach_runner.core import breach as br
from nd_breach_runner.core import terrain
from nd_breach_runner.core.grid import Grid
from nd_breach_runner.core.units import FT
from nd_breach_runner.tests.registry import test as register  # "test" would be collected by pytest
from nd_breach_runner.tests.synthetic import SyntheticTerrain, npy_writer, valley, TOP_M, H_M


class _Dam(object):
    crest_line_xy = None                    # a dam point only, no dam line


def _coulee(X, Y):
    """A reservoir west of an embankment at x = 0; the valley below it is H below the top,
    and a deeper coulee (1.74 H below the top, as at DWR-3034) runs east 50-70 m north of the
    dam point, inside the usual 90 m search."""
    z = valley(X, Y)
    deep = (X > 45) & (Y > 50) & (Y < 70)
    return np.where(deep, TOP_M - 1.74 * H_M - 0.001 * np.clip(X, 0, None), z)


def _pick(fn):
    """The workflow's automatic pick (no dam line), as workflow.run does it: the 20 m search
    grid, then the depth check on that grid."""
    from nd_breach_runner.core import workflow
    src = SyntheticTerrain(fn)
    rad = max(90.0, 8.0 * H_M)
    half = max(rad, 400.0) + 30.0
    local = src.read((-half, -half, half, half), 20.0)
    warnings, logs = [], []
    pt, why = workflow._downstream_inflow(src, local, _Dam(), (0.0, 0.0), H_M, TOP_M, rad, 20.0,
                                          warnings, logs.append)
    n_before = len(warnings)
    msg = workflow._inflow_depth_check(local, pt, H_M, TOP_M, False, warnings, logs.append)
    return pt, why, local, warnings, n_before, msg, logs


def _drop_h(local, pt):
    r, c = local.rc(pt[0], pt[1])
    return (TOP_M - local.data[r, c]) / H_M


@register
def inflow_depth_deep_coulee_beside_the_dam_warns():
    pt, why, local, warnings, n0, msg, logs = _pick(_coulee)
    # the pick itself is unchanged by the check: the coulee (lowest ground in the radius)
    assert pt is not None and pt[0] > 45 and 50 < pt[1] < 70, (pt, why)
    assert _drop_h(local, pt) > terrain.INFLOW_DEEP_FACTOR
    assert msg is not None and msg.startswith(terrain.INFLOW_DEEP), msg
    assert len(warnings) == n0 + 1 and warnings[0] == msg, warnings     # first, as CHECK flags
    assert "times the dam height ({:.1f} ft)".format(H_M / FT) in msg, msg
    assert "qa_inflow_map.png" in msg and "estimated from the DEM" not in msg, msg
    assert msg.endswith("give the inflow point or a dam line."), msg
    assert msg in logs


@register
def inflow_depth_normal_valley_does_not_warn():
    pt, why, local, warnings, n0, msg, _logs = _pick(valley)
    assert pt is not None and pt[0] > 0, (pt, why)
    d = _drop_h(local, pt)
    assert 1.0 <= d <= terrain.INFLOW_DEEP_FACTOR, d       # ~1.18 H
    assert msg is None and len(warnings) == n0, warnings
    assert not any(terrain.INFLOW_DEEP in w for w in warnings), warnings


@register
def inflow_depth_wording_when_the_top_is_from_the_dem():
    from nd_breach_runner.core import workflow
    g = Grid(np.full((5, 5), TOP_M - 2.0 * H_M), -25.0, 25.0, 10.0)
    warnings = []
    msg = workflow._inflow_depth_check(g, (0.0, 0.0), H_M, TOP_M, True, warnings,
                                       lambda m: None)
    assert msg and "top of dam estimated from the DEM may be high" in msg, msg
    assert "2.0 times the dam height" in msg and warnings == [msg], msg


@register
def inflow_depth_unknown_values_skip_quietly():
    from nd_breach_runner.core import workflow
    g = Grid(np.full((5, 5), TOP_M - 2.0 * H_M), -25.0, 25.0, 10.0)
    nod = Grid(np.full((5, 5), -9999.0), -25.0, 25.0, 10.0)
    warnings = []
    for args in ((g, (0.0, 0.0), None, TOP_M), (g, (0.0, 0.0), H_M, None),
                 (g, (0.0, 0.0), 0.0, TOP_M), (g, (0.0, 0.0), H_M, float("nan")),
                 (nod, (0.0, 0.0), H_M, TOP_M), (g, (500.0, 0.0), H_M, TOP_M),
                 (None, (0.0, 0.0), H_M, TOP_M)):
        assert workflow._inflow_depth_check(*args, top_from_dem=False, warnings=warnings,
                                            log=lambda m: None) is None, args
    assert warnings == []



@register
def inflow_depth_with_a_dam_line_does_not_ask_for_one():
    # a dam line was given but the toe band did not place the inflow (the fallback search did)
    from nd_breach_runner.core import workflow
    g = Grid(np.full((5, 5), TOP_M - 2.0 * H_M), -25.0, 25.0, 10.0)
    warnings, out = ["other"], {}
    msg = workflow._inflow_depth_check(g, (0.0, 0.0), H_M, TOP_M, False, warnings,
                                       lambda m: None, has_line=True, out=out)
    assert msg and msg.endswith("give the inflow point.") and "dam line" not in msg, msg
    assert warnings == [msg, "other"], warnings
    # recorded in run_report.json's inflow_point
    assert out == {"depth_below_top_h": 2.0, "depth_check": msg}, out


@register
def inflow_depth_is_recorded_without_a_warning():
    from nd_breach_runner.core import workflow
    g = Grid(np.full((5, 5), TOP_M - 1.1 * H_M), -25.0, 25.0, 10.0)
    warnings, out = [], {}
    assert workflow._inflow_depth_check(g, (0.0, 0.0), H_M, TOP_M, False, warnings,
                                        lambda m: None, out=out) is None
    assert warnings == [] and out == {"depth_below_top_h": 1.1}, out


# ------------------------------------------------------------------ through the workflow
class _Stop(Exception):
    pass


def _workflow_run(fn, line=None):
    """_run_dam_once on the synthetic terrain `fn`, stopped at the "Breach inflow at" log line
    (no model run): the run's warnings and report inflow_point at that point."""
    from nd_breach_runner.core import workflow
    d = tempfile.mkdtemp()
    dam = br.DamData(dam_id="T1", dam_height_ft=25.0, storage_top_acft=500.0,
                     top_of_dam_ft=1850.0, surface_area_top_ac=60.0)       # H_M, TOP_M
    if line is not None:
        dam.crest_line_xy = line
        dam.ds_slope_h = 3.0
    st = workflow.RunSettings(cell_size_m=20.0, initial_length_mi=1.5, half_width_mi=0.5,
                              max_iterations=1, lisflood_exe="", threads=2, sim_time_hr=4.0)
    seen = {}

    def log(m):
        if m.startswith("Breach inflow at "):
            f = inspect.currentframe().f_back                     # _run_dam_once
            seen["warnings"] = list(f.f_locals["warnings"])
            seen["ip"] = dict(f.f_locals["report"]["inflow_point"])
            raise _Stop()
    try:
        workflow._run_dam_once(dam, (0.0, 0.0), st, SyntheticTerrain(fn), npy_writer,
                               os.path.join(d, "T1"), log=log)
    except _Stop:
        pass
    finally:
        shutil.rmtree(d, ignore_errors=True)
    assert "ip" in seen, "the run did not reach the inflow point"
    return seen["warnings"], seen["ip"]


def _deep_toe(X, Y):
    """The valley with the ground 1.6 H below the top from 5 m east of the dam (x = 0) on:
    the toe band's pick is that deep."""
    return np.where((X > 5) & (np.abs(Y) < 200), TOP_M - 1.6 * H_M, valley(X, Y))


@register
def inflow_depth_workflow_warns_on_the_coulee():
    warnings, ip = _workflow_run(_coulee)
    deep = [w for w in warnings if w.startswith(terrain.INFLOW_DEEP)]
    assert len(deep) == 1 and warnings[0] == deep[0], warnings
    assert ip["xy"] == [60.0, 60.0] and ip["depth_check"] == deep[0], ip
    assert ip["depth_below_top_h"] > terrain.INFLOW_DEEP_FACTOR, ip


@register
def inflow_depth_workflow_normal_valley_is_recorded_only():
    warnings, ip = _workflow_run(valley)
    assert not any(terrain.INFLOW_DEEP in w for w in warnings), warnings
    assert ip["xy"] == [80.0, 0.0] and "depth_check" not in ip, ip
    assert 1.0 <= ip["depth_below_top_h"] <= terrain.INFLOW_DEEP_FACTOR, ip


@register
def inflow_depth_workflow_skips_a_toe_band_pick():
    # with a dam line the toe band (1 m DEM) places it: the toe, deep or not, is not checked
    warnings, ip = _workflow_run(_deep_toe, line=[(0.0, -100.0), (0.0, 100.0)])
    assert "downstream toe" in ip["how"], ip
    assert "depth_below_top_h" not in ip and "depth_check" not in ip, ip
    assert not any(terrain.INFLOW_DEEP in w for w in warnings), warnings
    # the same ground with no dam line is checked and warns
    warnings, ip = _workflow_run(_deep_toe)
    assert "downstream toe" not in ip["how"] and ip["depth_check"], ip
    assert warnings[0].startswith(terrain.INFLOW_DEEP), warnings
