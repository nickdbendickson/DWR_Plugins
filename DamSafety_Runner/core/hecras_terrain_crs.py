"""v3.10.56: the HEC-RAS package's terrain GeoTIFF must keep its coordinate system.

A terrain rewritten while GDAL could not reach the PROJ database (PROJ_DATA unset) came out
with an unnamed metre CRS instead of EPSG:2266 (ft); HEC-RAS's terrain step then stopped with
"Translating source or target SRS failed". check_terrain() reopens the GeoTIFF after it is
written or copied into a package and before zipping, and raises TerrainCRSError (a
HecRasModelError, with a plain reason and what to do) unless:

  * the PROJ database is reachable (EPSG:4326 can be looked up),
  * the GeoTIFF has an EPSG code of its own, and
  * that code, the linear unit (name and size in metres) and the CRS itself match
    GIS/Projection.prj.
"""
import os

from .hecras_model import HecRasModelError

UNIT_TOL = 1e-9                       # relative, metres per unit (0.3048 vs 0.3048006096)
FIX_PROJ = ("Set PROJ_DATA to the folder that holds proj.db (for QGIS that is its share/proj "
            "folder, e.g. <QGIS>/share/proj), restart, and build the package again.")


class TerrainCRSError(HecRasModelError):
    pass


def _osr():
    from osgeo import gdal, osr
    return gdal, osr


def proj_db_problem():
    """None when GDAL/PROJ can look up EPSG codes, else a plain reason."""
    gdal, osr = _osr()
    s = osr.SpatialReference()
    try:
        with _quiet(gdal):
            bad = s.ImportFromEPSG(4326) != 0
    except Exception as e:                        # gdal.UseExceptions(): RuntimeError
        bad, why = True, str(e).strip()
    else:
        why = gdal.GetLastErrorMsg().strip() if bad else ""
    if not bad:
        return None
    env = ", ".join("{}={}".format(k, os.environ[k]) for k in ("PROJ_DATA", "PROJ_LIB")
                    if os.environ.get(k)) or "PROJ_DATA and PROJ_LIB are not set"
    return ("GDAL cannot find the PROJ coordinate-system database (proj.db), so it cannot "
            "look up EPSG codes ({}; {}). A terrain written like this loses its coordinate "
            "system and HEC-RAS cannot make the terrain. {}".format(env, why or "no reason given",
                                                                     FIX_PROJ))


class _quiet:
    def __init__(self, gdal):
        self.gdal = gdal

    def __enter__(self):
        self.gdal.PushErrorHandler("CPLQuietErrorHandler")
        self.gdal.ErrorReset()

    def __exit__(self, *a):
        self.gdal.PopErrorHandler()
        return False


def _epsg(s):
    """The EPSG code of srs `s` (its own, else a 100 % match from the PROJ database)."""
    if (s.GetAuthorityName(None) or "").upper() == "EPSG" and s.GetAuthorityCode(None):
        return s.GetAuthorityCode(None)
    try:
        m = s.FindMatches() or []
    except Exception:
        m = []
    m = [x for x, conf in m if conf >= 100 and (x.GetAuthorityName(None) or "").upper() == "EPSG"]
    return m[0].GetAuthorityCode(None) if len(m) == 1 else None


def _unit(s):
    return (s.GetLinearUnitsName() or "?"), float(s.GetLinearUnits() or 0.0)


def _describe(s, code):
    name, f = _unit(s)
    nm = s.GetName()
    nm = "unnamed" if not nm or nm.lower() == "unknown" else nm
    return "{} ({}; unit {}, {:g} m)".format("EPSG:" + code if code else "a CRS with no EPSG code",
                                              nm, name, f)


def check_terrain(tif, prj, what="The terrain GeoTIFF"):
    """Raise TerrainCRSError unless `tif` carries an EPSG code that matches the package's
    Projection.prj `prj` (code, linear unit, CRS) and the PROJ database is reachable.
    Returns {"epsg", "unit", "unit_m"} when it is fine."""
    gdal, osr = _osr()
    p = proj_db_problem()
    if p:
        raise TerrainCRSError(p)
    base = os.path.basename(tif)
    fix = ("Rebuild the package (with PROJ_DATA set), or put back the original terrain "
           "GeoTIFF. Do not send this package to HEC-RAS.")
    if not os.path.exists(tif):
        raise TerrainCRSError("{} {} is missing.".format(what, tif))
    if not os.path.exists(prj):
        raise TerrainCRSError("The package has no {}, so the terrain's coordinate system "
                              "cannot be checked. {}".format(prj, fix))
    with _quiet(gdal):
        try:
            ds = gdal.Open(tif)
        except Exception:
            ds = None
        if ds is None:
            raise TerrainCRSError("{} {} cannot be opened as a GeoTIFF. {}".format(what, tif, fix))
        ts = ds.GetSpatialRef()
        ds = None
    if ts is None or not ts.ExportToWkt():
        raise TerrainCRSError("{} {} has no coordinate system. HEC-RAS cannot make a terrain "
                              "from it. {}".format(what, base, fix))
    with open(prj, encoding="utf-8", errors="replace") as f:
        ptext = f.read()
    ps = osr.SpatialReference()
    try:
        with _quiet(gdal):
            ok = ps.SetFromUserInput(ptext) == 0
    except Exception:
        ok = False
    if not ok:
        raise TerrainCRSError("{} is not a coordinate system HEC-RAS or GDAL can read. "
                              "{}".format(os.path.basename(prj), fix))
    tcode, pcode = _epsg_own(ts), _epsg(ps)
    if not tcode:
        raise TerrainCRSError(
            "{} {} has lost its EPSG coordinate-system code: it now has {}. This happens when "
            "it is rewritten while GDAL cannot find the PROJ database, and HEC-RAS then "
            "stops with 'Translating source or target SRS failed'. The package's "
            "Projection.prj is {}. {}".format(what, base, _describe(ts, None),
                                              _describe(ps, pcode), fix))
    tu, pu = _unit(ts), _unit(ps)
    same_unit = (tu[0].lower() == pu[0].lower()
                 and abs(tu[1] - pu[1]) <= UNIT_TOL * max(abs(pu[1]), 1e-12))
    try:
        same = bool(ts.IsSame(ps, ["IGNORE_DATA_AXIS_TO_SRS_AXIS_MAPPING=YES"]))
    except Exception:
        same = False
    if (pcode and tcode != pcode) or not same_unit or not same:
        raise TerrainCRSError(
            "{} {} does not match the package's coordinate system: the terrain is {}, "
            "GIS/Projection.prj is {}. HEC-RAS would place or scale the terrain wrongly or "
            "refuse it. {}".format(what, base, _describe(ts, tcode), _describe(ps, pcode), fix))
    return {"epsg": int(tcode), "unit": tu[0], "unit_m": tu[1]}


def _epsg_own(s):
    """Only the GeoTIFF's own EPSG code (no guessing from the parameters)."""
    if (s.GetAuthorityName(None) or "").upper() == "EPSG":
        return s.GetAuthorityCode(None)
    return None


def check_package(folder):
    """check_terrain on every Terrain/*.tif of a package folder against GIS/Projection.prj."""
    terrain = os.path.join(folder, "Terrain")
    tifs = sorted(f for f in (os.listdir(terrain) if os.path.isdir(terrain) else [])
                  if f.lower().endswith((".tif", ".tiff")))
    if not tifs:
        raise TerrainCRSError("The package {} has no terrain GeoTIFF in Terrain/.".format(folder))
    prj = os.path.join(folder, "GIS", "Projection.prj")
    return {f: check_terrain(os.path.join(terrain, f), prj) for f in tifs}
