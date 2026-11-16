"""3.20.7: the self-test report and the installation check say where the LISFLOOD-FP and ANUGA
paths in use came from (this tool's field, the suite setting, the Processing option or
auto-detection), and name a set path that was skipped because it is not a file. On a Mac with an
older install the report showed the old engines "used" while the settings named the new ones,
with no hint which source had won. Also: an ANUGA environment that loads two OpenMP runtimes
("OMP: Error #15") gets a hint that names the environment as the cause."""
import os
import tempfile

from nd_breach_runner.tests import fake_qgis as _fq
_fq.install()

from nd_breach_runner import algorithms as algs  # noqa: E402
from nd_breach_runner.core import anuga_model  # noqa: E402
from nd_breach_runner.tests.registry import test  # noqa: E402


class _Alg(algs._Base):
    def __init__(self, params=None):
        self._p = params or {}

    def _str(self, parameters, name, context):
        return self._p.get(name, "")


def _with(suite=None, proc=None, label=True):
    """Swap the suite store and the Processing options for dicts; returns a restore function.
    label: the suite value's label is the computer setting's too (the CI model-tests job sets
    NDBREACH_LISFLOOD, which the real lookup would name instead)."""
    old = (algs._suite_setting, algs._setting, algs._suite_source_label)
    algs._suite_setting = lambda key, default="": (suite or {}).get(key, default)
    algs._setting = lambda key, default="": (proc or {}).get(key, default) or default
    if label:
        algs._suite_source_label = lambda key: "suite setting ndsuite/" + key

    def restore():
        algs._suite_setting, algs._setting, algs._suite_source_label = old
    return restore


def _files(t, *names):
    out = []
    for n in names:
        p = os.path.join(t, n)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as fh:
            fh.write("x")
        out.append(p)
    return out


@test
def engine_sources_names_the_setting_that_won():
    with tempfile.TemporaryDirectory() as t:
        new_lf, old_lf, py = _files(t, "dam-suite/bin/lisflood", "old/lisflood", "envs/a/bin/python3.12")
        link = os.path.join(t, "envs", "a", "bin", "python")
        os.symlink(py, link)
        restore = _with({"lisflood_exe": new_lf}, {algs.SET_LISFLOOD: old_lf, algs.SET_ANUGA: link})
        try:
            lf_src, an_src, problems = _Alg().engine_sources({}, None, new_lf, py)
            assert lf_src == "suite setting ndsuite/lisflood_exe", lf_src
            assert an_src == "Processing option " + algs.SET_ANUGA, an_src
            assert problems == []
            # the tool's own field wins over both settings
            lf_src, an_src, _ = _Alg({"LISFLOOD_EXE": old_lf, "ANUGA_PYTHON": link}).engine_sources(
                {}, None, old_lf, py)
            assert lf_src == "this tool's LISFLOOD-FP field" and an_src == "this tool's ANUGA field"
            # no suite setting: the Processing option
            algs._suite_setting = lambda key, default="": default
            assert _Alg().engine_sources({}, None, old_lf, py)[0] == "Processing option " + algs.SET_LISFLOOD
        finally:
            restore()


@test
def engine_sources_reports_a_set_path_that_is_not_a_file():
    with tempfile.TemporaryDirectory() as t:
        old_lf, old_py = _files(t, "nd-dam-suite/bin/lisflood", "miniconda3/envs/anuga/bin/python3.12")
        gone = os.path.join(t, "dam-suite", "bin", "lisflood")
        dangling = os.path.join(t, "miniforge3", "envs", "ndanuga")
        os.makedirs(os.path.dirname(dangling))
        os.symlink(os.path.join(t, "micromamba", "envs", "ndanuga"), dangling)
        restore = _with({"lisflood_exe": gone},
                        {algs.SET_ANUGA: os.path.join(dangling, "bin", "python")})
        try:
            lf_src, an_src, problems = _Alg().engine_sources({}, None, old_lf, old_py)
            assert lf_src == "auto-detected: the suite setting ndsuite/lisflood_exe path is not a file", lf_src
            assert an_src.startswith("auto-detected: the Processing option"), an_src
            assert len(problems) == 2, problems
            assert gone in problems[0] and "does not exist" in problems[0] and old_lf in problems[0]
            assert "ANUGA Python set to" in problems[1] and old_py in problems[1]
            # a link whose target is gone says so. Nothing else found: find_lisflood hands the
            # set path back, so it is still the one in use, not "auto-detected" (review)
            restore2 = _with({}, {algs.SET_LISFLOOD: dangling, algs.SET_ANUGA: dangling})
            try:
                lf_src, an_src, problems = _Alg().engine_sources(
                    {}, None, dangling, os.path.realpath(dangling))
                assert "is a link to" in problems[0] and "does not exist" in problems[0], problems
                assert "is a folder" not in problems[0]
                assert "nothing else was found, so the runs still use this path" in problems[0]
                assert "instead" not in problems[0]
                assert lf_src == "Processing option {} (path is not a file; nothing auto-detected)".format(
                    algs.SET_LISFLOOD), lf_src
                assert an_src.endswith("(path is not a file; nothing auto-detected)"), an_src
                assert "auto-detected:" not in lf_src + an_src
            finally:
                restore2()
            # a folder named as the program
            restore3 = _with({"lisflood_exe": t}, {})
            try:
                assert "is a folder, not a program" in _Alg().engine_sources({}, None, old_lf, "")[2][0]
            finally:
                restore3()
        finally:
            restore()


@test
def engine_sources_nothing_set():
    restore = _with({}, {})
    try:
        assert _Alg().engine_sources({}, None, "/x/lisflood", "/x/python")[:2] == (
            "auto-detected (no path set)", "auto-detected (no path set)")
        assert _Alg().engine_sources({}, None, "", "") == (
            "none set, none found", "none set, none found", [])
        # 'builtin' is a choice, not a missing file
        restore2 = _with({"lisflood_exe": "builtin"}, {})
        try:
            src, _a, problems = _Alg().engine_sources({}, None, "builtin", "")
            assert src.endswith("(built-in engine)") and problems == []
        finally:
            restore2()
    finally:
        restore()


@test
def engine_sources_builtin_by_the_provider_flag_is_named():
    """review: the built-in engine chosen by NDBREACH_USE_BUILTIN is not 'auto-detected'."""
    from nd_breach_runner.core import lisflood as lfmod
    restore = _with({}, {algs.SET_BUILTIN: "1"})
    try:
        src, _a, problems = _Alg().engine_sources({}, None, lfmod.BUILTIN_EXE, "")
        assert src == "Processing option {} (built-in engine)".format(algs.SET_BUILTIN), src
        assert problems == []
    finally:
        restore()
    restore = _with({"lisflood_exe": "/gone/lisflood"}, {algs.SET_BUILTIN: "1"})
    try:
        src, _a, problems = _Alg().engine_sources({}, None, lfmod.BUILTIN_EXE, "")
        assert src.startswith("Processing option {} (built-in engine): the ".format(algs.SET_BUILTIN)), src
        assert len(problems) == 1 and "/gone/lisflood" in problems[0] and lfmod.BUILTIN_EXE in problems[0]
    finally:
        restore()


@test
def engine_sources_says_where_the_suite_value_came_from():
    """review: suite_config reads a project entry, then the computer setting, then the
    environment variable NDBREACH_LISFLOOD; the label names the one that gave the value."""
    from nd_breach_runner.damsuite import suite_config as sc
    old_source, old_env = sc.source, os.environ.get("NDBREACH_LISFLOOD")
    with tempfile.TemporaryDirectory() as t:
        (lf,) = _files(t, "bin/lisflood")
        restore = _with({"lisflood_exe": lf}, {}, label=False)
        try:
            for got, label in (("env", "environment variable NDBREACH_LISFLOOD"),
                               ("project", "project setting NDDamSuite/lisflood_exe"),
                               ("computer", "suite setting ndsuite/lisflood_exe"),
                               ("", "suite setting ndsuite/lisflood_exe")):
                sc.source = lambda key, project=None, g=got: g
                assert _Alg().engine_sources({}, None, lf, "")[0] == label, (got, label)
            # and with the real lookup: only the environment variable is set
            sc.source = old_source
            os.environ["NDBREACH_LISFLOOD"] = lf
            assert algs._suite_source_label("lisflood_exe") == "environment variable NDBREACH_LISFLOOD"
        finally:
            sc.source = old_source
            if old_env is None:
                os.environ.pop("NDBREACH_LISFLOOD", None)
            else:
                os.environ["NDBREACH_LISFLOOD"] = old_env
            restore()


@test
def omp_duplicate_runtime_gets_a_hint_naming_the_environment():
    tail = ("WARNING: Could not import mpi4py - defining sequential interface\n"
            "OMP: Error #15: Initializing libomp.dylib, but found libomp.dylib already initialized.\n")
    hint = anuga_model.omp_duplicate_hint(tail, "/Users/x/miniconda3/envs/anuga/bin/python3.12")
    assert "/Users/x/miniconda3/envs/anuga/bin/python3.12" in hint
    assert "two copies of the OpenMP runtime" in hint and "ndanuga" in hint
    assert "not a fix" in hint                     # never recommends KMP_DUPLICATE_LIB_OK
    assert anuga_model.omp_duplicate_hint("Traceback: ValueError", "/p") == ""
    assert anuga_model.omp_duplicate_hint("", "") == ""
    assert anuga_model.omp_duplicate_hint(tail).startswith("The ANUGA environment loads")
