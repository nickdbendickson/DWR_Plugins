"""M14 (regression): initGui must create the 'Dam suite home...' menu action wired to
run_home, so the Dam suite home window stays reachable. Before the fix initGui created no
such action (run_home was dead code), in both DamSafety_Runner and DamSafety_Hazard.

PyQGIS is not importable in this sandbox, so this parses plugin.py with ast (no import, no
Qt) and checks, inside each plugin's initGui: a QAction labelled "Dam suite home..." is
created, its triggered signal is connected to self.run_home, and it is added to a plugin
menu with addPluginToMenu. Mirrors the siblings nd_breach_runner/plugin.py and
ND_Dam_Hazard_Helper/plugin.py.

    python3 DamSafety_Runner/tests/test_m14_home_action.py
"""
import ast
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if REPO not in sys.path:                 # so 'nd_breach_runner' resolves when run directly
    sys.path.insert(0, REPO)

from nd_breach_runner.tests.registry import test

HOME_LABEL = "Dam suite home..."


def _init_gui(plugin_dir):
    """The ast.FunctionDef for initGui in <plugin_dir>/plugin.py (the method on the plugin
    class, not a stray module function)."""
    src = open(os.path.join(REPO, plugin_dir, "plugin.py"), encoding="utf-8").read()
    for cls in (n for n in ast.walk(ast.parse(src)) if isinstance(n, ast.ClassDef)):
        for fn in (n for n in cls.body if isinstance(n, ast.FunctionDef)):
            if fn.name == "initGui":
                return fn
    raise AssertionError(plugin_dir + ": no initGui method found")


def _calls(fn, dotted):
    """Every Call node in fn whose function is the attribute chain `dotted`
    (e.g. 'self.iface.addPluginToMenu')."""
    want = dotted.split(".")

    def chain(node):
        parts = []
        while isinstance(node, ast.Attribute):
            parts.append(node.attr)
            node = node.value
        if isinstance(node, ast.Name):
            parts.append(node.id)
        return list(reversed(parts))

    return [n for n in ast.walk(fn)
            if isinstance(n, ast.Call) and chain(n.func) == want]


def _has_home_everywhere(plugin_dir):
    fn = _init_gui(plugin_dir)
    # (1) a QAction labelled "Dam suite home..." is created in initGui
    strings = [n.value for n in ast.walk(fn)
               if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    assert HOME_LABEL in strings, \
        "{}: initGui creates no '{}' action".format(plugin_dir, HOME_LABEL)
    # (2) run_home is wired to a .triggered signal in initGui
    wired = [c for c in ast.walk(fn)
             if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
             and c.func.attr == "connect"
             and any(isinstance(a, ast.Attribute) and a.attr == "run_home" for a in c.args)]
    assert wired, "{}: initGui never connects a menu action to run_home".format(plugin_dir)
    # (3) self.home_action is added to a plugin menu in initGui
    added = _calls(fn, "self.iface.addPluginToMenu")
    assert any(isinstance(a, ast.Attribute) and a.attr == "home_action"
               for c in added for a in c.args), \
        "{}: initGui never adds self.home_action to a menu".format(plugin_dir)


@test
def m14_runner_initgui_adds_the_home_menu_action():
    _has_home_everywhere("DamSafety_Runner")


@test
def m14_hazard_initgui_adds_the_home_menu_action():
    _has_home_everywhere("DamSafety_Hazard")


def _run():
    failed = 0
    for fn in (m14_runner_initgui_adds_the_home_menu_action,
               m14_hazard_initgui_adds_the_home_menu_action):
        try:
            fn()
            print("ok    ", fn.__name__)
        except AssertionError as e:
            failed += 1
            print("FAIL  ", fn.__name__, ":", e)
    print("\n{} tests, {} failed".format(2, failed))
    return failed


if __name__ == "__main__":
    raise SystemExit(_run())
