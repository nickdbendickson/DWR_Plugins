# -*- coding: utf-8 -*-
"""2.21.0: add one model's results to a dam's folder in the suite's Dams folder (no Qt).

The user names the dam (its ID such as DWR-2801 / ND00199, or its name) and points at the
model results:

- DSS-WISE Lite: the package zip or its folder - converted to feet / ft/s / hours by the
  DSS-WISE import (dsswise.convert);
- HEC-RAS: rasters exported by RAS Mapper (a .vrt with its tiles, or .tif) - copied; when
  the raster or its HEC-RAS project says SI units, depth / velocity / D x V are converted
  to feet instead (a copy would lose the project that says so); or a plan's results file
  (.p##.hdf) - converted by the HEC-RAS plan import (rasimport.import_plan);
- LISFLOOD-FP: the native ESRI-ASCII outputs <run>.max (depth, m), .maxVc / .Vmax
  (velocity, m/s), .inittm (arrival, h) and .mxe (water surface, m) - converted to
  GeoTIFFs in feet; GeoTIFFs already exported are copied as for HEC-RAS.

The dam's folder is found by its folder name or the ID / name / NID / DWR ID in its
dam.json (case, spaces, '-' and '_' ignored; 'DWR-0123' = '123'). When there is none a
folder is made, with a dam.json (damsuite.dam_record). The results go to
<dam>/Hazard_inputs/<Breach|No_Breach>_<MODEL>/, where the batch classification
(autodetect.find_rasters) finds them; a no-breach run goes to a folder whose name starts
with 'No_Breach', so it is taken as the baseline. A folder that already holds results is
never written into: the next free number is used (Breach_LISFLOOD_2) and said so. The
same source added again is not copied twice. The source is only read. Each folder gets
results_provenance.json (source, sha256, model, time, plugin version, outputs) and
dam.json gets the status 'modelled'.
"""

import csv
import datetime
import hashlib
import json
import os
import re
import shutil
import tempfile
import uuid
import xml.etree.ElementTree as ET

from . import damfolder as DF

MODEL_AUTO, MODEL_DSSWISE, MODEL_HECRAS, MODEL_LISFLOOD = "auto", "dsswise", "hecras", "lisflood"
MODELS = [(MODEL_AUTO, "Auto-detect"), (MODEL_DSSWISE, "DSS-WISE Lite"),
          (MODEL_HECRAS, "HEC-RAS"), (MODEL_LISFLOOD, "LISFLOOD-FP")]
MODEL_DIR = {MODEL_DSSWISE: "DSSWISE", MODEL_HECRAS: "HECRAS", MODEL_LISFLOOD: "LISFLOOD"}
MODEL_NAME = dict(MODELS)
SCEN_BREACH, SCEN_BASELINE = "breach", "no_breach"
SCENARIOS = [(SCEN_BREACH, "Breach"), (SCEN_BASELINE, "No breach (baseline)")]
SCEN_DIR = {SCEN_BREACH: "Breach", SCEN_BASELINE: "No_Breach"}
INPUTS_DIR = "Hazard_inputs"
PROVENANCE_FILE = "results_provenance.json"
TOOL = "ndhazard:adddamresults"
FT_PER_M = 3.28083989501312
# LISFLOOD-FP native outputs: extension -> (role, factor to plugin units, output name)
LISFLOOD_OUT = {
    ".max": ("depth", FT_PER_M, "Depth_Hmax_ft.tif", "ft"),
    ".maxvc": ("vel", FT_PER_M, "Velocity_Vmax_ftps.tif", "ft/s"),
    ".vmax": ("vel", FT_PER_M, "Velocity_Vmax_ftps.tif", "ft/s"),
    ".inittm": ("arr", 1.0, "Arrival_hr.tif", "h"),
    ".mxe": ("wse", FT_PER_M, "WSE_Max_ft.tif", "ft"),
}
_PLAN_RE = re.compile(r"\.p\d{2}\.hdf$", re.I)
_BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {"con", "prn", "aux", "nul"} | {"com%d" % i for i in range(1, 10)} | \
            {"lpt%d" % i for i in range(1, 10)}
_SIDE_EXT = (".prj", ".hdr", ".aux.xml", ".tfw", ".blw", ".stx", ".clr", ".ovr")
_DSSWISE_HINT = re.compile(r"dss[\s_\-]*wise|maxdepth|max_?depth_|hcom|arrivaltime_|"
                           r"_in_ft|_in_m(?![a-z])", re.I)


class AddResultsError(Exception):
    """A clear reason why the results were not added (nothing was written)."""


# ------------------------------------------------------------------ the Dams folder ----
def dams_root(explicit=""):
    """(folder, note): the Dams folder results go to - `explicit`, else the suite's saved
    Dams folder (project, computer setting, NDSUITE_DAMS_FOLDER), else the Dam suite
    home's last one, else ~/DamSafety/Dams. Never the read-only ND_Dam_Suite_Pack."""
    from .damsuite import suite_config as SC
    text = str(explicit or "").strip()
    if text:
        if SC.in_suite_pack(text):
            raise AddResultsError("{} is in the read-only {} - choose the Dams folder "
                                  "instead".format(text, SC.PACK_NAME))
        return DF.normalize(text), "Dams folder: {} (as given)".format(DF.normalize(text))
    cands = []
    try:
        cands.append((SC.get("dams_folder"), "the suite's Dams folder setting"))
    except Exception:                                            # noqa
        pass
    try:
        from qgis.core import QgsSettings
        cands.append((QgsSettings().value("NDDamSuite/home_dams_folder", "") or "",
                      "the Dam suite home's Dams folder"))
    except Exception:                                            # noqa
        pass
    for folder, why in cands:
        folder = str(folder or "").strip()
        if folder and not SC.in_suite_pack(folder):
            return DF.normalize(folder), "Dams folder: {} ({})".format(DF.normalize(folder), why)
    d = DF.normalize(SC.default_dams_folder())
    return d, "Dams folder: {} (the default; set the Dams folder in the suite settings to " \
              "use another)".format(d)


def _check_root(root):
    from .damsuite import suite_config as SC
    if not root or DF.is_root(root):
        raise AddResultsError("'{}' cannot be the Dams folder".format(root))
    if SC.in_suite_pack(root):
        raise AddResultsError("{} is in the read-only {}".format(root, SC.PACK_NAME))
    if DF.has_record(root) or DF.has_run(root):
        raise AddResultsError("{} is a dam's own folder, not the Dams folder that holds the "
                              "dam folders".format(root))
    if os.path.exists(root) and not os.path.isdir(root):
        raise AddResultsError("{} is a file, not a folder".format(root))


# ------------------------------------------------------------------ matching a dam ------
def _id_key(text):
    """('DWR' / 'NID' / '', digits without leading zeros) when `text` is a dam ID, else
    None."""
    t = re.sub(r"[\s_\-.]+", "", str(text or "")).upper()
    m = re.match(r"^(DWR|NID)?(ND)?0*(\d+)$", t)
    if not m or (not m.group(1) and not m.group(2) and not 3 <= len(t) <= 6):
        return None
    kind = "DWR" if m.group(1) == "DWR" else ("NID" if (m.group(1) or m.group(2)) else "")
    return kind, m.group(3) or "0"


def _name_key(text):
    return re.sub(r"[^0-9a-z]+", "", str(text or "").casefold())


def looks_like_id(text):
    return _id_key(text) is not None


def _same_id(a, b):
    ka, kb = _id_key(a), _id_key(b)
    if ka is None or kb is None or ka[1] != kb[1]:
        return False
    return ka[0] == kb[0] or not ka[0] or not kb[0]


def _record_fields(folder):
    try:
        with open(os.path.join(folder, "dam.json"), encoding="utf-8-sig") as fh:
            f = json.load(fh).get("fields") or {}
        return f if isinstance(f, dict) else {}
    except (OSError, ValueError, AttributeError, RecursionError):
        return {}


def dam_folders(root):
    """[(folder, labels)] of the folders directly in the Dams folder (as the batch lists
    them); labels: the folder name and the dam.json ID / name / NID / DWR ID."""
    out = []
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return out
    from .constants import OUTPUT_FOLDER_NAME
    for n in names:
        p = os.path.join(root, n)
        if n.startswith((".", "_")) or n.lower() == OUTPUT_FOLDER_NAME or not os.path.isdir(p):
            continue
        f = _record_fields(p)
        labels = [n] + [str(f[k]) for k in ("dam_id", "name", "nid_id", "dwr_structure_id")
                        if f.get(k) not in (None, "")]
        out.append((p, labels))
    return out


def completions(root):
    """Sorted unique labels for the dam-name box's completer."""
    seen = {}
    for _p, labels in dam_folders(root):
        for lab in labels:
            seen.setdefault(lab.casefold(), lab)
    return sorted(seen.values(), key=str.casefold)


def match_dam(root, dam):
    """The dam folder in `root` for `dam` (ID or name), or None. Several matches are an
    error (never a guess)."""
    want = str(dam or "").strip()
    if not want:
        raise AddResultsError("Name the dam (its ID such as DWR-2801, or its name)")
    nk = _name_key(want)
    hits = []
    for p, labels in dam_folders(root):
        if any((_name_key(lab) == nk and nk) or _same_id(want, lab) for lab in labels):
            hits.append(p)
    if len(hits) > 1:
        raise AddResultsError("'{}' fits {} dam folders: {} - use the folder's name".format(
            want, len(hits), ", ".join(os.path.basename(h) for h in hits)))
    return hits[0] if hits else None


def new_folder_name(dam):
    """A safe folder name for a new dam folder (raises when there is none)."""
    from .constants import OUTPUT_FOLDER_NAME
    text = re.sub(r"\s+", " ", str(dam or "")).strip()
    if looks_like_id(text):
        text = text.upper()
    name = _BAD_CHARS.sub("_", text).strip(" .").lstrip("_.")
    if not name or name.lower() in _RESERVED or name.lower() == OUTPUT_FOLDER_NAME \
            or not re.search(r"[0-9A-Za-z]", name):
        raise AddResultsError("'{}' cannot be a folder name - type the dam's ID or name"
                              .format(dam))
    return name[:80].rstrip(" .")


def plugin_version():
    from .constants import PLUGIN_VERSION
    return PLUGIN_VERSION


def _make_dam_folder(root, dam, notes):
    folder = os.path.join(root, new_folder_name(dam))
    if os.path.exists(folder):         # a folder the batch skips / a file: never reused
        raise AddResultsError("{} exists but is not a dam folder that matches '{}'".format(
            folder, dam))
    os.makedirs(folder)
    from .damsuite import dam_record as DR
    rec = DR.load(folder, create=True)
    text = str(dam).strip()
    if looks_like_id(text):
        rec.set("dam_id", text.upper(), TOOL, plugin_version(), note="typed when its "
                "model results were added")
    else:
        rec.set("name", text, TOOL, plugin_version(), note="typed when its model results "
                "were added")
    rec.save()
    notes.append("New dam folder {} with dam.json ({} = {})".format(
        folder, "dam_id" if looks_like_id(text) else "name",
        text.upper() if looks_like_id(text) else text))
    return folder


# ------------------------------------------------------------------ the source ----------
def _files(folder):
    from .dsswise import is_junk_path
    out = []
    for dirpath, dirs, files in os.walk(folder):
        dirs[:] = sorted(d for d in dirs if not is_junk_path(d) and not d.startswith("."))
        for f in sorted(files):
            if not is_junk_path(f):
                out.append(os.path.join(dirpath, f))
    return out


def source_sha256(path):
    """sha256 of a file; of a folder: of every file's relative path and contents."""
    h = hashlib.sha256()

    def feed(p):
        with open(p, "rb") as fh:
            for block in iter(lambda: fh.read(1 << 20), b""):
                h.update(block)
    if os.path.isdir(path):
        for p in _files(path):
            h.update(os.path.relpath(p, path).replace("\\", "/").encode("utf-8") + b"\0")
            feed(p)
            h.update(b"\0")
    else:
        feed(path)
    return h.hexdigest()


def _lisflood_files(folder_or_file):
    """{ext: path} of one LISFLOOD-FP run's native outputs (raises with several runs)."""
    if os.path.isfile(folder_or_file):
        stem, ext = os.path.splitext(folder_or_file)
        if ext.lower() not in LISFLOOD_OUT:
            return {}
        folder, base = os.path.dirname(folder_or_file), os.path.basename(stem)
    else:
        folder, base = folder_or_file, None
    runs = {}
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        names = []
    for n in names:
        s, e = os.path.splitext(n)
        if e.lower() in LISFLOOD_OUT and os.path.isfile(os.path.join(folder, n)):
            runs.setdefault(s, {})[e.lower()] = os.path.join(folder, n)
    if base is not None:
        return runs.get(base, {})
    if len(runs) > 1:
        raise AddResultsError("{} holds the outputs of {} LISFLOOD-FP runs ({}) - choose "
                              "the run's .max file".format(folder, len(runs),
                                                           ", ".join(sorted(runs))))
    return next(iter(runs.values()), {})


def _plans(folder):
    try:
        return sorted(os.path.join(folder, n) for n in os.listdir(folder) if _PLAN_RE.search(n))
    except OSError:
        return []


def _raster_files(folder):
    from .dsswise import RASTER_EXT
    return [p for p in _files(folder) if os.path.splitext(p)[1].lower() in RASTER_EXT
            and not p.lower().endswith((".aux.xml", ".ovr"))]


def detect_model(path):
    """The model of the results at `path` (a MODEL_* other than auto), or raises."""
    low = path.lower()
    if os.path.isfile(path):
        if low.endswith(".zip"):
            return MODEL_DSSWISE
        if low.endswith(".hdf"):
            return MODEL_HECRAS
        if os.path.splitext(low)[1] in LISFLOOD_OUT:
            return MODEL_LISFLOOD
        if os.path.splitext(low)[1] in (".tif", ".tiff", ".vrt", ".img", ".flt", ".bil",
                                         ".grd"):
            return MODEL_DSSWISE if _DSSWISE_HINT.search(os.path.basename(low)) \
                else MODEL_HECRAS
        if low.endswith(".asc"):
            return MODEL_DSSWISE if _DSSWISE_HINT.search(os.path.basename(low)) \
                else MODEL_HECRAS
        raise AddResultsError("Cannot tell which model made {} - choose the model".format(path))
    if not os.path.isdir(path):
        raise AddResultsError("{} does not exist".format(path))
    if _lisflood_files(path):
        return MODEL_LISFLOOD
    rasters = _raster_files(path)
    if any(_DSSWISE_HINT.search(os.path.basename(p)) for p in rasters) or \
            any(n.lower().endswith(".zip") for n in os.listdir(path)) and not rasters:
        return MODEL_DSSWISE
    if rasters:
        return MODEL_HECRAS
    if _plans(path):
        return MODEL_HECRAS
    raise AddResultsError("No model results recognised in {} (rasters, a DSS-WISE zip, a "
                          "HEC-RAS .p##.hdf or LISFLOOD-FP .max files)".format(path))


# ------------------------------------------------------------------ writers -------------
def _set_band_unit(path, unit, srs_wkt=None):
    from osgeo import gdal
    ds = gdal.Open(path, gdal.GA_Update)
    if ds is None:
        return
    if srs_wkt and not ds.GetProjection():
        ds.SetProjection(srs_wkt)
    if unit:
        ds.GetRasterBand(1).SetUnitType(unit)
    ds.FlushCache()
    ds = None


def _srs_wkt(text):
    """WKT of a CRS given as EPSG:xxxx / WKT / proj string ('' when empty)."""
    text = str(text or "").strip()
    if not text:
        return ""
    from osgeo import osr
    s = osr.SpatialReference()
    if s.SetFromUserInput(text) != 0:
        raise AddResultsError("Coordinate system '{}' not understood".format(text))
    return s.ExportToWkt()


def _grid_in(path, other):
    """True when raster `path`'s centre lies inside raster `other`'s extent."""
    from osgeo import gdal
    a, b = gdal.Open(path), gdal.Open(other)
    if a is None or b is None:
        return False
    ga, gb = a.GetGeoTransform(), b.GetGeoTransform()
    cx = ga[0] + ga[1] * a.RasterXSize / 2.0
    cy = ga[3] + ga[5] * a.RasterYSize / 2.0
    xs = sorted((gb[0], gb[0] + gb[1] * b.RasterXSize))
    ys = sorted((gb[3], gb[3] + gb[5] * b.RasterYSize))
    return xs[0] <= cx <= xs[1] and ys[0] <= cy <= ys[1]


def _lisflood_crs(depth_path, crs, dam_folder, notes):
    """(wkt, why) of the LISFLOOD-FP grids' CRS: the CRS given, else the grid's own
    .prj, else any ESRI .prj next to it, else the dam's DEM when the grid lies on it."""
    from osgeo import gdal
    if crs:
        return _srs_wkt(crs), "the coordinate system given"
    ds = gdal.Open(depth_path)
    if ds is not None and ds.GetProjection():
        return ds.GetProjection(), "the grid's .prj file"
    folder = os.path.dirname(depth_path)
    from osgeo import osr
    for n in sorted(os.listdir(folder)):
        if n.lower().endswith(".prj"):
            try:
                with open(os.path.join(folder, n), encoding="utf-8", errors="replace") as fh:
                    txt = fh.read(20000)
            except OSError:
                continue
            if txt.lstrip().startswith(("PROJCS", "GEOGCS", "PROJCRS", "GEOGCRS")):
                s = osr.SpatialReference()
                if s.SetFromUserInput(txt) == 0:
                    return s.ExportToWkt(), n
    try:
        from .damsuite import damdata as DD
        dem = (DD.dam_data(dam_folder) or {}).get("dem") if dam_folder else None
    except Exception:                                            # noqa
        dem = None
    if dem and os.path.isfile(dem):
        d = gdal.Open(dem)
        if d is not None and d.GetProjection() and _grid_in(depth_path, dem):
            notes.append("LISFLOOD-FP grids have no .prj: the coordinate system of the dam's "
                         "DEM ({}) was used - the grid lies on it".format(os.path.basename(dem)))
            return d.GetProjection(), "the dam's DEM"
    raise AddResultsError("The LISFLOOD-FP grids say nothing about their coordinate system "
                          "(no .prj next to them, and no DEM in the dam folder that they lie "
                          "on) - give the coordinate system (e.g. EPSG:26914)")


def _add_lisflood(src, out, crs, dam_folder, progress, notes):
    from .dsswise import _convert_raster
    found = _lisflood_files(src)
    if ".max" not in found:
        if os.path.isdir(src) and _raster_files(src):
            notes.append("No native LISFLOOD-FP .max file: the rasters were copied as "
                         "exported")
            return _add_rasters(src, out, progress, notes)
        raise AddResultsError("No LISFLOOD-FP maximum-depth file (<run>.max) in {}".format(src))
    wkt, why = _lisflood_crs(found[".max"], crs, dam_folder, notes)
    paths = {}
    exts = [e for e in (".max", ".maxvc", ".vmax", ".inittm", ".mxe") if e in found]
    if ".maxvc" in exts and ".vmax" in exts:
        exts.remove(".vmax")
        notes.append("Both .maxVc and .Vmax found: .maxVc used for the velocity")
    for i, ext in enumerate(exts):
        role, factor, name, unit = LISFLOOD_OUT[ext]
        dst = os.path.join(out, name)
        _convert_raster(found[ext], dst, factor)
        _set_band_unit(dst, unit, wkt)
        paths[role] = dst
        if progress:
            progress(100.0 * (i + 1) / len(exts), "LISFLOOD-FP {}".format(name))
    notes.append("LISFLOOD-FP {} converted: depth and velocity metres -> feet (x{:.5f}), "
                 "arrival kept in hours; coordinate system from {}".format(
                     ", ".join(os.path.basename(found[e]) for e in exts), FT_PER_M, why))
    if "vel" not in paths:
        notes.append("No LISFLOOD-FP velocity output (.maxVc / .Vmax): the depth-only "
                     "criterion is used")
    if "arr" not in paths:
        notes.append("No LISFLOOD-FP arrival output (.inittm): no warning times")
    return paths


def _vrt_sources(vrt):
    """[(path, relative?)] of the files a VRT reads."""
    try:
        tree = ET.parse(vrt)
    except (ET.ParseError, OSError):
        return []
    out = []
    for el in tree.iter("SourceFilename"):
        rel = str(el.get("relativeToVRT", "0")).strip() == "1"
        if el.text:
            out.append((el.text.strip(), rel))
    return out


def _copy_with_sidecars(src, dst_folder, rel=""):
    target = os.path.join(dst_folder, rel) if rel else os.path.join(
        dst_folder, os.path.basename(src))
    os.makedirs(os.path.dirname(target), exist_ok=True)
    shutil.copy2(src, target)
    stem = os.path.splitext(src)[0]
    for ext in _SIDE_EXT:
        for side in (stem + ext, src + ext):
            if os.path.isfile(side):
                shutil.copy2(side, os.path.join(os.path.dirname(target),
                                                os.path.basename(side)))
    return target


def _metric(path):
    """True when the raster's band unit or its HEC-RAS project says SI units."""
    try:
        from .checks import _band_units, _ras_project_units, _name_units
    except Exception:                                            # noqa
        return False, ""
    paths = [path]
    if path.lower().endswith(".vrt"):        # RAS Mapper: the unit is on the tiles
        paths += [os.path.normpath(os.path.join(os.path.dirname(path), s)) if rel else s
                  for s, rel in _vrt_sources(path)][:3]
    for finder in (_band_units, _ras_project_units, _name_units):
        for p in paths:
            if not os.path.isfile(p):
                continue
            kind, why = finder(p)
            if kind:
                return kind == "metric", why
    return False, ""


_FACTOR = {"depth": FT_PER_M, "vel": FT_PER_M, "dv": FT_PER_M * FT_PER_M, "wse": FT_PER_M}
_UNIT = {"depth": "ft", "vel": "ft/s", "dv": "ft2/s", "wse": "ft"}


def _role_of(path):
    from .autodetect import _ROLES, _is_time_step, _DERIVED_RE
    from .dsswise import detect_role, R_WSE
    f = os.path.basename(path)
    if _is_time_step(f) or _DERIVED_RE.search(f):
        return None
    if re.match(r"^d\s*[*x_]\s*v(?![a-z])", f, re.I):
        return "dv"
    r = detect_role(f)
    return _ROLES.get(r) or ("wse" if r == R_WSE else None)


def _add_rasters(src, out, progress, notes):
    """Copy RAS Mapper / other exported rasters (a .vrt with its tiles); SI rasters are
    converted to feet."""
    from .dsswise import _convert_raster
    if os.path.isfile(src):
        files, base = [src], os.path.dirname(src)
    else:
        files, base = _raster_files(src), src
    tiles = set()
    for f in files:
        if f.lower().endswith(".vrt"):
            for s, rel in _vrt_sources(f):
                if rel:
                    tiles.add(os.path.normcase(os.path.normpath(
                        os.path.join(os.path.dirname(f), s))))
    picked = []
    for f in files:
        if os.path.normcase(os.path.normpath(f)) in tiles:
            continue
        role = _role_of(f)
        if role is None:
            continue
        picked.append((f, role))
    if not any(r == "depth" for _f, r in picked):
        raise AddResultsError("No maximum-depth raster recognised in {} (the file name should "
                              "say Depth (Max), Hmax or Dmax)".format(src))
    paths = {}
    for i, (f, role) in enumerate(picked):
        rel = os.path.relpath(f, base) if os.path.isdir(src) else os.path.basename(f)
        metric, why = _metric(f) if role in _FACTOR else (False, "")
        if metric:
            stem = os.path.splitext(os.path.basename(f))[0]
            dst = os.path.join(out, os.path.dirname(rel),
                               "{}_{}.tif".format(stem, _UNIT[role].replace("/", "p")))
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            _convert_raster(f, dst, _FACTOR[role])
            _set_band_unit(dst, _UNIT[role])
            notes.append("{}: SI units ({}) - converted to {} as {}".format(
                os.path.basename(f), why, _UNIT[role], os.path.basename(dst)))
        else:
            dst = _copy_with_sidecars(f, out, rel)
            if f.lower().endswith(".vrt"):
                for s, isrel in _vrt_sources(f):
                    if isrel:
                        sp = os.path.normpath(os.path.join(os.path.dirname(f), s))
                        if os.path.isfile(sp):
                            _copy_with_sidecars(sp, os.path.dirname(dst), s)
                    else:
                        notes.append("{} reads {} by its full path: the copy still reads "
                                     "it there".format(os.path.basename(f), s))
        paths.setdefault(role, dst)
        if progress:
            progress(100.0 * (i + 1) / len(picked), "Copying {}".format(os.path.basename(f)))
    notes.append("{} raster(s) copied: {}".format(len(picked), ", ".join(
        os.path.basename(f) for f, _r in picked)))
    return paths


def _add_dsswise(src, out, work, accept_assumed, progress, notes):
    from . import dsswise as DW
    if os.path.isfile(src) and not src.lower().endswith(".zip"):
        one = os.path.join(work, "single")          # one raster: scan a copy of it alone
        _copy_with_sidecars(src, one)
        src = one
    folder = DW.open_package(src, work)
    items = DW.scan(folder)
    assumed = DW.assumed_units(items)
    if assumed and not accept_assumed:
        raise AddResultsError("The units of {} are not stated in the file names (US units "
                              "would be assumed): check the DSS-WISE report, then tick 'Accept "
                              "assumed units'".format(", ".join(it.name for it in assumed)))
    try:
        res = DW.convert(items, out, name="DSS-WISE Lite", progress=progress)
    except RuntimeError as e:
        raise AddResultsError(str(e))
    notes.extend(res.get("notes") or [])
    notes.extend("WARNING: " + w for w in res.get("warnings") or [])
    p = res["paths"]
    return {k: _max_name(v) for k, v in (("depth", p.get(DW.R_DEPTH)), ("vel", p.get(DW.R_VEL)),
                                         ("arr", p.get(DW.R_ARR)), ("dv", p.get(DW.R_DV)))}


def _max_name(path):
    """The DSS-WISE import names its rasters Depth_ft.tif / Velocity_ftps.tif, which the
    batch (autodetect) reads as one output time ('Depth <time>') and passes over: rename
    them (and their sidecars) Depth_Max_ft.tif / Velocity_Max_ftps.tif."""
    if not path or not os.path.isfile(path):
        return path
    from .autodetect import _is_time_step
    folder, name = os.path.split(path)
    if not _is_time_step(name):
        return path
    m = re.match(r"^(depth|velocity|vel|speed)(?![a-z])[\s_\-]*(.*)$", name, re.I)
    new = "{}_Max_{}".format(m.group(1), m.group(2)) if m else "Max_" + name
    stem_old, stem_new = os.path.splitext(name)[0], os.path.splitext(new)[0]
    for f in os.listdir(folder):
        if f == stem_old or f.startswith(stem_old + "."):
            os.rename(os.path.join(folder, f), os.path.join(folder, stem_new + f[len(stem_old):]))
    return os.path.join(folder, new)


def _add_plan(src, out, terrain, dam_folder, progress, notes):
    from . import rasimport as RI
    hdf = src
    if os.path.isdir(src):
        plans = _plans(src)
        if len(plans) != 1:
            raise AddResultsError("{} holds {} HEC-RAS plan results (.p##.hdf) - choose the "
                                  "plan's file".format(src, len(plans)))
        hdf = plans[0]
    try:
        res = RI.import_plan(hdf, out, dem_path=terrain or None, progress=progress,
                             runner_dirs=[dam_folder] if dam_folder else None)
    except RI.RasImportError as e:
        raise AddResultsError(str(e))
    notes.extend(res.get("notes") or [])
    if res.get("depth_basis") == RI.DEPTH_BASIS_CELL_MIN:
        notes.append("WARNING: " + RI.CELL_MIN_WARNING)
    p = res["paths"]
    return {"depth": p.get("depth"), "vel": p.get("vel"), "arr": p.get("arr"), "wse": p.get("wse")}


# ------------------------------------------------------------------ provenance ----------
def _existing_same(inputs, sha, scenario):
    """An earlier folder holding the results of the same source and scenario."""
    try:
        names = sorted(os.listdir(inputs))
    except OSError:
        return None
    for n in names:
        p = os.path.join(inputs, n, PROVENANCE_FILE)
        try:
            with open(p, encoding="utf-8") as fh:
                d = json.load(fh)
        except (OSError, ValueError):
            continue
        if d.get("source_sha256") == sha and d.get("scenario") == scenario:
            return os.path.join(inputs, n)
    return None


def _target_name(inputs, base):
    n = 1
    while True:
        name = base if n == 1 else "{}_{}".format(base, n)
        p = os.path.join(inputs, name)
        if not os.path.exists(p) or (os.path.isdir(p) and not os.listdir(p)):
            return name
        n += 1


def _record_status(dam_folder, result, notes):
    try:
        from .damsuite import dam_record as DR
        rec = DR.load(dam_folder, create=True)
        rec.add_status("modelled", TOOL, plugin_version(), result=result)
        rec.save()
    except Exception as e:                                       # noqa
        notes.append("dam.json not updated ({}) - it is left as it is".format(e))


# ------------------------------------------------------------------ the tool ------------
def add_results(dam, source, model=MODEL_AUTO, scenario=SCEN_BREACH, dams_folder="",
                crs="", terrain="", accept_assumed=False, progress=None, create=True):
    """Add `source` (model results) to dam `dam`'s folder. Returns a dict: status
    ('added' / 'already added'), dam_folder, created (bool), folder (the results folder),
    paths {role: file}, model, scenario, notes [text], found (find_rasters of the dam
    folder) and picked_by_batch (bool: the batch takes this depth raster)."""
    notes = []
    src = DF.normalize(source)
    if not src or not os.path.exists(src):
        raise AddResultsError("The results '{}' do not exist".format(source))
    if scenario not in SCEN_DIR:
        raise AddResultsError("Unknown scenario '{}'".format(scenario))
    root, why = dams_root(dams_folder)
    _check_root(root)
    notes.append(why)
    if DF.inside(root, src):
        raise AddResultsError("The results folder {} holds the Dams folder - choose the "
                              "model's own results folder".format(src))
    dam_folder = match_dam(root, dam) if os.path.isdir(root) else None
    if dam_folder and DF.inside(src, dam_folder):
        raise AddResultsError("{} is already in the dam folder {} - the batch finds it "
                              "there".format(src, dam_folder))
    if model in (None, "", MODEL_AUTO):
        model = detect_model(src)
        notes.append("Model detected: {}".format(MODEL_NAME[model]))
    if model not in MODEL_DIR:
        raise AddResultsError("Unknown model '{}'".format(model))
    if progress:
        progress(1.0, "Checking the source")
    sha = source_sha256(src)
    created = False
    if dam_folder is None:
        if not create:
            raise AddResultsError("No dam folder for '{}' in {}".format(dam, root))
        os.makedirs(root, exist_ok=True)
        dam_folder = _make_dam_folder(root, dam, notes)
        created = True
    else:
        notes.append("Dam folder: {}".format(dam_folder))
    inputs = os.path.join(dam_folder, INPUTS_DIR)
    same = _existing_same(inputs, sha, scenario)
    if same:
        notes.append("These results (same sha256) were added before, to {} - nothing "
                     "copied".format(same))
        found = _found(dam_folder)
        return {"status": "already added", "dam_folder": dam_folder, "created": created,
                "folder": same, "paths": {}, "model": model, "scenario": scenario,
                "notes": notes, "found": found, "picked_by_batch": None, "sha256": sha}
    base = "{}_{}".format(SCEN_DIR[scenario], MODEL_DIR[model])
    name = _target_name(inputs, base)
    if name != base:
        notes.append("{} already holds results: these went to {} (nothing overwritten)"
                     .format(os.path.join(INPUTS_DIR, base), os.path.join(INPUTS_DIR, name)))
    os.makedirs(inputs, exist_ok=True)
    tmp = os.path.join(inputs, ".adding_" + uuid.uuid4().hex[:8])
    work = tempfile.mkdtemp(prefix="ndhazard_add_")
    os.makedirs(tmp)
    try:
        if model == MODEL_DSSWISE:
            paths = _add_dsswise(src, tmp, work, accept_assumed, progress, notes)
        elif model == MODEL_LISFLOOD:
            paths = _add_lisflood(src, tmp, crs, dam_folder, progress, notes)
        elif src.lower().endswith(".hdf") or (os.path.isdir(src) and not _raster_files(src)
                                               and _plans(src)):
            paths = _add_plan(src, tmp, terrain, dam_folder, progress, notes)
        else:
            if os.path.isfile(src) and src.lower().endswith(".zip"):
                from . import dsswise as DW
                src_dir = DW.open_package(src, work)
                paths = _add_rasters(src_dir, tmp, progress, notes)
            else:
                paths = _add_rasters(src, tmp, progress, notes)
        if not paths.get("depth"):
            raise AddResultsError("No maximum-depth raster in the results")
        final = os.path.join(inputs, name)
        if os.path.isdir(final):
            os.rmdir(final)                      # empty (checked by _target_name)
        os.rename(tmp, final)
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        if created:
            notes.append("(the new dam folder {} was kept)".format(dam_folder))
        raise
    finally:
        shutil.rmtree(work, ignore_errors=True)
    paths = {k: v.replace(tmp, final, 1) for k, v in paths.items() if v}
    when = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    prov = {"tool": TOOL, "plugin_version": plugin_version(), "when": when,
            "dam": str(dam), "dam_folder": dam_folder, "created_dam_folder": created,
            "model": MODEL_NAME[model], "scenario": scenario,
            "source": src, "source_kind": "folder" if os.path.isdir(src) else "file",
            "source_sha256": sha,
            "outputs": {k: os.path.relpath(v, final) for k, v in paths.items()},
            "notes": notes}
    with open(os.path.join(final, PROVENANCE_FILE), "w", encoding="utf-8") as fh:
        json.dump(prov, fh, indent=1, ensure_ascii=False)
    _record_status(dam_folder, {"model": MODEL_NAME[model], "scenario": scenario,
                                "folder": os.path.relpath(final, dam_folder),
                                "source": src, "source_sha256": sha}, notes)
    found = _found(dam_folder)
    key = "depth" if scenario == SCEN_BREACH else "base_depth"
    picked = bool(found.get(key)) and DF.inside(found[key], final)
    if not picked:
        notes.append("WARNING: the batch classification takes {} as the {} depth, not these "
                     "results (it prefers '(Max)' / Hmax names, a .vrt, then the raster "
                     "nearest the dam folder). Move the other rasters into a sub-folder named "
                     "'old' to use these.".format(
                         os.path.relpath(found[key], dam_folder) if found.get(key) else "nothing",
                         "breach" if scenario == SCEN_BREACH else "no-breach"))
    return {"status": "added", "dam_folder": dam_folder, "created": created, "folder": final,
            "paths": paths, "model": model, "scenario": scenario, "notes": notes,
            "found": found, "picked_by_batch": picked, "sha256": sha}


def _found(dam_folder):
    from .autodetect import find_rasters
    return find_rasters(dam_folder)


def summary(res):
    """Lines describing an add_results result."""
    lines = ["{}: {} ({}, {})".format(
        "Added" if res["status"] == "added" else "Already added", res["folder"],
        MODEL_NAME.get(res["model"], res["model"]), dict(SCENARIOS)[res["scenario"]])]
    for k, v in sorted(res.get("paths", {}).items()):
        lines.append("  {}: {}".format(k, os.path.basename(v)))
    lines += ["  " + n for n in res.get("notes", [])]
    return lines


# ------------------------------------------------------------------ many dams (CSV) -----
_CSV_KEYS = {"dam": ("dam", "damid", "damname", "name", "id", "nidid", "dwrid"),
             "source": ("source", "path", "results", "modelresults", "folder", "file"),
             "model": ("model",), "scenario": ("scenario",), "crs": ("crs",),
             "terrain": ("terrain", "dem")}


def read_csv(path):
    """[{dam, source, model, scenario, crs, terrain}] from a CSV (relative paths are from
    the CSV's folder)."""
    with open(path, encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    out = []
    base = os.path.dirname(os.path.abspath(path))
    for i, r in enumerate(rows, start=2):
        low = {_name_key(k): (v or "").strip() for k, v in r.items() if isinstance(v, str)}
        row = {k: next((low[a] for a in al if low.get(a)), "") for k, al in _CSV_KEYS.items()}
        if not row["dam"] and not row["source"]:
            continue
        if not row["dam"] or not row["source"]:
            raise AddResultsError("{} line {}: needs a dam and a source".format(path, i))
        for k in ("source", "terrain"):
            if row[k] and not os.path.isabs(os.path.expanduser(row[k])):
                row[k] = os.path.join(base, row[k])
        row["model"] = _pick(row["model"], MODELS, MODEL_AUTO)
        row["scenario"] = _pick(row["scenario"], SCENARIOS, SCEN_BREACH)
        row["line"] = i
        out.append(row)
    return out


def _pick(text, choices, default):
    t = _name_key(text)
    if not t:
        return default
    for key, label in choices:
        if t in (_name_key(key), _name_key(label)) or _name_key(label).startswith(t):
            return key
    if t in ("baseline", "nobreach", "nonbreach"):
        return SCEN_BASELINE
    raise AddResultsError("'{}' is not one of: {}".format(text, ", ".join(l for _k, l in
                                                                          choices)))
