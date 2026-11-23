"""0.21.0: the GPU engine as tool 7's engine "gpu". Its files are a copy of the Runner's
core/metalflood (checked byte for byte), it runs the LISFLOOD-FP ACC deck tool 7 writes, and its
results read like LISFLOOD-FP's. Runs on the CPU build of the kernels (a C++ compiler is needed;
skipped without one); on a Mac with a Metal GPU the engine picks the GPU."""
import os, shutil, sys, tempfile
import numpy as np
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from core import runner as rn, solver_io as sio  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
MINE = os.path.join(HERE, "..", "core", "metalflood")
RUNNER = os.path.join(HERE, "..", "..", "DamSafety_Runner", "core", "metalflood")
FILES = ("__init__.py", "__main__.py", "backends.py", "deckio.py", "engine.py", "kernels.metal",
         "cpu_kernels.cpp", "mf_shim.h", "engine_fv.py", "kernels_fv.metal", "cpu_kernels_fv.cpp",
         "engine_fv2.py", "kernels_fv2.metal", "cpu_kernels_fv2.cpp",
         "engine_dg2.py", "kernels_dg2.metal", "cpu_kernels_dg2.cpp",
         "engine_sgc.py", "kernels_sgc.metal", "cpu_kernels_sgc.cpp",
         "version.py", "cli.py", "boann.py")                # 0.38.0: Boann's standalone CLI

# the engine list: appended, older indices unchanged
assert rn.ENGINES[:4] == ("lisflood-acc", "lisflood-fv1", "builtin", "builtin-reservoir")
assert rn.ENGINES[4] == "gpu"
assert rn.deck_engine("gpu") and rn.deck_engine("lisflood-acc") and not rn.deck_engine("builtin")

if os.path.isdir(RUNNER):
    for f in FILES:
        with open(os.path.join(MINE, f), "rb") as a, open(os.path.join(RUNNER, f), "rb") as b:
            assert a.read() == b.read(), "core/metalflood/{} differs from the Runner's".format(f)
    extra = set(os.listdir(MINE)) - set(FILES) - {"__pycache__"}
    assert not extra, extra
    print("copy of the Runner's GPU engine: identical")
else:
    print("SKIP: the Runner is not beside Screening here (copy check)")

from core import metalflood as mf  # noqa: E402
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
