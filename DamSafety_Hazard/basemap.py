# -*- coding: utf-8 -*-
"""Aerial imagery base map: the Review tab and (v2.9) the report maps.

When the engineer zooms to a structure, an aerial photo under the map shows
what the structure is (house, shed, grain bin, ruin) - the question the
review is about. The imagery is an XYZ tile layer added once, at the bottom
of the layer tree (below the engineer's own layers and the plugin's groups),
so it never hides anything. It is a picture for looking at, not data the
classification uses; if it cannot be added (no network, unsupported QGIS
provider) the zoom goes on without it.

v2.9: the same layer goes under every map of the PDF report and of the EAP
map book (layout_imagery / with_imagery). Those map items keep an explicit
layer list, so the imagery is drawn there whether or not it is switched on
in the Layers panel: when a layout adds it, it is added switched OFF (the
canvas does not change and does not download tiles) and marked; the Review
tab's zoom switches such a layer on the first time it asks for it. A layer
already in the project is used as it is, visibility included. Offline (the
"offline only" setting, or the imagery service does not answer a one-tile
probe) the maps are made without imagery and the caller gets a note.

The service address is AERIAL_IMAGERY_URL in constants.py and can be changed
in the data-source file (Readme tab > Edit data sources).

2.19.0 (layouts): a chain of sources, the first that answers wins - ND DWR NAIP
2025 (WMS), Esri World Imagery, the USGS tiles (AERIAL_SOURCES /
AERIAL_SOURCE_ORDER; a changed AERIAL_IMAGERY_URL still means only that
service). Esri street / town label tiles go ABOVE the flood layers. Offline:
pack imagery (<pack>/imagery/*.tif|*.mbtiles covering the map), else a
hillshade of the DEM when one is known, else none. The credit names the
source and the flight dates of the map extent (NAIP seamlines; Esri
identify) - see layout_basemap()."""

import math
import os
import time
from urllib.parse import quote, unquote, urlparse

from qgis.core import QgsProject, QgsRasterLayer

from . import constants as C
from .helpers import log_message

# a layer added switched off for the layouts carries this custom property
# (saved with the project) until something shows it on the canvas
HIDDEN_PROP = "nd_dam_hazard_helper/added_hidden"
PROBE_ZOOM = 12                # tile level of the one-tile service check
PROBE_TIMEOUT_MS = 8000        # transfer timeout of that check (Qt >= 5.15)
PROBE_TTL_S = 300.0            # a check is re-used this long (report + map book)
PROBE_TTL_FAIL_S = 60.0        # ... a failed one only this long (reconnected?)
_PROBES = {}                   # probe_key(url, request_url) -> (time, ok, text)
LABEL_PROBE_TIMEOUT_MS = 3000  # 2.19.1: one label tile must answer this fast before the
                               # label layers go on the maps (a dead label host made each
                               # layout export wait ~2 min and the credit still named labels)
HILLSHADE_EXAGGERATION = 2.0   # 2.19.1: vertical exaggeration of the offline hillshade (ND
                               # is flat); the z factor also converts degrees to metres
_IMAGE_MAGIC = (b"\x89PNG", b"\xff\xd8\xff", b"GIF8")


def valid_url(url):
    """True for an http(s) XYZ address with {x}, {y} and {z}."""
    url = str(url or "")
    return url.startswith("http") and all(k in url for k in ("{x}", "{y}", "{z}"))


def source_string(url=None, zmax=None):
    """The provider string of an XYZ layer."""
    url = url or C.AERIAL_IMAGERY_URL
    return "type=xyz&url={}&zmin=0&zmax={}".format(quote(url, safe=":/{}"),
                                                    int(zmax or C.AERIAL_MAX_ZOOM))


def url_of(source):
    """The tile address in an XYZ provider string (or `source` itself when
    it is already an address)."""
    s = str(source or "")
    if s.startswith("http"):
        return s
    for part in s.split("&"):
        if part.startswith("url="):
            return unquote(part[4:])
    return ""


def source_name(source=None):
    """Short name of the imagery service for the map credit: the name in
    AERIAL_IMAGERY_CREDIT for the built-in address, else the host of the
    address set in the data-source file (the plugin does not guess a
    provider's name)."""
    url = url_of(source) if source else C.AERIAL_IMAGERY_URL
    named = getattr(C, "AERIAL_IMAGERY_SOURCE_NAME", None)     # optional override
    if named:
        return str(named)
    builtin = url == C.AERIAL_IMAGERY_URL and "AERIAL_IMAGERY_URL" not in C.SOURCE_OVERRIDES
    if builtin:
        credit = str(C.AERIAL_IMAGERY_CREDIT or "")
        name = credit.split(":", 1)[-1].split("(")[0].strip()
        if name:
            return name
    try:
        host = urlparse(url).netloc
    except Exception:
        host = ""
    return host or "aerial imagery service"


def caption(source=None):
    """'Imagery: <service>' - the credit printed under a map with imagery."""
    return "Imagery: {}".format(source_name(source))


def find_layer():
    """The imagery layer already in the project, or None."""
    try:
        found = QgsProject.instance().mapLayersByName(C.AERIAL_IMAGERY_NAME)
    except Exception:
        return None
    return found[0] if found else None


def new_layer(url=None):
    """A new imagery layer (not added to the project), or None. Resampled
    bilinearly so printed maps are not blocky between tile zoom levels."""
    url = url or C.AERIAL_IMAGERY_URL
    layer = QgsRasterLayer(source_string(url), C.AERIAL_IMAGERY_NAME, "wms")
    if not layer.isValid():
        return None
    try:
        from qgis.core import QgsBilinearRasterResampler
        rf = layer.resampleFilter()
        rf.setZoomedInResampler(QgsBilinearRasterResampler())
        rf.setZoomedOutResampler(QgsBilinearRasterResampler())
    except Exception:
        pass
    return layer


def _added_hidden(layer):
    try:
        v = layer.customProperty(HIDDEN_PROP, False)
    except Exception:
        return False
    return str(v).lower() in ("1", "true", "yes")


def ensure_aerial(visible=True):
    """Add the imagery layer at the bottom of the layer tree when the project
    has none. Returns (state, text): 'present' (already there, left as the
    user has it), 'added', or 'failed' (text says why).

    visible=False (the layouts): a new layer is added switched off and
    marked; a later ensure_aerial() (the Review tab's zoom) switches such a
    layer on once - it was never the user's choice to hide it."""
    layer = find_layer()
    if layer is not None:
        if visible and _added_hidden(layer):
            set_visible(True)                  # also drops the mark
        return "present", "aerial imagery already in the project"
    url = C.AERIAL_IMAGERY_URL
    if not valid_url(url):
        return "failed", "the aerial imagery address needs {x}, {y} and {z}: " + str(url)
    try:
        layer = new_layer(url)
        if layer is None:
            return "failed", "QGIS could not open the aerial imagery service"
        project = QgsProject.instance()
        project.addMapLayer(layer, False)
        node = project.layerTreeRoot().addLayer(layer)   # last child = bottom of the tree
        if not visible and node is not None:
            node.setItemVisibilityChecked(False)
    except Exception as e:
        return "failed", "aerial imagery not added ({})".format(e)
    if not visible:
        try:
            layer.setCustomProperty(HIDDEN_PROP, True)
        except Exception:
            pass
    log_message("Added aerial imagery base map{}. {}".format(
        "" if visible else " (switched off; used by the report maps)", C.AERIAL_IMAGERY_CREDIT))
    return "added", C.AERIAL_IMAGERY_CREDIT


def set_visible(on):
    """Show or hide the imagery layer if it is in the project; returns True
    when there was one."""
    layer = find_layer()
    if layer is None:
        return False
    try:
        node = QgsProject.instance().layerTreeRoot().findLayer(layer.id())
        if node is not None:
            node.setItemVisibilityChecked(bool(on))
    except Exception as e:
        log_message("Aerial imagery visibility not changed: {}".format(e))
        return False
    try:
        layer.removeCustomProperty(HIDDEN_PROP)          # now the user's choice
    except Exception:
        pass
    return True


# ------------------------------------------------------------ v2.9: layouts --
SUITE_ENV_VARS = ("ND_DAM_SUITE", "ND_DAM_OFFLINE")   # pipeline.py: no 25-layer download


def _truthy(v):
    """The Runner's rule (core/basemap._truthy): only 1 / true / yes / on switch a flag on."""
    return str(v or "").strip().lower() in ("1", "true", "yes", "on")


def suite_offline():
    """2.19.1: ND_DAM_SUITE set (a suite folder: any non-empty value) or ND_DAM_OFFLINE
    true (1 / true / yes / on). ND_DAM_OFFLINE=0 / false / off no longer means offline."""
    return bool(str(os.environ.get("ND_DAM_SUITE") or "").strip()) or \
        _truthy(os.environ.get("ND_DAM_OFFLINE"))


def offline_only():
    """True when the plugin must not ask the internet: the 'offline only'
    setting, an offline pack configured, or ND_DAM_SUITE / ND_DAM_OFFLINE set
    (the same no-download rule the pipeline applies to the layer downloads).
    Lumo v1.6: the imagery probe used to go out in suite / offline-pack mode
    and could block up to PROBE_TIMEOUT_MS on a hung service."""
    if suite_offline():
        return True
    try:
        from .download import offline_config
        pack, only = offline_config()
        return bool(only) or bool(str(pack or "").strip())
    except Exception as e:
        log_message("Offline setting not read (imagery check goes ahead): {}".format(e))
        return False


def forget_probe():
    """Forget the cached service checks (tests; a changed connection)."""
    _PROBES.clear()


def center_lonlat(rect, crs):
    """(lon, lat) of the centre of `rect` (in `crs`), or None."""
    try:
        from qgis.core import QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsPointXY
        cx = (rect.xMinimum() + rect.xMaximum()) / 2.0
        cy = (rect.yMinimum() + rect.yMaximum()) / 2.0
        xf = QgsCoordinateTransform(crs, QgsCoordinateReferenceSystem("EPSG:4326"),
                                    QgsProject.instance().transformContext())
        p = QgsPointXY(cx, cy) if xf.isShortCircuited() else xf.transform(QgsPointXY(cx, cy))
        lon, lat = float(p.x()), float(p.y())
    except Exception:
        return None
    if not (math.isfinite(lon) and math.isfinite(lat) and -180 <= lon <= 180
            and -85 <= lat <= 85):
        return None
    return lon, lat


def tile_xy(lon, lat, z):
    """Web Mercator XYZ tile holding lon / lat at level z."""
    n = 2 ** int(z)
    x = int((lon + 180.0) / 360.0 * n)
    r = math.radians(lat)
    y = int((1.0 - math.log(math.tan(r) + 1.0 / math.cos(r)) / math.pi) / 2.0 * n)
    return min(max(x, 0), n - 1), min(max(y, 0), n - 1)


def tile_url(url, x, y, z):
    return str(url).replace("{x}", str(x)).replace("{y}", str(y)).replace("{z}", str(z))


def probe_key(url, request_url=None):
    """2.19.1: the cache key of a service check: the URL plus the request's own parameters
    (a WMS layer / format / CRS), without the map position (BBOX, WIDTH, HEIGHT), so two
    layers of one WMS endpoint are checked separately."""
    if not request_url:
        return str(url)
    from urllib.parse import parse_qsl, urlsplit
    q = sorted((k.upper(), v) for k, v in parse_qsl(urlsplit(str(request_url)).query,
                                                    keep_blank_values=True)
               if k.upper() not in ("BBOX", "WIDTH", "HEIGHT"))
    return "{}|{}".format(url, "&".join("{}={}".format(k, v) for k, v in q))


def probe(url=None, lonlat=None, timeout_ms=PROBE_TIMEOUT_MS, request_url=None):
    """(ok, text): does the imagery service answer with a tile image? One
    small request (the tile at the map centre, level PROBE_ZOOM; level 0
    without a centre) through QGIS's network settings (proxy, auth). The
    answer is kept PROBE_TTL_S seconds so the report and the map book of a
    run ask once. Never raises."""
    url = url or C.AERIAL_IMAGERY_URL
    if offline_only():
        return False, "offline mode (no internet requests)"
    if request_url is None and not valid_url(url):
        return False, "the imagery address needs {x}, {y} and {z}"
    now = time.time()
    key = probe_key(url, request_url)
    hit = _PROBES.get(key)
    if hit is not None and now - hit[0] < (PROBE_TTL_S if hit[1] else PROBE_TTL_FAIL_S):
        return hit[1], hit[2]
    z = min(PROBE_ZOOM, int(C.AERIAL_MAX_ZOOM or PROBE_ZOOM)) if lonlat else 0
    x, y = tile_xy(lonlat[0], lonlat[1], z) if lonlat else (0, 0)
    ok, text = False, ""
    try:
        from qgis.core import QgsBlockingNetworkRequest
        from qgis.PyQt.QtCore import QUrl
        from qgis.PyQt.QtNetwork import QNetworkRequest
        from .compat import NET_NO_ERROR, enum_value
        req = QNetworkRequest(QUrl(request_url or tile_url(url, x, y, z)))
        try:
            req.setTransferTimeout(int(timeout_ms))          # Qt >= 5.15
        except Exception:
            pass
        blocking = QgsBlockingNetworkRequest()
        # forceRefresh: a tile in QGIS's network cache must not pass for a connection
        t0 = time.time()
        err = blocking.get(req, True)
        if enum_value(err) != enum_value(NET_NO_ERROR):
            text = probe_error_text(blocking.errorMessage(), time.time() - t0, timeout_ms)
        else:
            content = blocking.reply().content()
            data = bytes(content) if isinstance(content, (bytes, bytearray)) else \
                bytes(content.data())
            if data.startswith(_IMAGE_MAGIC) or (data[:4] == b"RIFF" and data[8:12] == b"WEBP"):
                ok, text = True, "imagery service answered"
            else:
                text = "the imagery service did not send an image (a login page or an " \
                       "error message?)"
    except Exception as e:
        text = "the imagery service could not be checked ({})".format(e)
    _PROBES[key] = (now, ok, text)
    if not ok:
        log_message("Aerial imagery for the maps: " + text)
    return ok, text


def probe_error_text(message, elapsed_s, timeout_ms):
    """2.19.1: the text of a failed check. A request that ran to its timeout (or that Qt
    cancelled with "Operation canceled", how a transfer timeout ends) says it timed out."""
    msg = str(message or "").strip()
    limit = float(timeout_ms or 0) / 1000.0
    timed_out = (limit > 0 and elapsed_s >= 0.9 * limit) or "timed out" in msg.lower() \
        or "canceled" in msg.lower() or "cancelled" in msg.lower()
    if timed_out:
        return "the imagery service did not answer within {:g} s (timed out{})".format(
            limit if limit > 0 else round(elapsed_s, 1),
            "; " + msg if msg and "cancel" not in msg.lower() else "")
    return "the imagery service did not answer ({})".format(msg or "network error")


def layout_imagery(add_to_project=True, rect=None, crs=None):
    """The imagery layer for the maps of a print layout: (layer or None,
    caption, note). Kept for callers of v2.9; 2.19.0 layouts use
    layout_basemap() (source chain, labels, dates, offline fallback). Never raises."""
    bm = layout_basemap(add_to_project, rect, crs, labels=False)
    return bm["layer"], bm["caption"], bm["note"]


# ------------------------------------------------------- 2.19.0: source chain --
DATE_NOT_PUBLISHED = "date not published by the service"
WMS_PROBE_PX = 64


def sources(choice="auto"):
    """The imagery sources to try, in order (dicts of constants.AERIAL_SOURCES with
    "url" filled). choice: "auto" or a source id (only that one). A data-source file that
    changed AERIAL_IMAGERY_URL keeps the pre-2.19 meaning: only that XYZ service."""
    if "AERIAL_IMAGERY_URL" in C.SOURCE_OVERRIDES:
        return [{"id": "custom_xyz", "kind": "xyz", "url": C.AERIAL_IMAGERY_URL,
                 "zmax": C.AERIAL_MAX_ZOOM, "name": source_name(C.AERIAL_IMAGERY_URL),
                 "resolution": "", "credit": source_name(C.AERIAL_IMAGERY_URL),
                 "date_service": ""}]
    by_id = {}
    for src in C.AERIAL_SOURCES:
        d = dict(src)
        if d.get("id") == "usgs_imagery" and not d.get("url"):
            d["url"] = C.AERIAL_IMAGERY_URL
        by_id[d.get("id")] = d
    choice = str(choice or "auto").strip()
    order = [choice] if choice in by_id else list(C.AERIAL_SOURCE_ORDER)
    if choice not in ("auto", "") and choice not in by_id:
        log_message("Base map source '{}' is not known - the usual order is used".format(choice))
    return [by_id[k] for k in order if k in by_id and by_id[k].get("url")]


def layer_name(src):
    """Project layer name of a source (the USGS one is the Review tab's layer)."""
    if src.get("id") in ("usgs_imagery", "custom_xyz"):
        return C.AERIAL_IMAGERY_NAME
    return "Aerial imagery - {} (base map)".format(src.get("name") or src.get("id"))


def wms_crs(src, crs=None):
    """The CRS asked of a WMS: the map's when the service offers it, else EPSG:3857."""
    try:
        auth = crs.authid() if crs is not None else ""
    except Exception:
        auth = ""
    offered = [str(c).upper() for c in (src.get("crs_list") or ["EPSG:3857"])]
    return auth if auth and auth.upper() in offered else "EPSG:3857"


def wms_source_string(src, crs=None):
    """Provider string of a WMS imagery layer."""
    url = str(src["url"]).rstrip("?&")
    return ("contextualWMSLegend=0&crs={}&dpiMode=7&format={}&layers={}&styles=&url={}"
            .format(wms_crs(src, crs), src.get("format") or "image/jpeg",
                    src.get("layer") or "", url))


def wms_probe_url(src, lonlat):
    """A small GetMap (EPSG:4326, ~1 km) at the map centre - the WMS service check."""
    from urllib.parse import urlencode
    lon, lat = lonlat if lonlat else (-100.5, 47.5)
    d = 0.006
    q = {"SERVICE": "WMS", "VERSION": "1.1.1", "REQUEST": "GetMap",
         "LAYERS": src.get("layer") or "", "STYLES": "", "SRS": "EPSG:4326",
         "BBOX": "{:.6f},{:.6f},{:.6f},{:.6f}".format(lon - d, lat - d, lon + d, lat + d),
         "WIDTH": str(WMS_PROBE_PX), "HEIGHT": str(WMS_PROBE_PX),
         "FORMAT": src.get("format") or "image/jpeg"}
    return str(src["url"]).rstrip("?&") + "?" + urlencode(q)


def _style(layer):
    try:
        from qgis.core import QgsBilinearRasterResampler
        rf = layer.resampleFilter()
        rf.setZoomedInResampler(QgsBilinearRasterResampler())
        rf.setZoomedOutResampler(QgsBilinearRasterResampler())
    except Exception:
        pass
    return layer


def _open_layer(uri, name, provider):
    """QgsRasterLayer(uri, name, provider) - the one place web layers are opened (a WMS
    reads its capabilities here; the tests replace it)."""
    return QgsRasterLayer(uri, name, provider)


def source_uri(src, crs=None):
    """Provider string ("wms" provider) of an imagery source."""
    if src.get("kind") == "wms":
        return wms_source_string(src, crs)
    return source_string(src["url"], src.get("zmax"))


def source_layer(src, crs=None, name=None):
    """A new (private) QGIS layer of an imagery source, or None."""
    layer = _open_layer(source_uri(src, crs), name or layer_name(src), "wms")
    return _style(layer) if layer is not None and layer.isValid() else None


def label_checks(lonlat=None):
    """2.19.1: [(url, ok, text)] of the label services: one tile each (probe(), cached like
    the imagery checks, LABEL_PROBE_TIMEOUT_MS). A host that failed is not asked again for
    its other label service. Never raises."""
    out, dead = [], {}
    for url in C.AERIAL_LABEL_URLS or []:
        if not valid_url(url):
            out.append((url, False, "the label address needs {x}, {y} and {z}"))
            continue
        host = urlparse(str(url)).netloc
        if host in dead:
            out.append((url, False, dead[host]))
            continue
        try:
            ok, text = probe(url, lonlat, timeout_ms=LABEL_PROBE_TIMEOUT_MS)
        except Exception as e:
            ok, text = False, "the label service could not be checked ({})".format(e)
        if not ok:
            text = str(text).replace("the imagery service", "the label service")
            dead[host] = text
        out.append((url, ok, text))
    return out


def label_name(url):
    """2.19.1: a label service's short name for the run log: the ArcGIS service
    ('World_Transportation') or the host."""
    parts = [p for p in urlparse(str(url)).path.split("/") if p]
    if "MapServer" in parts and parts.index("MapServer") > 0:
        return parts[parts.index("MapServer") - 1]
    return urlparse(str(url)).netloc or str(url)


def label_layers_new(urls=None):
    """New (private) label layers, top first: XYZ with tilePixelRatio=2, i.e. tiles one
    zoom level coarser at double size (readable on paper), capped at AERIAL_LABEL_MAX_ZOOM.
    urls: the label services to use (2.19.1: the ones whose check passed); None = all."""
    out = []
    for i, url in enumerate(C.AERIAL_LABEL_URLS or []):
        if not valid_url(url) or (urls is not None and url not in urls):
            continue
        uri = source_string(url, C.AERIAL_LABEL_MAX_ZOOM) + "&tilePixelRatio=2"
        name = C.AERIAL_LABEL_NAME + ("" if i == 0 else " {}".format(i + 1))
        layer = _open_layer(uri, name, "wms")
        if layer is not None and layer.isValid():
            out.append(layer)
    return list(reversed(out))          # places above streets


def _project_layer(name, make):
    """The project layer called `name`, else make() added switched off at the bottom of
    the tree and marked HIDDEN_PROP (the Review tab's rule). None when it cannot be."""
    project = QgsProject.instance()
    found = project.mapLayersByName(name)
    if found:
        return found[0]
    layer = make()
    if layer is None:
        return None
    project.addMapLayer(layer, False)
    node = project.layerTreeRoot().addLayer(layer)
    if node is not None:
        node.setItemVisibilityChecked(False)
    try:
        layer.setCustomProperty(HIDDEN_PROP, True)
    except Exception:
        pass
    return layer


def _extent_4326(rect, crs):
    try:
        from qgis.core import QgsCoordinateReferenceSystem, QgsCoordinateTransform
        xf = QgsCoordinateTransform(crs, QgsCoordinateReferenceSystem("EPSG:4326"),
                                    QgsProject.instance().transformContext())
        r = rect if xf.isShortCircuited() else xf.transformBoundingBox(rect)
        return (r.xMinimum(), r.yMinimum(), r.xMaximum(), r.yMaximum())
    except Exception:
        return None


def http_get(url, timeout_s):
    """GET through QGIS's network stack (proxy, auth, bundled certificates); patched in
    the tests."""
    from . import download as DL
    return DL.http_get_bytes(url, DL.HTTP_USER_AGENT, timeout_s=timeout_s)


def _esri_dates(service, lonlat, timeout_s=8.0):
    """Esri World Imagery capture date(s) at the centre (identify, 'SRC_DATE'/'DATE')."""
    import datetime
    import json
    from urllib.parse import urlencode
    lon, lat = lonlat
    d = 0.01
    q = {"geometry": "{:.6f},{:.6f}".format(lon, lat), "geometryType": "esriGeometryPoint",
         "sr": "4326", "layers": "all", "tolerance": "2", "returnGeometry": "false",
         "mapExtent": "{},{},{},{}".format(lon - d, lat - d, lon + d, lat + d),
         "imageDisplay": "400,400,96", "f": "json"}
    raw = http_get(service.rstrip("/") + "/identify?" + urlencode(q), timeout_s)
    data = json.loads(raw.decode("utf-8", "replace"))
    out = set()
    for r in data.get("results") or []:
        a = r.get("attributes") or {}
        for k, v in a.items():
            ku = str(k).upper()
            if not (ku.startswith("SRC_DATE") or ku.startswith("DATE")
                    or ku.startswith("ACQUISITION")):
                continue
            v = str(v).strip()
            try:
                if len(v) == 8 and v.isdigit():
                    out.add(datetime.date(int(v[:4]), int(v[4:6]), int(v[6:])))
                else:
                    from .lone911 import parse_flight_date
                    dd = parse_flight_date(v)
                    if dd:
                        out.add(dd)
            except ValueError:
                pass
    return sorted(out)


def flight_dates(src, rect, crs):
    """(dates, text) of the source over the map extent. dates: sorted list ([] = none
    known); text: the 'flown ...' part of the credit. Never raises."""
    from . import lone911 as LN
    svc = str(src.get("date_service") or "")
    try:
        if svc.startswith("naip:"):
            ext = _extent_4326(rect, crs) if rect is not None and crs is not None else None
            if ext is None:
                return [], "flight date not read (no map extent)"
            dates = LN.imagery_dates(ext, year=int(svc.split(":", 1)[1]), get=http_get)
            return dates, ("flown " + LN.dates_text(dates) if dates else
                           "outside the NAIP flight-date seamlines")
        if svc.startswith("esri:"):
            ll = center_lonlat(rect, crs) if rect is not None and crs is not None else None
            dates = _esri_dates(svc.split(":", 1)[1], ll) if ll else []
            return dates, ("captured " + LN.dates_text(dates) if dates else DATE_NOT_PUBLISHED)
    except Exception as e:
        log_message("Imagery date not read ({}): {}".format(src.get("id"), e))
        return [], "flight date not read ({})".format(str(e)[:80])
    return [], DATE_NOT_PUBLISHED


def probe_source(src, lonlat):
    """(ok, text) of one source: a tile (XYZ) or a 64 px GetMap (WMS) must come back."""
    if src.get("kind") == "wms":
        return probe(src["url"], lonlat, request_url=wms_probe_url(src, lonlat))
    return probe(src["url"], lonlat)


# --------------------------------------------------------- offline fallback --
def pack_dirs():
    """Folders searched for offline imagery: <pack>/imagery of the configured offline
    pack, ND_DAM_SUITE / NDSUITE_HOME /imagery (existing folders only)."""
    out = []
    roots = []
    try:
        from .download import offline_config
        pack = str(offline_config()[0] or "").strip()
        if pack:
            roots.append(pack if os.path.isdir(pack) else os.path.dirname(pack))
    except Exception:
        pass
    for k in ("ND_DAM_SUITE", "NDSUITE_HOME", "ND_DAM_HAZARD_OFFLINE_PACK"):
        v = os.environ.get(k)
        if v and v not in ("1", "true", "yes"):
            roots.append(v if os.path.isdir(v) else os.path.dirname(v))
    for r in roots:
        d = os.path.join(r, "imagery")
        if os.path.isdir(d) and d not in out:
            out.append(d)
    return out


def pack_imagery(rect, crs, dirs=None):
    """(layer, path) of the first pack imagery file (*.tif / *.tiff / *.mbtiles, any CRS)
    whose extent covers the map, opened read-only without writing .aux.xml; (None, "")."""
    from qgis.core import QgsCoordinateTransform
    for d in (pack_dirs() if dirs is None else dirs):
        try:
            names = sorted(os.listdir(d))
        except OSError:
            continue
        for n in names:
            if not n.lower().endswith((".tif", ".tiff", ".mbtiles")):
                continue
            path = os.path.join(d, n)
            old = os.environ.get("GDAL_PAM_ENABLED")
            os.environ["GDAL_PAM_ENABLED"] = "NO"
            try:
                layer = QgsRasterLayer(path, "Aerial imagery - pack {} (base map)".format(n),
                                       "gdal")
            finally:
                if old is None:
                    os.environ.pop("GDAL_PAM_ENABLED", None)
                else:
                    os.environ["GDAL_PAM_ENABLED"] = old
            if not layer.isValid():
                continue
            try:
                xf = QgsCoordinateTransform(crs, layer.crs(),
                                            QgsProject.instance().transformContext())
                r = rect if xf.isShortCircuited() else xf.transformBoundingBox(rect)
                if layer.extent().contains(r):
                    return _style(layer), path
            except Exception:
                continue
    return None, ""


def pack_date(path):
    """ACQUISITION_DATE / TIFFTAG_DATETIME of a pack file ('' when not set)."""
    try:
        from osgeo import gdal
        gdal.SetConfigOption("GDAL_PAM_ENABLED", "NO")
        ds = gdal.Open(path)
        md = ds.GetMetadata() or {} if ds is not None else {}
        v = md.get("ACQUISITION_DATE") or md.get("TIFFTAG_DATETIME") or ""
        return str(v)[:10].replace(":", "-")
    except Exception:
        return ""
    finally:
        try:
            gdal.SetConfigOption("GDAL_PAM_ENABLED", None)
        except Exception:
            pass


def hillshade_layer(dem_path):
    """A hillshade of the DEM (private layer) or None."""
    if not dem_path or not os.path.isfile(str(dem_path)):
        return None
    try:
        from qgis.core import QgsHillshadeRenderer
        layer = QgsRasterLayer(str(dem_path), "Hillshade of {} (base map)".format(
            os.path.basename(str(dem_path))), "gdal")
        if not layer.isValid():
            return None
        r = QgsHillshadeRenderer(layer.dataProvider(), 1, 315.0, 45.0)
        r.setZFactor(hillshade_z_factor(layer))
        layer.setRenderer(r)
        return layer
    except Exception as e:
        log_message("Hillshade base map not made: {}".format(e))
        return None


def hillshade_z_factor(layer, exaggeration=None):
    """2.19.1: the hillshade's z factor: HILLSHADE_EXAGGERATION (2x; ND is flat) on a
    projected DEM (elevations in its horizontal unit); on a geographic DEM (degrees) also
    1 / metres per degree at the DEM's latitude, without which the relief vanishes."""
    ex = HILLSHADE_EXAGGERATION if exaggeration is None else float(exaggeration)
    try:
        if layer.crs().isGeographic():
            e = layer.extent()
            lat = math.radians(max(-85.0, min(85.0, (e.yMinimum() + e.yMaximum()) / 2.0)))
            return ex / (111320.0 * max(math.cos(lat), 0.05))
    except Exception:
        pass
    return ex


def credit(info, crs=None):
    """The credit line under a map: 'Imagery: <source> (<res>), flown <dates>; labels:
    Esri reference (...); <CRS>; <plugin version>'."""
    parts = []
    if info.get("kind") == "imagery":
        src = info.get("source") or {}
        res = src.get("resolution")
        parts.append("Imagery: {}{}, {}".format(src.get("credit") or src.get("name"),
                                                " ({})".format(res) if res else "",
                                                info.get("dates_text") or DATE_NOT_PUBLISHED))
    elif info.get("kind") == "pack":
        import os
        parts.append("Imagery: offline pack {}{}".format(
            os.path.basename(info.get("path") or ""),
            ", flown " + info["dates_text"] if info.get("dates_text") else
            ", " + DATE_NOT_PUBLISHED))
    elif info.get("kind") == "hillshade":
        import os
        parts.append("Imagery not available ({}): hillshade of {}".format(
            info.get("why") or "offline", os.path.basename(info.get("path") or "the DEM")))
    else:
        return ""
    if info.get("labels"):
        parts.append("labels: " + C.AERIAL_LABEL_CREDIT)
    try:
        if crs is not None and crs.authid():
            parts.append(crs.authid())
    except Exception:
        pass
    parts.append("{} v{}".format(C.PLUGIN_NAME, C.PLUGIN_VERSION))
    return "; ".join(parts)


def layout_basemap(add_to_project=True, rect=None, crs=None, source="auto", labels=True,
                   dem_path=None):
    """2.19.0: the base map of a print layout's maps. Returns a dict:
      layer   - imagery (or pack / hillshade) layer, None without one
      labels  - label layers (top first, [] when off / offline / not reachable)
      caption - the credit line ('' without a base map)
      note    - why the online imagery was not used ('' when it was)
      kind    - "imagery" | "pack" | "hillshade" | ""
      source / dates / dates_text / path / tried - what the summary DATA SOURCES records.
    add_to_project: layers are project layers (added switched off, marked) so the kept
    layout keeps them; else private layers (keep references). Never raises."""
    info = {"layer": None, "labels": [], "caption": "", "note": "", "kind": "",
            "source": None, "dates": [], "dates_text": "", "path": "", "tried": []}
    try:
        from .helpers import on_main_thread
        in_project = bool(add_to_project) and on_main_thread()
        why = ""
        lonlat = None
        if offline_only():
            why = "offline mode (no internet requests)"
        else:
            lonlat = center_lonlat(rect, crs) if rect is not None and crs is not None else None
            for src in sources(source):
                ok, text = probe_source(src, lonlat)
                if not ok:
                    info["tried"].append("{}: {}".format(src["id"], text))
                    continue
                dates, dtext = flight_dates(src, rect, crs)
                if dtext.startswith("outside the NAIP"):
                    info["tried"].append("{}: {}".format(src["id"], dtext))
                    continue
                if in_project:
                    lyr = _project_layer(layer_name(src), lambda s=src: source_layer(s, crs))
                else:
                    lyr = source_layer(src, crs)
                if lyr is None or not lyr.isValid():
                    info["tried"].append("{}: QGIS could not open it".format(src["id"]))
                    continue
                info.update(layer=lyr, kind="imagery", source=src, dates=dates,
                            dates_text=dtext)
                break
            if info["layer"] is None:
                why = "; ".join(info["tried"]) or "no imagery source set"
        if info["layer"] is not None and labels:
            # 2.19.1: a label service is used only when one of its tiles came back
            checks = label_checks(lonlat)
            good = [u for u, ok, _t in checks if ok]
            info["labels_tried"] = ["labels {}: {}".format(label_name(u), t)
                                    for u, ok, t in checks if not ok]
            info["label_urls"] = good
            if good:
                info["labels"] = _project_labels(good) if in_project else \
                    label_layers_new(good)
        if info["layer"] is None:
            info["note"] = why
            lyr, path = pack_imagery(rect, crs) if rect is not None and crs is not None \
                else (None, "")
            if lyr is not None:
                if in_project:
                    lyr = _project_layer(lyr.name(), lambda l=lyr: l)
                d = pack_date(path)
                info.update(layer=lyr, kind="pack", path=path, dates_text=d)
            else:
                lyr = hillshade_layer(dem_path)
                if lyr is not None:
                    if in_project:
                        lyr = _project_layer(lyr.name(), lambda l=lyr: l)
                    info.update(layer=lyr, kind="hillshade", path=str(dem_path),
                                why="offline" if why.startswith("offline") else
                                "no imagery service answered")
        info["caption"] = credit(info, crs) if info["layer"] is not None else ""
        info["labels"] = [x for x in info["labels"] if x is not None]
        info["labels_used"] = bool(info["labels"])
        if info["labels"] and info["caption"] and "labels:" not in info["caption"]:
            info["caption"] = credit(dict(info, labels=True), crs)
    except Exception as e:
        log_message("Base map for the maps skipped: {}".format(e))
        info.update(layer=None, labels=[], caption="",
                    note="aerial imagery not available ({})".format(e))
    return info


def _project_labels(urls=None):
    """The label layers as project layers (top first)."""
    out = []
    for lyr in label_layers_new(urls):
        name = lyr.name()
        out.append(_project_layer(name, lambda l=lyr: l))
    return out


def source_line(info):
    """DATA SOURCES line of the report / map book base map."""
    kind = info.get("kind")
    src = info.get("source") or {}
    if kind == "imagery":
        line = "Map base: {} [{}] {} - {}".format(src.get("name"), src.get("id"),
                                                 src.get("url"), info.get("dates_text"))
    elif kind == "pack":
        line = "Map base: offline pack imagery {}{}".format(
            info.get("path"), " - flown " + info["dates_text"] if info.get("dates_text")
            else "")
    elif kind == "hillshade":
        # 2.19.1: the failed sources are listed once, under "not used" (the note is the
        # same list); offline mode has no list, so the note says why
        line = "Map base: hillshade of {} (no aerial imagery{})".format(
            info.get("path"), "" if info.get("tried") else ": {}".format(
                info.get("note") or "offline"))
    else:
        return "Map base: none ({})".format(info.get("note") or "off")
    if info.get("labels"):
        line += "; labels: " + ", ".join(info.get("label_urls") or C.AERIAL_LABEL_URLS)
    not_used = list(info.get("tried") or []) + list(info.get("labels_tried") or [])
    if not_used:
        line += "; not used: " + "; ".join(not_used)
    return line


def with_imagery(layers, imagery):
    """Layer list of a map item (top first) with the imagery LAST, i.e. under
    everything. An empty list is returned unchanged: such a map item follows
    the project's visible layers (the imagery then shows only if it is
    switched on in the project, which the plugin never does itself)."""
    layers = list(layers or [])
    if imagery is None or not layers:
        return layers
    return [lyr for lyr in layers if lyr is not imagery] + [imagery]
