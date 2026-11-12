"""One copy of the shared code: `<Plugin>.damsuite` IS `DamSafety_Core.damsuite`.

Until Core 1.0.0 every DamSafety plugin vendored a byte-identical copy of `damsuite/` (and of
the shared window files), kept equal by tools/sync_damsuite.py and a pin. Now each plugin's
`damsuite/__init__.py` is a shim that calls `install(__name__)` here, and the real package lives
in this plugin only. The plugins keep writing `from .damsuite import x` and
`from ..damsuite.x import y`: both resolve to the very module objects of
`DamSafety_Core.damsuite`, so there is never a second copy of a module in one QGIS.

How: `install("DamSafety_Hazard.damsuite")` imports `DamSafety_Core.damsuite`, puts that package
object into `sys.modules["DamSafety_Hazard.damsuite"]` and registers the alias with a
`MetaPathFinder` (first on `sys.meta_path`) that answers every `DamSafety_Hazard.damsuite.<sub>`
with a spec whose loader returns the already-imported `DamSafety_Core.damsuite.<sub>` module.
Without the finder Python would find `<sub>.py` through the aliased package's `__path__` and
build a second module object under the alias name.

`importlib.reload()` of an aliased module reloads the core module (its `__spec__.name` is the
core name), in place, so every alias keeps pointing at the same object. `install()` is
idempotent. Plain Python 3.9+, no Qt: tests/test_alias.py runs it outside QGIS.
"""
import importlib
import importlib.abc
import importlib.machinery
import sys

CORE_PACKAGE = __name__.rsplit(".", 1)[0]          # "DamSafety_Core"
CORE_NAME = "DamSafety Core"                       # the plugin's name= (what to install)
_ALIASES = {}                                      # alias package name -> core package name


class _AliasLoader(importlib.abc.Loader):
    """Hands importlib the core module object instead of making a new one."""

    def __init__(self, core_name):
        self.core_name = core_name

    def create_module(self, spec):
        return importlib.import_module(self.core_name)

    def exec_module(self, module):
        pass                                       # already executed as the core module


class _AliasFinder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        for alias, core in _ALIASES.items():
            if fullname == alias or fullname.startswith(alias + "."):
                core_name = core + fullname[len(alias):]
                try:
                    mod = importlib.import_module(core_name)
                except ImportError:
                    return None                    # not a core module: the normal error follows
                spec = importlib.machinery.ModuleSpec(
                    fullname, _AliasLoader(core_name),
                    is_package=hasattr(mod, "__path__"))
                return spec
        return None


_FINDER = _AliasFinder()


def _ensure_finder():
    if _FINDER not in sys.meta_path:
        sys.meta_path.insert(0, _FINDER)


def install(alias_name, core_name=None):
    """Make the package `alias_name` (e.g. "DamSafety_Hazard.damsuite") be
    `DamSafety_Core.damsuite` (or `core_name`), submodules included. Returns the core package.
    Call it from the alias package's own __init__.py: importlib then returns the core package to
    whoever imported the alias, because the package exec replaced sys.modules[alias_name]."""
    core_name = core_name or CORE_PACKAGE + ".damsuite"
    try:
        core = importlib.import_module(core_name)
    except ImportError as e:
        raise ImportError("{} ({}) is not installed or does not import ({}): install the {} "
                          "plugin, which holds the shared code of the DamSafety plugins".format(
                              CORE_NAME, CORE_PACKAGE, e, CORE_NAME)) from None
    _ALIASES[alias_name] = core_name
    _ensure_finder()
    sys.modules[alias_name] = core
    # submodules already imported under the core name are visible under the alias too
    prefix = core_name + "."
    for name, mod in list(sys.modules.items()):
        if name.startswith(prefix) and mod is not None:
            sys.modules.setdefault(alias_name + name[len(core_name):], mod)
    return core


def aliases():
    """{alias package: core package} installed so far (for diagnostics and tests)."""
    return dict(_ALIASES)


def uninstall(alias_name):
    """Forget an alias (tests only): its sys.modules entries are dropped."""
    _ALIASES.pop(alias_name, None)
    for name in [n for n in sys.modules if n == alias_name or n.startswith(alias_name + ".")]:
        sys.modules.pop(name, None)
    if not _ALIASES and _FINDER in sys.meta_path:
        sys.meta_path.remove(_FINDER)
