"""Max extent is a main (not Advanced) setting with a 20 mi default in the Runner's run tool
(Setup's own Run page, which also set it, went in Setup 1.0.14: Start here… continues into
the Runner's wizard, and the tool's dialog shows the setting). Runs without QGIS:
    python3 -m pytest DamSafety_Setup/tests"""
import ast
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)



def _src(*parts):
    return open(os.path.join(ROOT, *parts)).read()


def _module_constant(src, name):
    for node in ast.parse(src).body:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == name:
            return ast.literal_eval(node.value)
    raise AssertionError(name + " not found")


def _num_call(src, pname):
    for node in ast.walk(ast.parse(src)):
        if (isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "_num"
                and node.args and isinstance(node.args[0], ast.Constant)
                and node.args[0].value == pname):
            return node
    raise AssertionError(pname + " not declared")


def test_runner_default_is_20_mi_and_visible():
    src = _src("DamSafety_Runner", "algorithms.py")
    assert _module_constant(src, "DEFAULT_MAX_EXTENT_MI") == 20.0
    call = _num_call(src, "MAX_EXTENT_MI")
    assert ast.unparse(call.args[2]) == "DEFAULT_MAX_EXTENT_MI"
    adv = [k for k in call.keywords if k.arg == "advanced"]
    assert not adv or ast.literal_eval(adv[0].value) is False
