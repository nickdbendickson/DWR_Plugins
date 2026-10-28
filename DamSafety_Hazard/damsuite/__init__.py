"""damsuite - the shared package of the ND dam suite (ND Dam Hazard Classification Helper,
ND Dam Breach Runner, ND Dam Breach Screening).

Pure Python 3.9+ standard library: no QGIS, no numpy, no Qt. Importable anywhere (gdalsafe
imports osgeo only when one of its helpers is called).

The master copy lives in the suite folder; each plugin carries a byte-identical copy as a
sub-package (`<plugin>/damsuite/`) written by `sync_damsuite.py`, which also writes
`damsuite/FILES.json` = {file name: sha256 of its bytes} for every .py file of the package.
Never edit a vendored copy: edit the master and run the sync.

Modules:
  dam_record  one record per dam (<dam folder>/dam.json) shared by the three plugins
  versions    version checks between the plugins at start-up
  chain       model -> classification fingerprint chain (raster digests vs the runner's
              run_manifest.json)
  tm11_curves the ND REG_05.2026 Fig. 5.1 / 5.2 depth-velocity flood danger curves (one
              table for all three plugins, digitized by the engineer)
  suite_home  one view of a Dams folder (next action per dam, which plugin) for the three
              plugins' "Dam suite home" windows
  gdalsafe    GDAL calls that behave the same with GDAL's exceptions on or off (1.3.0: the
              plugins no longer switch that process-wide setting)
  settings    the suite settings file (1.4.0): one table of every engineer-chosen value of the
              three plugins, read as the tools' defaults and edited in the popups' grids
  xlsx_lite   a dependency-free .xlsx writer / reader for that file (1.4.0)
  units       feet / acres / acre-feet <-> metres for the user-facing values (1.4.0)
  suitedata   the suite data folder and Dams folder set once per computer (1.5.0)
  services    the WMS / WMTS / WCS / ArcGIS REST servers, the user's own, and the data
              source picked for each role (1.5.0)
  terrain_cache  the shared terrain tile cache (1.5.0)
  damdata     "Get data for this dam": <dam>/data/ and dam.json's data section, and the
              autofill of tool inputs from it (1.5.0)
  offline_pack  the statewide offline data pack reader / writer (moved from ND Dam Hazard,
              1.5.0)
  suitegpkg   EPSG:5070 projection and a pure-Python GeoPackage writer / reader (moved from
              ND Dam Hazard, 1.5.0)
  keepawake   keeps the computer from sleeping while a flood model runs, beside the solver
              (1.8.0)
  solvegate   checks before a flood solve starts: LISFLOOD-FP starts, the DEM covers the dam,
              the hydrograph has a peak and a volume, a layer meets the study area (1.9.0)
  gpkgpack    the offline pack as a folder with one GeoPackage per layer, manifest.json and
              LAYERS.txt (pack format 2): writer and reader (1.10.0)
  netproxy    the proxy set in QGIS, for the downloads that go through urllib (1.16.2)
  structure_flags  flagged structures: records that are not a dam to breach (a road fill
              over backwater, DWR-3161) with the hazard class of record (1.17.2)
  packcheck   an offline data pack checked against its manifest.json on load: every layer
              there, with its count, fields, geometry type and CRS; format 1 / 2 detection
              (1.17.5)
  vdatum      NGVD29 -> NAVD88 for dam elevations (VERTCON at the dam, offline): datum
              detection, the shift and its record for the reports and dam.json (1.18.0)
  vertcon_nd  NOAA's VERTCON grid clipped to North Dakota (public domain), read by vdatum
              (1.18.0; written by tools/make_vertcon_nd.py)

`selfcheck()` compares this package's files with FILES.json and returns a list of problems
("missing: x.py", "changed: x.py", "unexpected file: x.py"); [] means the copy is intact.
The comparison is on raw bytes (a CRLF conversion counts as a change).

Nothing here has been run inside a real QGIS; the QGIS-facing part (versions.installed) is
written defensively and returns None on any failure.
"""
import hashlib
import json
import os

from . import chain, dam_record, gdalsafe, settings, suite_home, tm11_curves, units, versions, xlsx_lite   # noqa: F401,E501  (sub-modules, relative imports only)
from . import offline_pack, suitegpkg, suitedata, services, terrain_cache, damdata   # noqa: F401,E501  (1.5.0)
from . import keepawake   # noqa: F401  (1.8.0)
from . import solvegate   # noqa: F401  (1.9.0)
from . import gpkgpack   # noqa: F401  (1.10.0)
from . import hydro3dhp   # noqa: F401  (1.15.0)
from . import netproxy   # noqa: F401  (1.16.2)
from . import structure_flags   # noqa: F401  (1.17.2)
from . import packcheck   # noqa: F401  (1.17.5)
from . import vdatum   # noqa: F401  (1.18.0; vertcon_nd is read on first use)

SUITE_VERSION = "1.18.1"   # 1.18.1: vdatum local datums - local_check (outside 500-3600 ft, > 50 ft from the DEM crest, a round 100 ft multiple > 10 ft from it), mode "local", plan/summary_lines for elevations taken from the DEM; 1.18.0: vdatum + vertcon_nd - NGVD29 -> NAVD88 (VERTCON, bundled ND grid, pyproj fallback) for dam elevations not stated as NAVD88; 1.17.7: ND DWR dugouts - suite_fill dugout_polygons / unknown / DWR export names, the format-2 pack's dwr/DWR_*.gpkg as a fallback for the DWR slots (an engineer's file wins), suitedata.dwr_layers drop-in first (pack=), damdata reads an engineer's dwr/ file before the pack; 1.17.6: packcheck never raises - a broken manifest.json or a cut-short GeoPackage is a FAILED / unreadable result, zipped format-2 packs against their zip listing, a UTF-8 BOM manifest is FAILED (folder and zip), the summary names pack-level problems, pack_signature (#171 follow-up); 1.17.5: packcheck (offline pack vs manifest.json on load), damdata logs it; 1.17.4: suitedata skips dem/lidar_1m (Screening 1 m tile cache, #105); 1.17.3: record.py, pack_convert fixes (#94); 1.17.2: structure_flags (flagged structures); 1.17.1: one canonical package
FILES_NAME = "FILES.json"

__all__ = ["SUITE_VERSION", "FILES_NAME", "selfcheck", "package_files", "chain", "dam_record",
           "gdalsafe", "settings", "suite_home", "tm11_curves", "units", "versions", "xlsx_lite",
           "offline_pack", "suitegpkg", "suitedata", "services", "terrain_cache", "damdata",
           "keepawake", "solvegate", "gpkgpack", "hydro3dhp", "netproxy", "structure_flags",
           "packcheck", "vdatum"]


def package_files(folder=None):
    """{file name: sha256} of the .py files directly in the package folder (sorted)."""
    folder = folder or os.path.dirname(os.path.abspath(__file__))
    out = {}
    for name in sorted(os.listdir(folder)):
        p = os.path.join(folder, name)
        if name.endswith(".py") and os.path.isfile(p):
            with open(p, "rb") as f:
                out[name] = hashlib.sha256(f.read()).hexdigest()
    return out


def selfcheck(folder=None):
    """Problems of this package copy against its FILES.json; [] = OK.

    `folder` defaults to this package's own folder (the optional argument is for tests)."""
    folder = folder or os.path.dirname(os.path.abspath(__file__))
    fpath = os.path.join(folder, FILES_NAME)
    if not os.path.isfile(fpath):
        return ["FILES.json missing"]
    try:
        with open(fpath, encoding="utf-8") as f:
            expected = json.load(f)
        if not isinstance(expected, dict):
            raise ValueError("not a JSON object")
    except (OSError, ValueError) as e:
        return ["FILES.json unreadable: {}".format(e)]
    actual = package_files(folder)
    problems = []
    for name in sorted(expected):
        if name not in actual:
            problems.append("missing: {}".format(name))
        elif actual[name] != expected[name]:
            problems.append("changed: {}".format(name))
    for name in sorted(set(actual) - set(expected)):
        problems.append("unexpected file: {}".format(name))
    return problems
