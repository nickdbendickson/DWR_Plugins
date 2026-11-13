"""damsuite.sample_dam (Core 1.2.0): a ready-made dam to try the suite on.

A new user has no dam folder, no terrain and no idea what a finished run looks like. The sample
is one real North Dakota dam with everything a first run needs:

    <root>/sample_dam/<dam ID>/dam.json                 the dam record (damsuite.dam_record)
    <root>/sample_dam/<dam ID>/<dam ID>_10m_5070.tif    10 m terrain, EPSG:5070, metres
    <root>/sample_dam/<dam ID>/sample.json              what it is and where it came from
    <root>/sample_dam/<dam ID>/README.txt

`<root>` is the offline pack folder: the pack builder writes the sample as its last step. A
window never writes into a pack, so for a pack built before 1.2.0 the home window's "Try the
sample dam" downloads the terrain itself, straight into the Dams folder.

"Try the sample dam" copies the sample to <Dams folder>/<dam ID>/. The terrain keeps the name
the suite's own "Download DEM" writes, so the Runner finds it in the dam folder by itself, and
its band unit says metres, so "Detect from the raster" works.

SAMPLES holds the dams; DEFAULT is the one behind the button. It is a sample to learn the tools
on. A run of it is NOT a hazard determination for that dam.

Pure Python. GDAL (osgeo) is needed only by build(); it is imported there.
"""
import collections
import json
import os
import shutil
import threading
import time
import urllib.parse
import urllib.request

from . import dam_record as DR

VERSION = "1.0"
FOLDER = "sample_dam"
CELL_M = 10.0
INFO_NAME = "sample.json"
README_NAME = "README.txt"
TOOL = "DamSafety sample dam"
SERVICE = "https://elevation.nationalmap.gov/arcgis/rest/services/3DEPElevation/ImageServer"
SOURCE = "USGS 3D Elevation Program (3DEP) elevation service, The National Map"
TILE_PX = 2000                                 # pixels per side of one request
TIMEOUT_S = 180
RETRIES = 3
RETRY_WAIT_S = 5.0                             # times the try number
MAX_NODATA_FRAC = 0.001
MIN_DEM_BYTES = 100000                         # smaller is not a whole terrain file

# fields: the dam record (damsuite.dam_record names). box: the terrain, EPSG:5070 metres on the
# 10 m grid (x0, y0, x1, y1): the dam, its pool and the reach the breach flood covers, with a
# margin. z_range_m: plausible ground there, a check on what the elevation service returns.
Sample = collections.namedtuple("Sample", "dam_id name fields box z_range_m about note")

SAMPLES = collections.OrderedDict()
# Swan Buffalo Detention Dam 12 near Absaraka, Cass County: the suite's most checked dam (the
# grid-size study, the comparison with DSS-WISE). Height, storage, length and the hazard class
# of record are the ND DWR inventory's values; the area is the DWR maximum-pool area the
# reference runs use (154.5 ac; the 111.6 ac of the screening record is the normal pool); the
# point and the top of dam are the 2026-10 screening's (crest point; top of dam read from the
# LiDAR DEM, so its datum is "DEM": the Runner uses it as given, with no datum shift). It sits
# on a flat plain, the hard case for a breach model: the flood spreads for miles and creeps
# for days.
SAMPLES["DWR-2801"] = Sample(
    dam_id="DWR-2801", name="Swan Buffalo Detention Dam 12",
    fields={
        "dam_id": "DWR-2801", "name": "Swan Buffalo Detention Dam 12", "nid_id": "ND00199",
        "dwr_structure_id": "2801", "county": "Cass", "lon": -97.3914888, "lat": 46.9830314,
        "x": -106343.832, "y": 2666591.505, "crs": "EPSG:5070",
        "height_ft": 27.5, "storage_acft": 1615.496, "surface_area_ac": 154.5,
        "top_of_dam_ft": 1069.117, "top_of_dam_datum": "DEM", "dam_length_ft": 1700.0,
        "existing_hazard_class": "Significant"},
    box=(-111500.0, 2649500.0, -78500.0, 2671500.0),
    z_range_m=(230.0, 420.0),
    about=("near Absaraka in Cass County, on a flat plain where a breach flood spreads over "
           "many square miles and keeps creeping for a day or more. That makes it slow to "
           "model at the 10 m default cell. A 30 m cell has nine times fewer cells and runs "
           "many times faster, which suits a first try."),
    note=("sample dam shipped with the suite (ND DWR inventory values, area at the DWR "
          "maximum pool; point and top of dam from a LiDAR screening, 2026-10). A sample to "
          "learn the tools on, not a determination."))
DEFAULT = "DWR-2801"
FIRST_TRY_CELL_M = 30.0                        # the model cell a first try should use


class SampleError(Exception):
    """The sample could not be built or installed; the text says why in plain words."""


def get(sample_id=None):
    """The Sample for an ID (None = the default one). Raises SampleError for an unknown ID."""
    key = sample_id or DEFAULT
    if key not in SAMPLES:
        raise SampleError("there is no sample dam called {}".format(key))
    return SAMPLES[key]


def dem_name(sample_id=None):
    """The terrain file's name: what 'Download DEM' writes for this dam."""
    return get(sample_id).dam_id + "_10m_5070.tif"


def sample_folder(root, sample_id=None):
    return os.path.join(root, FOLDER, get(sample_id).dam_id)


def complete(folder, sample_id=None):
    """True when `folder` holds a usable sample: the record and a terrain file with content."""
    try:
        return (os.path.isfile(os.path.join(folder, DR.FILE_NAME))
                and os.path.getsize(os.path.join(folder, dem_name(sample_id))) > MIN_DEM_BYTES)
    except OSError:
        return False


def find(*roots, **kw):
    """The first complete sample under the given roots (the pack folder, the suite data
    folder ...), or ''. Empty roots are skipped. sample_id= picks another sample."""
    sid = kw.get("sample_id")
    for root in roots:
        if not root:
            continue
        f = sample_folder(str(root), sid)
        if complete(f, sid):
            return f
    return ""


def pack_roots():
    """Where an offline pack's sample may be: the pack chosen in Suite data and servers, the
    pack in the suite data folder, then that folder. [] when nothing is set."""
    out = []
    try:
        from . import gpkgpack as GP
        from . import suitedata as SU
        loc = SU.pack_location()
        if loc:
            here = GP.pack_folder_of(loc) or (loc if os.path.isdir(loc)
                                              else os.path.dirname(loc))
            # a format-1 pack (folder or zip) has the format-2 pack beside it
            out += [here, os.path.join(os.path.dirname(here.rstrip("/\\")), GP.PACK_FOLDER)]
        data = SU.data_folder()
        if data:
            pack = SU.find_pack(data)
            if pack and os.path.isdir(pack):
                out.append(pack)
            out += [os.path.join(data, GP.PACK_FOLDER), data]
    except Exception:                                             # noqa: BLE001
        pass
    seen = []
    for r in out:
        if r and r not in seen:
            seen.append(r)
    return seen


def grid(sample_id=None):
    """(nx, ny) of the sample's terrain."""
    x0, y0, x1, y1 = get(sample_id).box
    return int(round((x1 - x0) / CELL_M)), int(round((y1 - y0) / CELL_M))


def download_mb(sample_id=None):
    """About how much the terrain download is, in MB (32-bit values, not compressed)."""
    nx, ny = grid(sample_id)
    return int(round(nx * ny * 4 / 1e6))


def tiles(sample_id=None, tile_px=None):
    """[(x0, y0, x1, y1, width, height)] covering the terrain in requests of at most
    tile_px a side."""
    x0, _y0, _x1, y1 = get(sample_id).box
    nx, ny = grid(sample_id)
    if nx <= 0 or ny <= 0:
        raise SampleError("the sample terrain box is empty")
    tile_px = int(tile_px or TILE_PX)
    out = []
    for j in range(0, ny, tile_px):
        for i in range(0, nx, tile_px):
            w, h = min(tile_px, nx - i), min(tile_px, ny - j)
            out.append((x0 + i * CELL_M, y1 - (j + h) * CELL_M, x0 + (i + w) * CELL_M,
                        y1 - j * CELL_M, w, h))
    return out


def tile_url(tile):
    a, b, c, d, w, h = tile
    q = {"bbox": "{},{},{},{}".format(a, b, c, d), "bboxSR": 5070, "imageSR": 5070,
         "size": "{},{}".format(w, h), "format": "tiff", "pixelType": "F32",
         "interpolation": "RSP_BilinearInterpolation", "f": "image"}
    return SERVICE + "/exportImage?" + urllib.parse.urlencode(q)


def _fetch(url, timeout=TIMEOUT_S):
    req = urllib.request.Request(url, headers={"User-Agent": "DamSafety-suite"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _is_tiff(raw):
    return raw[:4] in (b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+")


def info(sample_id=None):
    """What sample.json says (no dates: the same sample gives the same file)."""
    sm = get(sample_id)
    return {"format": "nd_dam_suite/sample_dam", "format_version": 1, "version": VERSION,
            "dam_id": sm.dam_id, "name": sm.name,
            "terrain": {"file": dem_name(sample_id), "crs": "EPSG:5070", "cell_m": CELL_M,
                        "units": "metres", "box": list(sm.box), "source": SOURCE,
                        "service": SERVICE},
            "record": dict(sm.fields), "about": sm.about, "note": sm.note}


def readme(sample_id=None):
    sm = get(sample_id)
    return "\n".join([
        "Sample dam: {} ({}, NID {})".format(sm.name, sm.dam_id, sm.fields.get("nid_id", "")),
        "",
        "A ready-made dam to try the DamSafety suite on. Open 'Dam suite home' in QGIS and",
        "click 'Try the sample dam': it is copied to your Dams folder and the breach Runner",
        "opens with the dam and its terrain filled in.",
        "",
        "The dam is " + sm.about,
        "",
        "  dam.json                  the dam record (ND DWR inventory values)",
        "  {}    10 m terrain, EPSG:5070, elevations in metres".format(dem_name(sample_id)),
        "                            ({})".format(SOURCE),
        "",
        "This is a sample to learn the tools on. A run of it is not a hazard determination",
        "for this dam.",
        ""])


def write_record(folder, sample_id=None, overwrite=False):
    """Write the sample's dam.json into `folder`. An existing record that already names a dam
    is kept (returns False) unless overwrite: a user's own values are never replaced."""
    sm = get(sample_id)
    rec = DR.load(folder, create=True)
    if not overwrite and rec.get("dam_id"):
        return False
    rec.set_many(sm.fields, TOOL, VERSION, sm.note, overwrite=True)
    rec.save()
    return True


def build(root, sample_id=None, fetch=None, log=None, cancel=None, refresh=False,
          folder=None):
    """Make the sample under `root` (see the module text) and return its folder; `folder`
    names the folder itself instead (the home window builds straight into the dam folder).
    The terrain is downloaded part by part (kept between tries, so a second call resumes),
    joined, checked and tagged as metres. Needs GDAL and, for the download, the internet.
    Raises SampleError."""
    sm = get(sample_id)
    say = log or (lambda _m: None)
    fetch = fetch or _fetch
    folder = folder or sample_folder(root, sample_id)
    dem = os.path.join(folder, dem_name(sample_id))
    if complete(folder, sample_id) and not refresh:
        say("Sample dam: already there ({})".format(folder))
        return folder
    try:
        from osgeo import gdal
    except Exception as e:                                        # noqa: BLE001
        raise SampleError("the sample dam's terrain needs GDAL, which this Python does not "
                          "have ({})".format(e))
    work = os.path.join(folder, "_sample_download")
    try:
        os.makedirs(work, exist_ok=True)
    except OSError as e:
        raise SampleError("cannot write the sample dam to {} ({})".format(folder, e))
    todo = tiles(sample_id)
    parts = []
    for k, t in enumerate(todo):
        if cancel and cancel():
            raise SampleError("cancelled")
        part = os.path.join(work, "part_{:02d}.tif".format(k))
        if refresh or not _tile_ok(gdal, part, t):
            _get_tile(gdal, fetch, t, part)
        parts.append(part)
        say("Sample dam terrain: part {} of {}".format(k + 1, len(todo)))
    have_dem = os.path.isfile(dem) and not check_terrain(gdal, dem, sample_id)
    tmp = dem + ".part.tif"
    try:
        if not have_dem or refresh:
            vrt = gdal.BuildVRT(os.path.join(work, "mosaic.vrt"), parts)
            if vrt is None:
                raise SampleError("the terrain parts could not be joined")
            out = gdal.Translate(tmp, vrt, creationOptions=[
                "COMPRESS=DEFLATE", "PREDICTOR=3", "TILED=YES", "ZLEVEL=9"])
            vrt = None
            if out is None:
                raise SampleError("the terrain file could not be written")
            out.GetRasterBand(1).SetUnitType("metre")
            out.SetMetadata({"SOURCE": SOURCE, "CRS": "EPSG:5070", "UNITS": "metres",
                             "FILE_TYPE": "GeoTIFF", "CELL_M": "{:g}".format(CELL_M),
                             "SAMPLE_DAM": sm.dam_id})
            out.FlushCache()
            out = None
            problem = check_terrain(gdal, tmp, sample_id)
            if problem:
                raise SampleError("the downloaded terrain is not usable: " + problem)
            os.replace(tmp, dem)
    except SampleError:
        _rm(tmp)
        raise
    except Exception as e:                                        # noqa: BLE001
        _rm(tmp)
        raise SampleError("the sample dam's terrain could not be written ({})".format(e))
    write_record(folder, sample_id)
    for name, text in ((INFO_NAME, json.dumps(info(sample_id), indent=1, sort_keys=True)),
                       (README_NAME, readme(sample_id))):
        with open(os.path.join(folder, name), "w", encoding="utf-8") as f:
            f.write(text)
    shutil.rmtree(work, ignore_errors=True)
    say("Sample dam: written to {}".format(folder))
    return folder


def _rm(path):
    try:
        os.remove(path)
    except OSError:
        pass


def _tile_ok(gdal, path, tile):
    if not os.path.isfile(path):
        return False
    try:
        ds = gdal.Open(path)
        return ds is not None and (ds.RasterXSize, ds.RasterYSize) == (tile[4], tile[5])
    except Exception:                                             # noqa: BLE001
        return False


def _get_tile(gdal, fetch, tile, dest):
    url, last = tile_url(tile), ""
    for attempt in range(RETRIES):
        try:
            raw = fetch(url)
            if not _is_tiff(raw):
                raise SampleError("the elevation service answered with something that is not "
                                  "a GeoTIFF ({} bytes)".format(len(raw)))
            with open(dest + ".part", "wb") as f:
                f.write(raw)
            if not _tile_ok(gdal, dest + ".part", tile):
                raise SampleError("a terrain part came back with the wrong size")
            os.replace(dest + ".part", dest)
            return
        except Exception as e:                                    # noqa: BLE001
            last = str(e)
            _rm(dest + ".part")
            if attempt + 1 < RETRIES and RETRY_WAIT_S > 0:
                time.sleep(RETRY_WAIT_S * (attempt + 1))
    raise SampleError("the sample dam's terrain could not be downloaded from the USGS "
                      "elevation service ({}). Check the internet connection and try "
                      "again.".format(last))


def check_terrain(gdal, path, sample_id=None):
    """'' when the terrain file is the sample's (its grid, plausible ground, no holes), else
    why not."""
    sm = get(sample_id)
    try:
        ds = gdal.Open(path)
    except Exception:                                             # noqa: BLE001
        ds = None
    if ds is None:
        return "it cannot be opened"
    x0, _y0, _x1, y1 = sm.box
    nx, ny = grid(sample_id)
    if (ds.RasterXSize, ds.RasterYSize) != (nx, ny):
        return "its size is {} x {}, not {} x {}".format(ds.RasterXSize, ds.RasterYSize, nx, ny)
    gt = ds.GetGeoTransform()
    if abs(gt[0] - x0) > 0.01 or abs(gt[3] - y1) > 0.01 or abs(gt[1] - CELL_M) > 1e-6:
        return "its grid is not the sample's"
    band = ds.GetRasterBand(1)
    lo, hi = band.ComputeRasterMinMax(False)
    if not (sm.z_range_m[0] <= lo and hi <= sm.z_range_m[1]):
        return ("its elevations ({:.0f} to {:.0f}) are outside the ground range of the area in "
                "metres".format(lo, hi))
    nod = band.GetNoDataValue()
    if nod is not None:
        try:
            a = band.ReadAsArray()
            frac = float((a == nod).sum()) / a.size
            if frac > MAX_NODATA_FRAC:
                return "{:.1%} of it has no data".format(frac)
        except Exception:                                         # noqa: BLE001
            pass
    return ""


def installed(dams_folder, sample_id=None):
    """The sample's dam folder in a Dams folder when the sample is complete there, else ''."""
    if not str(dams_folder or "").strip():
        return ""
    target = os.path.join(str(dams_folder), get(sample_id).dam_id)
    return target if complete(target, sample_id) else ""


def install(sample, dams_folder, sample_id=None):
    """Copy the sample folder `sample` into <dams_folder>/<dam ID>/ and return what was done:
    {"folder", "dem", "record" ("written" / "kept"), "dem_copied" (bool)}. A dam record or a
    terrain file already there is kept, so trying the sample twice never undoes a user's work."""
    sm = get(sample_id)
    name = dem_name(sample_id)
    if not complete(sample, sample_id):
        raise SampleError("the sample dam is not complete in {}".format(sample))
    if not str(dams_folder or "").strip():
        raise SampleError("choose the Dams folder first")
    target = os.path.join(str(dams_folder), sm.dam_id)
    try:
        os.makedirs(target, exist_ok=True)
        dem = os.path.join(target, name)
        copied = False
        src = os.path.join(sample, name)
        if not os.path.isfile(dem) or os.path.getsize(dem) != os.path.getsize(src):
            shutil.copyfile(src, dem + ".part")
            os.replace(dem + ".part", dem)
            copied = True
        for extra in (README_NAME, INFO_NAME):
            src = os.path.join(sample, extra)
            if os.path.isfile(src) and not os.path.isfile(os.path.join(target, extra)):
                shutil.copyfile(src, os.path.join(target, extra))
    except OSError as e:
        raise SampleError("the sample dam could not be copied to {} ({})".format(target, e))
    wrote = write_record(target, sample_id)
    return {"folder": target, "dem": dem, "record": "written" if wrote else "kept",
            "dem_copied": copied}


class Job(object):
    """build() on a background thread, for a window that must stay usable while the terrain
    downloads: start(), then poll `done`; `messages` grows as parts arrive, `error` is '' or
    why it failed, `folder` is the result. cancel() stops it before the next part."""

    def __init__(self, **build_args):
        self.messages, self.error, self.folder, self.done = [], "", "", False
        self.cancelled = False
        self._args = build_args
        self._thread = threading.Thread(target=self._run, name="sample-dam", daemon=True)

    def start(self):
        self._thread.start()
        return self

    def cancel(self):
        self.cancelled = True

    def wait(self, timeout=None):
        self._thread.join(timeout)
        return self.done

    def _run(self):
        try:
            self.folder = build(self._args.pop("root", None), log=self.messages.append,
                                cancel=lambda: self.cancelled, **self._args)
        except SampleError as e:
            self.error = str(e)
        except Exception as e:                                    # noqa: BLE001
            self.error = "the sample dam could not be made ({})".format(e)
        finally:
            self.done = True
