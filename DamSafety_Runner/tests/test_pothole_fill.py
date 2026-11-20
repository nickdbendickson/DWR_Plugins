"""3.35.0 (Nick, 2026-11-20): closed depressions (potholes) of the model DEM can start partly or
fully full (core/pothole_fill.py, setting pothole_start_fill; 0 = dry, the default). Synthetic
grids; the fill is checked against an independent numpy reconstruction."""
import numpy as np

from nd_breach_runner.tests.registry import test, model_test


def _reconstruct_d4(z):
    """Depression fill by iterated 4-neighbour erosion from the border (morphological
    reconstruction): a separate route from the priority flood under test."""
    ny, nx = z.shape
    w = np.full(z.shape, np.inf)
    w[0, :], w[-1, :], w[:, 0], w[:, -1] = z[0, :], z[-1, :], z[:, 0], z[:, -1]
    while True:
        m = w.copy()
        m[1:, :] = np.minimum(m[1:, :], w[:-1, :])
        m[:-1, :] = np.minimum(m[:-1, :], w[1:, :])
        m[:, 1:] = np.minimum(m[:, 1:], w[:, :-1])
        m[:, :-1] = np.minimum(m[:, :-1], w[:, 1:])
        new = np.maximum(z, m)
        if np.array_equal(new, w):
            return w
        w = new


@test
def pothole_fill_d4_matches_reconstruction_on_random_terrain():
    from nd_breach_runner.core import pothole_fill as pf
    rng = np.random.default_rng(7)
    z = rng.normal(0, 1, (60, 80)).cumsum(0).cumsum(1) * 0.05 + rng.normal(0, 0.3, (60, 80))
    assert np.allclose(pf.fill_d4(z), _reconstruct_d4(z))


@test
def pothole_fill_drains_only_across_faces():
    """A pit whose only lower neighbour is diagonal holds water in a D4 model."""
    from nd_breach_runner.core import pothole_fill as pf
    z = np.full((5, 5), 10.0)
    z[2, 2] = 5.0           # the pit
    z[1, 1] = 4.0           # diagonal, lower
    z[0, 1] = 3.0           # and on to the border
    f = pf.fill_d4(z)
    assert f[2, 2] == 10.0 and f[1, 1] == 4.0
    d, info = pf.start_fill(z, 1.0, 2.0)
    assert d[2, 2] == 5.0 and info["depressions"] == 1 and abs(info["volume_m3"] - 20.0) < 1e-9


@test
def pothole_fill_fraction_puts_that_share_of_each_volume_at_one_level():
    from nd_breach_runner.core import pothole_fill as pf
    y, x = np.mgrid[0:41, 0:41]
    z = np.minimum(10.0, 5.0 + 0.04 * ((x - 12) ** 2 + (y - 20) ** 2))      # bowl 1, floor 5
    z = np.minimum(z, 7.0 + 0.1 * ((x - 30) ** 2 + (y - 20) ** 2))          # bowl 2, floor 7
    z[20, 21] = 9.0          # a one-cell pit on the plateau: a third depression
    full, _ = pf.start_fill(z, 1.0, 10.0)
    cap = pf.fill_d4(z) - z
    assert np.allclose(full, np.where(cap > pf.MIN_DEPTH_M, cap, 0.0))
    for f in (0.1, 0.5, 0.9):
        d, info = pf.start_fill(z, f, 10.0)
        assert abs(info["volume_m3"] - f * info["capacity_m3"]) < 1e-6 * info["capacity_m3"]
        from scipy import ndimage
        lab, n = ndimage.label(cap > pf.MIN_DEPTH_M)
        assert n == info["depressions"] >= 2
        for k in range(1, n + 1):
            m = lab == k
            assert abs(d[m].sum() - f * cap[m].sum()) < 1e-6 * cap[m].sum()
            wet = m & (d > 0)
            lev = (z + d)[wet]
            assert np.ptp(lev) < 1e-9           # one flat water surface per depression
            assert np.all(z[m & ~wet] >= lev[0] - 1e-9)


@test
def pothole_fill_leaves_basins_open_to_nodata_and_defaults_dry():
    from nd_breach_runner.core import pothole_fill as pf
    z = np.full((7, 7), 10.0)
    z[2:5, 2:5] = 5.0
    d, info = pf.start_fill(z, 1.0, 1.0)
    assert info["depressions"] == 1 and np.allclose(d[2:5, 2:5], 5.0)
    z2 = z.copy()
    z2[3, 1] = np.nan          # the basin's rim opens onto NoData
    z2[3, 2] = 5.0
    d2, info2 = pf.start_fill(z2, 1.0, 1.0)
    assert info2["depressions"] == 0 and np.nansum(d2) == 0 and np.isnan(d2[3, 1])
    d0, info0 = pf.start_fill(z, 0.0, 1.0)
    assert np.all(d0 == 0) and info0["depressions"] == 0
    assert pf.describe(info0) == "Potholes start dry."
    try:
        pf.start_fill(z, 1.5, 1.0)
        raise AssertionError("fraction > 1 accepted")
    except ValueError:
        pass


@test
def pothole_fill_off_by_default_and_out_of_the_checkpoint_key():
    from nd_breach_runner.core import workflow
    st = workflow.RunSettings()
    assert st.pothole_start_fill == 0.0
    assert "pothole_start_fill" not in workflow.key_settings(st)
    st.pothole_start_fill = 0.5
    assert workflow.key_settings(st)["pothole_start_fill"] == 0.5


def _pothole_valley(x, y):
    """The synthetic valley with a 1.5 m deep pothole on the floodplain 600 m below the dam."""
    from nd_breach_runner.tests.synthetic import valley
    z = valley(x, y)
    r2 = (x - 600.0) ** 2 + (y - 150.0) ** 2
    return z - np.where(r2 < 100.0 ** 2, 1.5 * (1.0 - r2 / 100.0 ** 2), 0.0)


@model_test
def pothole_start_fill_runs_end_to_end_on_the_builtin_engine():
    """Full potholes: the run reports them, and the water already in the pothole is not
    counted as flood depth there (it is with dry potholes, where the breach water fills it)."""
    import json
    import os
    import shutil
    import tempfile
    from nd_breach_runner.core import breach as br
    from nd_breach_runner.core import lisflood as lf
    from nd_breach_runner.core import workflow
    from nd_breach_runner.tests.synthetic import SyntheticTerrain, npy_writer
    dm = br.DamData(dam_id="T1", dam_height_ft=25.0, storage_top_acft=500.0,
                    top_of_dam_ft=1850.0, surface_area_top_ac=60.0)
    out = {}
    d = tempfile.mkdtemp()
    try:
        for frac in (0.0, 1.0):
            st = workflow.RunSettings(cell_size_m=40.0, initial_length_mi=1.0, half_width_mi=0.4,
                                      max_iterations=1, lisflood_exe=lf.BUILTIN_EXE, threads=1,
                                      sim_time_hr=2.0, products=False, pothole_start_fill=frac)
            st.pothole_start_fill = frac
            r = workflow.run_dam(dm, (0.0, 0.0), st, SyntheticTerrain(_pothole_valley),
                                 npy_writer, os.path.join(d, "f{:g}".format(frac)))
            rep = json.load(open(r.report_json))
            dep = np.load(os.path.splitext(r.final["depth"])[0] + ".npy")
            meta = json.load(open(os.path.splitext(r.final["depth"])[0] + ".json"))
            col = int((600.0 - meta["x0"]) // meta["dx"])
            row = int((meta["y0"] - 150.0) // meta["dx"])
            out[frac] = (rep, float(dep[row, col]))
        rep0, d0 = out[0.0]
        rep1, d1 = out[1.0]
        assert "pothole_start_fill" not in rep0
        pf = rep1["pothole_start_fill"]
        assert pf["fraction"] == 1.0 and pf["depressions"] >= 1 and pf["volume_m3"] > 0
        assert any(w.startswith("Potholes start 100% full") for w in rep1.get("warnings", []))
        assert d1 < d0, (d0, d1)
    finally:
        shutil.rmtree(d, ignore_errors=True)
