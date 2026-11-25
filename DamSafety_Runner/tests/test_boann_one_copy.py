"""Runner 3.97.2 / DamSafety Core 1.7.0 / Boann 6.3.2: Boann lives once, in DamSafety_Core/boann.

- the Runner's core.metalflood / core.metalterrain are the very Core packages (one module object)
- `python -m <runner>.core.metalflood ...` and `-m ...core.metalterrain verify` still run (paste kits)
- the Runner lends Boann its channel builder and fill oracle (hydro/host.py): the SGC engine still
  builds channels from the DEM inside the Runner
- the engine "executables" are the Core files; an old core/metalflood/engine*.py path maps to them"""
import os
import subprocess
import sys
import tempfile

from nd_breach_runner.core import lisflood as lf
from nd_breach_runner.core import metalflood as mf
from nd_breach_runner.core import metalterrain as mt
from nd_breach_runner.core import solve_gate as sg
from nd_breach_runner.tests.registry import Skip, test

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@test
def boann_runner_paths_are_the_core_packages():
    import DamSafety_Core.boann.grid as core_grid
    import DamSafety_Core.boann.hydro as core_hydro
    from nd_breach_runner.core.metalflood import engine_subli
    assert mf is core_hydro and mt is core_grid
    assert engine_subli.__name__ == "DamSafety_Core.boann.hydro.engine_subli"
    assert lf.BOANN_DIR == os.path.dirname(os.path.abspath(core_hydro.__file__))
    shim = os.path.join(REPO, "DamSafety_Runner", "core", "metalflood")
    assert sorted(set(os.listdir(shim)) - {"__pycache__"}) == ["__init__.py"]


@test
def boann_python_dash_m_through_the_old_paths():
    env = dict(os.environ, PYTHONPATH=REPO)
    r = subprocess.run([sys.executable, "-m", "nd_breach_runner.core.metalflood", "version"],
                       cwd=tempfile.gettempdir(), env=env, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0 and ("boann " + mf.BOANN_VERSION) in r.stdout, (r.stdout, r.stderr)
    from nd_breach_runner.core.metalflood import backends as mfb
    if not (mfb.prebuilt_available() or mfb.find_compiler()) or os.name == "nt":
        raise Skip("no CPU build of BoannGrid here")
    r = subprocess.run([sys.executable, "-m", "nd_breach_runner.core.metalterrain", "verify",
                        "-backend", "cpu"], cwd=tempfile.gettempdir(), env=env,
                       capture_output=True, text=True, timeout=600)
    assert r.returncode == 0 and "VERDICT       : PASS" in r.stdout, (r.stdout, r.stderr)
    assert "vs terrain.priority_flood" in r.stdout, r.stdout       # the Runner lent its oracle


@test
def boann_sgc_builds_channels_from_the_dem_inside_the_runner():
    from nd_breach_runner.core.metalflood import cases, engine_sgc, backends as mfb
    from DamSafety_Core.boann.hydro import host
    assert host.get("channel_builder") is not None and host.get("priority_flood") is not None
    if not (mfb.prebuilt_available() or mfb.find_compiler()) or os.name == "nt":
        raise Skip("no CPU build of the engine here")
    par = cases.valley_deck(tempfile.mkdtemp(prefix="sgc1copy_"), sim_s=600.0)
    s = engine_sgc.run_par(par, backend="cpu")
    assert s["sgc_source"] == "sgc.build" and s["channel_cells"] > 0, s


@test
def boann_old_engine_paths_map_to_the_core_files():
    for name, exe in (("engine.py", lf.GPU_EXE), ("engine_fv.py", lf.GPU_FV_EXE),
                      ("engine_fv2.py", lf.GPU_FV2_EXE), ("engine_dg2.py", lf.GPU_DG2_EXE),
                      ("engine_sgc.py", lf.GPU_SGC_EXE), ("engine_subfv.py", lf.GPU_SUBFV_EXE),
                      ("engine_subli.py", lf.GPU_SUBLI_EXE)):
        assert os.path.isfile(exe), exe
        assert sg.is_inprocess_engine(exe), exe
        for old in ("/Users/x/QGIS/plugins/DamSafety_Runner/core/metalflood/" + name,
                    "C:\\x\\DamSafety_Runner\\core\\metalflood\\" + name):
            assert lf.resolve_exe(old) == exe, (old, lf.resolve_exe(old))
    assert lf.resolve_exe("/opt/lisflood/core/metalflood/lisflood") == \
        "/opt/lisflood/core/metalflood/lisflood"                  # only Boann's own file names
    from nd_breach_runner.damsuite import record
    for path in ("C:\\x\\core\\metalflood\\engine_sgc.py", "/p/DamSafety_Core/boann/hydro/engine_sgc.py"):
        blk = record._engine_block(None, {"software": {"lisflood_exe": {"path": path}}})
        assert "sub-grid channels" in blk["model"], blk
