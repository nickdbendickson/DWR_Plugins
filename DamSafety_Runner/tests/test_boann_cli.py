"""Runner 3.62.0: Boann as a standalone program (core/metalflood/cli.py, boann.py, version.py).

- boann_folder_imports_nothing_from_outside: every .py in Boann's engine folder (DamSafety_Core/
  boann/hydro since Runner 3.97.2; core/metalflood is the shim onto it) imports only the standard
  library, numpy and its own folder, with no exception (engine_sgc's channel builder comes in
  through hydro/host.py since 3.97.1, not by reaching up into the Runner).
- boann_standalone_copy_runs_bit_for_bit: the folder COPIED ALONE to a temp directory runs a deck
  with `python -I boann.py run ...` for every solver it can run standalone, and its result files
  are byte-identical to the in-plugin run_par's (the standalone path changes no number).
- boann_cli_solver_words_and_sgc_guard: the Runner's engine words pick the same solvers, the
  standalone sgc solver refuses a deck without channel files, the old `-m metalflood run.par` form
  still runs the ACC solver.
- boann_version_is_in_the_run_manifest: provenance.software_versions records software.boann for a
  Boann engine and nothing for LISFLOOD-FP."""
import ast
import filecmp
import glob
import os
import shutil
import subprocess
import sys
import tempfile

from nd_breach_runner.core import lisflood as lf
from nd_breach_runner.core import metalflood as mf
from nd_breach_runner.core import provenance
from nd_breach_runner.core.metalflood import backends as mfb
from nd_breach_runner.core.metalflood import cli
from nd_breach_runner.core.metalflood.cases import valley_deck
from nd_breach_runner.tests.registry import Skip, test

MF = os.path.dirname(os.path.abspath(mf.__file__))
STDLIB_OK = {"argparse", "ast", "ctypes", "ctypes.util", "dataclasses", "functools", "glob",
             "hashlib", "importlib", "importlib.util", "json", "math", "os", "platform", "re",
             "shutil", "struct", "subprocess", "sys", "sysconfig", "tempfile", "textwrap",
             "threading", "time", "typing", "warnings", "collections", "itertools", "heapq",
             "contextlib", "pathlib", "io", "uuid", "datetime", "logging", "site", "zlib",
             "numpy", "random", "fcntl", "errno", "stat", "traceback", "copy", "string", "atexit"}


def _need_cpu_build():
    if mfb.prebuilt_available():
        return
    if os.name == "nt":
        raise Skip("the engine's CPU build is not made on Windows (a release ships it prebuilt)")
    if not mfb.find_compiler():
        raise Skip("no C++ compiler for the engine's CPU build")


@test
def boann_folder_imports_nothing_from_outside():
    bad = []
    for path in sorted(glob.glob(os.path.join(MF, "*.py"))):
        tree = ast.parse(open(path, encoding="utf-8").read(), path)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.level == 1:
                    continue                                    # this folder
                if node.level >= 2:
                    names = {a.name for a in node.names}
                    bad.append("{}: from {}{} import {}".format(os.path.basename(path),
                               "." * node.level, node.module or "", sorted(names)))
                    continue
                mods = [node.module or ""]
            elif isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            else:
                continue
            for m in mods:
                if m not in STDLIB_OK and m.split(".")[0] not in STDLIB_OK:
                    bad.append("{}: import {}".format(os.path.basename(path), m))
    assert not bad, "Boann's engine folder imports from outside itself:\n" + "\n".join(bad)


def _standalone_copy(tmp):
    dst = os.path.join(tmp, "boann_alone")
    shutil.copytree(MF, dst, ignore=shutil.ignore_patterns("__pycache__", "prebuilt"))
    pre = os.path.join(MF, "prebuilt")
    if os.path.isdir(pre):                       # a release zip's shipped CPU build travels too
        shutil.copytree(pre, os.path.join(dst, "prebuilt"))
    return os.path.join(dst, "boann.py")


def _run_alone(launcher, args, cwd):
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    return subprocess.run([sys.executable, "-I", launcher] + args, cwd=cwd, env=env,
                          capture_output=True, text=True, timeout=600)


@test
def boann_standalone_copy_runs_bit_for_bit():
    _need_cpu_build()
    from nd_breach_runner.core.metalflood import engine_subfv, engine_subli
    runners = {"acc": mf.run_par, "fv": mf.run_par_fv, "fv2": mf.run_par_fv2,
               "dg2": mf.run_par_dg2, "subfv": engine_subfv.run_par,
               "subli": engine_subli.run_par}
    tmp = tempfile.mkdtemp(prefix="boann_cli_")
    try:
        launcher = _standalone_copy(tmp)
        r = _run_alone(launcher, ["version"], tmp)
        assert r.returncode == 0 and ("boann " + mf.BOANN_VERSION) in r.stdout, (r.stdout, r.stderr)
        for key, fn in runners.items():
            a = valley_deck(os.path.join(tmp, key, "plugin"), sim_s=900.0)
            b = valley_deck(os.path.join(tmp, key, "alone"), sim_s=900.0)
            fn(a, backend="cpu")
            js = os.path.join(tmp, key, "stats.json")
            r = _run_alone(launcher, ["run", b, "--solver", key, "--backend", "cpu", "-q",
                                      "--stats-json", js], tmp)
            assert r.returncode == 0, (key, r.stdout, r.stderr)
            ra, rb = os.path.join(tmp, key, "plugin", "results"), os.path.join(tmp, key, "alone",
                                                                                "results")
            names = sorted(os.listdir(ra))
            assert names == sorted(os.listdir(rb)), (key, names, os.listdir(rb))
            assert "run.max" in names and "run.mass" in names, names
            diff = [n for n in names if not filecmp.cmp(os.path.join(ra, n), os.path.join(rb, n),
                                                        shallow=False)]
            assert not diff, "{}: standalone results differ: {}".format(key, diff)
            import json
            st = json.load(open(js))
            assert st["boann_version"] == mf.BOANN_VERSION and st["solver"] == key, st
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@test
def boann_cli_solver_words_and_sgc_guard():
    for word, key in (("gpu", "acc"), ("GPU-FV", "fv"), ("gpu-fv2", "fv2"), ("gpu-dg2", "dg2"),
                      ("gpu-sgc", "sgc"), ("gpu-subfv", "subfv"), ("gpu-subli", "subli"),
                      ("shock", "fv")):
        assert cli.solver_key(word) == key, (word, cli.solver_key(word))
    try:
        cli.solver_key("lisflood")
        assert False, "expected CliError"
    except cli.CliError:
        pass
    _need_cpu_build()
    tmp = tempfile.mkdtemp(prefix="boann_cli_")
    try:
        launcher = _standalone_copy(tmp)
        par = valley_deck(os.path.join(tmp, "sgc"), sim_s=300.0)
        r = _run_alone(launcher, ["run", par, "--solver", "sgc", "--backend", "cpu"], tmp)
        assert r.returncode == 1 and "channel files" in r.stderr, (r.stdout, r.stderr)
        assert not os.path.exists(os.path.join(tmp, "sgc", "results", "run.max"))
        # the older form: a .par first runs the ACC solver, as before
        par = valley_deck(os.path.join(tmp, "legacy"), sim_s=300.0)
        r = _run_alone(launcher, [par, "-backend", "cpu"], tmp)
        assert r.returncode == 0, (r.stdout, r.stderr)
        assert os.path.isfile(os.path.join(tmp, "legacy", "results", "run.max"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@test
def boann_version_is_in_the_run_manifest():
    for exe, key in ((lf.GPU_EXE, "acc"), (lf.GPU_FV_EXE, "fv"), (lf.GPU_FV2_EXE, "fv2"),
                     (lf.GPU_DG2_EXE, "dg2"), (lf.GPU_SGC_EXE, "sgc"),
                     (lf.GPU_SUBFV_EXE, "subfv"), (lf.GPU_SUBLI_EXE, "subli")):
        b = provenance.software_versions(exe).get("boann")
        assert b and b["version"] == mf.BOANN_VERSION and b["solver"] == key, (exe, b)
        assert b["engine_version"], b
    assert "boann" not in provenance.software_versions("/opt/lisflood/lisflood")
    assert "boann" not in provenance.software_versions(lf.BUILTIN_EXE)
