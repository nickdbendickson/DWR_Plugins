"""Fixes from the 2026-10-15 audit verification (plugin_analysis/audit_verification.md):

  1. storage: the DEM crest is capped at pool + dam height (a road or hill near the point);
  2. dem_download: ND LiDAR foot grids are scaled to metres and never mosaicked with the metre
     Imagine DEMs (the metre DEM wins, as the ASCII_FT option says);
  3. tool 3: the dam's grid position is set before the water-body lookup, so the dry-dam
     fallback samples the real ground, not 0 m;
  5. tool 7: a flood reaching the edge of a cached-only clip is flagged TRUNCATED;
  7. tool 7: LISFLOOD-FP found on PATH is vetted (real executable, no world-writable folder)
     and its resolved path is logged; a configured path wins.
"""
import inspect
import os
import shutil
import stat
import sys
import tempfile

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(HERE, "qgis_mock"))
sys.path.insert(0, os.path.dirname(PLUGIN))
sys.path.insert(0, HERE)

from nd_dam_screening.core import storage as st  # noqa: E402


def test_crest_is_capped_at_pool_plus_height():
    seen = {}

    def fake_above(dem, mask, pool, crest, cell, steps=20):
        seen["crest"] = crest
        return [(pool, 0.0), (crest, 1.0)], None, None
    saved = (st.shore_slope_deg, st.below_pool_volume, st.above_pool_curve_guarded)
    st.shore_slope_deg = lambda *a, **k: 8.0
    st.below_pool_volume = lambda *a, **k: (0.0, 0.0)
    st.above_pool_curve_guarded = fake_above
    try:
        dem = np.full((21, 21), 300.0)
        mask = np.zeros_like(dem, dtype=bool)
        mask[:, :8] = True                     # the pool, west of the dam point
        dem[:, 8:] = 330.0                     # a road embankment 30 m above the pool
        res = st.estimate_storage(dem, mask, 5.0, (10, 10), known_height_m=8.0)
        assert abs(seen["crest"] - 308.0) < 1e-9, seen
        assert abs(res.crest_elev - 308.0) < 1e-9
        assert any("more than the dam height" in n for n in res.notes), res.notes
        # no height known: the DEM crest stands
        st.estimate_storage(dem, mask, 5.0, (10, 10), known_height_m=None)
        assert abs(seen["crest"] - 330.0) < 1e-9, seen
        # a crest within height + 0.3 m is not touched
        dem[:, 8:] = 307.0
        st.estimate_storage(dem, mask, 5.0, (10, 10), known_height_m=8.0)
        assert abs(seen["crest"] - 307.0) < 1e-9, seen
    finally:
        st.shore_slope_deg, st.below_pool_volume, st.above_pool_curve_guarded = saved


_GDAL_CHECK = r"""
import os, sys, shutil, tempfile
import numpy as np
sys.path.insert(0, sys.argv[1])
from osgeo import gdal, osr
if not hasattr(gdal, "BuildVRT"):
    print("SKIP: no real GDAL"); sys.exit(0)
from nd_dam_screening.core import dem_download as dd

def write(path, value, driver):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    mem = gdal.GetDriverByName("MEM").Create("", 4, 4, 1, gdal.GDT_Float32)
    mem.SetGeoTransform((0, 1, 0, 4, 0, -1))
    srs = osr.SpatialReference(); srs.ImportFromEPSG(26914)
    mem.SetProjection(srs.ExportToWkt())
    mem.GetRasterBand(1).Fill(value)
    gdal.GetDriverByName(driver).CreateCopy(path, mem)

d = tempfile.mkdtemp(prefix="ftm_")
try:
    ft = os.path.join(d, "ASCII_Grids_ft", "gft1.asc"); write(ft, 1000.0, "AAIGrid")
    img = os.path.join(d, "DEM_Imagine", "t1.img"); write(img, 300.0, "HFA")
    v = dd.build_vrts([ft], d)
    a = gdal.Open(v).ReadAsArray()
    assert np.allclose(a, 304.8), a                       # feet alone: converted to metres
    msgs = []
    class Fb(object):
        def pushInfo(self, m): msgs.append(m)
    v = dd.build_vrts([ft, img, img[:-4] + ".rrd"], d, Fb())
    ds = gdal.Open(v)
    assert np.allclose(ds.ReadAsArray(), 300.0)           # the metre DEM wins
    assert all(not f.endswith(".asc") for f in ds.GetFileList()), ds.GetFileList()
    assert any("not mosaicked" in m for m in msgs), msgs
    assert dd.build_vrts([], d).endswith("nd_lidar.vrt")
    print("ok")
finally:
    shutil.rmtree(d, ignore_errors=True)
"""


def test_feet_grids_scaled_and_never_mixed_with_metres():
    # real GDAL, so a separate process without tests/qgis_mock (its osgeo is a stub)
    import subprocess
    p = subprocess.run([sys.executable, "-c", _GDAL_CHECK, os.path.dirname(PLUGIN)],
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    out = p.stdout.strip()
    if out.startswith("SKIP") or "No module named 'osgeo'" in out:
        print("SKIP (dem_download): no real GDAL"); return
    assert p.returncode == 0 and out.endswith("ok"), out


def test_grid_position_set_before_the_waterbody_lookup():
    from nd_dam_screening.algorithms import estimate_storage as es
    src = inspect.getsource(es.EstimateStorage.processAlgorithm)
    pos = src.index("row = int((p.y()")
    assert pos < src.index('raise LookupError("no waterbody'), "row/col must precede the lookup"
    assert src.count("row = int((p.y()") == 1


def test_inventory_curve_uses_the_ground_when_row_col_given():
    from nd_dam_screening.algorithms.estimate_storage import EstimateStorage

    class Alg(object):
        def parameterDefinition(self, name):
            return None
    f = {"height_m": 8.0, "inv_max_m3": 1e6, "inv_norm_m3": 0.0}
    arr = np.full((10, 10), 420.0)
    res = EstimateStorage._inventory_curve(Alg(), f, list(f), arr, 5, 5, {}, None, "no waterbody")
    assert res.curve[0][0] == 420.0 and res.pool_elev == 420.0, res.curve[0]


def test_clip_edge_warning():
    from nd_dam_screening.algorithms import run_breach_sim as rb
    assert rb.clip_edge_warning(True, "cache") and "TRUNCATED" in rb.clip_edge_warning(True, "cache")
    assert rb.clip_edge_warning(False, "cache") is None
    assert rb.clip_edge_warning(True, "3dep") is None


def test_larger_clips_default_is_not_cache_only():
    # H3 (2026-10-18): the "Terrain for larger clips" default must NOT be "Cached clips only"
    # (index 0). Cache-only cannot grow, so a flood that reaches the step-2 clip edge is truncated
    # and the dam under-classified. The default now follows the terrain source (3DEP / local).
    from nd_dam_screening.algorithms import run_breach_sim as rb
    from nd_dam_screening.core import workflow as wf
    TSRC = ["cache", "3dep", "local"]                 # run_breach_sim.py:433 enum -> source mapping
    assert rb.TERRAIN[0].startswith("Cached")         # index 0 is still the cache-only option
    # processing-parameter default (the headless / Processing path)
    alg = rb.RunBreachSim()
    alg._params = {}
    alg.initAlgorithm()
    tsrc_default = alg.parameterDefinition("TSRC").defaultValue
    assert tsrc_default in (1, 2) and TSRC[tsrc_default] != "cache", tsrc_default
    # window default (put() as TSRC in workflow.py); kept in the 0/1/2 enum range so the 3-item
    # dialog combo round-trips it unchanged (a 4th sentinel would be clobbered back to 0 = cache)
    win = wf.DEFAULT_SETTINGS["larger_clips"]
    assert isinstance(win, int) and 0 <= win < len(rb.TERRAIN), win
    assert win != 0 and TSRC[win] != "cache", win
    # truncation is STILL flagged when cache-only is chosen deliberately and the flood hits the edge
    assert rb.clip_edge_warning(True, "cache")
    assert rb.clip_edge_warning(True, TSRC[win]) is None


def test_lisflood_is_vetted_and_logged():
    if os.name == "nt":
        print("SKIP: POSIX modes"); return
    from nd_dam_screening.algorithms import run_breach_sim as rb
    d = tempfile.mkdtemp(prefix="lf_")
    old_path, old_env = os.environ.get("PATH", ""), os.environ.pop("NDBREACH_LISFLOOD", None)
    try:
        good = os.path.join(d, "good"); os.makedirs(good); os.chmod(good, 0o755)
        bad = os.path.join(d, "bad"); os.makedirs(bad); os.chmod(bad, 0o777)
        for folder in (good, bad):
            exe = os.path.join(folder, "lisflood")
            with open(exe, "w") as fh:
                fh.write("#!/bin/sh\n")
            os.chmod(exe, 0o755)
        noexec = os.path.join(good, "noexec"); open(noexec, "w").close(); os.chmod(noexec, 0o644)
        assert rb.vet_lisflood(os.path.join(good, "lisflood"))[0] == os.path.realpath(os.path.join(good, "lisflood"))
        assert "world-writable" in rb.vet_lisflood(os.path.join(bad, "lisflood"))[1]
        assert "not executable" in rb.vet_lisflood(noexec)[1]
        assert "not a file" in rb.vet_lisflood(os.path.join(d, "missing"))[1]
        # PATH with the world-writable folder first: refused (and said so), the good one used
        logs = []
        os.environ["PATH"] = bad + os.pathsep + good
        got = rb._find_lisflood("", logs.append)
        if got == os.path.realpath(os.path.join(good, "lisflood")):
            assert any("refused" in m and bad in m for m in logs), logs
            assert any("not configured; using" in m for m in logs), logs
        else:      # a vetted /usr/local/bin/lisflood can never come before PATH; anything else is wrong
            raise AssertionError((got, logs))
        # only the unsafe one: nothing is chosen
        os.environ["PATH"] = bad
        logs = []
        got = rb._find_lisflood("", logs.append)
        assert got in ("",) or os.path.dirname(got) not in (bad,), (got, logs)
        # a configured path wins and is logged with its absolute path
        logs = []
        cfg = os.path.join(good, "lisflood")
        assert rb._find_lisflood(cfg, logs.append) == os.path.realpath(cfg)
        assert "configured" in logs[0]
    finally:
        os.environ["PATH"] = old_path
        if old_env is not None:
            os.environ["NDBREACH_LISFLOOD"] = old_env
        os.chmod(os.path.join(d, "bad"), 0o755) if os.path.isdir(os.path.join(d, "bad")) else None
        shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn(); n += 1
            print("ok", name)
    print("OK ({} audit-fix tests)".format(n))
