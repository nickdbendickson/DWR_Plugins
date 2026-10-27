"""The elevation unit a DEM records, read one way by the Runner and Screening (Runner 3.10.89,
Screening 0.17.50).

    recorded(ds, path)   -> (choice, how): FEET / US_FEET / METERS, or ("", why) when the DEM
                            records no unit that can be trusted
    band_tag(choice)     -> the band unit type to write for a choice ("m", "ft", ...)

What counts as recorded, in this order:
  1. band 1's unit type (m / metre / ft / US survey foot ...), unless band 1 has a scale or an
     offset: GDAL applies the unit to the scaled values, the model reads the raw ones;
  2. a compound CRS's vertical unit;
  3. the Screening 'Download DEM' tool's own record of a file it wrote: the dataset metadata
     SOURCE / CRS / UNITS / FILE_TYPE / CELL_M of write_local_dem (a warped copy keeps them), else
     the <stem>_dem.txt facts file beside it when its file type, CRS and cell size are this
     raster's. Only a USGS 3DEP record (ImageServer or the 1 m lidar tiles, metres in every
     version) is trusted; an ND DWR LiDAR record is not, because Download DEM before Screening
     0.17.50 could write ND DWR values that are not metres under 'units: metres' (older builds
     mosaicked the ASCII_Grids_ft feet grids unconverted; a DEM_Imagine metre tile in a folder
     named gft* was scaled x 0.3048).
A 1 m lidar VRT that Screening built before 0.17.50 over a fallback DEM (its <stem>.json has no
'fallback_units'), read as metres by any of these, is trusted only when that fallback records
metres: before, a feet fallback showed through the VRT as metres.
Never a guess from the horizontal CRS or the values.

Same file in DamSafety_Runner/core and DamSafety_Screening/core (tools/ci_checks.py keeps them
byte-identical). GDAL only; no QGIS imports."""
import json
import os

FEET, US_FEET, METERS = "feet", "us_feet", "meters"
FT = 0.3048                         # m per international foot
US_FT = 1200.0 / 3937.0             # m per US survey foot
TO_M = {FEET: FT, US_FEET: US_FT, METERS: 1.0}
NAMES = {FEET: "feet", US_FEET: "US survey feet", METERS: "metres"}

FEET_NAMES = ("ft", "foot", "feet", "international foot", "intl foot")
US_FEET_NAMES = ("us survey foot", "us-ft", "ftus", "us_ft", "foot_us", "us foot",
                 "us survey feet", "ft (us)", "survey foot")
METER_NAMES = ("m", "metre", "meter", "metres", "meters")

DOWNLOAD_KEYS = ("SOURCE", "CRS", "UNITS", "FILE_TYPE", "CELL_M")    # write_local_dem
FACTS_KEYS = ("source", "crs", "units", "file_type", "cell_m")         # write_sidecar
LIDAR_VRT_FORMAT = "damsuite/lidar_1m_vrt"                              # lidar_tnm.build_vrt
NONE_RECORDED = ("no elevation unit is recorded in the raster (no band unit, no vertical CRS, "
                 "no Download DEM record)")


def unit_choice(text):
    """FEET / US_FEET / METERS for a unit name, else ""."""
    unit = str(text or "").strip().lower()
    if unit in US_FEET_NAMES:
        return US_FEET
    if unit in FEET_NAMES:
        return FEET
    if unit in METER_NAMES:
        return METERS
    return ""


def band_tag(choice):
    """The band unit type written for a choice ("" for none)."""
    return {FEET: "ft", US_FEET: "US survey foot", METERS: "m"}.get(choice, "")


def _file(path):
    return str(path or "").split("|", 1)[0]


def _open(path):
    try:
        from osgeo import gdal
        return gdal.Open(_file(path))
    except Exception:                                    # noqa - unreadable: treated as absent
        return None


def _srs(text):
    from osgeo import osr
    srs = osr.SpatialReference()
    try:
        if srs.SetFromUserInput(str(text)) != 0:
            return None
    except Exception:                                    # noqa - not a CRS
        return None
    return srs


def scale_offset_problem(band):
    """Why band 1's scale / offset stops a detection, or ""."""
    try:
        scale, offset = band.GetScale(), band.GetOffset()
    except Exception:                                    # noqa
        return ""
    if (scale is not None and abs(scale - 1.0) > 1e-12) or \
            (offset is not None and abs(offset) > 1e-12):
        return ("band 1 has a scale {} / offset {}: its unit applies to the scaled values, but a "
                "run reads the raw ones. Write an unscaled copy (gdal_translate -unscale) and use "
                "that").format(scale, offset)
    return ""


def vertical_unit(ds):
    """FEET / US_FEET / METERS from a compound CRS's vertical unit, else ""."""
    wkt = ds.GetProjection()
    if not wkt:
        return ""
    srs = _srs(wkt)
    if srs is None:
        return ""
    try:
        vert = srs.GetTargetLinearUnits("VERT_CS") if srs.IsCompound() else None
    except Exception:                                    # noqa
        vert = None
    if vert and abs(vert - US_FT) < 1e-7:
        return US_FEET
    if vert and abs(vert - FT) < 1e-7:
        return FEET
    if vert and abs(vert - 1.0) < 1e-9:
        return METERS
    return ""


def _from_record(source, units, where):
    source = str(source or "").strip()
    if source.startswith("USGS 3DEP") and unit_choice(units) == METERS:
        return METERS, "{}: {}, units {}".format(where, source, str(units).strip())
    if source.startswith("ND DWR"):
        return "", ("{} says {}, units {}; not trusted - Download DEM before Screening 0.17.50 "
                    "could write ND DWR ASCII_Grids_ft feet values under that label. Choose "
                    "Meters only for a DEM_Imagine download, or delete the file and download "
                    "it again").format(where, source, str(units).strip())
    return "", ""


def read_facts(path):
    """{key: value} of a <stem>_dem.txt facts file, or None."""
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except (OSError, UnicodeDecodeError):
        return None
    out = {}
    for line in lines:
        key, sep, val = line.partition(":")
        if sep:
            out[key.strip().lower()] = val.strip()
    return out


def _facts_mismatch(ds, facts):
    """Why a facts file does not describe the raster `ds`, or "" when it does: file type, CRS and
    cell size must all be the raster's."""
    ft = facts["file_type"].lower()
    drv = ds.GetDriver().ShortName if ds.GetDriver() is not None else ""
    want = "GTiff" if ft.startswith("geotiff") else ("VRT" if ft.startswith("vrt") else "")
    if not want or drv != want:
        return "file type {!r}, the raster is {}".format(facts["file_type"], drv or "unknown")
    side, mine = _srs(facts["crs"]), _srs(ds.GetProjection()) if ds.GetProjection() else None
    if side is None or mine is None:
        return "CRS {!r}, the raster's cannot be compared".format(facts["crs"])
    try:
        same = mine.IsSame(side, ["IGNORE_DATA_AXIS_TO_SRS_AXIS_MAPPING=YES"])
    except Exception:                                    # noqa - older osr: no options
        same = mine.IsSame(side)
    if not same:
        return "CRS {}, not the raster's".format(facts["crs"])
    try:
        cell = float(facts["cell_m"])
        px = abs(float(ds.GetGeoTransform()[1]))
    except (TypeError, ValueError, IndexError):
        return "cell size {!r} unreadable".format(facts["cell_m"])
    if cell <= 0 or abs(px - cell) > 0.01 * cell:
        return "cell {} m, the raster's is {:g}".format(facts["cell_m"], px)
    return ""


def download_record(ds, path=""):
    """(METERS, how) from the Download DEM tool's own record of the raster, ("", why) for a record
    that is not trusted or does not describe it, ("", "") when there is none."""
    try:
        md = ds.GetMetadata() or {}
    except Exception:                                    # noqa
        md = {}
    if all(str(md.get(k) or "").strip() for k in DOWNLOAD_KEYS):
        got = _from_record(md["SOURCE"], md["UNITS"], "Download DEM record in the file")
        if got[0] or got[1]:
            return got
    if not path:
        return "", ""
    side = os.path.splitext(_file(path))[0] + "_dem.txt"
    facts = read_facts(side)
    if not facts or not all(facts.get(k) for k in FACTS_KEYS):
        return "", ""
    name = os.path.basename(side)
    bad = _facts_mismatch(ds, facts)
    if bad:
        return "", "the facts file {} does not describe this raster ({})".format(name, bad)
    return _from_record(facts["source"], facts["units"], "Download DEM facts file " + name)


def _old_lidar_vrt_problem(ds, path, depth):
    """Why a 1 m lidar VRT tagged metres from Screening before 0.17.50 cannot be trusted, or ""."""
    try:
        if ds.GetDriver().ShortName != "VRT" or not path:
            return ""
    except Exception:                                    # noqa
        return ""
    js = os.path.splitext(_file(path))[0] + ".json"
    try:
        with open(js, encoding="utf-8") as fh:
            info = json.load(fh)
    except (OSError, ValueError):
        return ""
    if not isinstance(info, dict) or info.get("format") != LIDAR_VRT_FORMAT:
        return ""
    fb = str(info.get("fallback") or "")
    if not fb or "fallback_units" in info:
        return ""
    if not os.path.isabs(fb):
        fb = os.path.join(os.path.dirname(os.path.abspath(js)), fb)
    fds = _open(fb) if depth < 4 else None
    if fds is None:
        return ""                                        # gone: the VRT cannot be read there
    got, how = recorded(fds, fb, depth + 1)
    fds = None
    if got == METERS:
        return ""
    what = "in {} ({})".format(NAMES[got], how) if got else "that records no unit ({})".format(how)
    return ("it is a 1 m lidar VRT tagged metres, built by Screening before 0.17.50 over the "
            "fallback DEM {} {}, which shows through as metres: run Download DEM again "
            "(Screening 0.17.50 converts the fallback to metres or leaves it out) rather than "
            "choosing a unit for this VRT").format(os.path.basename(fb), what)


def recorded(ds, path="", _depth=0):
    """(choice, how) from the elevation unit the open raster `ds` (file `path`) records, or
    ("", why). See the module docstring for the order."""
    band = ds.GetRasterBand(1)
    bad = scale_offset_problem(band)
    if bad:
        return "", bad
    raw = (band.GetUnitType() or "").strip()
    if raw:
        got = unit_choice(raw)
        if not got:
            return "", "the raster's unit '{}' is not feet or metres".format(raw.lower())
        how = "raster unit '{}'".format(raw.lower())
    else:
        got = vertical_unit(ds)
        how = "vertical CRS in {}".format(NAMES[got]) if got else ""
    if not got:
        got, how = download_record(ds, path)
        if not got:
            return "", (how or NONE_RECORDED)
    if got == METERS:
        bad = _old_lidar_vrt_problem(ds, path, _depth)
        if bad:
            return "", bad
    return got, how
