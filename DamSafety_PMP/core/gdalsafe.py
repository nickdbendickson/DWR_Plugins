"""GDAL calls that behave the same whether GDAL's Python exceptions are on or off.

audit R3 (2026-11-07): `gdal.UseExceptions()` is process-wide in the GDAL bindings QGIS ships.
This plugin's core modules (pmp.py, gridutil.py, pothole.py) called it at import, so loading
the PMP plugin switched GDAL into exception mode for QGIS itself and every other plugin (code
written for the default mode checks `if ds is None` and meets an exception instead). The suite
policy (shared/damsuite/gdalsafe.py) is that no plugin changes the process mode. These helpers
give the core what it relied on: a missing file or a failed Open / Create / Warp raises
RuntimeError with GDAL's own message in BOTH modes, instead of an AttributeError on None in
the default mode. `gdal_exceptions()` turns exceptions on for one block and restores the
previous setting, for the few GDAL utilities (Warp, Polygonize) whose failures are only
reported through the error handler. A local copy of the damsuite helper: the PMP plugin does
not vendor damsuite. Only osgeo.
"""
from contextlib import contextmanager

# audit P3-PMP (0.4.3): pmp.py imports this module at top level, and pmp.py must import without
# osgeo so its pure parts and the light tests run outside QGIS. osgeo is therefore optional here;
# every helper below is only ever called on the GDAL path, where osgeo is present.
try:
    from osgeo import gdal, ogr
except ImportError:                                  # pragma: no cover - no GDAL here
    gdal = ogr = None


def last_error():
    """GDAL's last error message, '' when there is none (never raises)."""
    try:
        return (gdal.GetLastErrorMsg() or "").strip()
    except Exception:                                            # noqa
        return ""


def checked(result, what):
    """`result` of a GDAL / OGR call (Open, OpenEx, Create, CreateDataSource, Warp, ...);
    RuntimeError "<what> failed: <GDAL message>" when it is None."""
    if result is None:
        msg = last_error()
        raise RuntimeError("{} failed{}".format(what, ": " + msg if msg else ""))
    return result


def open_raster(path, update=False):
    """gdal.Open(path), or RuntimeError naming the file (in both GDAL modes)."""
    try:
        ds = gdal.Open(path, gdal.GA_Update if update else gdal.GA_ReadOnly)
    except RuntimeError as e:                    # exception mode: add the file name
        raise RuntimeError("GDAL cannot open {}: {}".format(path, e)) from None
    return checked(ds, "GDAL cannot open {} - opening".format(path))


def open_vector(path, update=False):
    """ogr.Open(path), or RuntimeError naming the file (in both GDAL modes)."""
    try:
        ds = ogr.Open(path, 1 if update else 0)
    except RuntimeError as e:
        raise RuntimeError("OGR cannot open {}: {}".format(path, e)) from None
    return checked(ds, "OGR cannot open {} - opening".format(path))


def layer(ds, name=None, what="the data source"):
    """ds.GetLayerByName(name) (or the first layer), or RuntimeError naming it."""
    lyr = ds.GetLayerByName(name) if name else ds.GetLayer(0)
    if lyr is None:
        raise RuntimeError("no layer {} in {}".format(repr(name) if name else 0, what))
    return lyr


def _uses_exceptions(module):
    try:
        return bool(module.GetUseExceptions())
    except Exception:                                            # noqa
        return False


@contextmanager
def gdal_exceptions():
    """GDAL / OGR Python exceptions on for the enclosed block only; the previous setting is
    restored afterwards (ogr before gdal: GDAL >= 3.7 keeps the handlers on a stack), so the
    process mode QGIS and the other plugins see is not changed."""
    switched = []
    for module in (gdal, ogr):
        if not _uses_exceptions(module):
            module.UseExceptions()
            switched.append(module)
    try:
        yield
    finally:
        for module in reversed(switched):
            try:
                module.DontUseExceptions()
            except Exception:                                    # noqa - never fails a run
                pass
