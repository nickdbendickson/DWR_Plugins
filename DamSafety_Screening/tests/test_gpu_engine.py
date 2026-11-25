"""0.21.0: the GPU engine as tool 7's engine "gpu". It runs the LISFLOOD-FP ACC deck tool 7 writes,
and its results read like LISFLOOD-FP's. Runs on the CPU build of the kernels (a C++ compiler is
needed; skipped without one); on a Mac with a Metal GPU the engine picks the GPU.
Screening 0.60.2 (DamSafety Core 1.7.0): core/metalflood is no longer a copy of the Runner's but a
shim onto the one copy of Boann in DamSafety_Core/boann/hydro (tools/ci_checks.py "one Boann
copy" guards it); this checks Screening gets that very package."""
import os, shutil, sys, tempfile
import numpy as np
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))   # DamSafety_Core
from core import runner as rn, solver_io as sio  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
MINE = os.path.join(HERE, "..", "core", "metalflood")

# the engine list: appended, older indices unchanged
assert rn.ENGINES[:4] == ("lisflood-acc", "lisflood-fv1", "builtin", "builtin-reservoir")
assert rn.ENGINES[4] == "gpu"
assert rn.deck_engine("gpu") and rn.deck_engine("lisflood-acc") and not rn.deck_engine("builtin")

assert sorted(set(os.listdir(MINE)) - {"__pycache__"}) == ["__init__.py"], os.listdir(MINE)
from core import metalflood as mf  # noqa: E402
import DamSafety_Core.boann.hydro as _core_boann  # noqa: E402
assert mf is _core_boann, (mf, _core_boann)
print("core/metalflood is DamSafety Core's Boann ({})".format(mf.BOANN_VERSION))
ok, why = mf.available()
if not ok:
    print("SKIP: " + why); sys.exit(0)

cell, ny, nx = 20.0, 60, 40
yy, xx = np.mgrid[0:ny, 0:nx]
Z = 100.0 - 0.003 * yy * cell + 0.0008 * (np.abs(xx - nx / 2) * cell) ** 1.6
Z[0, :] = Z[-1, :] = sio.NODATA; Z[:, 0] = Z[:, -1] = sio.NODATA
pts = [((nx / 2 + k) * cell, (ny - 6) * cell) for k in (-1, 0, 1)]
t, q = [0.0, 600.0, 1800.0, 3600.0], [0.0, 150.0, 60.0, 0.0]
d = tempfile.mkdtemp()
try:
    sio.write_ascii_grid(os.path.join(d, "dem.asc"), Z, 0.0, 0.0, cell)
    sio.write_bci(os.path.join(d, "run.bci"), pts)
    sio.write_bdy(os.path.join(d, "run.bdy"), "bc1", t, q, len(pts), cell)
    os.makedirs(os.path.join(d, "out"))
    sio.write_par(os.path.join(d, "run.par"), "dem.asc", "run", "out", 3600.0, 3600.0, "acc", 0.05)
    rn.run_gpu("run.par", d, timeout_s=600)
    out = sio.read_results(os.path.join(d, "out"), "run", "acc", 3600.0)
    wet = out["depth"] > 0.05
    assert wet.sum() > 50, wet.sum()
    assert np.isfinite(out["vel"][wet]).all() and out["vel"].max() > 0.1
    assert (out["arrive_h"][wet] >= 0).all()
    with open(os.path.join(d, "lisflood.log")) as f:
        assert f.readline().startswith(mf.NAME)
        log = f.read()
    assert "volume error" in log, log
    # a cancel stops it
    try:
        rn.run_gpu("run.par", d, timeout_s=600, cancel=lambda: True)
        assert False, "not cancelled"
    except rn.Cancelled:
        pass
    print("GPU engine runs tool 7's deck: {} wet cells, max {:.2f} m, {:.2f} m/s ({})".format(
        int(wet.sum()), float(out["depth"].max()), float(out["vel"].max()), why))
finally:
    shutil.rmtree(d, True)
print("ok")
