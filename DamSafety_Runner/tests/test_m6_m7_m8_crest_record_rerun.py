"""Regression tests for three workflow.run_dam fixes (no QGIS / LISFLOOD-FP; the model run is
stubbed on the synthetic valley, as test_v320 does):

  M6  the DEM top of dam is the 98th percentile within 30 m, not the single highest cell, so a
      hill / road / spike within 30 m cannot set the crest.
  M7  a DEM-estimated top of dam is recorded in dam.json with a note that marks it as an
      estimate (not a surveyed crest), so a later run re-estimates it.
  M8  a scenario rerun into the same dam folder clears the previous run's Baseline / Incremental
      rasters instead of leaving them.
"""
import json
import os
import shutil
import tempfile

import numpy as np

from nd_breach_runner.tests.registry import test
from nd_breach_runner.tests.test_v320 import fake_models, _dam, _settings
from nd_breach_runner.tests.synthetic import SyntheticTerrain, npy_writer, valley
from nd_breach_runner.core import workflow


def _tmp():
    return tempfile.mkdtemp(prefix="ndbr_m678_")


def _run(folder, st, dam=None, terrain=None, **fm):
    with fake_models(**fm):
        return workflow.run_dam(dam or _dam(), (0.0, 0.0), st,
                                terrain or SyntheticTerrain(), npy_writer, folder)


@test
def m6_dem_crest_ignores_a_single_spike():
    """M6: one tall DEM cell (a hill / road embankment / spike) within 30 m of the dam point
    does not set the estimated top of dam. nanmax would read it ~140 ft high."""
    def spiky(x, y):
        z = valley(x, y)
        spike = (np.abs(x - 6.0) <= 2.0) & (np.abs(y - 6.0) <= 2.0)      # about one fine cell
        return np.where(spike, z + 45.0, z)                             # +45 m (~148 ft)

    d = _tmp()
    try:
        folder = os.path.join(d, "T1")
        r = _run(folder, _settings(max_iterations=1), dam=_dam(top_of_dam_ft=None),
                 terrain=SyntheticTerrain(fn=spiky))
        rep = json.load(open(r.report_json))
        # the real crest is ~1850 ft; nanmax would read ~1850 + 148 ~ 2000 ft
        assert 1830.0 < rep["dem_crest_ft"] < 1900.0, rep["dem_crest_ft"]
        assert rep["dem_crest_from"] == "DEM near the dam point (98th percentile, 30 m)", rep
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test
def m7_dem_estimated_top_recorded_as_estimate():
    """M7: an estimated top of dam is written to dam.json with an estimate note (so a later run
    does not trust one run's DEM guess as a surveyed crest)."""
    from nd_breach_runner.damsuite import dam_record as dr
    d = _tmp()
    try:
        folder = os.path.join(d, "T1")
        st = _settings(max_iterations=1, dam_record={
            "read": [], "top_source": "parameter",
            "point": {"x": 0.0, "y": 0.0, "crs": "EPSG:26914"}})
        _run(folder, st, dam=_dam(top_of_dam_ft=None))
        rec = dr.load(folder)
        note = rec.sources["top_of_dam_ft"]["note"]
        assert "not a surveyed or as-built crest" in note, note
        assert "98th percentile" in note, note
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test
def m8_rerun_clears_stale_baseline_and_incremental():
    """M8: a baseline run writes Baseline / Incremental rasters; a later plain (no-baseline) run
    into the same folder clears them rather than leaving the previous scenario's rasters."""
    d = _tmp()
    try:
        folder = os.path.join(d, "T1")
        base = os.path.join(folder, workflow.BASELINE_NAMES["depth"])
        incr = os.path.join(folder, workflow.INCREMENT_NAMES["depth"])
        # run 1: ambient stream flow -> a no-breach baseline -> Baseline / Incremental rasters
        _run(folder, _settings(max_iterations=1, ambient_cfs=500.0, baseline=True))
        assert os.path.exists(base) and os.path.exists(incr), "run 1 wrote no baseline/incremental"
        # run 2: a plain run, no baseline, into the same folder
        _run(folder, _settings(max_iterations=1))
        assert os.path.exists(os.path.join(folder, workflow.FINAL_NAMES["depth"]))
        assert not os.path.exists(base), "stale Baseline raster left after the rerun"
        assert not os.path.exists(incr), "stale Incremental raster left after the rerun"
    finally:
        shutil.rmtree(d, ignore_errors=True)
