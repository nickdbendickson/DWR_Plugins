"""Download the layers all three plugins need, and connect a package in QGIS.

Writes the GeoPackage with GDAL's Python bindings. QGIS on macOS has those
bindings and does not put ogr2ogr on PATH.
"""
import json
import os
import urllib.parse
import urllib.request

try:
    from ..damsuite.servers import SERVERS, VECTOR_SOURCES
except ImportError:
    from damsuite.servers import SERVERS, VECTOR_SOURCES

try:
    from .dem_download import COUNTIES as _COUNTY_BOXES
except ImportError:
    _COUNTY_BOXES = {}

# Name -> west, south, east, north. The full North Dakota list, not one county.
COUNTIES = dict(_COUNTY_BOXES) or {
    "Eddy": (-99.25, 47.50, -98.45, 47.95),
}


def servers_path(gpkg):
    return os.path.splitext(gpkg)[0] + "_servers.json"


def write_servers(gpkg):
    path = servers_path(gpkg)
    json.dump({"servers": SERVERS, "vectors": [s["layer"] for s in VECTOR_SOURCES]},
              open(path, "w"), indent=2)
    return path


def _query_page(url, bbox, offset, page):
    west, south, east, north = bbox
    geom = {"xmin": west, "ymin": south, "xmax": east, "ymax": north,
            "spatialReference": {"wkid": 4326}}
    q = urllib.parse.urlencode({
        "where": "1=1", "geometry": json.dumps(geom), "geometryType": "esriGeometryEnvelope",
        "inSR": "4326", "spatialRel": "esriSpatialRelIntersects", "outFields": "*",
        "returnGeometry": "true", "f": "geojson",
        "resultRecordCount": str(page), "resultOffset": str(offset),
    })
    req = urllib.request.Request(url + "/query?" + q, headers={"User-Agent": "nd-dam-offline"})
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _query(url, bbox, feedback=None, name=""):
    """All features in the box. ArcGIS pages at 2,000 records."""
    feats = []
    offset = 0
    page = 2000
    while offset < 250000:
        fc = _query_page(url, bbox, offset, page)
        if "error" in fc:
            raise RuntimeError(fc["error"].get("message") or fc["error"])
        batch = fc.get("features") or []
        feats.extend(batch)
        if feedback is not None and (offset == 0 or len(feats) % 2000 == 0):
            feedback.pushInfo("{}: {} features".format(name, len(feats)))
        if len(batch) < page or not fc.get("exceededTransferLimit", len(batch) == page):
            if len(batch) < page:
                break
        offset += len(batch)
        if not batch:
            break
    return feats


def _to_gpkg(geojson_path, gpkg, layer_name):
    """GDAL Python, not the ogr2ogr program. QGIS always has the bindings."""
    from osgeo import gdal
    gdal.UseExceptions()
    exists = os.path.exists(gpkg)
    options = gdal.VectorTranslateOptions(
        format="GPKG",
        layerName=layer_name,
        dstSRS="EPSG:4326",
        accessMode="append" if exists else "overwrite",
    )
    gdal.VectorTranslate(gpkg, geojson_path, options=options)


def download_county(gpkg, county="Eddy", feedback=None):
    """Write one GeoPackage of the vector layers for a county, plus the server list."""
    key = county
    bbox = COUNTIES.get(key)
    if bbox is None:
        for name, box in COUNTIES.items():
            if name.lower() == str(county).lower():
                key, bbox = name, box
                break
    if bbox is None:
        raise ValueError("no box for county {}; known: {}".format(county, ", ".join(sorted(COUNTIES))))
    os.makedirs(os.path.dirname(os.path.abspath(gpkg)) or ".", exist_ok=True)
    if os.path.exists(gpkg):
        os.remove(gpkg)
    notes = []
    tmp = os.path.join(os.path.dirname(os.path.abspath(gpkg)) or ".", "_layers")
    os.makedirs(tmp, exist_ok=True)
    total = max(len(VECTOR_SOURCES), 1)
    for i, src in enumerate(VECTOR_SOURCES):
        if feedback is not None:
            feedback.setProgress(int(100 * i / total))
            feedback.pushInfo("downloading {} ({}/{})".format(src["name"], i + 1, total))
        geo = os.path.join(tmp, src["layer"] + ".geojson")
        try:
            feats = _query(src["url"], bbox, feedback, src["name"])
            if not feats:
                notes.append("{}: no features in {}".format(src["layer"], key))
                continue
            with open(geo, "w") as handle:
                json.dump({"type": "FeatureCollection", "features": feats}, handle)
            _to_gpkg(geo, gpkg, src["layer"])
            notes.append("{}: {}".format(src["layer"], len(feats)))
            if feedback is not None:
                feedback.pushInfo("{}: {} features written".format(src["layer"], len(feats)))
        except Exception as e:
            notes.append("{}: {}".format(src["layer"], e))
            if feedback is not None:
                feedback.reportError("{}: {}".format(src["layer"], e))
    if feedback is not None:
        feedback.setProgress(100)
    side = write_servers(gpkg)
    return {"gpkg": gpkg, "servers": side, "notes": notes, "county": key}


def connect_package(gpkg):
    """Add the package layers and the raster servers to the open QGIS project."""
    from qgis.core import QgsProject, QgsRasterLayer, QgsVectorLayer
    from urllib.parse import quote
    project = QgsProject.instance()
    added = []
    root = project.layerTreeRoot()
    group = root.findGroup("ND Dam offline") or root.addGroup("ND Dam offline")
    if os.path.isfile(gpkg):
        try:
            from osgeo import ogr
            ds = ogr.Open(gpkg)
            names = [ds.GetLayer(i).GetName() for i in range(ds.GetLayerCount())] if ds else []
        except Exception:
            names = []
        for name in names:
            layer = QgsVectorLayer("{}|layername={}".format(gpkg, name), name, "ogr")
            if layer.isValid():
                project.addMapLayer(layer, False)
                group.addLayer(layer)
                added.append(name)
    side = servers_path(gpkg)
    servers = SERVERS
    if os.path.isfile(side):
        servers = json.load(open(side)).get("servers") or SERVERS
    for s in servers:
        if s["kind"] == "xyz":
            src = "type=xyz&url={}&zmin=0&zmax=19".format(quote(s["url"], safe=":/{}"))
            layer = QgsRasterLayer(src, s["name"], "wms")
        elif s["kind"] == "wms":
            src = "contextualWMSLegend=0&crs=EPSG:3857&dpiMode=7&format=image/png&styles=&url={}".format(
                quote(s["url"], safe=""))
            layer = QgsRasterLayer(src, s["name"], "wms")
        else:
            src = "url={}".format(s["url"])
            layer = QgsRasterLayer(src, s["name"], "arcgismapserver")
            if not layer.isValid():
                layer = QgsRasterLayer(src, s["name"], "wms")
        if layer.isValid():
            project.addMapLayer(layer, False)
            group.addLayer(layer)
            added.append(s["name"])
    return added
