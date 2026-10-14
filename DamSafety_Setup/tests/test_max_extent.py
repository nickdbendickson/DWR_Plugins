"""Max extent is a main (not Advanced) setting with a 20 mi default, in the Setup wizard
and in the Runner's run tool. Runs without QGIS:  python3 -m pytest DamSafety_Setup/tests"""
import ast
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from DamSafety_Setup import runner_params as rp  # noqa: E402


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


def test_setup_and_runner_defaults_agree():
    src = _src("DamSafety_Runner", "algorithms.py")
    assert rp.DEFAULT_MAX_EXTENT_MI == _module_constant(src, "DEFAULT_MAX_EXTENT_MI") == 20.0


def test_setup_passes_max_extent():
    assert rp.build_params({})["MAX_EXTENT_MI"] == 20.0
    assert rp.build_params({"max_extent_mi": 35.0})["MAX_EXTENT_MI"] == 35.0


def test_setup_run_page_shows_max_extent_outside_advanced():
    src = _src("DamSafety_Setup", "plugin.py")
    init = [n for n in ast.walk(ast.parse(src)) if isinstance(n, ast.ClassDef)
            and n.name == "RunPage"][0]
    body = ast.get_source_segment(src, init)
    assert "self.max_extent.setValue(DEFAULT_MAX_EXTENT_MI)" in body
    # added to the main layout, before the Advanced group box is created
    assert body.index("main.addRow(\"Max extent") < body.index("QGroupBox(\"Advanced")
    assert "box.addRow" not in body and "form.addRow(\"Max extent" not in body
    assert "\"max_extent_mi\": self.max_extent.value()" in body
