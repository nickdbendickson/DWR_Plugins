"""USGS 3DEP download extents for a dam, a North Dakota county, or the state.

County boxes are download clips, padded, not survey boundaries. The service
returns metres in EPSG:5070.
"""
import math
import os

from osgeo import gdal, osr

from . import dem_zunit as ZU, nd_counties, terrain
from .warp_opts import no_overviews, no_vshift


def _safe_extract(zf, dest):
    """Extract a zip into dest, refusing any entry that leaves dest."""
    root = os.path.realpath(dest)
    for member in zf.infolist():
        target = os.path.realpath(os.path.join(dest, member.filename))
        if target != root and not target.startswith(root + os.sep):
            raise ValueError("archive entry outside the destination: " + member.filename)
    for member in zf.infolist():
        zf.extract(member, dest)

# west, south, east, north in degrees: each county's outline box, padded so a 5 mi box around
# any dam in the county is inside (0.12 deg >= 5 mi of longitude up to 52 N).
COUNTY_PAD_DEG = 0.12
COUNTIES = {name: nd_counties.box([name], COUNTY_PAD_DEG) for name in nd_counties.names()}
STATE = (-104.06, 45.93, -96.54, 49.01)
MI = 1609.344


def county_names():
    return sorted(COUNTIES)


def to_5070(west, south, east, north):
    src = osr.SpatialReference()
    src.ImportFromEPSG(4326)
    src.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    dst = osr.SpatialReference()
    dst.ImportFromEPSG(5070)
    dst.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    ct = osr.CoordinateTransformation(src, dst)
    xs, ys = [], []
    for lon, lat in ((west, south), (west, north), (east, south), (east, north)):
        x, y, _z = ct.TransformPoint(lon, lat)
        xs.append(x)
        ys.append(y)
    return (min(xs), min(ys), max(xs), max(ys))


# When a downstream bearing is given, the box is shifted so the dam sits near the upstream edge
# (a quarter of the half-buffer upstream of it) and the rest reaches downstream, instead of the
# dam-centred square. The box keeps the same size, so the download is no larger.
_UPSTREAM_MARGIN_FRAC = 0.25


def point_box(lon, lat, buffer_mi, bearing_deg=None):
    """EPSG:5070 box for a dam point and a per-side buffer (miles). bearing_deg (compass degrees
    clockwise from north, the downstream direction) shifts the box downstream; None centres it."""
    x0, y0, _x1, _y1 = to_5070(lon, lat, lon, lat)
    b = buffer_mi * MI
    if bearing_deg is None:
        return (x0 - b, y0 - b, x0 + b, y0 + b)
    import math
    br = math.radians(float(bearing_deg))
    ux, uy = math.sin(br), math.cos(br)            # downstream unit vector (east=+x, north=+y)
    shift = b * (1.0 - _UPSTREAM_MARGIN_FRAC)       # move the centre downstream
    cx, cy = x0 + ux * shift, y0 + uy * shift
    return (cx - b, cy - b, cx + b, cy + b)


def imageserver_chunks(extent, cell_m):
    """(count, side_km) of the 3DEP ImageServer export requests for an EPSG:5070 extent at
    cell_m: the box is cut into TILE_PX x TILE_PX pixel pieces (1.2 km at 1 m, 12 km at 10 m).
    These are request chunks, not lidar tiles (0.17.50)."""
    from .solve_gate import TILE_PX
    step = TILE_PX * float(cell_m)
    nx = max(1, int(math.ceil((extent[2] - extent[0]) / step - 1e-9)))
    ny = max(1, int(math.ceil((extent[3] - extent[1]) / step - 1e-9)))
    return nx * ny, step / 1000.0


TNM_SOURCE = "USGS 3DEP 1 m lidar tiles (native, TNM)"      # Download DEM's SOURCE 2


def chunk_note(count, cell_m, side_km, width_km=None, height_km=None, scope=0):
    """One line for the log / the Runner's Download box about the ImageServer requests: at 1 m
    over a dam's buffer they are hundreds of 1.2 km pieces (a 10 mi buffer: 27 x 27 = 729),
    which is not the number of lidar tiles; the native 1 m source lists the 10 km lidar
    project tiles instead (about 20 for that box). Empty when there is nothing to say.
    scope is Download DEM's Area (0 dam, 1 county, 2 state): a smaller buffer is suggested only
    for a dam, and the native tiles never for the state (that source refuses it)."""
    if int(round(cell_m)) != 1 or count <= 100:
        return ""
    if width_km and height_km:
        what = " over a {:.0f} x {:.0f} km box".format(width_km, height_km)
    elif width_km:
        what = " over a box {:.0f} km wide".format(width_km)
    else:
        what = ""
    text = ("USGS 3DEP ImageServer at 1 m{}: {} export requests of {:.1f} km x {:.1f} km "
            "(request chunks, not lidar tiles), mosaicked and warped into one GeoTIFF. ").format(
                what, count, side_km, side_km)
    if scope == 2:
        return text + ("For the whole state use 10 m, or at 1 m a county or a box (the source "
                       "'{}' does not take the whole state).").format(TNM_SOURCE)
    text += ("For 1 m the source '{}' downloads the 10 km lidar tiles themselves (lists them "
             "first)").format(TNM_SOURCE)
    return text + (", or use a smaller buffer." if scope == 0 else ".")


def set_unit(path, choice):
    """Tag band 1 of a written DEM with the unit `choice` (dem_zunit FEET / US_FEET / METERS),
    GeoTIFF or VRT. BuildVRT does not carry a band unit and the ImageServer tiles have none, so
    the local copies had none and the Runner's 'Detect from the raster' could not tell feet
    from metres (0.17.50). True when the tag was written."""
    tag = ZU.band_tag(choice)
    if not tag:
        return False
    try:
        ds = gdal.Open(path, gdal.GA_Update)
        if ds is None:
            return False
        ds.GetRasterBand(1).SetUnitType(tag)
        ds.FlushCache()
        ds = None
        return True
    except Exception:                                    # noqa - the sidecar still says metres
        return False


def set_metres(path):
    """set_unit(path, METERS)."""
    return set_unit(path, ZU.METERS)


def band_unit(path):
    """Band 1's unit type text of a raster ('' for none or unreadable)."""
    try:
        ds = gdal.Open(path)
        return (ds.GetRasterBand(1).GetUnitType() or "").strip() if ds is not None else ""
    except Exception:                                    # noqa
        return ""


def recorded_unit(path):
    """(choice, how) of the elevation unit a raster records - core/dem_zunit.recorded, the rule
    the Runner's 'Detect from the raster' applies - or ("", why) (0.17.50)."""
    try:
        ds = gdal.Open(path)
    except Exception as e:                               # noqa - exception mode
        return "", "cannot open {} ({})".format(path, e)
    if ds is None:
        return "", "cannot open {}".format(path)
    try:
        return ZU.recorded(ds, path)
    finally:
        ds = None


def record_source(path):
    """The SOURCE of a Download DEM record of a raster - its metadata, else its <stem>_dem.txt
    facts file - or "" (0.17.50 review round 3: the reuse warning names the right unit)."""
    try:
        ds = gdal.Open(path)
        src = str((ds.GetMetadata() or {}).get("SOURCE") or "").strip() if ds is not None else ""
        ds = None
    except Exception:                                    # noqa - exception mode
        src = ""
    if not src:
        facts = ZU.read_facts(os.path.splitext(path)[0] + "_dem.txt") or {}
        src = str(facts.get("source") or "").strip()
    return src


def data_problem(path):
    """dem_zunit.data_problem of a raster file: why no unit reads its values right, or ""."""
    try:
        ds = gdal.Open(path)
    except Exception:                                    # noqa - exception mode
        return ""
    if ds is None:
        return ""
    try:
        return ZU.data_problem(ds, path)
    finally:
        ds = None


class Cancelled(Exception):
    """The user cancelled a 3DEP download (feedback.isCanceled(); 0.17.55)."""


def download(extent, cell_m, out_dir, stem, feedback=None, scope=0):
    """Download 3DEP tiles for an EPSG:5070 extent and write dem.vrt. Returns the VRT path.
    scope (0 dam, 1 county, 2 state) only shapes the request-count note (chunk_note).
    0.17.55: raises Cancelled before the next request once the user cancels."""
    os.makedirs(out_dir, exist_ok=True)
    from .solve_gate import TILE_PX
    tiles = list(terrain.tile_extents(*extent, cell_m))
    if feedback is not None:
        feedback.pushInfo("{} ImageServer export chunks at {} m ({} px = {:.1f} km a side; "
                          "request pieces, not lidar tiles)".format(
                              len(tiles), cell_m, TILE_PX, TILE_PX * float(cell_m) / 1000.0))
        note = chunk_note(len(tiles), cell_m, TILE_PX * float(cell_m) / 1000.0,
                          (extent[2] - extent[0]) / 1000.0, (extent[3] - extent[1]) / 1000.0,
                          scope)
        if note:
            getattr(feedback, "pushWarning", feedback.pushInfo)(note)
    n = max(len(tiles), 1)

    def log(msg):
        if feedback is not None:
            feedback.pushInfo(msg)

    def tick(i, total):
        if feedback is None:
            return
        if getattr(feedback, "isCanceled", lambda: False)():
            raise Cancelled("3DEP download cancelled after {} of {} requests".format(i, total))
        feedback.setProgress(int(80 * i / max(total, 1)))
        feedback.pushInfo("3DEP tile {} of {}".format(i + 1, total))

    paths = terrain.fetch_3dep(extent, cell_m, out_dir, stem, log=log, progress=tick)
    if feedback is not None:
        feedback.setProgress(85)
        feedback.pushInfo("mosaicking {} tiles".format(len(paths)))
    vrt = os.path.join(out_dir, stem + ".vrt")
    gdal.BuildVRT(vrt, paths)
    return vrt


DWR_HOST = "lidarftp.swc.nd.gov"
DWR_USER = "swc"
DWR_PASSWORD = "water"   # published guest login on the ND LiDAR download page
FT_TO_M = 0.3048


def _product_of(path, root=None):
    """'ASCII_Grids_ft' or 'DEM_Imagine' for a raster unpacked under `root` (the download
    folder): a .asc file, or a folder / file below root named ASCII_Grids_ft or gft<tile> (the
    feet grids' zips), is a feet grid. 0.17.50: only the part of the path below root counts -
    before, '/gft' anywhere in the whole path did, so with an output folder such as D:/gft_dems
    or C:/Users/gftaylor every metre DEM_Imagine tile was taken for a feet grid (x 0.3048, then
    labelled metres). Outside root, only the file's own folder and name count."""
    p = os.path.abspath(path)
    rel = None
    if root:
        try:
            rel = os.path.relpath(p, os.path.abspath(root))
        except ValueError:                               # another drive (Windows)
            rel = None
        if rel is not None and (rel == os.pardir or rel.startswith(os.pardir + os.sep)):
            rel = None
    if rel is None:
        rel = os.path.join(os.path.basename(os.path.dirname(p)), os.path.basename(p))
    parts = [x.lower() for x in rel.replace("\\", "/").split("/") if x]
    if p.lower().endswith(".asc") or "ascii_grids_ft" in parts or \
            any(x.startswith("gft") for x in parts):
        return "ASCII_Grids_ft"
    return "DEM_Imagine"


def build_vrts(rasters, out_dir, feedback=None):
    """One VRT per product, never a mosaic of feet and metres (ported from the ND copy).

    The metre Imagine DEMs win when present (the foot grids are then not used for the local
    GeoTIFF, as the ASCII_FT option says). Foot grids alone get a second VRT scaled by 0.3048, so
    the elevations really are metres when the output is labelled metres. Returns the metres VRT,
    or out_dir/nd_lidar.vrt (not written) when there are no rasters."""
    rasters = [r for r in rasters if not r.lower().endswith(".rrd")]
    by = {}
    for r in rasters:
        by.setdefault(_product_of(r, out_dir), []).append(r)
    if by.get("DEM_Imagine"):
        use = os.path.join(out_dir, "nd_lidar_imagine_m.vrt")
        gdal.BuildVRT(use, by["DEM_Imagine"])
        ds = gdal.Open(use, gdal.GA_Update)
        if ds is not None:
            ds.GetRasterBand(1).SetUnitType("m")
            ds = None
        if by.get("ASCII_Grids_ft") and feedback is not None:
            feedback.pushInfo("ND LiDAR: the metre Imagine DEM is used; the {} ASCII foot grid(s) "
                              "are kept on disk but not mosaicked with it".format(len(by["ASCII_Grids_ft"])))
        return use
    if by.get("ASCII_Grids_ft"):
        ft = os.path.join(out_dir, "nd_lidar_ascii_ft.vrt")
        ds = gdal.BuildVRT(ft, by["ASCII_Grids_ft"])
        if ds is not None:
            ds.GetRasterBand(1).SetUnitType("ft")
            ds = None
        m = os.path.join(out_dir, "nd_lidar_ascii_m.vrt")
        gdal.Translate(m, ft, format="VRT", outputType=gdal.GDT_Float32,
                       scaleParams=[[0.0, 1.0, 0.0, FT_TO_M]])
        ds = gdal.Open(m, gdal.GA_Update)
        if ds is not None:
            ds.GetRasterBand(1).SetUnitType("m")
            ds = None
        if feedback is not None:
            feedback.pushInfo("ND LiDAR: ASCII foot grids converted to metres (x 0.3048)")
        return m
    return os.path.join(out_dir, "nd_lidar.vrt")


DWR_PRODUCTS = ("ASCII_Grids_ft", "DEM_Imagine")


def parse_dwr_path(text):
    """Accept a pasted FTP URL, a /temp/<id> path, or the clipboard text from the map site."""
    raw = (text or "").strip()
    for line in raw.splitlines():
        if "path:" in line.lower():
            raw = line.split(":", 1)[1].strip()
            break
    raw = raw.replace("ftp://", "")
    if "@" in raw:
        raw = raw.split("@", 1)[1]
    if raw.startswith(DWR_HOST):
        raw = raw[len(DWR_HOST):]
    raw = raw.split("?", 1)[0].strip().strip("/")
    if not raw.startswith("temp/"):
        raise ValueError("expected an ND LiDAR path like /temp/<id> from lidar.dwr.nd.gov")
    return "/" + raw


def fetch_dwr(package, out_dir, products, feedback=None):
    """Download QL2 ASCII foot grids and/or Imagine DEMs from a staged map-site package."""
    import zipfile
    path = parse_dwr_path(package)
    wanted = [p for p in products if p in DWR_PRODUCTS]
    if not wanted:
        raise ValueError("choose ASCII_Grids_ft, DEM_Imagine, or both")
    os.makedirs(out_dir, exist_ok=True)
    ftp = _ftp()
    try:
        found = []

        def walk(folder):
            lines = []
            ftp.cwd(folder)
            ftp.retrlines("LIST", lines.append)
            for line in lines:
                name = line.split(None, 8)[-1]
                if name in (".", ".."):
                    continue
                full = folder.rstrip("/") + "/" + name
                if line.startswith("d"):
                    walk(full)
                elif any("/" + product + "/" in full + "/" or full.endswith("/" + product) for product in wanted):
                    if name.lower().endswith((".zip", ".img", ".asc", ".grd")):
                        found.append(full)

        walk(path)
        if not found:
            ftp.quit()
            raise IOError("no {} files under {}".format(", ".join(wanted), path))
        if feedback is not None:
            feedback.pushInfo("{} ND LiDAR files in {}".format(len(found), path))
        rasters = []
        for i, remote in enumerate(found):
            local = os.path.join(out_dir, os.path.basename(remote))
            if feedback is not None:
                feedback.pushInfo("getting {} ({}/{})".format(os.path.basename(remote), i + 1, len(found)))
            _ftp_retr(ftp, remote, local)
            if local.lower().endswith(".zip"):
                dest = os.path.splitext(local)[0]
                os.makedirs(dest, exist_ok=True)
                with zipfile.ZipFile(local) as z:
                    _safe_extract(z, dest)
                rasters += [os.path.join(root, fn) for root, _d, files in os.walk(dest)
                            for fn in files if fn.lower().endswith((".img", ".asc", ".grd", ".tif"))]
            else:
                rasters.append(local)
        ftp.quit()
        return build_vrts(rasters, out_dir, feedback)
    finally:
        _ftp_close(ftp)


INDEX_DIR = "LiDAR_NorthDakota_QL2"
INDEX_NAME = "IndexLiDARNorthDakotaQL2"


def _ftp_close(ftp):
    """End an FTP session: QUIT when it is still open, else drop the socket. Never raises."""
    if ftp is None or getattr(ftp, "sock", None) is None:
        return
    try:
        ftp.quit()
    except Exception:                                            # noqa - server gone: just close
        try:
            ftp.close()
        except OSError:
            pass


def _ftp_retr(ftp, remote, local):
    """RETR `remote` into `local`; a partial file is deleted when the transfer fails."""
    try:
        with open(local, "wb") as f:
            ftp.retrbinary("RETR " + remote, f.write)
    except BaseException:
        try:
            os.remove(local)
        except OSError:
            pass
        raise


# FTP to the ND LiDAR server is an explicit opt-in, off by default: it sends the (published)
# guest login and the data in clear text. Tiles and the tile index come over HTTPS; only a staged
# FTP package path (DWR_PATH), which HTTPS cannot list, needs FTP. Turn it on with the QGIS
# setting below (Settings > Options > Advanced) or the environment variable ND_LIDAR_ALLOW_FTP=1.
FTP_SETTING = "DamSafety_Screening/allow_ftp_fallback"


def ftp_allowed():
    """True when the FTP opt-in setting (or ND_LIDAR_ALLOW_FTP=1) is on."""
    v = os.environ.get("ND_LIDAR_ALLOW_FTP", "")
    if not v:
        try:
            from qgis.core import QgsSettings
            v = QgsSettings().value(FTP_SETTING, False)
        except Exception:                                        # noqa - no QGIS (tests, CLI)
            v = ""
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def _log_warning(message):
    try:
        from qgis.core import QgsMessageLog, Qgis
        QgsMessageLog.logMessage(str(message), "ND LiDAR", getattr(Qgis, 'MessageLevel', Qgis).Warning)
    except Exception:                                            # noqa - no QGIS (tests, CLI)
        import logging
        logging.getLogger(__name__).warning("%s", message)


def _ftp():
    import ftplib
    if not ftp_allowed():
        raise IOError("FTP to {} is off (plain-text protocol); ND LiDAR tiles and the tile index "
                      "come over HTTPS. A staged FTP package path needs FTP: to allow it, set the "
                      "QGIS setting {} to true (Settings > Options > Advanced) or "
                      "ND_LIDAR_ALLOW_FTP=1.".format(DWR_HOST, FTP_SETTING))
    _log_warning("Using plain-text FTP to {} (opt-in setting {} is on)".format(DWR_HOST, FTP_SETTING))
    ftp = ftplib.FTP(DWR_HOST, timeout=120)
    try:
        ftp.login(DWR_USER, DWR_PASSWORD)
    except BaseException:
        _ftp_close(ftp)
        raise
    return ftp


def ensure_index(cache_dir, feedback=None):
    """Cache the statewide QL2 tile index (EPSG:2266 feet). Returns the shapefile path."""
    os.makedirs(cache_dir, exist_ok=True)
    shp = os.path.join(cache_dir, INDEX_NAME + ".shp")
    if os.path.isfile(shp) and os.path.isfile(shp.replace(".shp", ".dbf")):
        return shp
    ftp = _ftp()
    try:
        for ext in (".shp", ".shx", ".dbf", ".prj", ".cpg"):
            remote = "/{}/{}{}".format(INDEX_DIR, INDEX_NAME, ext)
            local = os.path.join(cache_dir, INDEX_NAME + ext)
            if feedback is not None:
                feedback.pushInfo("index {}".format(ext))
            _ftp_retr(ftp, remote, local)
        ftp.quit()
        return shp
    finally:
        _ftp_close(ftp)


def _box_2266(west, south, east, north):
    src = osr.SpatialReference()
    src.ImportFromEPSG(4326)
    src.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    dst = osr.SpatialReference()
    dst.ImportFromEPSG(2266)
    dst.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    ct = osr.CoordinateTransformation(src, dst)
    xs, ys = [], []
    for lon, lat in ((west, south), (west, north), (east, south), (east, north)):
        x, y, _z = ct.TransformPoint(lon, lat)
        xs.append(x)
        ys.append(y)
    return (min(xs), min(ys), max(xs), max(ys))


# HTTPS (ported from nd_dam_screening 0.18.1): the statewide index and the tile zips are on the
# map site at https://lidar.dwr.nd.gov/mapdata/<project>/<path>/<product>/<pre><tile>.zip, the
# same files as on the FTP server. HTTPS is the default; FTP is the opt-in fallback (_ftp()).
HTTPS_ROOT = "https://lidar.dwr.nd.gov/mapdata"
INDEX_URL = "{}/{}/{}.shp".format(HTTPS_ROOT, INDEX_DIR, INDEX_NAME)


def _open_index(cache_dir, feedback=None):
    """The tile index as an OGR data source: a cached copy, else read in place over HTTPS (GDAL
    /vsicurl/ reads only the records it needs), else the FTP copy (opt-in)."""
    from osgeo import ogr
    shp = os.path.join(cache_dir, INDEX_NAME + ".shp") if cache_dir else ""
    if shp and os.path.isfile(shp) and os.path.isfile(shp.replace(".shp", ".dbf")):
        return ogr.Open(shp)
    ds = ogr.Open("/vsicurl/" + INDEX_URL)
    if ds is not None:
        if feedback is not None:
            feedback.pushInfo("tile index: {}".format(INDEX_URL))
        return ds
    if feedback is not None:
        feedback.pushInfo("tile index not readable over HTTPS; trying the FTP copy")
    return ogr.Open(ensure_index(cache_dir, feedback))


def _https_get(url, local, retries=3, timeout=120):
    """Download url to local through local.part, so a cut transfer is never kept."""
    import shutil
    import time
    import urllib.request
    part = local + ".part"
    err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "DamSafety_Screening"})
            with urllib.request.urlopen(req, timeout=timeout) as r, open(part, "wb") as f:
                shutil.copyfileobj(r, f, 1 << 20)
            os.replace(part, local)
            return
        except Exception as e:                                  # noqa - retried, then reported
            err = e
            time.sleep(2 * (attempt + 1))
    try:
        os.remove(part)
    except OSError:
        pass
    raise IOError(str(err))


def _index_or_fail(cache_dir, feedback):
    ds = _open_index(cache_dir, feedback)
    if ds is None:
        raise IOError("the ND LiDAR tile index could not be read ({})".format(INDEX_URL))
    return ds


def tiles_for(scope, county, cache_dir, feedback=None):
    """Tile records whose box meets a county clip or the state. scope 1 = county, 2 = state."""
    if scope == 1:
        box = COUNTIES.get(county)
        if box is None:
            raise ValueError("Unknown county {!r}".format(county))
    else:
        box = STATE
    xmin, ymin, xmax, ymax = _box_2266(*box)
    ds = _index_or_fail(cache_dir, feedback)
    layer = ds.GetLayer(0)
    layer.SetSpatialFilterRect(xmin, ymin, xmax, ymax)
    tiles = []
    for feat in layer:
        tiles.append({
            "tile": feat.GetField("TileNum"),
            "path": feat.GetField("Path"),
            "project": feat.GetField("Project") or INDEX_DIR,
            "ascii": feat.GetField("ASCII_Path"),
        })
    return tiles


def fetch_dwr_area(scope, county, out_dir, products, feedback=None, cache_dir=None, tiles=None,
                   transport="https"):
    """Download QL2 tiles for one county, the state, or a preselected tile list. Over HTTPS
    from lidar.dwr.nd.gov by default; transport "ftp" is the old route (opt-in, see _ftp())."""
    import zipfile
    cache_dir = cache_dir or os.path.join(out_dir, "_index")
    if tiles is None:
        tiles = tiles_for(scope, county, cache_dir, feedback)
    if feedback is not None:
        feedback.pushInfo("{} QL2 tiles ({})".format(len(tiles), transport.upper()))
    if not tiles:
        raise IOError("no QL2 tiles in that area")
    os.makedirs(out_dir, exist_ok=True)
    ftp = _ftp() if transport == "ftp" else None
    try:
        rasters = []
        for i, tile in enumerate(tiles):
            for product in products:
                if product == "ASCII_Grids_ft":
                    remote = "/{}/{}/ASCII_Grids_ft/gft{}.zip".format(
                        tile["project"], tile["path"], tile["tile"])
                else:
                    remote = "/{}/{}/DEM_Imagine/dem{}.zip".format(
                        tile["project"], tile["path"], tile["tile"])
                local = os.path.join(out_dir, os.path.basename(remote))
                if feedback is not None:
                    feedback.setProgress(int(80 * i / max(len(tiles), 1)))
                    feedback.pushInfo("{} {}/{}".format(os.path.basename(remote), i + 1, len(tiles)))
                try:
                    if ftp is not None:
                        with open(local, "wb") as f:
                            ftp.retrbinary("RETR " + remote, f.write)
                    else:
                        _https_get(HTTPS_ROOT + remote, local)
                except Exception:
                    try:
                        os.remove(local)
                    except OSError:
                        pass
                    continue
                dest = os.path.splitext(local)[0]
                os.makedirs(dest, exist_ok=True)
                try:
                    with zipfile.ZipFile(local) as z:
                        _safe_extract(z, dest)
                except (zipfile.BadZipFile, ValueError):
                    continue
                rasters += [os.path.join(root, fn) for root, _d, files in os.walk(dest)
                            for fn in files if fn.lower().endswith((".img", ".asc", ".grd", ".tif"))]
        _ftp_close(ftp)
        return build_vrts(rasters, out_dir, feedback), len(tiles)
    finally:
        _ftp_close(ftp)


WORK_CRS = (
    ("EPSG:5070", "EPSG:5070 metres (3DEP / screening)"),
    ("EPSG:26914", "EPSG:26914 UTM 14N metres (QL2 Imagine, eastern ND)"),
    ("EPSG:2265", "EPSG:2265 ND North feet"),
    ("EPSG:2266", "EPSG:2266 ND South feet"),
)


def county_features():
    """(name, ring of lon/lat) for the download clips."""
    out = []
    for name, (w, s, e, n) in sorted(COUNTIES.items()):
        out.append((name, [(w, s), (e, s), (e, n), (w, n), (w, s)]))
    return out


def warp_to(src, dest, epsg):
    """Warp one raster to dest in the given EPSG. Returns dest. 0.17.50: dest carries the unit
    src records (its band unit, which gdal.Warp keeps, else its Download DEM record); a source
    that records none gives an untagged copy. The values are copied as stored (no vertical
    shift: GDAL's default one converts them when a CRS is compound, and the tag would then be
    wrong)."""
    gdal.Warp(dest, src, **no_vshift(gdal, dict(
        dstSRS="EPSG:{}".format(epsg), format="GTiff", resampleAlg="bilinear",
        creationOptions=["COMPRESS=DEFLATE", "TILED=YES"])))
    got, _how = recorded_unit(src)
    if got:
        set_unit(dest, got)
    return dest


RESOLUTIONS = (10, 1)


def cell_from_choice(index):
    """0 is 10 m, 1 is 1 m. Anything else stays 10 m."""
    try:
        return RESOLUTIONS[int(index)]
    except (TypeError, ValueError, IndexError):
        return 10


def point_ll_box(lon, lat, buffer_mi):
    """Longitude/latitude box around a dam. Miles, not a survey boundary."""
    import math
    dlat = float(buffer_mi) / 69.0
    dlon = float(buffer_mi) / (69.0 * max(0.2, math.cos(math.radians(float(lat)))))
    return (float(lon) - dlon, float(lat) - dlat, float(lon) + dlon, float(lat) + dlat)


def tiles_for_box(west, south, east, north, cache_dir, feedback=None):
    """QL2 index tiles that meet a longitude/latitude box."""
    xmin, ymin, xmax, ymax = _box_2266(west, south, east, north)
    ds = _index_or_fail(cache_dir, feedback)
    layer = ds.GetLayer(0)
    layer.SetSpatialFilterRect(xmin, ymin, xmax, ymax)
    tiles = []
    for feat in layer:
        tiles.append({
            "tile": feat.GetField("TileNum"),
            "path": feat.GetField("Path"),
            "project": feat.GetField("Project") or INDEX_DIR,
            "ascii": feat.GetField("ASCII_Path"),
        })
    return tiles


def product_info(source, cell_m, native=None):
    """Facts written beside the local DEM. source is 'usgs', 'ndlidar' or (0.17.29) 'tnm1m',
    the native USGS 3DEP 1 m lidar tiles (core/lidar_tnm.py), which stay in their UTM CRS."""
    native = native or {}
    cell_m = int(cell_m)
    if source == "tnm1m":
        return {
            "source": "USGS 3DEP 1 m lidar DEM tiles (native, The National Map)",
            "crs": native.get("crs") or "NAD83 UTM (the tiles' zone)",
            "units": "metres",
            "file_type": "VRT over the native GeoTIFF tiles (and the fallback DEM underneath, if any)",
            "cell_m": 1,
            "native_source": "TNM Access API, dataset 'Digital Elevation Model (DEM) 1 meter', GeoTIFF",
            "native_crs": "NAD83 / UTM zone of each tile",
            "native_units": "metres (NAVD88)",
            "native_file_type": "GeoTIFF float32, 10 x 10 km tiles at 1 m",
        }
    if source == "usgs":
        return {
            "source": "USGS 3DEP ImageServer (elevation.nationalmap.gov)",
            "crs": "EPSG:5070",
            "units": "metres",
            "file_type": "GeoTIFF float32, tiled, DEFLATE",
            "cell_m": cell_m,
            "native_source": "USGS 3DEP Elevation ImageServer exportImage",
            "native_crs": "EPSG:5070",
            "native_units": "metres",
            "native_file_type": "GeoTIFF float32",
        }
    return {
        "source": "ND DWR QL2 LiDAR (lidarftp.swc.nd.gov, lidar.dwr.nd.gov)",
        "crs": "EPSG:5070",
        "units": "metres",
        "file_type": "GeoTIFF float32, tiled, DEFLATE",
        "cell_m": cell_m,
        "native_source": native.get("source") or "ND DWR LiDAR_NorthDakota_QL2 DEM_Imagine",
        # 0.17.50: DEM_Imagine is UTM metres (EPSG:26914 / 3721 are UTM 14N metres, which is
        # how build_vrts reads it); only ASCII_Grids_ft is ND State Plane feet
        "native_crs": native.get("crs") or ("QL2 tile CRS: DEM_Imagine UTM metres (EPSG:26914 or "
                                            "EPSG:3721), ASCII_Grids_ft ND State Plane feet "
                                            "(EPSG:2265 or EPSG:2266)"),
        "native_units": native.get("units") or ("metres (DEM_Imagine); ASCII_Grids_ft feet, "
                                                "converted x 0.3048"),
        "native_file_type": native.get("file_type") or "Erdas Imagine .img, 1 m",
    }


def describe_product(info):
    return "source: {source}; crs: {crs}; units: {units}; file type: {file_type}; cell: {cell_m} m".format(**info)


def write_sidecar(tif_path, info):
    path = os.path.splitext(tif_path)[0] + "_dem.txt"
    lines = [
        "source: {}".format(info["source"]),
        "crs: {}".format(info["crs"]),
        "units: {}".format(info["units"]),
        "file_type: {}".format(info["file_type"]),
        "cell_m: {}".format(info["cell_m"]),
        "native_source: {}".format(info["native_source"]),
        "native_crs: {}".format(info["native_crs"]),
        "native_units: {}".format(info["native_units"]),
        "native_file_type: {}".format(info["native_file_type"]),
        # 0.17.50: which build wrote it - an ND DWR download without this line is older
        # (dem_zunit.old_nd_dwr_problem)
        "{}: {}".format(ZU.FACTS_WRITER_KEY, ZU.WRITER),
    ]
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
    return path


def native_from_raster(path):
    """CRS, units and file type of the file GDAL opened, before the local warp."""
    info = {"source": "", "crs": "", "units": "", "file_type": ""}
    if not path:
        return info
    ds = gdal.Open(path)
    if ds is None:
        info["file_type"] = os.path.splitext(path)[1].lower() or "unknown"
        return info
    wkt = ds.GetProjection() or ""
    srs = osr.SpatialReference()
    if wkt:
        srs.ImportFromWkt(wkt)
        auth = srs.GetAuthorityCode(None)
        info["crs"] = "EPSG:{}".format(auth) if auth else srs.GetName()
        unit = srs.GetLinearUnitsName() or ""
        info["units"] = "feet" if "foot" in unit.lower() or "feet" in unit.lower() else (unit or "unknown")
    driver = ds.GetDriver()
    info["file_type"] = driver.ShortName if driver else os.path.splitext(path)[1]
    ds = None
    return info


def local_unit(src, info):
    """(choice, how) of the elevation unit write_local_dem writes for `src`: the unit src records
    (core/dem_zunit.recorded: build_vrts tags its VRTs), else metres for the USGS 3DEP
    ImageServer, which serves metres and whose mosaic of tiles records no unit. Any other source
    that records none gives ("", why): the copy is left untagged (0.17.50)."""
    got, how = recorded_unit(src)
    if got:
        return got, how
    if str(info.get("source") or "").startswith("USGS 3DEP") and \
            ZU.unit_choice(info.get("units")) == ZU.METERS:
        return ZU.METERS, "the USGS 3DEP ImageServer serves metres"
    return "", how


def write_local_dem(src, dest, cell_m, info, feedback=None):
    """Warp src to one tiled EPSG:5070 GeoTIFF at cell_m and write the facts beside it. 0.17.50:
    band 1 is tagged with the unit of local_unit(src) (untagged when it finds none), and for a
    feet unit UNITS and the facts file say feet, not the product's metres."""
    os.makedirs(os.path.dirname(os.path.abspath(dest)) or ".", exist_ok=True)
    got, how = local_unit(src, info)
    if got and got != ZU.METERS:
        info = dict(info, units=ZU.NAMES[got])
    if feedback is not None and got != ZU.METERS:
        getattr(feedback, "pushWarning", feedback.pushInfo)(
            "the local DEM is {} ({}): the Runner's 'DEM elevation units' must say so".format(
                ZU.NAMES[got], how) if got else
            "the local DEM records no elevation unit ({}): set 'DEM elevation units' in the "
            "Runner".format(how))
    if feedback is not None:
        feedback.pushInfo(describe_product(info))
        feedback.pushInfo("writing {}".format(dest))
    # 0.17.35: no source overviews; 0.17.50: the values as stored (no vertical shift), so the
    # band unit of local_unit(src) is the unit of what is written
    gdal.Warp(dest, src, **no_vshift(gdal, no_overviews(gdal, dict(
        dstSRS="EPSG:5070", xRes=float(cell_m), yRes=float(cell_m),
        resampleAlg="bilinear", outputType=gdal.GDT_Float32, dstNodata=-9999,
        format="GTiff",
        creationOptions=["COMPRESS=DEFLATE", "TILED=YES", "BIGTIFF=IF_SAFER"]))))
    ds = gdal.Open(dest, gdal.GA_Update)
    if ds is not None:
        ds.SetMetadataItem("SOURCE", info["source"])
        ds.SetMetadataItem("CRS", info["crs"])
        ds.SetMetadataItem("UNITS", info["units"])
        ds.SetMetadataItem("FILE_TYPE", info["file_type"])
        ds.SetMetadataItem("CELL_M", str(info["cell_m"]))
        ds.SetMetadataItem(ZU.WRITER_KEY, ZU.WRITER)     # 0.17.50: which build wrote it
        ds.SetDescription(describe_product(info))
        ds = None
    if got:
        set_unit(dest, got)                              # 0.17.50: the band records its unit
    side = write_sidecar(dest, info)
    return dest, side
