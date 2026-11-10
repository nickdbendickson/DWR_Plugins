"""M21 (Runner 3.17.4, product-write defence, signed off 2026-11-10): a LISFLOOD-FP build that
writes no maximum-velocity raster (.maxVc) leaves read_results with an all-zeros velocity grid.
Published as velocity 0 that would class velocity-driven shallow-fast cells LOW off the v=0 ACER
TM-11 curves; the Runner now OMITS the velocity product and reports the maximum velocity (and the
report-metrics velocity figures) as None instead, so the downstream Hazard pre-flight refuses
(checks.py "no velocity raster") rather than silently under-classifying. No shipped engine
triggers it (the built-in engine always writes .maxVc, ANUGA always has velocity, and the flag
defaults True); only a non-conforming user binary does. The mid-run hard-stop is NOT added.

These run the built-in engine (numpy, no real binary) on the synthetic channel, so they are fast
tests. The no-velocity case is simulated by wrapping lisflood.read_results to clear the
velocity_written flag - exactly the single chokepoint a no-.maxVc build would hit.

    QT_QPA_PLATFORM=offscreen python3 -m nd_breach_runner.tests.run_tests
"""
import glob
import os
import tempfile

from nd_breach_runner.core import workflow
from nd_breach_runner.core import lisflood as lf
from nd_breach_runner.core import breach as br
from nd_breach_runner.tests.synthetic import SyntheticTerrain, npy_writer
from nd_breach_runner.tests.registry import test

VEL_METRIC_KEYS = ("velocity_p95_fps", "velocity_p99_fps", "max_velocity_depth_ft",
                   "max_velocity_xy")


def _dam():
    return br.DamData(dam_id="M21", dam_height_ft=25.0, storage_top_acft=5.0,
                      top_of_dam_ft=1850.0, surface_area_top_ac=2.0, crest_width_ft=12,
                      us_slope_h=3, ds_slope_h=3)


def _run(out):
    st = workflow.RunSettings(breach_method=br.METHOD_TR60, cell_size_m=20.0,
                              initial_length_mi=1.0, half_width_mi=0.3, max_iterations=1,
                              lisflood_exe=lf.BUILTIN_EXE, threads=1)
    r = workflow.run_dam(_dam(), (0.0, 0.0), st, SyntheticTerrain(), npy_writer, out)
    vel_files = (glob.glob(os.path.join(out, "Velocity (Max).*"))
                 + glob.glob(os.path.join(out, "Velocity Polygons.*")))
    return r, vel_files


@test
def m21_velocity_present_is_unchanged():
    """A build that writes .maxVc (the built-in engine always does): the velocity product and
    every velocity figure are reported exactly as before - the defence is inert."""
    d = tempfile.mkdtemp(prefix="m21_ok_")
    try:
        r, vel_files = _run(os.path.join(d, "M21"))
    finally:
        pass
    s = r.summary
    assert "velocity" in r.final and vel_files, (sorted(r.final), vel_files)
    v = s.get("max_velocity_fps")
    assert isinstance(v, float) and v > 0.0, v
    assert s.get("velocity_p95_fps") is not None, s.get("velocity_p95_fps")
    print("ok m21: velocity present -> product written, max_velocity_fps={} (defence inert)"
          .format(v))


@test
def m21_no_maxVc_omits_velocity_and_reports_none():
    """A build that writes no .maxVc (velocity_written=False): the velocity product is omitted
    (so the Hazard pre-flight refuses) and the maximum velocity and all report-metrics velocity
    figures are None, while depth/arrival/acres are unchanged. The warning is recorded."""
    orig = lf.read_results

    def _no_vel(workdir, dem, st, hydrograph, name="run"):
        env = orig(workdir, dem, st, hydrograph, name=name)
        env.stats["velocity_written"] = False
        return env

    # baseline (velocity present) for the depth/acres comparison
    d0 = tempfile.mkdtemp(prefix="m21_base_")
    base, _ = _run(os.path.join(d0, "M21"))

    d = tempfile.mkdtemp(prefix="m21_novel_")
    lf.read_results = _no_vel
    try:
        r, vel_files = _run(os.path.join(d, "M21"))
    finally:
        lf.read_results = orig

    s = r.summary
    # the velocity product is omitted entirely (no raster, no polygons, not in res.final)
    assert not vel_files, vel_files
    assert "velocity" not in r.final, sorted(r.final)
    # the maximum velocity and every derived velocity figure are None, not 0
    assert s.get("max_velocity_fps") is None, s.get("max_velocity_fps")
    for k in VEL_METRIC_KEYS:
        assert s.get(k) is None, (k, s.get(k))
    # depth / arrival / flooded area are unchanged by the defence
    assert s.get("max_depth_ft") == base.summary.get("max_depth_ft"), (
        s.get("max_depth_ft"), base.summary.get("max_depth_ft"))
    assert s.get("flooded_acres_over_arrival_depth") == \
        base.summary.get("flooded_acres_over_arrival_depth")
    # the omission is announced to the user
    warns = " ".join(getattr(r, "warnings", None) or [])
    assert ".maxVc" in warns or "maximum-velocity raster" in warns, warns
    print("ok m21: no .maxVc -> velocity product omitted, max_velocity_fps/None, depth intact")


@test
def m21_receptor_dv_is_unknown_not_zero_without_velocity():
    """impacts.summarize: with velocity absent (None), the depth x velocity swept-off-feet
    danger count is reported unknown (None) and the CSV velocity_fps is blank - not a falsely
    safe 0 - while every depth-only receptor figure is unchanged."""
    import numpy as np
    from nd_breach_runner.core import impacts as im
    from nd_breach_runner.core.grid import Grid
    from nd_breach_runner.core.units import FT

    g = Grid(np.zeros((50, 50)), 0.0, 500.0, 10.0)
    rec = im.Receptors()
    rec.add_points([[100.0, 400.0]], ids=["a"])
    depth = np.zeros((50, 50))
    depth[5:15, 5:20] = 3.0 * FT                       # a wet block covering the structure
    d = np.where(depth > 0, depth, np.nan)
    vel = np.where(depth > 0, 6.0 * FT, np.nan)        # 6 ft/s -> dv ~18 ft2/s >= 7
    arr = np.where(depth > 0, 0.5 * 3600.0, np.nan)
    key = "dv_ge_7_ft2s"
    s1, r1 = im.summarize(rec, g, {"depth": d, "velocity": vel, "arrival": arr}, 0.1 * FT)
    s2, r2 = im.summarize(rec, g, {"depth": d, "velocity": None, "arrival": arr}, 0.1 * FT)
    assert s1[im.KIND_STRUCTURE][key] == 1, s1
    assert s2[im.KIND_STRUCTURE][key] is None, s2
    assert isinstance(r1[0]["velocity_fps"], float) and r1[0]["velocity_fps"] > 0
    assert r2[0]["velocity_fps"] == "", r2
    assert s1[im.KIND_STRUCTURE]["flooded"] == s2[im.KIND_STRUCTURE]["flooded"]
    assert s1[im.KIND_STRUCTURE]["depth_ge_3ft"] == s2[im.KIND_STRUCTURE]["depth_ge_3ft"]
    print("ok m21: receptor dv None (not 0) and velocity_fps blank when velocity absent")


if __name__ == "__main__":
    m21_velocity_present_is_unchanged()
    m21_no_maxVc_omits_velocity_and_reports_none()
    m21_receptor_dv_is_unknown_not_zero_without_velocity()
    print("all ok")
