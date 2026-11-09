"""GUI simplification, first steps: one suite home per QGIS, the settings grids open on the
engineer's own values, the Data plugin can be opened by the home and does not auto-download."""
import ast
import importlib.util
import os
import sys
import types

from nd_breach_runner.tests.registry import test as register  # "test" would be collected by pytest

# realpath: the suite is run through a nd_breach_runner -> DamSafety_Runner symlink in a
# temporary folder (/workspace/rt.sh), and the other plugins sit next to the real folder
REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
PLUGINS = ("DamSafety_Runner", "DamSafety_Screening", "DamSafety_Hazard")


def _open_home_module(plugin_dir):
    """suite_home.open_home and its helpers from the one copy (Core 1.0.0: each plugin's
    suite_home.py is a shim onto DamSafety_Core/ui/suite_home.py), without Qt. Exercised once
    per plugin as before: the registry in sys.modules is what makes the home window one."""
    src = open(os.path.join(REPO, "DamSafety_Core", "ui", "suite_home.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    keep = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in ("_shared", "_alive", "open_home")]
    mod = types.ModuleType("home_" + plugin_dir)
    exec(compile(ast.Module(body=keep, type_ignores=[]), plugin_dir, "exec"), mod.__dict__)
    return mod


class _Dlg(object):
    made = 0

    def __init__(self, **k):
        _Dlg.made += 1
        self.shown = 0

    def show(self):
        self.shown += 1

    def raise_(self):
        pass

    def activateWindow(self):
        pass


@register
def one_suite_home_for_all_three_plugins():
    sys.modules.pop("_damsuite_shared_windows", None)
    _Dlg.made = 0
    mods = [_open_home_module(p) for p in PLUGINS]
    for m in mods:
        m.SuiteHomeDialog = _Dlg
        m._alive = lambda d: d is not None
    plugs = [types.SimpleNamespace(home_dialog=None) for _ in mods]
    homes = [m.open_home(p, None, "x") for m, p in zip(mods, plugs)]
    assert _Dlg.made == 1 and homes[0] is homes[1] is homes[2] and homes[0].shown == 3
    assert all(p.home_dialog is homes[0] for p in plugs)
    # closed by Qt: the next opener makes a new one, and it is shared again
    for m in mods:
        m._alive = lambda d: False
    h2 = mods[1].open_home(plugs[1], None, "x")
    for m in mods:
        m._alive = lambda d: d is not None
    assert _Dlg.made == 2 and mods[2].open_home(plugs[2], None, "x") is h2
    sys.modules.pop("_damsuite_shared_windows", None)


@register
def settings_grids_open_on_your_values_and_copies_are_identical():
    # Core 1.0.0: one copy in DamSafety_Core/ui; each plugin's file is the three-line shim
    srcs = {open(os.path.join(REPO, p, "suite_settings.py"), "rb").read() for p in PLUGINS}
    assert len(srcs) == 1 and b"from DamSafety_Core.ui import suite_settings as _m" in srcs.pop()
    core = open(os.path.join(REPO, "DamSafety_Core", "ui", "suite_settings.py"), "rb").read()
    assert b"self.only_your.setChecked(True)" in core
    homes = {open(os.path.join(REPO, p, "suite_home.py"), "rb").read() for p in PLUGINS}
    assert len(homes) == 1 and b"from DamSafety_Core.ui import suite_home as _m" in homes.pop()


@register
def data_plugin_opens_from_the_home_and_does_not_auto_download():
    src = open(os.path.join(REPO, "DamSafety_Data", "plugin.py"), encoding="utf-8").read()
    cls = [n for n in ast.parse(src).body if isinstance(n, ast.ClassDef) and n.name == "NdDataUpdaterPlugin"][0]
    names = {n.name for n in cls.body if isinstance(n, ast.FunctionDef)}
    assert "run_plugin" in names and "show_settings" in names
    st = open(os.path.join(REPO, "DamSafety_Data", "settings.py"), encoding="utf-8").read()
    assert 'self._get("auto_check", False, bool)' in st
