"""GDAL side: read the (statewide) DEM into the model's metric CRS and write
GeoTIFFs. Works offline on local files, VRT mosaics included."""
import math
import os

import numpy as np

from .grid import Grid, NODATA
from .units import FT, US_FT
from ..damsuite import gdalsafe as GS
from . import dem_zunit as ZU
from .warp_opts import no_overviews, no_vshift

Z_AUTO, Z_FEET, Z_US_FEET, Z_METERS = "auto", ZU.FEET, ZU.US_FEET, ZU.METERS
# 3.10.89 review round 3: an explicit unit used although the raster's unit tag says otherwise
# ('Feet (the raster's unit tag is wrong)' ...). Appended: the DEM_Z indices 0-3 of saved
# parameters, rundam.json and the wizard keep their meaning.
Z_FEET_TAG_WRONG, Z_US_FEET_TAG_WRONG, Z_METERS_TAG_WRONG = (
    "feet_tag_wrong", "us_feet_tag_wrong", "meters_tag_wrong")
Z_TAG_WRONG = {Z_FEET_TAG_WRONG: Z_FEET, Z_US_FEET_TAG_WRONG: Z_US_FEET,
               Z_METERS_TAG_WRONG: Z_METERS}
Z_CHOICES = (Z_AUTO, Z_FEET, Z_US_FEET, Z_METERS, Z_FEET_TAG_WRONG, Z_US_FEET_TAG_WRONG,
             Z_METERS_TAG_WRONG)
Z_LABELS = {Z_FEET: "Feet", Z_US_FEET: "US survey feet", Z_METERS: "Meters"}
Z_LABELS.update({k: "{} (the raster's unit tag is wrong)".format(Z_LABELS[v])
                 for k, v in Z_TAG_WRONG.items()})
Z_CHOICE_LABELS = dict(Z_LABELS, **{Z_AUTO: "Detect from the raster"})   # the DEM_Z enum text
_TO_M = {Z_FEET: FT, Z_US_FEET: US_FT, Z_METERS: 1.0}

# 3.10.89: one list of unit names with Screening (core/dem_zunit.py, byte-identical there)
_FEET_NAMES = ZU.FEET_NAMES
_US_FEET_NAMES = ZU.US_FEET_NAMES
_METER_NAMES = ZU.METER_NAMES


def _gdal():
    # 3.4.1: no gdal.UseExceptions() here any more - it is process-wide and changed GDAL for
    # QGIS and the other plugins. Calls go through damsuite.gdalsafe (same errors, both modes).
    from osgeo import gdal, osr
    return gdal, osr


def _file(path):
    """The file of a QGIS raster source: 'x.tif|option:...' -> 'x.tif' (3.10.89)."""
    return str(path).split("|", 1)[0]


def base_choice(choice):
    """The unit of a DEM_Z choice: 'feet_tag_wrong' -> 'feet'; others as they are."""
    return Z_TAG_WRONG.get(choice, choice)


def z_to_m_factor(path, choice=Z_AUTO):
    """Factor that converts the DEM's elevations to metres, and how it was decided. An explicit
    choice as given; 'auto' (Detect from the raster) the unit the raster records, through
    resolve_dem_z - the rule of a model run (3.10.89: the culvert pre-check, the CLI and the
    self-test guessed feet or metres from the CRS units and the median here, so they could read
    a DEM in another unit than the run did). ValueError, naming the setting, when 'auto' finds
    no unit it can trust. A tool without that setting uses z_factor_or_guess."""
    choice = base_choice(choice)
    if choice == Z_FEET:
        return FT, "feet (chosen)"
    if choice == Z_US_FEET:
        return US_FT, "US survey feet (chosen)"
    if choice == Z_METERS:
        return 1.0, "metres (chosen)"
    got, how = resolve_dem_z(path, Z_AUTO)
    return _TO_M[got], detected_z_note(got, how)


def guess_z_from_crs(path):
    """(factor, note) by the rule of Runner 3.10.0-3.10.87 for a raster that records no unit: a
    feet (or US feet) projected CRS gives feet unless the median fits North Dakota's ground only
    as metres; any other CRS metres. Never for a model run (resolve_dem_z): only for what a tool
    without a 'DEM elevation units' setting must read anyway - a HEC-RAS terrain, the
    animation's hillshade, the HEC-RAS export of a run report older than 3.10.16 - and always
    with a warning (z_factor_or_guess)."""
    _, osr = _gdal()
    ds = GS.open_raster(_file(path))
    unit = (ds.GetRasterBand(1).GetUnitType() or "").strip()
    got = ZU.unit_choice(unit) or ZU.vertical_unit(ds)
    if got:                     # a band unit or vertical CRS the record rules did not trust
        return _TO_M[got], "{} (assumed from the raster's {})".format(
            ZU.NAMES[got], "band unit '{}'".format(unit.lower()) if unit else "vertical CRS")
    wkt = ds.GetProjection()
    lin = None
    if wkt:
        srs = osr.SpatialReference()
        srs.ImportFromWkt(wkt)
        lin = srs.GetLinearUnits() if srs.IsProjected() else None
    if lin and (abs(lin - US_FT) < 1e-7 or abs(lin - FT) < 1e-7):
        f, name = (US_FT, "US survey feet") if abs(lin - US_FT) < 1e-7 else (FT, "feet")
        med, _n = sample_median(ds)
        if med is not None and _nd_ground(med) and not _nd_ground(med * f):
            return 1.0, ("metres (assumed: the CRS is in {} but a median of {:.0f} is North "
                         "Dakota ground only as metres)").format(name, med)
        return f, "{} (assumed from the CRS's linear unit)".format(name)
    return 1.0, "metres (assumed: the CRS is not in feet)"


def z_factor_or_guess(path, choice=Z_AUTO, setting="'DEM elevation units'"):
    """(factor, how, warning) for a tool that must read the raster's values whatever it records:
    an explicit choice as given; 'auto' the unit the raster records (resolve_dem_z), else
    guess_z_from_crs - the rule before 3.10.88 - with a warning naming the unit used and
    `setting`, the tool's own way to choose it. 3.10.89 review round 3: the HEC-RAS terrain
    stopped there, naming a 'DEM elevation units' its tools do not have."""
    unit = base_choice(choice)
    name = os.path.basename(_file(path)) or str(path)
    if unit in _TO_M:
        f, how = z_to_m_factor(path, unit)
        try:
            got, rhow = recorded_z_unit(path)
        except Exception:                                # noqa - GDAL reports it on the read
            got, rhow = "", ""
        if got and not _same_family(got, unit):
            return f, how, ("{} records {} ({}), but {} is {}: read as {}, as chosen.".format(
                name, ZU.NAMES[got], rhow, setting, Z_LABELS[unit], ZU.NAMES[unit]))
        return f, how, ""
    try:
        f, how = z_to_m_factor(path, Z_AUTO)
        return f, how, ""
    except ValueError:
        pass
    got, why = recorded_z_unit(path)
    f, how = guess_z_from_crs(path)
    return f, how, ("{}: {}; its elevations are read as {}, as before Runner 3.10.88. If that is "
                    "wrong, set {}.").format(name, why or ZU.NONE_RECORDED, how, setting)


def recorded_z_unit(path):
    """(choice, how) from the elevation unit the DEM itself records - its band unit type (not
    when band 1 has a scale / offset), else a compound CRS's vertical unit, else (3.10.89) the
    Download DEM tool's own record of a USGS 3DEP download - or ("", why) when it records none
    that can be trusted. choice is Z_FEET, Z_US_FEET or Z_METERS. Never guesses from the
    horizontal CRS or the values (3.10.88). The rules are core/dem_zunit.recorded, which
    Screening's Download DEM uses for what it writes."""
    _gdal()
    ds = GS.open_raster(_file(path))                     # 3.10.89: a QGIS '|option:' source
    return ZU.recorded(ds, path)


DEM_Z_HELP = ("Set 'DEM elevation units' to Meters or Feet. Metres: the 'Download DEM' "
              "outputs (<id>_10m_5070.tif / <id>_1m_5070.tif - ND DWR ASCII_Grids_ft are "
              "converted x 0.3048 - and <id>_1m_lidar.vrt), USGS 3DEP 1 m lidar tiles, ND DWR "
              "DEM_Imagine (.img, UTM metres). Feet: the raw ND DWR QL2 ASCII_Grids_ft grids "
              "(.asc, ND State Plane feet).")


_GDAL_EDIT_UNIT = {Z_FEET: "ft", Z_US_FEET: '"US survey foot"', Z_METERS: "m"}


def _same_family(a, b):
    """Feet and US survey feet (2 ppm apart) count as one unit; metres is the other."""
    return (a == Z_METERS) == (b == Z_METERS)


def file_problem(ds, path=""):
    """Why no unit choice reads this raster's values right, or "": band 1's scale / offset (a
    run reads the raw values), a warped VRT whose vertical shift converted the values under the
    source's band unit, an old Download DEM file (dem_zunit.data_problem). 3.10.89 review round
    3: an explicit unit stops on these as 'Detect from the raster' does - before, Meters on a
    raw-feet band with scale 0.3048 read the feet as metres, and Feet on a vertical-shift VRT
    converted its metres again."""
    try:
        band = ds.GetRasterBand(1)
    except Exception:                                    # noqa - unreadable: GDAL says so later
        return ""
    return ZU.scale_offset_problem(band) or ZU.vshift_problem(ds) or ZU.data_problem(ds, path)


def _ground_verdict(med, chosen, rec):
    """Which of two readings of a DEM median is North Dakota ground: 'chosen', 'record', or
    'unclear' (both, neither, or no valid cell found)."""
    if med is None:
        return "unclear"
    c, r = _nd_ground(med * _TO_M[chosen]), _nd_ground(med * _TO_M[rec])
    if c and not r:
        return "chosen"
    if r and not c:
        return "record"
    return "unclear"


def _readings(med, a, b):
    return "{:.0f} m as {}, {:.0f} m as {}".format(med * _TO_M[a], ZU.NAMES[a],
                                                     med * _TO_M[b], ZU.NAMES[b])


def resolve_dem_z(path, choice=Z_AUTO):
    """The DEM elevation unit a model run uses, and how: "chosen", "chosen: <warning>" (an
    explicit unit used against the raster's record - the run report says so; z_how_parts), or
    "detected: ...". 'auto' (Detect from the raster): the unit the raster records
    (recorded_z_unit); ValueError naming the setting when it records none it can trust - a run
    never guesses feet or metres (3.10.88; before, 'auto' was refused even for a raster that
    records its unit).

    3.10.89: an explicit Feet / US survey feet / Meters is checked against the unit the raster
    records - a Download DEM output tagged metres run as Feet read the ground x 0.3048, and the
    ND ground check let it through above 656 m (south-west North Dakota). Feet and US survey
    feet do not stop each other (2 ppm). A raster whose values no single unit reads right
    (file_problem) stops whatever the choice. Review round 3: when the record says the other
    unit, the DEM's median decides - North Dakota ground only in the chosen unit: the choice is
    used, with a warning to correct the record; only in the recorded unit: stop, the record is
    right; both, neither or no sample: stop, naming 'Feet (the raster's unit tag is wrong)' (the
    Z_TAG_WRONG choices), the way through inside QGIS when the record is the wrong one."""
    file_ = _file(path)
    name = os.path.basename(file_) or str(path)
    _gdal()
    ds = GS.open_raster(file_)
    if choice in Z_LABELS:
        unit = base_choice(choice)
        bad = file_problem(ds, path)
        if bad:
            raise ValueError("'DEM elevation units' is {}, but {}: {}. No unit choice reads this "
                             "file right.".format(Z_LABELS[choice], name, bad))
        got, how = ZU.recorded(ds, path)
        if not got or _same_family(got, unit):
            return unit, "chosen"
        med, _n = sample_median(ds)
        ds = None
        verdict = _ground_verdict(med, unit, got)
        fix = ("gdal_edit -units {} \"{}\" (for a format GDAL cannot edit, make a GeoTIFF copy "
               "with gdal_translate and edit that)").format(_GDAL_EDIT_UNIT[unit], file_)
        if verdict == "record":
            raise ValueError(
                "'DEM elevation units' is {}, but {} records {} ({}) and its median {:.1f} is "
                "North Dakota ground only as {} ({}): set 'DEM elevation units' to {} (or "
                "'Detect from the raster').".format(
                    Z_LABELS[choice], name, ZU.NAMES[got], how, med, ZU.NAMES[got],
                    _readings(med, got, unit), Z_LABELS[got]))
        if verdict == "chosen" or choice in Z_TAG_WRONG:
            why = ("its median {:.1f} is North Dakota ground only as {} ({})".format(
                med, ZU.NAMES[unit], _readings(med, unit, got)) if verdict == "chosen" else
                "'DEM elevation units' says the tag is wrong")
            return unit, ("chosen: {} records {} ({}), but {}: read as {}, as chosen. Correct "
                          "the record so 'Detect from the raster' reads it: {}.").format(
                              name, ZU.NAMES[got], how, why, ZU.NAMES[unit], fix)
        seen = ("no valid elevation was found to check which is right" if med is None else
                "its median {:.1f} cannot tell which is right ({})".format(
                    med, _readings(med, unit, got)))
        raise ValueError(
            "'DEM elevation units' is {}, but {} records {} ({}), and {}. If the record is right, "
            "set 'DEM elevation units' to {} (or 'Detect from the raster'). If the elevations "
            "really are {}, choose '{}', or correct the record first: {}.".format(
                Z_LABELS[choice], name, ZU.NAMES[got], how, seen, Z_LABELS[got],
                ZU.NAMES[unit], Z_LABELS[{v: k for k, v in Z_TAG_WRONG.items()}[unit]], fix))
    got, how = ZU.recorded(ds, path)
    if got:
        return got, "detected: " + how
    bad = file_problem(ds, path)
    ds = None
    if bad:                     # no choice reads it right: do not send the user to one
        raise ValueError("'DEM elevation units' is 'Detect from the raster', but {}: {}. No "
                         "unit choice reads this file right.".format(name, bad))
    raise ValueError("'DEM elevation units' is 'Detect from the raster', but {}: {}. {}".format(
        name, how, DEM_Z_HELP))


def z_how_parts(how):
    """(detected, warning) of resolve_dem_z's how: "detected: ..." -> (how, ""); "chosen" ->
    ("", ""); "chosen: <warning>" -> ("", warning) - the run report's units warning."""
    how = str(how or "")
    if how.startswith("chosen: "):
        return "", how[len("chosen: "):]
    if how == "chosen":
        return "", ""
    return how, ""


def detected_z_note(choice, how):
    """The 'DEM elevations in ...' note (run log, report.json dem_elevation_units, the docx) of a
    run whose unit was detected: "metres (detected: raster unit 'm')". 3.10.89: it said
    "metres (chosen)" although the user had picked 'Detect from the raster'."""
    return "{} ({})".format(ZU.NAMES.get(choice, choice), how)


ND_GROUND_M = (200.0, 1100.0)      # North Dakota: 229 m (Pembina) to 1069 m (White Butte)


def _nd_ground(z_m):
    return ND_GROUND_M[0] <= z_m <= ND_GROUND_M[1]


def median_unit_problem(med, zc, how=""):
    """None when a DEM median `med` read in the unit `zc` is plausible North Dakota ground; else
    the message, naming the unit that does fit (3.10.89). Stops when the metres value is outside
    100-1200 m (as before), and also when the chosen reading is outside North Dakota's ground
    (200-1100 m) while the other unit's fits: a 3DEP metre DEM run as Feet (about 99-200 m),
    which passed before, and (3.10.89) a feet DEM read as metres at 1100-1200 m. `how` (the
    detection, "detected: raster unit 'm'") is named when the unit was not chosen."""
    zc = base_choice(zc)
    f = _TO_M.get(zc, 1.0)
    med_m = med * f
    alt = Z_METERS if f != 1.0 else Z_FEET
    alt_m = med * _TO_M[alt]
    bad = med_m < 100 or med_m > 1200 or (not _nd_ground(med_m) and _nd_ground(alt_m))
    if not bad:
        return None
    text = ("DEM median {:.1f} with units '{}'{} is {:.0f} m, outside the North Dakota ground "
            "range (about 230-1070 m). ").format(
                med, Z_LABELS.get(zc, zc), " ({})".format(how) if how else "", med_m)
    if _nd_ground(alt_m):
        text += ("Read as {} it is {:.0f} m, which fits: set 'DEM elevation units' to {}{}.").format(
            "metres" if alt == Z_METERS else "feet", alt_m, Z_LABELS[alt],
            " (USGS 3DEP downloads are metres)" if alt == Z_METERS else "")
        if how:
            # review round 3: that choice runs (resolve_dem_z: the values fit only it), with a
            # warning in the run report; correcting the record lets Detect read it
            text += (" The unit was read from the raster, so its record is wrong: the run goes "
                     "on as {} with a warning in its report, or correct the record "
                     "(gdal_edit -units {}) and keep 'Detect from the raster'.").format(
                         Z_LABELS[alt], _GDAL_EDIT_UNIT[alt])
    else:
        text += ("Neither feet nor metres fits: check the DEM (a nodata value that is not set, "
                 "or not this dam's DEM).")
    return text


def sample_median(ds, grid=7, win=24, size=256):
    """(median, cells) of the DEM's valid cells (finite, not its nodata value - as stored: an
    unset nodata of -3.4e38 makes the median fail the ground check, which says so) in a
    grid x grid pattern of win x win full-resolution windows spread over the whole raster -
    corners, edges and centre - else in a decimated read of the whole raster (size x size);
    (None, 0) when neither finds a valid cell. 3.10.89: the run's ND ground check read only the
    top-left 200 x 200 cells and was skipped without a word when they were nodata - a 1 m DEM
    clipped to a dam's box or warped to EPSG:5070 - so Feet ran a metre DEM x 0.3048."""
    def valid(a, nd):
        a = np.asarray(a, dtype=np.float64).ravel()
        ok = np.isfinite(a)
        if nd is not None:
            ok &= a != nd
        return a[ok]
    try:
        b = ds.GetRasterBand(1)
        nx, ny = ds.RasterXSize, ds.RasterYSize
        nd = b.GetNoDataValue()
        w, h = min(win, nx), min(win, ny)
        xs = sorted({int(round((nx - w) * i / float(grid - 1))) for i in range(grid)})
        ys = sorted({int(round((ny - h) * j / float(grid - 1))) for j in range(grid)})
        parts = [valid(b.ReadAsArray(x, y, w, h), nd) for y in ys for x in xs]
        flat = np.concatenate(parts) if parts else np.empty(0)
        if flat.size <= 10:
            flat = valid(b.ReadAsArray(0, 0, nx, ny, buf_xsize=min(nx, size),
                                       buf_ysize=min(ny, size)), nd)
    except Exception:                                    # noqa - unreadable: no sample
        flat = np.empty(0)
    if flat.size > 10:
        return float(np.median(flat)), int(flat.size)
    return None, 0


NO_SAMPLE = ("the North Dakota ground check of the elevation unit found no valid elevation in a "
             "sample of the whole DEM (check its nodata value); the unit is not checked")


def utm_epsg(lon, lat):
    """NAD83 / UTM zone N for a longitude (North Dakota: zones 13 and 14)."""
    zone = int((lon + 180.0) // 6.0) + 1
    if lat < 0 or not (1 <= zone <= 23):
        return 32600 + zone if lat >= 0 else 32700 + zone
    return 26900 + zone


# DEM resampling onto the model grid. Precedence: an explicit value (the DEM_RESAMPLE tool
# parameter / the CLI dem "resample" key) > the NDBREACH_DEM_RESAMPLE environment variable >
# DEFAULT_RESAMPLE. "average" area-averages fine terrain (e.g. 5 m to a 40 m grid), which is
# what the regression baselines use; "bilinear" samples it, which can miss channel bottoms and
# berms at coarse cells (flooded areas came out 3-40% high on the baselines).
ENV_RESAMPLE = "NDBREACH_DEM_RESAMPLE"
DEFAULT_RESAMPLE = "average"
RESAMPLE_METHODS = ("bilinear", "average", "near", "cubic", "cubicspline", "lanczos",
                    "mode", "min", "max", "med", "q1", "q3")


def check_resample(method, what="DEM resample method"):
    """`method` lower-cased if it is a GDAL warp resampling name, else ValueError."""
    m = str(method).strip().lower()
    if m not in RESAMPLE_METHODS:
        raise ValueError("{} {!r} is not one of: {}".format(what, method, ", ".join(RESAMPLE_METHODS)))
    return m


def resolve_resample(explicit=None, environ=None):
    """(method, source) for the DEM warp: source is "parameter", "environment" or "default"."""
    if explicit is not None and str(explicit).strip():
        return check_resample(explicit), "parameter"
    env = (os.environ if environ is None else environ).get(ENV_RESAMPLE, "")
    if str(env).strip():
        return check_resample(env, "{} value".format(ENV_RESAMPLE)), "environment"
    return DEFAULT_RESAMPLE, "default"


class GdalTerrain:
    """terrain_src for workflow.run_dam."""

    def __init__(self, dem_path, work_srs, z_choice=Z_AUTO, manning_path=None,
                 default_n=0.06, resample=None, manning_lut=None):
        gdal, _ = _gdal()
        self.dem_path = _file(dem_path)   # 3.10.89: a QGIS 'x.tif|option:...' source
        self.work_srs = work_srs          # "EPSG:26914" or WKT
        self.z_to_m, self.z_note = z_to_m_factor(dem_path, z_choice)
        self.z_check = ""            # 3.10.89: why the unit needs checking (a run report warning)
        self.manning_path = manning_path
        self.manning_lut = manning_lut       # set: the raster holds NLCD class codes
        self.default_n = default_n
        self.resample, self.resample_source = resolve_resample(resample)
        ds = GS.open_raster(self.dem_path)
        self.src_nodata = ds.GetRasterBand(1).GetNoDataValue()

    def _warp(self, path, bounds, cell, alg, src_nodata=None):
        gdal, _ = _gdal()
        xmin, ymin, xmax, ymax = bounds
        nx = int(round((xmax - xmin) / cell))
        ny = int(round((ymax - ymin) / cell))
        xmax, ymin = xmin + nx * cell, ymax - ny * cell
        # 3.10.43: from the full-resolution data, never the source's overviews (core/warp_opts).
        # 3.10.89: the values as stored (no vertical shift): read() applies z_to_m once - GDAL's
        # default shift turned a feet compound-CRS DEM into metres when the work CRS is 2D, and
        # z_to_m then converted it again (1300 ftUS read as 120.8 m, not 396.2 m)
        opts = gdal.WarpOptions(**no_vshift(gdal, no_overviews(gdal, dict(
            format="MEM", dstSRS=self.work_srs, outputBounds=(xmin, ymin, xmax, ymax),
            width=nx, height=ny, resampleAlg=alg, dstNodata=NODATA,
            outputType=gdal.GDT_Float32, srcNodata=src_nodata, multithread=True))))
        out = GS.checked(gdal.Warp("", path, options=opts), "warping {}".format(path))
        a = out.GetRasterBand(1).ReadAsArray().astype(np.float64)
        a[a == NODATA] = np.nan
        return Grid(a, xmin, ymax, cell, crs_wkt=out.GetProjection())

    def native_cell_m(self):
        """The DEM's own cell size in metres (3.10.31: ANUGA's bed is read at it). A
        geographic raster's degrees are converted at its centre latitude."""
        gdal, osr = _gdal()
        ds = GS.open_raster(self.dem_path)
        gt = ds.GetGeoTransform()
        px = min(abs(gt[1]), abs(gt[5]))
        wkt = ds.GetProjection()
        if not wkt:
            return float(px)
        srs = osr.SpatialReference()
        srs.ImportFromWkt(wkt)
        if srs.IsGeographic():
            lat = gt[3] + gt[5] * ds.RasterYSize / 2.0
            return float(px * 111320.0 * max(math.cos(math.radians(lat)), 0.1))
        return float(px * (srs.GetLinearUnits() or 1.0))

    def read(self, bounds, cell):
        g = self._warp(self.dem_path, bounds, cell, self.resample, self.src_nodata)
        g.data = g.data * self.z_to_m
        return g

    def manning(self, bounds, cell):
        if not self.manning_path:
            return None
        g = self._warp(self.manning_path, bounds, cell, "near")
        if self.manning_lut:
            from . import nd_data
            lut = dict(self.manning_lut)
            lut["default"] = self.default_n
            g.data = nd_data.nlcd_to_manning(g.data, lut)
            return g
        bad = ~np.isfinite(g.data) | (g.data <= 0) | (g.data > 1.0)
        g.data[bad] = self.default_n
        return g


def horizontal_wkt(text):
    """The WKT of a CRS (any SetFromUserInput text) without its vertical part, or `text` when it
    has none or cannot be read: 'NAD83 / North Dakota North (ft) + NAVD88 height (ftUS)' ->
    'NAD83 / North Dakota North (ft)'."""
    _, osr = _gdal()
    s = osr.SpatialReference()
    try:
        if s.SetFromUserInput(str(text)) != 0 or not s.IsCompound():
            return text
        if hasattr(s, "StripVertical"):                  # GDAL 3.6+
            s.StripVertical()
            return s.ExportToWkt()
        h = osr.SpatialReference()                       # older GDAL: the PROJCS / GEOGCS node
        for node in ("PROJCS", "GEOGCS"):
            wkt = s.ExportToWkt()
            i = wkt.find(node + "[")
            if i < 0:
                continue
            depth, j = 0, i
            for j in range(i, len(wkt)):
                depth += {"[": 1, "]": -1}.get(wkt[j], 0)
                if depth == 0 and wkt[j] == "]":
                    break
            if h.ImportFromWkt(wkt[i:j + 1]) == 0:
                return h.ExportToWkt()
    except Exception:                                    # noqa - not a CRS: as given
        return text
    return text


class GeoTiffWriter:
    """writer for workflow.run_dam: writes the grid in the model CRS, then
    (optionally) re-projects it to the DEM's CRS with nearest-neighbour.

    3.10.89 review round 3: in the horizontal part of those CRSs only. Depth, velocity, WSE and
    arrival are written as computed; a compound target CRS (the DEM's 'ND North (ft) + NAVD88
    height (ftUS)') labelled them with a vertical unit they are not in, and a later warp of the
    file without -novshift (DamSafety Hazard's reproject_raster, QGIS's Warp) rescaled them by
    that unit - depths x 0.3048. Main's writer had scaled them x 3.28 on the way in instead."""

    def __init__(self, work_srs, target_srs=None, target_res=None):
        self.work_srs = work_srs
        self.target_srs = target_srs
        self.target_res = target_res

    def __call__(self, grid, path):
        gdal, osr = _gdal()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = path + ".work.tif" if self.target_srs else path
        drv = gdal.GetDriverByName("GTiff")
        if os.path.exists(tmp):
            drv.Delete(tmp)
        ds = GS.checked(drv.Create(tmp, grid.nx, grid.ny, 1, gdal.GDT_Float32,
                                   options=["COMPRESS=DEFLATE", "TILED=YES", "PREDICTOR=3"]),
                        "creating {}".format(tmp))
        ds.SetGeoTransform((grid.x0, grid.dx, 0.0, grid.y0, 0.0, -grid.dx))
        srs = osr.SpatialReference()
        srs.SetFromUserInput(horizontal_wkt(self.work_srs))
        ds.SetProjection(srs.ExportToWkt())
        b = ds.GetRasterBand(1)
        b.SetNoDataValue(NODATA)
        a = np.where(np.isfinite(grid.data), grid.data, NODATA).astype(np.float32)
        b.WriteArray(a)
        b.FlushCache()
        ds = None
        if self.target_srs:
            if os.path.exists(path):
                drv.Delete(path)
            kw = dict(dstSRS=horizontal_wkt(self.target_srs), resampleAlg="near", srcNodata=NODATA,
                      dstNodata=NODATA, creationOptions=["COMPRESS=DEFLATE", "TILED=YES"])
            if self.target_res:
                kw.update(xRes=self.target_res, yRes=self.target_res)
            # 3.10.89: metres stay metres in a compound target CRS (no vertical shift)
            GS.checked(gdal.Warp(path, tmp, options=gdal.WarpOptions(**no_vshift(gdal, kw))),
                       "reprojecting {}".format(path))
            drv.Delete(tmp)
        return path


def same_crs(wkt_a, wkt_b):
    _, osr = _gdal()
    a, b = osr.SpatialReference(), osr.SpatialReference()
    try:
        a.SetFromUserInput(wkt_a)
        b.SetFromUserInput(wkt_b)
        return bool(a.IsSame(b))
    except Exception:
        return False


def linear_unit_m(wkt):
    """Metres per CRS unit, or None for geographic CRSs."""
    if not wkt:
        return None
    _, osr = _gdal()
    s = osr.SpatialReference()
    try:
        s.SetFromUserInput(wkt)
    except Exception:
        return None
    return s.GetLinearUnits() if s.IsProjected() else None


# --------------------------------------------------------------------------------------
# statewide LiDAR: tiles -> one VRT, gaps -> filled
# --------------------------------------------------------------------------------------
RASTER_EXT = (".tif", ".tiff", ".img", ".asc", ".vrt", ".dt2", ".hgt", ".bil")


def find_rasters(folder, recursive=True, exts=RASTER_EXT):
    out = []
    if recursive:
        for root, _d, files in os.walk(folder):
            out += [os.path.join(root, f) for f in files if f.lower().endswith(exts)]
    else:
        out = [os.path.join(folder, f) for f in os.listdir(folder) if f.lower().endswith(exts)]
    return sorted(out)


def build_vrt(sources, out_path, nodata=None, resolution="highest"):
    """Mosaic tiles (a folder or a list of files) into a VRT without copying
    any data. Returns a dict describing what went in and any problems: tiles
    in different CRSs cannot be mosaicked and are left out and listed."""
    gdal, osr = _gdal()
    if isinstance(sources, str):
        files = find_rasters(sources) if os.path.isdir(sources) else [sources]
        if os.path.isdir(sources):      # a mosaic or filled copy in the tile folder is no tile
            files = [f for f in files if not f.lower().endswith(".vrt")
                     and "_filled" not in os.path.basename(f).lower()]
    else:
        files = list(sources)
    files = [f for f in files if os.path.abspath(f) != os.path.abspath(out_path)]
    if not files:
        raise ValueError("no raster tiles found")
    info, crs_groups, known = [], {}, []
    for f in files:
        try:
            ds = GS.open_raster(f)
        except RuntimeError as e:
            info.append({"file": f, "problem": str(e)[:100]})
            continue
        gt = ds.GetGeoTransform()
        wkt = ds.GetProjection()
        srs = osr.SpatialReference()
        key = "none"
        if wkt:
            srs.SetFromUserInput(wkt)
            for k, other in known:      # the same CRS however it is labelled
                if srs.IsSame(other):
                    key = k
                    break
            else:
                key = srs.GetAuthorityCode(None) or srs.GetName() or "crs{}".format(len(known))
                if any(k == key for k, _o in known):
                    key = "{} ({})".format(key, len(known))
                known.append((key, srs))
        b = ds.GetRasterBand(1)
        rec = {"file": f, "res": abs(gt[1]), "crs": key, "nodata": b.GetNoDataValue(),
               "unit": (b.GetUnitType() or ""), "size": (ds.RasterXSize, ds.RasterYSize),
               "bounds": (gt[0], gt[3] + gt[5] * ds.RasterYSize, gt[0] + gt[1] * ds.RasterXSize,
                          gt[3])}
        info.append(rec)
        crs_groups.setdefault(key, []).append(rec)
    good = [r for r in info if "crs" in r]
    if not good:
        raise ValueError("none of the tiles could be opened")
    main_key = max(crs_groups, key=lambda k: len(crs_groups[k]))
    use = crs_groups[main_key]
    left_out = [r["file"] for k, v in crs_groups.items() if k != main_key for r in v]
    kw = {"resolution": resolution}
    nd = nodata if nodata is not None else use[0]["nodata"]
    if nd is not None:
        kw["srcNodata"] = nd
        kw["VRTNodata"] = nd
    if os.path.exists(out_path):
        os.remove(out_path)
    vrt = GS.checked(gdal.BuildVRT(out_path, [r["file"] for r in use],
                                   options=gdal.BuildVRTOptions(**kw)), "building {}".format(out_path))
    vrt = None
    res = sorted({round(r["res"], 4) for r in use})
    units = sorted({r["unit"] for r in use})
    if len(units) == 1 and units[0]:        # keep the elevation unit on the mosaic
        try:
            v = GS.open_raster(out_path, update=True)
            v.GetRasterBand(1).SetUnitType(units[0])
            v = None
        except Exception:
            pass
    b = [r["bounds"] for r in use]
    notes = []
    if left_out:
        notes.append("{} tile(s) in a different CRS than the majority ({}) were left out; "
                     "reproject them first: {}".format(len(left_out), main_key,
                                                       ", ".join(os.path.basename(x)
                                                                 for x in left_out[:5])))
    if len(res) > 1:
        notes.append("the tiles have different cell sizes {}; the VRT uses the {}".format(
            res, resolution))
    if len(units) > 1:
        notes.append("the tiles record different elevation units {}".format(units))
    nds = sorted({str(r["nodata"]) for r in use})
    if len(nds) > 1:
        notes.append("the tiles have different no-data values {}; only {} is applied, so "
                     "voids in the others read as terrain. Give the no-data value "
                     "explicitly or fix the tiles.".format(nds, nd))
    return {"vrt": out_path, "tiles": len(use), "crs": main_key, "cell_sizes": res,
            "elevation_units": units, "bounds": (min(x[0] for x in b), min(x[1] for x in b),
                                                 max(x[2] for x in b), max(x[3] for x in b)),
            "left_out": left_out, "problems": [r for r in info if "problem" in r],
            "notes": notes}


def fill_gaps(src_path, out_path, extent=None, max_distance_px=100, smoothing=0):
    """Fill no-data holes (water bodies, missing returns) by interpolation
    from the edges, on the whole raster or only `extent` (xmin, ymin, xmax,
    ymax in the raster's CRS). The whole file is read into memory, so clip to
    the area of interest for a statewide mosaic. Returns
    {"out", "cells_filled", "cells_left"}."""
    gdal, _ = _gdal()
    ds = GS.open_raster(src_path)
    nodata = ds.GetRasterBand(1).GetNoDataValue()
    if nodata is None:
        raise ValueError("the raster has no no-data value, so there are no gaps to fill")
    kw = {"format": "GTiff", "creationOptions": ["COMPRESS=DEFLATE", "TILED=YES",
                                                 "BIGTIFF=IF_SAFER"]}
    if extent:
        kw["projWin"] = (extent[0], extent[3], extent[2], extent[1])
    tmp = out_path + ".tmp.tif"
    GS.checked(gdal.Translate(tmp, ds, options=gdal.TranslateOptions(**kw)), "copying {}".format(src_path))
    o = GS.open_raster(tmp, update=True)
    band = o.GetRasterBand(1)
    a0 = band.ReadAsArray()
    holes0 = int((a0 == nodata).sum() if not np.isnan(nodata) else np.isnan(a0).sum())
    mask = o.GetRasterBand(1).GetMaskBand()
    gdal.FillNodata(band, mask, max_distance_px, smoothing)
    band.FlushCache()
    a1 = band.ReadAsArray()
    left = int((a1 == nodata).sum() if not np.isnan(nodata) else np.isnan(a1).sum())
    o = None
    ds = None
    if os.path.exists(out_path):
        os.remove(out_path)
    os.replace(tmp, out_path)
    return {"out": out_path, "cells_filled": holes0 - left, "cells_left": left}


def nlcd_to_manning_raster(nlcd_path, out_path, lut=None, like_path=None):
    """Write a Manning's n raster from an NLCD class raster (offline). The
    output is on the NLCD grid unless `like_path` is given (a DEM, say), in
    which case it is resampled (nearest) onto that grid first."""
    from . import nd_data
    gdal, _ = _gdal()
    src = GS.open_raster(nlcd_path)
    if like_path:
        like = GS.open_raster(like_path)
        gt = like.GetGeoTransform()
        opts = gdal.WarpOptions(**no_vshift(gdal, dict(   # 3.10.89: class codes as stored
            format="MEM", dstSRS=like.GetProjection(), xRes=abs(gt[1]), yRes=abs(gt[5]),
            outputBounds=(gt[0], gt[3] + gt[5] * like.RasterYSize,
                          gt[0] + gt[1] * like.RasterXSize, gt[3]),
            resampleAlg="near", outputType=gdal.GDT_Float32)))
        src = GS.checked(gdal.Warp("", src, options=opts), "warping {}".format(nlcd_path))
    band = src.GetRasterBand(1)
    codes = band.ReadAsArray().astype(np.float64)
    nd = band.GetNoDataValue()
    if nd is not None:
        codes[codes == nd] = np.nan
    n = nd_data.nlcd_to_manning(codes, lut or nd_data.manning_lut()).astype(np.float32)
    drv = gdal.GetDriverByName("GTiff")
    if os.path.exists(out_path):
        drv.Delete(out_path)
    out = GS.checked(drv.Create(out_path, src.RasterXSize, src.RasterYSize, 1, gdal.GDT_Float32,
                                options=["COMPRESS=DEFLATE", "TILED=YES"]), "creating {}".format(out_path))
    out.SetGeoTransform(src.GetGeoTransform())
    out.SetProjection(src.GetProjection())
    out.GetRasterBand(1).WriteArray(n)
    out.GetRasterBand(1).SetNoDataValue(-9999)
    out = None
    seen = sorted({int(c) for c in np.unique(codes[np.isfinite(codes)])})
    unknown = [c for c in seen if c not in (lut or nd_data.NLCD_N)]
    return {"out": out_path, "classes": seen, "unknown_classes": unknown}


def read_like(path, grid, alg="bilinear"):
    """Read any raster onto the grid of `grid` (a core.Grid, model CRS) as an
    array with NaN for no data. Used to compare with another model's rasters."""
    gdal, _ = _gdal()
    xmin, ymin, xmax, ymax = grid.bounds
    opts = gdal.WarpOptions(**no_vshift(gdal, no_overviews(gdal, dict(   # 3.10.43 / 3.10.89
        format="MEM", dstSRS=grid.crs_wkt or None, outputBounds=(xmin, ymin, xmax, ymax),
        width=grid.nx, height=grid.ny, resampleAlg=alg, dstNodata=NODATA,
        outputType=gdal.GDT_Float32))))
    out = GS.checked(gdal.Warp("", _file(path), options=opts), "warping {}".format(path))
    a = out.GetRasterBand(1).ReadAsArray().astype(np.float64)
    a[a == NODATA] = np.nan
    return a


def read_grid(path):
    """A raster file as a core Grid (north-up, square cells assumed), NaN for
    no data, with its CRS."""
    gdal, _ = _gdal()
    ds = GS.open_raster(path)
    gt = ds.GetGeoTransform()
    b = ds.GetRasterBand(1)
    raw = b.ReadAsArray()
    a = raw.astype(np.float64)
    nd = b.GetNoDataValue()
    if nd is not None:
        if np.isnan(nd):
            pass
        elif raw.dtype.kind == "f":      # the file's own precision: -3.4e38 is not exact in f32
            a[raw == raw.dtype.type(nd)] = np.nan
        else:
            a[a == nd] = np.nan
    a[~np.isfinite(a)] = np.nan
    if abs(abs(gt[1]) - abs(gt[5])) > 1e-6 * abs(gt[1]) or gt[2] or gt[4] or gt[5] > 0:
        raise ValueError("{}: the cells are not square and north-up".format(path))
    return Grid(a, gt[0], gt[3], abs(gt[1]), crs_wkt=ds.GetProjection())


def epsg_of(wkt):
    """'EPSG:26914' for a CRS when it has a code, else None."""
    _, osr = _gdal()
    s = osr.SpatialReference()
    try:
        s.SetFromUserInput(wkt)
        code = s.GetAuthorityCode(None)
        if not code:
            try:
                s.AutoIdentifyEPSG()
            except Exception:
                pass
            code = s.GetAuthorityCode(None)
        return "EPSG:{}".format(code) if code else None
    except Exception:
        return None
