"""DamSafety Core 1.0.0: `<Plugin>.damsuite` IS `DamSafety_Core.damsuite` (alias.py).

Plain Python, no Qt, no QGIS: a throw-away plugin package is written to a temp folder with the
exact two-line shim every DamSafety plugin ships, and imported from there beside the real
DamSafety_Core (the repo root). Run:

    python3 DamSafety_Core/tests/test_alias.py
"""
import importlib
import os
import shutil
import sys
import tempfile
import textwrap

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

SHIM = textwrap.dedent('''\
    """damsuite: the shared package of the DamSafety plugins, in the DamSafety Core plugin."""
    from DamSafety_Core import alias as _alias
    _alias.install(__name__)
    ''')
_FAILS = []


def check(label, ok, detail=""):
    print(("ok   " if ok else "FAIL ") + label + ("" if ok else " - " + str(detail)))
    if not ok:
        _FAILS.append(label)


def _write(root, rel, text):
    p = os.path.join(root, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write(text)


def _fake_plugin(root, name):
    """A plugin package with the shim and the import forms the real plugins use."""
    _write(root, name + "/__init__.py", "")
    _write(root, name + "/damsuite/__init__.py", SHIM)
    _write(root, name + "/user.py", textwrap.dedent('''\
        from .damsuite import tm11_curves, versions
        from .damsuite import loss            # not imported by damsuite/__init__.py
        from .damsuite.tm11_curves import CURVES
        from .damsuite.suite_fill import scan
        from . import damsuite
        '''))
    _write(root, name + "/core/__init__.py", "")
    _write(root, name + "/core/deep.py", textwrap.dedent('''\
        from ..damsuite import dam_record as dr
        from ..damsuite.units import FT
        '''))


def _forget(prefix):
    for n in [n for n in sys.modules if n == prefix or n.startswith(prefix + ".")]:
        sys.modules.pop(n, None)


def main():
    work = tempfile.mkdtemp(prefix="core_alias_")
    try:
        _fake_plugin(work, "Fake_Hazard")
        _fake_plugin(work, "Fake_Runner")
        sys.path.insert(0, work)
        import DamSafety_Core.damsuite as core
        from DamSafety_Core.damsuite import tm11_curves as core_tm11

        # --- both import forms, from two plugins, give the core's module objects -----------
        user = importlib.import_module("Fake_Hazard.user")
        deep = importlib.import_module("Fake_Hazard.core.deep")
        check("from .damsuite import x is the core module", user.tm11_curves is core_tm11)
        check("from .damsuite.x import y reads the core module", user.CURVES is core_tm11.CURVES)
        check("from . import damsuite is the core package", user.damsuite is core)
        check("a module __init__ does not import is still the core object",
              user.loss is sys.modules["DamSafety_Core.damsuite.loss"]
              and user.loss.__name__ == "DamSafety_Core.damsuite.loss")
        check("from .damsuite.suite_fill import scan", user.scan is core.suite_fill.scan)
        check("from ..damsuite import x (core/) is the core module",
              deep.dr is sys.modules["DamSafety_Core.damsuite.dam_record"])
        check("from ..damsuite.x import y (core/)", deep.FT == core.units.FT)
        other = importlib.import_module("Fake_Runner.user")
        check("a second plugin gets the same objects", other.tm11_curves is user.tm11_curves)
        check("sys.modules alias entries are the core objects",
              sys.modules["Fake_Hazard.damsuite"] is core and
              sys.modules["Fake_Runner.damsuite.tm11_curves"] is core_tm11)
        check("importlib.import_module of the alias name",
              importlib.import_module("Fake_Hazard.damsuite.units") is core.units)
        # a module nobody imported yet under the core name, first reached through an alias
        _forget("DamSafety_Core.damsuite.xlsx_lite")
        _forget("Fake_Hazard.damsuite.xlsx_lite")
        via_alias = importlib.import_module("Fake_Hazard.damsuite.xlsx_lite")
        check("alias import of a not-yet-imported module gives the core object",
              via_alias is sys.modules["DamSafety_Core.damsuite.xlsx_lite"]
              and via_alias.__name__ == "DamSafety_Core.damsuite.xlsx_lite")
        # a name that is not a damsuite module fails the normal way
        try:
            importlib.import_module("Fake_Hazard.damsuite.no_such_module")
            check("unknown submodule raises ImportError", False, "no error")
        except ImportError as e:
            check("unknown submodule raises ImportError", "no_such_module" in str(e), e)

        # --- one module object per module, whichever name it was reached by ------------------
        ids = {id(m) for n, m in sys.modules.items()
               if n.endswith(".damsuite.tm11_curves") and m is not None}
        check("exactly one tm11_curves module object in sys.modules", len(ids) == 1, ids)
        check("the alias package keeps the core __path__ (one folder)",
              list(user.damsuite.__path__) == [os.path.join(ROOT, "DamSafety_Core", "damsuite")],
              list(user.damsuite.__path__))
        check("selfcheck runs on the core folder", core.selfcheck() == [], core.selfcheck())

        # --- reload safety -------------------------------------------------------------------
        before = user.tm11_curves
        after = importlib.reload(sys.modules["Fake_Hazard.damsuite.tm11_curves"])
        check("reload of an aliased module reloads the core module in place",
              after is before and after is sys.modules["DamSafety_Core.damsuite.tm11_curves"])
        check("reload keeps the alias entries", sys.modules["Fake_Runner.damsuite.tm11_curves"] is after)
        again = importlib.reload(sys.modules["Fake_Hazard.damsuite"])
        check("reload of the alias package itself is harmless", again is core and
              sys.modules["Fake_Hazard.damsuite"] is core)
        from DamSafety_Core import alias
        n = sys.meta_path.count(alias._FINDER)
        check("install() twice leaves one finder on sys.meta_path", n == 1, n)

        # --- the core plugin absent: a clear message naming it -----------------------------
        _forget("Fake_Hazard")
        saved = {n: sys.modules.pop(n) for n in list(sys.modules) if n.startswith("DamSafety_Core")}
        saved_path = list(sys.path)
        sys.path[:] = [work] + [p for p in sys.path if os.path.abspath(p) != os.path.abspath(ROOT)]
        try:
            try:
                importlib.import_module("Fake_Hazard.damsuite")
                check("missing core raises ImportError", False, "no error")
            except ImportError as e:
                check("missing core raises ImportError naming DamSafety Core",
                      "DamSafety Core" in str(e) or "DamSafety_Core" in str(e), e)
        finally:
            sys.path[:] = saved_path
            sys.modules.update(saved)
            _forget("Fake_Hazard")
        # with the core back, the plugin imports again
        user2 = importlib.import_module("Fake_Hazard.user")
        check("the plugin imports again once the core is there", user2.damsuite is
              sys.modules["DamSafety_Core.damsuite"])
    finally:
        shutil.rmtree(work, ignore_errors=True)
    if _FAILS:
        print("{} check(s) failed".format(len(_FAILS)))
        return 1
    print("OK: alias")
    return 0


if __name__ == "__main__":
    sys.exit(main())
