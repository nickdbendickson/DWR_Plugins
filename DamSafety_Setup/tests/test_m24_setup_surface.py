"""M24 (regression): the Setup plugin's suite-configuration surface must be wired into the
GUI. initGui has to create menu actions that open the setup/status dialog (open_status) and
the always-on status dock (show_dock -> status_dock.SuiteStatusDock), and unload has to
remove them again. Before the fix initGui created only the "Start here..." action, so
open_status and show_dock were dead code (reachable from no menu) and unload referenced a
self.dock_action that initGui never made.

PyQGIS is not importable in this sandbox (no PyQt, no qgis), so this parses plugin.py with
ast -- no import, no Qt -- and checks the wiring inside initGui and unload. Mirrors
DamSafety_Runner/tests/test_m14_home_action.py.

    python3 DamSafety_Setup/tests/test_m24_setup_surface.py
"""
import ast
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

PLUGIN = os.path.join(REPO, "DamSafety_Setup", "plugin.py")


def _method(name):
    """The ast.FunctionDef for method `name` on the plugin class in plugin.py."""
    src = open(PLUGIN, encoding="utf-8").read()
    for cls in (n for n in ast.walk(ast.parse(src)) if isinstance(n, ast.ClassDef)):
        for fn in (n for n in cls.body if isinstance(n, ast.FunctionDef)):
            if fn.name == name:
                return fn
    raise AssertionError("no {} method found in plugin.py".format(name))


def _connected_slots(fn):
    """attr names wired to any `.triggered.connect(self.<attr>)` inside fn."""
    slots = set()
    for c in ast.walk(fn):
        if (isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
                and c.func.attr == "connect"):
            for a in c.args:
                if isinstance(a, ast.Attribute):
                    slots.add(a.attr)
    return slots


def _added_to_menu(fn):
    """attr names passed as the action to self.iface.addPluginToMenu(...) inside fn."""
    added = set()
    for c in ast.walk(fn):
        if not (isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
                and c.func.attr == "addPluginToMenu"):
            continue
        for a in c.args:
            if isinstance(a, ast.Attribute):
                added.add(a.attr)
    return added


def _removed_from_menu(fn):
    added = set()
    for c in ast.walk(fn):
        if not (isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
                and c.func.attr == "removePluginMenu"):
            continue
        for a in c.args:
            if isinstance(a, ast.Attribute):
                added.add(a.attr)
    return added


def test_initgui_wires_the_setup_dialog():
    fn = _method("initGui")
    assert "open_status" in _connected_slots(fn), \
        "initGui never connects a menu action to self.open_status (setup/status dialog)"


def test_initgui_wires_the_status_dock():
    fn = _method("initGui")
    assert "show_dock" in _connected_slots(fn), \
        "initGui never connects a menu action to self.show_dock (status dock)"


def test_initgui_adds_both_actions_to_the_menu():
    added = _added_to_menu(_method("initGui"))
    assert "setup_action" in added, "initGui never adds the setup dialog action to the menu"
    assert "dock_action" in added, "initGui never adds the status dock action to the menu"


def test_unload_removes_both_actions_and_the_dock():
    fn = _method("unload")
    removed = _removed_from_menu(fn)
    assert "setup_action" in removed, "unload never removes the setup dialog action"
    assert "dock_action" in removed, "unload never removes the status dock action"
    assert any(isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
               and c.func.attr == "removeDockWidget" for c in ast.walk(fn)), \
        "unload never removes the dock widget"


if __name__ == "__main__":
    failed = 0
    for fn in (test_initgui_wires_the_setup_dialog,
               test_initgui_wires_the_status_dock,
               test_initgui_adds_both_actions_to_the_menu,
               test_unload_removes_both_actions_and_the_dock):
        try:
            fn()
            print("ok    ", fn.__name__)
        except AssertionError as e:
            failed += 1
            print("FAIL  ", fn.__name__, ":", e)
    print("\n{} tests, {} failed".format(4, failed))
    raise SystemExit(failed)
