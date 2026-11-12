"""Pure-Python core of the ND PMP tool (no QGIS imports). Requires GDAL>=3.7 (osgeo), numpy.

0.4.3 (audit P3-PMP, 2026-11-07): the names are resolved lazily, so `from core.hydro import ...`
(numpy only) and the light tests work without GDAL; `from core import run_pmp, PMPConfig` still
works as before (the first use imports core.pmp)."""
import importlib

__all__ = ["run_pmp", "dad_lookup", "py2round", "py2round_array", "PMPConfig"]


def __getattr__(name):
    if name in __all__:
        mod = importlib.import_module(".pmp", __name__)
        return getattr(mod, name)
    raise AttributeError("module {!r} has no attribute {!r}".format(__name__, name))
