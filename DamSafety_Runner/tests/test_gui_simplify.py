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
    """suite_home.open_home and its helpers from one plugin's copy, without Qt."""
    src = open(os.path.join(REPO, plugin_dir, "suite_home.py"), encoding="utf-8").read()
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
    srcs = {open(os.path.join(REPO, p, "suite_settings.py"), "rb").read() for p in PLUGINS}
    assert len(srcs) == 1
    assert b"self.only_your.setChecked(True)" in srcs.pop()
    homes = {open(os.path.join(REPO, p, "suite_home.py"), "rb").read() for p in PLUGINS}
    assert len(homes) == 1


@register
def data_plugin_opens_from_the_home_and_does_not_auto_download():
    src = open(os.path.join(REPO, "DamSafety_Data", "plugin.py"), encoding="utf-8").read()
    cls = [n for n in ast.parse(src).body if isinstance(n, ast.ClassDef) and n.name == "NdDataUpdaterPlugin"][0]
    names = {n.name for n in cls.body if isinstance(n, ast.FunctionDef)}
    assert "run_plugin" in names and "show_settings" in names
    st = open(os.path.join(REPO, "DamSafety_Data", "settings.py"), encoding="utf-8").read()
    assert 'self._get("auto_check", False, bool)' in st


@register
def per_run_parameters_stay_out_of_the_settings_grid():
    """Usability plan change 2 (2026-11-08): the dam, scenario, reviewer and report-signature
    parameters name one run and are not settings - they are left out of the catalog the three
    settings grids show, while the engineer's thresholds stay in."""
    from nd_breach_runner import provider as prov, popup_logic as pl, suite_settings_tools as RT
    cat = RT.full_catalog(prov.ALGORITHMS, pl.PLUGIN_KEY, pl.settings_group, pl.TAB_ORDER)
    keys = {(s.tool, s.key) for s in cat}
    for gone in (("rundam", "DAM_ID"), ("rundam", "DAM_NAME"), ("products", "DAM_ID"),
                 ("submittal", "TITLE"), ("submittal", "ENGINEER"), ("validationrecord", "APPROVAL")):
        assert gone not in keys, gone
    for kept in (("rundam", "INCR_FT"), ("rundam", "LISFLOOD_EXE"), ("rundam", "CONTOURS_HR")):
        assert kept in keys, kept
    assert "DAM_ID" in RT.PER_RUN_KEYS and "REVIEWER" in RT.PER_RUN_KEYS
    # the tool dialogs still have them: the skip is the grid's, not the tool's
    for c in prov.ALGORITHMS:
        inst = RT.fresh(c)
        if RT.tool_name(inst) == "rundam":
            assert "DAM_ID" in {d["name"] for d in RT.param_defs(inst)}
    # the three plugins carry the same adapter (sync_ui)
    srcs = {open(os.path.join(REPO, p, "suite_settings_tools.py"), "rb").read() for p in PLUGINS}
    assert len(srcs) == 1 and b"PER_RUN_KEYS" in srcs.pop()


@register
def chain_steps_and_self_tests_are_hidden_from_the_toolbox_but_still_run():
    """Usability plan change 3 (2026-11-08): FlagHideFromToolbox on the tools only another tool
    or a window button runs; every tool stays registered and runnable."""
    from qgis.core import Qgis
    from nd_breach_runner import provider as prov, suite_settings_tools as RT
    names = {RT.tool_name(c()): c for c in prov.ALGORITHMS}
    assert prov.HIDDEN_FROM_TOOLBOX == {"selftest", "verification", "classify"}
    assert set(prov.HIDDEN_FROM_TOOLBOX) <= set(names)          # all still registered
    hidden = {n for n, c in names.items() if getattr(c, "_nd_hidden", False)}
    assert hidden == set(prov.HIDDEN_FROM_TOOLBOX), hidden
    # the fake QGIS has no such flag: flags() stays what the tool had (0), nothing breaks
    assert RT.hide_from_toolbox_flag() == 0
    assert names["selftest"]().flags() == names["rundam"]().flags() == 0
    # a QGIS with the flag: the hidden tools carry it, the others do not (the fake's
    # NoThreading is a string, so the tools with their own flags() get a number for the check)
    from nd_breach_runner import compat, algorithms_tools as AT
    Qgis.ProcessingAlgorithmFlag.HideFromToolbox = 1024
    saved = compat.ALG_NO_THREADING, AT.ALG_NO_THREADING
    compat.ALG_NO_THREADING = AT.ALG_NO_THREADING = 4
    try:
        assert RT.hide_from_toolbox_flag() == 1024
        assert names["selftest"]().flags() & 1024 and names["classify"]().flags() & 1024
        assert names["classify"]().flags() & 4                  # its own NoThreading kept
        assert not (names["rundam"]().flags() & 1024)
    finally:
        del Qgis.ProcessingAlgorithmFlag.HideFromToolbox
        compat.ALG_NO_THREADING, AT.ALG_NO_THREADING = saved
    # the hidden tools still build their parameters (a hidden tool is a runnable tool)
    inst = names["selftest"]()
    inst.initAlgorithm()
    assert RT.param_defs(inst)


@register
def every_suite_plugin_adds_the_one_dam_suite_home_entry():
    """Usability plan change 4 (2026-11-08): the Dam suite home had no menu entry in any plugin
    (Screening's helper was never called), so the front door was unreachable. Each of the three
    plugins' initGui now calls suite_home.add_home_action, which adds the one "Dam suite home…"
    entry of Plugins > Dam safety once per QGIS, and unload removes it (suite_actions). Hazard's
    plugin.py cannot be imported without QGIS here, so this reads the three files with ast."""
    for p in PLUGINS:
        src = open(os.path.join(REPO, p, "plugin.py"), encoding="utf-8").read()
        tree = ast.parse(src)
        fns = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
        assert "run_home" in fns and "initGui" in fns and "unload" in fns, p
        init_calls = {n.func.attr for n in ast.walk(fns["initGui"])
                      if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
        if p == "DamSafety_Screening":
            init_calls |= {n.func.attr for n in ast.walk(fns["_add_suite_menu"])
                           if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
            assert "_add_suite_menu" in init_calls, p
        assert "add_home_action" in init_calls, (p, init_calls)
        unload_calls = {n.func.attr for n in ast.walk(fns["unload"])
                        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
        assert "remove_suite_actions" in unload_calls, p
    home = open(os.path.join(REPO, "shared", "ui", "suite_home.py"), encoding="utf-8").read()
    assert 'SUITE_MENU = "Dam safety"' in home and '"NDSuiteHome"' in home


@register
def hazard_suite_tool_launchers_are_a_gear_submenu_not_buttons():
    """Usability plan change 8 (2026-11-08): the Hazard Classify view's nine Dam suite tool
    buttons (wider than the window; the whole tab scrolled sideways) are the gear's "Dam suite
    tools" submenu. Hazard's window cannot be built without QGIS here: ast over suite_tab.py
    and dialog.py."""
    src = open(os.path.join(REPO, "DamSafety_Hazard", "suite_tab.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    fns = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    assert "_build_suite_tools_menu" in fns and "_build_suite_group" in fns
    calls_group = {n.func.attr for n in ast.walk(fns["_build_suite_group"])
                   if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    names_group = {n.id for n in ast.walk(fns["_build_suite_group"]) if isinstance(n, ast.Name)}
    assert "QPushButton" in names_group                     # the dam.json… button stays
    assert "QGridLayout" not in names_group                 # the 9-button grid does not
    assert "setattr" not in calls_group                     # no suite_*_btn attributes
    calls_menu = {n.func.attr for n in ast.walk(fns["_build_suite_tools_menu"])
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert "addAction" in calls_menu and "TOOLS" in {
        n.id for n in ast.walk(fns["_build_suite_tools_menu"]) if isinstance(n, ast.Name)}
    assert "suite_tools_menu" in src and "suite_tool_actions" in src
    dlg = open(os.path.join(REPO, "DamSafety_Hazard", "dialog.py"), encoding="utf-8").read()
    assert "_build_suite_tools_menu(menu)" in dlg          # wired into the gear
    # the busy set names the menu, not nine button attributes; TOOLS still has the nine
    cls = [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "SuiteTabMixin"][0]
    busy = [n for n in cls.body if isinstance(n, ast.Assign) and n.targets[0].id == "BUSY_WIDGETS_2100"][0]
    busy_names = set(ast.literal_eval(busy.value))
    assert busy_names == {"suite_gate_check", "suite_gate_stop_check", "suite_record_btn", "suite_tools_menu"}
    tools = [n for n in tree.body if isinstance(n, ast.Assign) and n.targets[0].id == "TOOLS"][0]
    assert len(ast.literal_eval(tools.value)) == 9
