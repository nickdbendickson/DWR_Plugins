"""Area boxes when the PROJ grid server cannot be reached (Screening 0.18.7).

With PROJ_NETWORK=ON (QGIS: Settings > Options > Transformations, or the environment) PROJ may
pick a network grid for a datum step, e.g. NAD83 -> NAD83(HARN) via us_noaa_ndhpgn.tif from
cdn.proj.org. Where that server cannot be reached, building or using the transform raises
RuntimeError or gives inf, so a DEM download stopped before it began. A download / tile box
needs no grid (a few metres either way are inside its buffer), so box() tries once as-is and,
on failure, again with the PROJ network off; the setting is then restored. Heights and DEM
values never pass through here. No QGIS imports."""
import math

from osgeo import osr


def _finite(value):
    try:
        return all(math.isfinite(v) for v in value)
    except TypeError:
        return math.isfinite(value)


def box(fn):
    """fn() -> a tuple of numbers (a box). Retried with the PROJ network off when it raises
    RuntimeError or returns a non-finite value while the network is on."""
    try:
        out = fn()
        if _finite(out):
            return out
        failure = RuntimeError("PROJ returned a non-finite box: {}".format(out))
    except RuntimeError as e:
        failure = e
    get = getattr(osr, "GetPROJEnableNetwork", None)
    put = getattr(osr, "SetPROJEnableNetwork", None)
    if get is None or put is None or not get():
        raise failure
    put(False)
    try:
        out = fn()
    finally:
        put(True)
    if not _finite(out):
        raise RuntimeError("PROJ returned a non-finite box: {}".format(out))
    return out
