"""M21 follow-up (Runner 3.17.5): the cascade / sensitivity / probabilistic tools build their own
velocity products from the sub-runs' velocity arrays, which 3.17.4 leaves all-NaN when a build
wrote no .maxVc. They must OMIT their velocity product (not write a zeros one) so a downstream
Hazard classification of their output refuses rather than reading velocity 0 off the v=0 ACER
TM-11 curves; the velocity-present path stays byte-identical.

Pure-numpy unit tests of the three gated writers (no run_dam, no real binary).

    QT_QPA_PLATFORM=offscreen python3 -m nd_breach_runner.tests.run_tests
"""
import glob
import os
import tempfile
import types

import numpy as np

from nd_breach_runner.core.grid import Grid
from nd_breach_runner.core.units import FT
from nd_breach_runner.core import sensitivity as SENS
from nd_breach_runner.core import probabilistic as PROB
from nd_breach_runner.core import cascade as CASC
from nd_breach_runner.core import impacts as im
from nd_breach_runner.tests.synthetic import npy_writer
from nd_breach_runner.tests.registry import test

NY, NX = 20, 20


def _grid():
    return Grid(np.zeros((NY, NX)), 0.0, 200.0, 10.0)


def _final_si(has_velocity):
    """SI arrays like run_dam's final_si; velocity real (has_velocity) or all-NaN (M21 scrub)."""
    d = np.full((NY, NX), 2.0 * FT)
    d[:5] = np.nan                                   # a dry band
    v = np.where(np.isfinite(d), (6.0 * FT if has_velocity else np.nan), np.nan)
    return {"depth": d, "velocity": v, "wse": d + 1000.0,
            "arrival": np.where(np.isfinite(d), 1800.0, np.nan)}


def _vel_files(folder):
    return sorted(glob.glob(os.path.join(folder, "Velocity (Max).*"))
                  + glob.glob(os.path.join(folder, "Cascade Velocity (Max).*")))


@test
def m21sec_receptor_maxima_none_without_velocity():
    """sensitivity.receptor_maxima: absent velocity -> fastest water None (not 0), depth kept."""
    g = _grid()
    rec = im.Receptors()
    rec.add_points([[100.0, 100.0]], ids=["a"])
    dmax_p, vmax_p = SENS.receptor_maxima(rec, g, _final_si(True))
    dmax_a, vmax_a = SENS.receptor_maxima(rec, g, _final_si(False))
    assert isinstance(vmax_p, float) and vmax_p > 0.0, vmax_p
    assert vmax_a is None, vmax_a
    assert dmax_a == dmax_p, (dmax_a, dmax_p)          # depth unaffected
    print("ok m21 follow-up: receptor_maxima fastest water None when velocity absent")


@test
def m21sec_probabilistic_run_rasters_omit_velocity():
    """probabilistic.write_run_rasters: absent velocity -> no per-run velocity raster, depth kept."""
    for has_vel in (True, False):
        d = tempfile.mkdtemp(prefix="m21sec_prob_")
        PROB.write_run_rasters(d, _grid(), npy_writer, _final_si(has_vel))
        vel = _vel_files(d)
        depth = glob.glob(os.path.join(d, "Depth (Max).*"))
        assert depth, "depth raster must always be written"
        assert bool(vel) == has_vel, (has_vel, vel)
    print("ok m21 follow-up: probabilistic per-run velocity raster omitted when absent")


@test
def m21sec_cascade_combined_omit_velocity():
    """cascade._combine: when no run wrote velocity, the combined velocity product is omitted and
    the other three combined rasters are unchanged."""
    for has_vel in (True, False):
        d = tempfile.mkdtemp(prefix="m21sec_casc_")
        runs = [("A", types.SimpleNamespace(grid=_grid(), final_si=_final_si(has_vel))),
                ("B", types.SimpleNamespace(grid=_grid(), final_si=_final_si(has_vel)))]
        out = types.SimpleNamespace(runs=runs, folder=d, combined={})
        CASC._combine(out, types.SimpleNamespace(), npy_writer)
        assert "Cascade Depth (Max).tif" in out.combined, out.combined
        assert "Cascade Arrival Time.tif" in out.combined, out.combined
        assert ("Cascade Velocity (Max).tif" in out.combined) == has_vel, (has_vel, sorted(out.combined))
        assert bool(_vel_files(d)) == has_vel, (has_vel, _vel_files(d))
    print("ok m21 follow-up: cascade combined velocity product omitted when no run wrote velocity")


if __name__ == "__main__":
    m21sec_receptor_maxima_none_without_velocity()
    m21sec_probabilistic_run_rasters_omit_velocity()
    m21sec_cascade_combined_omit_velocity()
    print("all ok")
