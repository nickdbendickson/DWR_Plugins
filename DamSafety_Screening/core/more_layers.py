"""Individual GeoPackage paths passed to a processing tool as one string."""
import os


def uris(text):
    """Split 'a.gpkg|layername=a;;b.gpkg|layername=b' into paths."""
    return [p.strip() for p in str(text or "").split(";;") if p.strip()]


def pick(names, *cands):
    """The layer's own spelling of the first candidate it has, or None."""
    low = {str(n).lower(): n for n in (names or [])}
    for cand in cands:
        if cand and str(cand).lower() in low:
            return low[str(cand).lower()]
    return None


def layer_name(lyr):
    """QgsMapLayer.name() in QGIS; some test doubles store the name as a string."""
    n = getattr(lyr, "name", None)
    if callable(n):
        try:
            n = n()
        except Exception:
            n = ""
    return str(n or "layer")


def open_uris(text):
    """Open each GeoPackage URI. Returns (valid layers, URIs that did not open)."""
    from qgis.core import QgsVectorLayer
    layers, bad = [], []
    for uri in uris(text):
        stem = os.path.splitext(os.path.basename(uri.split("|", 1)[0]))[0] or "layer"
        lyr = QgsVectorLayer(uri, stem, "ogr")
        try:
            ok = bool(lyr.isValid())
        except Exception:
            ok = False
        if ok:
            layers.append(lyr)
        else:
            bad.append(uri)
    return layers, bad
