"""USGS 3DEP download extents for a dam, a North Dakota county, or the state.

County boxes are download clips, padded, not survey boundaries. The service
returns metres in EPSG:5070.
"""
import os
import zipfile

from osgeo import gdal, osr

from . import terrain


def _safe_extract(zf, dest):
    """Extract a zip into dest, refusing any entry that leaves dest."""
    root = os.path.realpath(dest)
    for member in zf.infolist():
        target = os.path.realpath(os.path.join(dest, member.filename))
        if target != root and not target.startswith(root + os.sep):
            raise ValueError("archive entry outside the destination: " + member.filename)
    for member in zf.infolist():
        zf.extract(member, dest)

# west, south, east, north in degrees. Padded so a dam on the county line is inside.
COUNTIES = {
    "Adams": (-103.00, 45.93, -102.05, 46.45),
    "Barnes": (-98.35, 46.60, -97.45, 47.35),
    "Benson": (-99.70, 47.75, -98.70, 48.35),
    "Billings": (-103.70, 46.65, -102.90, 47.30),
    "Bottineau": (-101.40, 48.45, -100.15, 49.01),
    "Bowman": (-104.05, 45.93, -102.95, 46.45),
    "Burke": (-102.95, 48.45, -102.05, 49.01),
    "Burleigh": (-101.05, 46.55, -100.05, 47.30),
    "Cass": (-97.45, 46.60, -96.55, 47.35),
    "Cavalier": (-98.95, 48.55, -97.85, 49.01),
    "Dickey": (-98.95, 45.93, -98.05, 46.45),
    "Divide": (-104.05, 48.50, -103.05, 49.01),
    "Dunn": (-103.25, 47.15, -102.15, 47.85),
    "Eddy": (-99.15, 47.45, -98.45, 47.95),
    "Emmons": (-100.55, 45.95, -99.55, 46.65),
    "Foster": (-99.35, 47.15, -98.55, 47.75),
    "Golden Valley": (-104.05, 46.55, -103.45, 47.25),
    "Grand Forks": (-97.65, 47.55, -96.75, 48.15),
    "Grant": (-102.05, 46.05, -101.00, 46.65),
    "Griggs": (-98.55, 47.25, -97.85, 47.75),
    "Hettinger": (-102.85, 46.05, -101.95, 46.70),
    "Kidder": (-100.25, 46.55, -99.25, 47.35),
    "LaMoure": (-98.95, 46.15, -98.05, 46.75),
    "Logan": (-99.65, 46.15, -98.85, 46.75),
    "McHenry": (-100.95, 47.85, -99.95, 48.55),
    "McIntosh": (-99.75, 45.93, -98.95, 46.45),
    "McKenzie": (-104.05, 47.35, -102.65, 48.15),
    "McLean": (-101.65, 47.25, -100.35, 48.05),
    "Mercer": (-102.35, 47.05, -101.35, 47.65),
    "Morton": (-101.65, 46.35, -100.35, 47.05),
    "Mountrail": (-102.85, 47.75, -101.55, 48.55),
    "Nelson": (-98.45, 47.65, -97.75, 48.25),
    "Oliver": (-101.65, 46.95, -100.85, 47.45),
    "Pembina": (-97.75, 48.55, -96.75, 49.01),
    "Pierce": (-100.35, 47.85, -99.45, 48.55),
    "Ramsey": (-99.05, 47.95, -98.25, 48.55),
    "Ransom": (-97.95, 46.25, -97.15, 46.85),
    "Renville": (-102.05, 48.45, -101.15, 49.01),
    "Richland": (-97.35, 45.93, -96.55, 46.55),
    "Rolette": (-100.25, 48.45, -99.45, 49.01),
    "Sargent": (-97.85, 45.93, -97.05, 46.45),
    "Sheridan": (-100.55, 47.25, -99.55, 47.95),
    "Sioux": (-101.65, 45.95, -100.45, 46.55),
    "Slope": (-104.05, 46.15, -103.15, 46.75),
    "Stark": (-103.25, 46.45, -101.95, 47.25),
    "Steele": (-98.05, 47.15, -97.35, 47.65),
    "Stutsman": (-99.35, 46.55, -98.25, 47.35),
    "Towner": (-99.55, 48.35, -98.75, 48.95),
    "Traill": (-97.55, 47.15, -96.65, 47.65),
    "Walsh": (-98.15, 48.15, -97.05, 48.65),
    "Ward": (-102.05, 47.85, -100.75, 48.65),
    "Wells": (-100.05, 47.25, -99.15, 47.95),
    "Williams": (-104.05, 47.95, -102.65, 48.65),
}
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


def point_box(lon, lat, buffer_mi):
    x0, y0, x1, y1 = to_5070(lon, lat, lon, lat)
    b = buffer_mi * MI
    return (x0 - b, y0 - b, x1 + b, y1 + b)


def download(extent, cell_m, out_dir, stem, feedback=None):
    """Download 3DEP tiles for an EPSG:5070 extent and write dem.vrt. Returns the VRT path."""
    os.makedirs(out_dir, exist_ok=True)
    from .solve_gate import TILE_PX
    tiles = list(terrain.tile_extents(*extent, cell_m))
    if feedback is not None:
        feedback.pushInfo("{} tiles at {} m ({} px a side)".format(len(tiles), cell_m, TILE_PX))
    n = max(len(tiles), 1)

    def log(msg):
        if feedback is not None:
            feedback.pushInfo(msg)

    def tick(i, total):
        if feedback is None:
            return
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
    import ftplib
    import zipfile
    path = parse_dwr_path(package)
    wanted = [p for p in products if p in DWR_PRODUCTS]
    if not wanted:
        raise ValueError("choose ASCII_Grids_ft, DEM_Imagine, or both")
    os.makedirs(out_dir, exist_ok=True)
    ftp = ftplib.FTP(DWR_HOST, timeout=120)
    ftp.login(DWR_USER, DWR_PASSWORD)
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
        with open(local, "wb") as f:
            ftp.retrbinary("RETR " + remote, f.write)
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
    rasters = [r for r in rasters if not r.lower().endswith(".rrd")]
    vrt = os.path.join(out_dir, "nd_lidar.vrt")
    if rasters:
        gdal.BuildVRT(vrt, rasters)
    return vrt


INDEX_DIR = "LiDAR_NorthDakota_QL2"
INDEX_NAME = "IndexLiDARNorthDakotaQL2"


def _ftp():
    import ftplib
    ftp = ftplib.FTP(DWR_HOST, timeout=120)
    ftp.login(DWR_USER, DWR_PASSWORD)
    return ftp


def ensure_index(cache_dir, feedback=None):
    """Cache the statewide QL2 tile index (EPSG:2266 feet). Returns the shapefile path."""
    os.makedirs(cache_dir, exist_ok=True)
    shp = os.path.join(cache_dir, INDEX_NAME + ".shp")
    if os.path.isfile(shp) and os.path.isfile(shp.replace(".shp", ".dbf")):
        return shp
    ftp = _ftp()
    for ext in (".shp", ".shx", ".dbf", ".prj", ".cpg"):
        remote = "/{}/{}{}".format(INDEX_DIR, INDEX_NAME, ext)
        local = os.path.join(cache_dir, INDEX_NAME + ext)
        if feedback is not None:
            feedback.pushInfo("index {}".format(ext))
        with open(local, "wb") as f:
            ftp.retrbinary("RETR " + remote, f.write)
    ftp.quit()
    return shp


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


def tiles_for(scope, county, cache_dir, feedback=None):
    """Tile records whose box meets a county clip or the state. scope 1 = county, 2 = state."""
    from osgeo import ogr
    shp = ensure_index(cache_dir, feedback)
    if scope == 1:
        box = COUNTIES.get(county)
        if box is None:
            raise ValueError("Unknown county {!r}".format(county))
    else:
        box = STATE
    xmin, ymin, xmax, ymax = _box_2266(*box)
    ds = ogr.Open(shp)
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


def fetch_dwr_area(scope, county, out_dir, products, feedback=None, cache_dir=None, tiles=None):
    """Download QL2 tiles for one county, the state, or a preselected tile list."""
    import zipfile
    cache_dir = cache_dir or os.path.join(out_dir, "_index")
    if tiles is None:
        tiles = tiles_for(scope, county, cache_dir, feedback)
    if feedback is not None:
        feedback.pushInfo("{} QL2 tiles".format(len(tiles)))
    if not tiles:
        raise IOError("no QL2 tiles in that area")
    os.makedirs(out_dir, exist_ok=True)
    ftp = _ftp()
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
                with open(local, "wb") as f:
                    ftp.retrbinary("RETR " + remote, f.write)
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
    ftp.quit()
    rasters = [r for r in rasters if not r.lower().endswith(".rrd")]
    vrt = os.path.join(out_dir, "nd_lidar.vrt")
    if rasters:
        gdal.BuildVRT(vrt, rasters)
    return vrt, len(tiles)


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
    """Warp one raster to dest in the given EPSG. Returns dest."""
    gdal.Warp(dest, src, dstSRS="EPSG:{}".format(epsg), format="GTiff",
              resampleAlg="bilinear", creationOptions=["COMPRESS=DEFLATE", "TILED=YES"])
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
    from osgeo import ogr
    shp = ensure_index(cache_dir, feedback)
    xmin, ymin, xmax, ymax = _box_2266(west, south, east, north)
    ds = ogr.Open(shp)
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
    """Facts written beside the local DEM. source is 'usgs' or 'ndlidar'."""
    native = native or {}
    cell_m = int(cell_m)
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
        "native_crs": native.get("crs") or "QL2 tile CRS (feet; often EPSG:3721 or EPSG:2266)",
        "native_units": native.get("units") or "feet",
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


def write_local_dem(src, dest, cell_m, info, feedback=None):
    """Warp src to one tiled EPSG:5070 GeoTIFF at cell_m and write the facts beside it."""
    os.makedirs(os.path.dirname(os.path.abspath(dest)) or ".", exist_ok=True)
    if feedback is not None:
        feedback.pushInfo(describe_product(info))
        feedback.pushInfo("writing {}".format(dest))
    gdal.Warp(
        dest, src, dstSRS="EPSG:5070", xRes=float(cell_m), yRes=float(cell_m),
        resampleAlg="bilinear", outputType=gdal.GDT_Float32, dstNodata=-9999,
        format="GTiff",
        creationOptions=["COMPRESS=DEFLATE", "TILED=YES", "BIGTIFF=IF_SAFER"])
    ds = gdal.Open(dest, gdal.GA_Update)
    if ds is not None:
        ds.SetMetadataItem("SOURCE", info["source"])
        ds.SetMetadataItem("CRS", info["crs"])
        ds.SetMetadataItem("UNITS", info["units"])
        ds.SetMetadataItem("FILE_TYPE", info["file_type"])
        ds.SetMetadataItem("CELL_M", str(info["cell_m"]))
        ds.SetDescription(describe_product(info))
        ds = None
    side = write_sidecar(dest, info)
    return dest, side
