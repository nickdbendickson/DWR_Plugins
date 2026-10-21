"""3.10.45: the arrival depth (ARRIVAL_FT) is 0.1 ft by default (was 1 ft).

REG_05.2026 5.4(c) asks for the arrival time at each critical feature. The Runner's arrival time
is when the depth first exceeds its arrival depth; at 1 ft a structure flooded 0.1-1 ft deep
(wet by the ND hazard 'any depth', 0.1 ft) had no arrival time - 9 of the 10 wet structures in
the DWR-2801 sample. 0.1 ft is the ND hazard wet depth, so every wet structure gets one. These
tests run the built-in engine on a small synthetic flood (seconds) at both depths."""
import importlib
import os
import tempfile

import numpy as np

from nd_breach_runner.core import lisflood as lf, workflow, impacts as impmod
from nd_breach_runner.core import anuga_model, hecras, hecras_engine, products
from nd_breach_runner.core.breach import Hydrograph
from nd_breach_runner.core.grid import Grid
from nd_breach_runner.core.units import ARRIVAL_DEPTH_FT, FT
from nd_breach_runner.tests.registry import test


def _defaults(fn, name):
    import inspect
    return inspect.signature(fn).parameters[name].default


@test
def arrival_depth_default_is_the_wet_depth_everywhere():
    assert ARRIVAL_DEPTH_FT == 0.1
    assert workflow.RunSettings().arrival_depth_ft == 0.1
    assert abs(lf.LisfloodSettings("x").arrival_depth_m - 0.1 * FT) < 1e-12
    assert abs(anuga_model.AnugaSettings("x").arrival_depth_m - 0.1 * FT) < 1e-12
    assert _defaults(hecras.read_mesh, "arrival_depth_ft") == 0.1
    assert abs(_defaults(hecras_engine.results, "arrival_m") - 0.1 * FT) < 1e-12
    assert _defaults(products.products_from_rasters, "arrival_depth_ft") == 0.1
    # the LISFLOOD-FP .par gets the same depth (inittm_depth, metres)
    d = tempfile.mkdtemp(prefix="arrpar_")
    st = lf.LisfloodSettings(lf.BUILTIN_EXE, sim_time_s=60.0)
    dem = Grid(np.full((4, 4), 10.0), 0.0, 40.0, 10.0)
    par = lf.write_inputs(d, dem, (5.0, 20.0), Hydrograph(np.array([0.0, 60.0]),
                                                          np.array([1.0, 0.0]), "t"), st)
    assert "inittm_depth 0.0305" in open(par, encoding="utf-8").read()
    # the CLI template
    import nd_breach_runner.scripts.ndbreach_cli as cli
    src = open(cli.__file__, encoding="utf-8").read()
    assert '"arrival_depth_ft": 0.1,' in src and '"arrival_depth_ft": 1.0,' not in src


@test
def arrival_depth_tools_start_at_the_wet_depth():
    from nd_breach_runner.tests import fake_qgis
    fake_qgis.install()
    from nd_breach_runner import algorithms as A, algorithms_hecras as AH
    from nd_breach_runner import algorithms_tools as AT
    for m in (A, AH, AT):
        importlib.reload(m)
    algs = [A.RunDamAlgorithm()]
    algs += [c() for m in (AH, AT) for c in vars(m).values()
             if isinstance(c, type) and c.__module__ == m.__name__ and hasattr(c, "initAlgorithm")
             and c.__name__.endswith("Algorithm")]
    seen = 0
    for a in algs:
        try:
            a.initAlgorithm()
        except Exception:
            continue
        p = {q.name(): q for q in a.parameterDefinitions()}.get("ARRIVAL_FT")
        if p is None:
            continue
        seen += 1
        assert p.defaultValue() == 0.1, (type(a).__name__, p.defaultValue())
        assert "starting value 1 is" not in p.description, (type(a).__name__, p.description)
    assert seen >= 3, seen          # run dam, HEC-RAS import, products from rasters


def _run(arrival_m=None):
    """The golden-baseline synthetic breach on the built-in engine; final arrays (SI)."""
    nr, nc, dx = 20, 100, 20.0
    X, Y = np.meshgrid((np.arange(nc) + 0.5) * dx, (np.arange(nr) + 0.5) * dx)
    z = 100.0 - 0.002 * X + 0.02 * np.abs(Y - nr * dx / 2)
    dem = Grid(z[::-1], 0.0, nr * dx, dx)
    hy = Hydrograph(np.array([0.0, 600.0, 1200.0]), np.array([30.0, 30.0, 0.0]), "test")
    kw = {} if arrival_m is None else {"arrival_depth_m": arrival_m}
    st = lf.LisfloodSettings(lf.BUILTIN_EXE, sim_time_s=1200.0, massint_s=60.0,
                             initial_tstep_s=1.0, **kw)
    wd = tempfile.mkdtemp(prefix="arrdepth_")
    par = lf.write_inputs(wd, dem, (30.0, nr * dx / 2 + 5), hy, st)
    lf.run(wd, par, st)
    env = lf.read_results(wd, dem, st, hy)
    return dem, st, workflow._final_arrays(env, st.arrival_depth_m)


@test
def arrival_depth_shallow_structures_get_an_arrival_time():
    dem, st, fin = _run()                       # default: 0.1 ft
    _, st1, old = _run(1.0 * FT)                # the old default
    d = fin["depth"] / FT
    shallow = np.isfinite(d) & (d >= 0.1) & (d < 1.0)
    deep = np.nan_to_num(d) >= 1.0
    assert shallow.sum() >= 10 and deep.any(), (int(shallow.sum()), int(deep.sum()))
    # every cell flooded 0.1-1 ft deep has an arrival time now; at 1 ft none did
    assert np.isfinite(fin["arrival"][shallow]).all()
    assert not np.isfinite(old["arrival"][shallow]).any()
    # deeper cells keep theirs, and arrive no later at the lower depth
    assert np.isfinite(fin["arrival"][deep]).all() and np.isfinite(old["arrival"][deep]).all()
    assert (fin["arrival"][deep] <= old["arrival"][deep] + 1e-6).all()
    # structures on shallow cells: flooded with an arrival time in the impacts summary
    rows, cols = np.nonzero(shallow)
    pick = list(range(0, len(rows), max(1, len(rows) // 5)))[:5]
    xy = [(dem.x0 + (cols[i] + 0.5) * dem.dx, dem.y0 - (rows[i] + 0.5) * dem.dx) for i in pick]
    rec = impmod.Receptors()
    rec.add_points(xy, ids=["S{}".format(i) for i in range(len(xy))])
    a = rec.sample(dem, fin["arrival"])
    assert np.isfinite(a).all(), a
    s, _ = impmod.summarize(rec, dem, fin, st.arrival_depth_m)
    assert s[impmod.KIND_STRUCTURE]["flooded"] == len(xy), s
    assert s[impmod.KIND_STRUCTURE]["earliest_arrival_hr"] is not None, s
    s1, _ = impmod.summarize(rec, dem, old, st1.arrival_depth_m)
    assert not s1.get(impmod.KIND_STRUCTURE), s1     # at 1 ft they were not counted at all
