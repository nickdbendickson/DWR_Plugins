"""Security audit run-2 (2026-11-24): a suite settings workbook cannot choose which program a
tool starts. No QGIS.

A workbook is a file people pass around, and a project's saved Runner / Screening window state
used to name one. Its rows become the defaults of the tools' parameters
(suite_settings_tools.apply_file_defaults), so a row for LISFLOOD_EXE / ANUGA_PYTHON / EXE /
WBT_EXE made the next run start a program the workbook's author chose. Those parameters are now
never in the catalog, and the windows no longer save or read the workbook path in the project.

    python3 DamSafety_Core/tests/test_settings_program_keys.py
"""
import ast
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)

from DamSafety_Core.damsuite import settings as DS  # noqa: E402
from DamSafety_Core.ui import suite_settings_tools as ST  # noqa: E402

TMP = tempfile.mkdtemp(prefix="settings_program_keys_")
EVIL = "/tmp/not_the_solver"


class _P(object):
    def __init__(self, name, type_, default):
        self.name, self.type, self.defaultValue = name, type_, default
        self.options, self.minimum, self.maximum = None, None, None
        self.description, self.flags, self.isDestination, self.help = name, 0, False, ""


class _Alg(object):
    def __init__(self, name, params):
        self.name = name
        self._params = {p.name: p for p in params}


def _group(tool):
    return ("Run", 0)


def _rundam():
    return _Alg("rundam", [_P("LISFLOOD_EXE", "string", ""), _P("ANUGA_PYTHON", "string", ""),
                           _P("SIMH", "number", 24.0)])


def test_program_keys_are_never_in_the_catalog():
    alg = _Alg("t", [_P(k, "string", "") for k in sorted(ST.PROGRAM_KEYS)] + [_P("SIMH", "number", 1.0)])
    for skip in (ST.PER_RUN_KEYS, (), None, frozenset(["SIMH"])):
        keys = {s.key for s in ST.catalog_of(alg, "ndbreach", _group, skip_keys=skip)}
        assert not keys & ST.PROGRAM_KEYS, (skip, keys)
    assert {"LISFLOOD_EXE", "ANUGA_PYTHON", "EXE", "WBT_EXE"} <= ST.PROGRAM_KEYS


def test_a_workbook_row_does_not_set_the_solver():
    alg = _rundam()
    wb = DS.Settings({("ndbreach", "rundam", "LISFLOOD_EXE"): EVIL,
                      ("ndbreach", "rundam", "ANUGA_PYTHON"): EVIL,
                      ("ndbreach", "rundam", "SIMH"): 12.0})
    applied, bad = ST.apply_file_defaults(alg, "ndbreach", _group, settings=wb)
    assert dict(applied) == {"SIMH": 12.0}, (applied, bad)
    assert alg._params["LISFLOOD_EXE"].defaultValue == ""
    assert alg._params["ANUGA_PYTHON"].defaultValue == ""
    sim = _Alg("runbreachsim", [_P("EXE", "string", "")])
    ST.apply_file_defaults(sim, "ndscreen", _group,
                           settings=DS.Settings({("ndscreen", "runbreachsim", "EXE"): EVIL}))
    assert sim._params["EXE"].defaultValue == ""


def test_a_saved_csv_workbook_does_not_set_the_solver():
    path = os.path.join(TMP, "shared.csv")
    DS.Settings({("ndbreach", "rundam", "LISFLOOD_EXE"): EVIL,
                 ("ndbreach", "rundam", "SIMH"): 6.0}).save(path)
    wb = DS.Settings.load(path)
    alg = _rundam()
    applied, _bad = ST.apply_file_defaults(alg, "ndbreach", _group, settings=wb)
    assert alg._params["LISFLOOD_EXE"].defaultValue == "", applied
    assert alg._params["SIMH"].defaultValue == 6.0, applied


def _pops_settings_file(path, func):
    """True when function `func` of `path` calls <something>.pop("settings_file", ...)."""
    tree = ast.parse(open(os.path.join(ROOT, path), encoding="utf-8").read())
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == func:
            for c in ast.walk(node):
                if (isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
                        and c.func.attr == "pop" and c.args
                        and isinstance(c.args[0], ast.Constant) and c.args[0].value == "settings_file"):
                    return True
            return False
    raise AssertionError("{} has no {}".format(path, func))


def test_windows_keep_the_workbook_path_out_of_the_project():
    for path in ("DamSafety_Runner/dialog.py", "DamSafety_Screening/dialog.py"):
        for func in ("_save_state", "_load_state"):
            assert _pops_settings_file(path, func), "{}:{} must drop settings_file".format(path, func)


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print("ok", t.__name__)
    print("{} tests ok".format(len(tests)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
