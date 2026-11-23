"""Write the receptors tool 8 classified for one dam to a JSON file (known-class benchmark, 2026-11-23).

Off unless the environment variable DAMSAFETY_RECEPTOR_DUMP names a folder. Then tool 8 writes
<folder>/<dam_id>.json for every dam it classifies from flood rasters: the structures, roads,
railroads, lifelines and downstream dams it found wet (the exact lists it passed to
core/hazard.classify_nd), the settings it used and the class it got.

tools/benchmark_known_class/ keeps these files as fixtures. Classifying them again with any
classifier (this plugin's, the Runner's identical copy, DamIT Catalog presets, or a QGIS-free
rewrite) must give the same class and drivers; see that folder's README.

Only the standard library is used; no QGIS.
"""
import json
import os
import re
from dataclasses import asdict, is_dataclass

ENV = "DAMSAFETY_RECEPTOR_DUMP"
SCHEMA = "damsafety-receptors/1"


def folder():
    """The dump folder, or "" when dumping is off."""
    return (os.environ.get(ENV) or "").strip()


def _plain(v):
    """A JSON value for an attribute: QVariant NULL and NaN become None, numbers stay numbers,
    anything else becomes its text."""
    if v is None or isinstance(v, (bool, int, str)):
        return v
    if isinstance(v, float):
        return None if v != v else v
    try:                                    # QVariant NULL (PyQGIS) without importing QGIS
        if hasattr(v, "isNull") and v.isNull():
            return None
    except Exception:                       # noqa
        pass
    if isinstance(v, (list, tuple)):
        return [_plain(x) for x in v]
    if isinstance(v, dict):
        return {str(k): _plain(x) for k, x in v.items()}
    try:
        f = float(v)
        return None if f != f else f
    except (TypeError, ValueError):
        return str(v)


def safe_name(dam_id):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", str(dam_id)) or "dam"


def record(dam_id, settings, structures, roads, railroads, lifelines, downstream_dams, result=None,
           extra=None):
    """The JSON-ready dict of one dam's receptors (see the module doc)."""
    dd = [asdict(d) if is_dataclass(d) else d for d in downstream_dams or ()]
    rec = {"schema": SCHEMA, "dam_id": str(dam_id), "settings": _plain(dict(settings or {})),
           "structures": _plain(list(structures or ())), "roads": _plain(list(roads or ())),
           "railroads": _plain(list(railroads or ())), "lifelines": _plain(list(lifelines or ())),
           "downstream_dams": _plain(dd)}
    if result is not None:
        rec["result"] = _plain(result)
    if extra:
        rec.update(_plain(dict(extra)))
    return rec


def write(dam_id, settings, structures, roads, railroads, lifelines, downstream_dams, result=None,
          extra=None, out_dir=None):
    """Write <folder>/<dam_id>.json when dumping is on; return the path or ""."""
    d = out_dir or folder()
    if not d:
        return ""
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, safe_name(dam_id) + ".json")
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(record(dam_id, settings, structures, roads, railroads, lifelines, downstream_dams,
                         result, extra), fh, indent=1, sort_keys=True)
    return p
