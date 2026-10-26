"""HEC-RAS interchange algorithms: export a breach to HEC-RAS, import 2D results as rasters.

Register with `HECRAS_ALGORITHMS` (see the end of the file)."""
import os

import numpy as np
from qgis.core import (QgsProcessingException, QgsProcessingOutputFile,
                       QgsProcessingOutputFolder, QgsProcessingOutputString,
                       QgsProcessingParameterBoolean, QgsProcessingParameterEnum,
                       QgsProcessingParameterExtent, QgsProcessingParameterRasterLayer,
                       QgsProcessingParameterString)

from .algorithms_tools import _ToolBase
from .core import benchmark, gdal_io, hecras, hecras_model, hecras_package, workflow
from .core.units import ARRIVAL_DEPTH_FT

STEPS = [(hecras.ras_interval_label(m), m) for m in hecras.RAS_INTERVALS_MIN]
STEP_DEFAULT = [m for _, m in STEPS].index(5)
IMPORT_NAMES = {"depth": "HECRAS Depth (Max).tif", "velocity": "HECRAS Velocity (Max).tif",
                "arrival": "HECRAS Arrival Time.tif", "wse": "HECRAS WSE (Max).tif",
                "time_of_max": "HECRAS Time of Max WSE.tif"}
# v3.10.22: computation points of the HEC-RAS 2D area (core/hecras_seeds.py)
MESH_SEEDS_LABELS = ["Regular grid (default)",
                     "gmsh layout, finer along the dam, breach and channel (needs gmsh; falls "
                     "back to the grid)"]
MESH_SEEDS_MODES = ["grid", "gmsh"]
# the HEC-RAS dam line (core/hecras_crest.py)
DAM_AXIS_LABELS = ["Along the embankment crest, traced from the DEM (default)",
                   "Straight line through the dam point, across the flow (the old line)"]
DAM_AXIS_MODES = ["crest", "straight"]
US_SI = ["Detect from the file (US customary when it says nothing)", "US customary (ft)",
         "SI metric (m)"]
# 3.10.84: what the imported HEC-RAS depth is measured from (core/hecras.depth_basis)
DEPTH_BASIS_OPTIONS = ["Cell minimum: max WSE minus the HEC-RAS cell's minimum terrain "
                       "elevation (default)",
                       "Per pixel: max WSE minus the terrain under each grid cell (needs the "
                       "Terrain)"]
DEPTH_BASIS_KEYS = [hecras.DEPTH_BASIS_CELL_MIN, hecras.DEPTH_BASIS_TERRAIN]


def _depth_basis_default():
    """Index of NDBREACH_HECRAS_DEPTH_BASIS (else cell minimum) in DEPTH_BASIS_KEYS."""
    try:
        return DEPTH_BASIS_KEYS.index(hecras.depth_basis())
    except (hecras.HecRasError, ValueError):
        return 0


class HecRasExportAlgorithm(_ToolBase):
    def name(self):
        return "hecrasexport"

    def displayName(self):
        return "Export the breach to HEC-RAS (2D model package, hydrograph and breach table)"

    def shortHelpString(self):
        return ("v3.10.16: builds a complete HEC-RAS 6.x 2D dam-breach model of a finished run "
                "(reservoir storage area, the dam as an SA/2D connection with the breach, the "
                "flooded valley as a 2D area, normal-depth outflow, the run's culverts and "
                "bridges) in '<dam folder>/_breach_model/hecras/<DamID>_<date>/' with a .zip "
                "of it to copy to a Windows PC with HEC-RAS 6.6, and WINDOWS_README.txt with "
                "the steps left to do in HEC-RAS (terrain, mesh, run) and what to send back. "
                "The model has NOT been opened in HEC-RAS by the plugin; with an output folder "
                "chosen, the package goes in '<folder>/hecras'. "
                "Also reads a finished run's breach hydrograph and parameters from its dam folder "
                "and writes (1) the hydrograph as an unsteady-flow text block for a HEC-RAS "
                "'.u##' file ('Flow Hydrograph= N', 10 values a line, 8-character fields) and "
                "as a CSV, on the time step you choose (the peak is kept exactly), and (2) "
                "the values HEC-RAS's breach editor asks for (final bottom elevation and "
                "width, side slopes, weir coefficient, formation time, trigger) as CSV and "
                "markdown. The values marked 'Plugin estimate' come from regression equations "
                "and are not design values; the breach center station is left blank. Use the "
                "same data interval in the HEC-RAS boundary condition as the step chosen "
                "here. Output goes to '<dam folder>/_hecras' unless you choose a folder.")

    def initAlgorithm(self, config=None):
        self._file("DAM_FOLDER", "Dam folder of a finished run", folder=True, optional=False)
        self.addParameter(QgsProcessingParameterEnum(
            "STEP", "Hydrograph time step (HEC-RAS data interval)", [s[0] for s in STEPS],
            defaultValue=STEP_DEFAULT))
        self._file("OUT_FOLDER", "Output folder (empty = <dam folder>/_hecras)", folder=True)
        self.addParameter(QgsProcessingParameterBoolean(
            "BUILD_MODEL", "Build the HEC-RAS 2D model package (needs the run's DEM and "
            "Depth (Max).tif)", defaultValue=True))
        self.addParameter(QgsProcessingParameterString(
            "MODEL_CRS", "HEC-RAS model CRS (empty = the DEM's when in feet, else North Dakota "
            "State Plane ft)", defaultValue="", optional=True))
        self._num("DAM_LENGTH_FT", "Dam crest length, ft (empty = the run's, else dam.json)",
                  optional=True, minimum=1.0)
        # v3.10.22
        self._num("SPILLWAY_CREST_FT", "Spillway crest elevation, ft (empty = dam.json "
                  "spillway_crest_ft, else no spillway)", optional=True)
        self._num("SPILLWAY_WIDTH_FT", "Spillway width, ft (empty = dam.json spillway_width_ft)",
                  optional=True, minimum=1.0)
        self.addParameter(QgsProcessingParameterEnum(
            "MESH_SEEDS", "2D computation points", MESH_SEEDS_LABELS, defaultValue=0,
            optional=True))
        self.addParameter(QgsProcessingParameterEnum(
            "DAM_AXIS", "Dam line (the SA/2D connection)", DAM_AXIS_LABELS, defaultValue=0,
            optional=True))
        self._num("BREACH_STATION_FT", "Breach centre, ft along the dam line from its first "
                  "end (empty = the deepest dam section)", optional=True, minimum=0.0)
        # 3.10.56: culverts and bridges of another run (the ANUGA run of the same dam)
        self._file("STRUCTURES_FROM", "Culverts and bridges from another run (its dam folder, "
                   "e.g. the ANUGA run; empty = this run's own)", folder=True)
        self.addOutput(QgsProcessingOutputFolder("MODEL_FOLDER", "HEC-RAS model package folder"))
        self.addOutput(QgsProcessingOutputFile("MODEL_ZIP", "HEC-RAS model package (.zip)"))
        self.addOutput(QgsProcessingOutputFile("HYDROGRAPH_TXT", "Hydrograph, .u## block"))
        self.addOutput(QgsProcessingOutputFile("HYDROGRAPH_CSV", "Hydrograph CSV"))
        self.addOutput(QgsProcessingOutputFile("BREACH_TABLE", "Breach parameters CSV"))
        self.addOutput(QgsProcessingOutputFile("BREACH_TABLE_MD", "Breach parameters markdown"))
        self.addOutput(QgsProcessingOutputString("RESULT", "Result"))

    def processAlgorithm(self, parameters, context, feedback):
        folder = self.parameterAsFile(parameters, "DAM_FOLDER", context)
        step = STEPS[self.parameterAsEnum(parameters, "STEP", context)]
        out = self._fileparam(parameters, "OUT_FOLDER", context) or None
        try:
            r = hecras.export_run(folder, out, step[1], workflow.MODEL_FOLDER)
        except (hecras.HecRasError, OSError, ValueError) as e:
            raise QgsProcessingException(str(e))
        i = r["info"]
        msg = ("{} values every {}; peak {:,.0f} cfs at {:.2f} h (the record's peak is at "
               "{:.2f} h); volume {:,.1f} ac-ft ({:+.2f}% against the record)".format(
                   i["n"], step[0], i["peak_cfs"], i["peak_time_exported_hr"], i["peak_time_hr"],
                   i["volume_exported_acft"], i["volume_change_pct"]))
        feedback.pushInfo(msg)
        feedback.pushInfo("Set the boundary condition's data interval to {}.".format(step[0]))
        for n in i["notes"]:
            feedback.reportError("Check: " + n, fatalError=False)
        feedback.reportError("Check: the breach values are the plugin's estimates; the center "
                             "station is blank. See the basis column.", fatalError=False)
        res = {"HYDROGRAPH_TXT": r["hydrograph_txt"], "HYDROGRAPH_CSV": r["hydrograph_csv"],
               "BREACH_TABLE": r["table_csv"], "BREACH_TABLE_MD": r["table_md"],
               "MODEL_FOLDER": "", "MODEL_ZIP": ""}
        build = (self.parameterAsBool(parameters, "BUILD_MODEL", context)
                 if "BUILD_MODEL" in parameters else True)
        if build:
            crs = self._str(parameters, "MODEL_CRS", context) or None
            opts = {}
            if parameters.get("DAM_LENGTH_FT") not in (None, ""):
                opts["dam_length_ft"] = self.parameterAsDouble(parameters, "DAM_LENGTH_FT",
                                                               context)
            for key, opt in (("SPILLWAY_CREST_FT", "spillway_crest_ft"),
                             ("SPILLWAY_WIDTH_FT", "spillway_width_ft")):
                if parameters.get(key) not in (None, ""):
                    opts[opt] = self.parameterAsDouble(parameters, key, context)
            if parameters.get("MESH_SEEDS") not in (None, ""):
                opts["mesh_seeds"] = MESH_SEEDS_MODES[self.parameterAsEnum(parameters,
                                                                           "MESH_SEEDS", context)]
            if parameters.get("DAM_AXIS") not in (None, ""):
                opts["dam_axis"] = DAM_AXIS_MODES[self.parameterAsEnum(parameters, "DAM_AXIS",
                                                                       context)]
            if parameters.get("BREACH_STATION_FT") not in (None, ""):
                opts["breach_station_ft"] = self.parameterAsDouble(parameters,
                                                                   "BREACH_STATION_FT", context)
            sfrom = self._fileparam(parameters, "STRUCTURES_FROM", context) or None
            if sfrom:
                opts["structures_from"] = sfrom
            try:
                p = hecras_package.build_package(
                    folder, out_root=os.path.join(out, "hecras") if out else None,
                    dt_min=step[1], crs=crs, options=opts, log=feedback.pushInfo)
            except (hecras_model.HecRasModelError, hecras.HecRasError, OSError, ValueError,
                    ImportError, RuntimeError) as e:
                feedback.reportError("HEC-RAS 2D model not built: {}".format(e),
                                     fatalError=False)
                msg += "; HEC-RAS 2D model not built ({})".format(e)
            else:
                res["MODEL_FOLDER"], res["MODEL_ZIP"] = p["folder"], p["zip"] or ""
                pi = p["info"]
                feedback.pushInfo("HEC-RAS 2D model: {} cells of {:g} {} in {}, dam line "
                                  "{:,.0f} {}".format(pi["cells"], pi["cell"], pi["len_name"],
                                                      pi["crs"], pi["dam_line"],
                                                      pi["len_name"]))
                for n in p["notes"]:
                    feedback.reportError("Check: " + n, fatalError=False)
                feedback.pushInfo("Copy {} to the Windows PC and follow {} inside it.".format(
                    p["zip"] or p["folder"], hecras_package.README))
                msg += "; HEC-RAS 2D model package {}".format(p["zip"] or p["folder"])
        res["RESULT"] = msg
        return res


class HecRasImportAlgorithm(_ToolBase):
    def name(self):
        return "hecrasimport"

    def displayName(self):
        return "Import HEC-RAS 2D results as rasters"

    def shortHelpString(self):
        return ("Reads the maximum results of a HEC-RAS 2D plan HDF (.p##.hdf) and writes "
                "GeoTIFFs 'HECRAS Depth (Max).tif', 'HECRAS Velocity (Max).tif', 'HECRAS "
                "Arrival Time.tif' and 'HECRAS WSE (Max).tif' in ft, ft/s, hours and ft. Give the "
                "terrain the model used: depth is then the cell's maximum water surface minus "
                "the terrain under each grid cell, and the wet area and arrival time follow "
                "that depth. Without a terrain, depth is the maximum water surface minus the "
                "cell's minimum elevation, which gives a cell's ditch depth to the whole cell "
                "(too deep and too wide on coarse cells). Velocity is "
                "the per-cell maximum when the file has one, else the maximum face velocities "
                "averaged onto the cells. Arrival is the first time the depth reaches the "
                "arrival depth, from the water surface time series, counted from the "
                "'time of breach initiation' you give (hours after the start of the HEC-RAS "
                "run). Cells are sampled by nearest cell centre onto the grid of the 'like' "
                "raster (or of the dam folder's Depth (Max).tif), else onto a grid you set, "
                "else onto one at the median cell spacing. Coordinates must be in the "
                "same system as the like raster. Needs h5py in QGIS's Python. Tick 'compare' "
                "to run the benchmark against the dam folder's rasters. The layout of HDF "
                "files varies between HEC-RAS versions and was not tested against real "
                "files: if it cannot find the datasets it lists what the file contains.")

    def initAlgorithm(self, config=None):
        self._file("HDF", "HEC-RAS 2D plan results (.p##.hdf)", "hdf", optional=False)
        self._file("DAM_FOLDER", "Dam folder (its Depth (Max).tif sets the grid; outputs "
                   "go to <dam folder>/_hecras)", folder=True)
        self.addParameter(QgsProcessingParameterRasterLayer(
            "TERRAIN", "Terrain the HEC-RAS model used (needed for the per-pixel depth "
            "basis; with the cell-minimum basis it is only compared)", optional=True))
        # 3.10.84: cell minimum is the default again; per pixel stays selectable
        self.addParameter(QgsProcessingParameterEnum(
            "DEPTH_BASIS", "Depth basis (wet area and arrival follow it)", DEPTH_BASIS_OPTIONS,
            defaultValue=_depth_basis_default()))
        self.addParameter(QgsProcessingParameterRasterLayer(
            "LIKE", "Grid of this raster (empty = the dam folder's, else set below)",
            optional=True))
        self._num("CELL", "Cell size when there is no raster to match (in the plan's CRS "
                  "units, usually US ft or m; 0 = median cell spacing)", 0.0, minimum=0.0)
        self.addParameter(QgsProcessingParameterExtent(
            "EXTENT", "Extent when there is no raster to match (empty = the 2D area)",
            optional=True))
        self._text("AREA", "2D flow area name (empty = the largest)")
        self.addParameter(QgsProcessingParameterEnum("UNITS", "HEC-RAS model units", US_SI,
                                                     defaultValue=0))
        self._num("ARRIVAL_FT", "Arrival depth (ft; starting value 0.1 is the engineer's: the "
                  "ND hazard 'any depth', so every wet structure gets an arrival time)",
                  ARRIVAL_DEPTH_FT,
                  minimum=0.01)
        self._num("T0_HR", "Time of breach initiation in the HEC-RAS run (hr after its start)",
                  0.0, minimum=0.0)
        self.addParameter(QgsProcessingParameterBoolean(
            "COMPARE", "Compare with the dam folder's rasters (benchmark)", False))
        self._num("THRESH_FT", "Benchmark: wet where the depth is at least (ft; starting "
                  "value 1 is the engineer's, a screening value you choose)", 1.0,
                  minimum=0.01)
        self._file("OUT_FOLDER", "Output folder (empty = <dam folder>/_hecras)", folder=True)
        self.addOutput(QgsProcessingOutputFolder("FOLDER", "Output folder"))
        self.addOutput(QgsProcessingOutputFile("DEPTH", "HECRAS Depth (Max)"))
        self.addOutput(QgsProcessingOutputFile("SUMMARY", "Benchmark_Summary.csv"))
        self.addOutput(QgsProcessingOutputString("RESULT", "Result"))

    def processAlgorithm(self, parameters, context, feedback):
        self._need_gdal()
        hdf = self.parameterAsFile(parameters, "HDF", context)
        dam_folder = self._fileparam(parameters, "DAM_FOLDER", context)
        out_dir = self._fileparam(parameters, "OUT_FOLDER", context) or (
            os.path.join(dam_folder, "_hecras") if dam_folder else "")
        if not out_dir:
            raise QgsProcessingException("Give a dam folder or an output folder")
        like = self.parameterAsRasterLayer(parameters, "LIKE", context)
        depth_path = os.path.join(dam_folder, workflow.FINAL_NAMES["depth"]) if dam_folder \
            else ""
        compare = self.parameterAsBool(parameters, "COMPARE", context)
        if compare and not os.path.exists(depth_path):
            raise QgsProcessingException("To compare, the dam folder needs '{}'".format(
                workflow.FINAL_NAMES["depth"]))
        like_grid = None
        try:
            if like is not None:
                like_grid = gdal_io.read_grid(like.source())
            elif os.path.exists(depth_path):
                like_grid = gdal_io.read_grid(depth_path)
            kw = {"units": (None, "us", "si")[self.parameterAsEnum(parameters, "UNITS",
                                                                     context)],
                  "arrival_depth_ft": self.parameterAsDouble(parameters, "ARRIVAL_FT", context),
                  "t0_hr": self.parameterAsDouble(parameters, "T0_HR", context)}
            mesh = hecras.read_mesh(hdf, area=self._str(parameters, "AREA", context) or None,
                                    **kw)
            if like_grid is not None:
                x0, ytop, dx, nx, ny = (like_grid.x0, like_grid.y0, like_grid.dx,
                                        like_grid.nx, like_grid.ny)
            else:
                ext = None
                if parameters.get("EXTENT"):
                    r = self.parameterAsExtent(parameters, "EXTENT", context)
                    if r is not None and not r.isNull():
                        ext = (r.xMinimum(), r.yMinimum(), r.xMaximum(), r.yMaximum())
                x0, ytop, dx, nx, ny = hecras.grid_spec(
                    mesh, self._opt(parameters, "CELL", context) or None, ext)
            crs = (like_grid.crs_wkt if like_grid is not None else None) or mesh.crs_wkt
            if not crs:
                raise QgsProcessingException(
                    "The HDF file records no projection; give a raster to match so the "
                    "rasters can be georeferenced.")
            from .core.grid import Grid
            grid = Grid(np.zeros((ny, nx)), x0, ytop, dx, crs_wkt=crs)
            terrain = self.parameterAsRasterLayer(parameters, "TERRAIN", context)
            basis = DEPTH_BASIS_KEYS[self.parameterAsEnum(parameters, "DEPTH_BASIS", context)]
            if basis == hecras.DEPTH_BASIS_TERRAIN and terrain is None:
                raise QgsProcessingException("The per-pixel terrain depth basis needs the "
                                             "Terrain the HEC-RAS model used")
            # 3.10.84: depth, wet area and arrival on the chosen basis (cell minimum default)
            t_ft = None
            if terrain is not None:
                from .core import hecras_engine
                t_ft = hecras_engine.terrain_on_grid(terrain.source(), grid)
            arrays, tinfo = hecras.terrain_depth(
                hdf, x0, ytop, dx, nx, ny, t_ft,
                area=self._str(parameters, "AREA", context) or None, basis=basis, **kw)
            tinfo.pop("depth_cell_min", None)
            tinfo.pop("depth_terrain", None)
            tinfo["notes"] = list(tinfo.get("notes") or []) + [
                "Depth basis: " + hecras.DEPTH_BASIS_TEXT[basis] + "."]
            mesh.info = tinfo
        except (hecras.HecRasError, OSError, ValueError, RuntimeError) as e:
            raise QgsProcessingException(str(e))
        writer = gdal_io.GeoTiffWriter(crs)
        os.makedirs(out_dir, exist_ok=True)
        paths = {}
        for k, a in arrays.items():
            paths[k] = os.path.join(out_dir, IMPORT_NAMES[k])
            writer(grid.like(a, np.nan), paths[k])
            self.load(context, paths[k], IMPORT_NAMES[k][:-4])
        info = mesh.info
        feedback.pushInfo("Area '{}', {:,} cells, units read as {}; grid {} x {} at {:g}.".format(
            info["area"], info["n_cells"], info["units"], nx, ny, dx))
        feedback.pushInfo("Used: " + "; ".join("{}: {}".format(k, v) for k, v
                                               in info["used"].items()))
        for n in info["notes"]:
            feedback.reportError("Check: " + n, fatalError=False)
        summary, msg = "", "wrote {}".format(", ".join(sorted(arrays)))
        if compare:
            try:
                summary, verdict = self._compare(parameters, context, dam_folder, grid, arrays,
                                                 out_dir, writer, feedback)
            except (ValueError, RuntimeError, OSError) as e:
                raise QgsProcessingException(str(e))
            msg += "; " + verdict
        return {"FOLDER": out_dir, "DEPTH": paths.get("depth", ""), "SUMMARY": summary,
                "RESULT": msg}

    def _compare(self, parameters, context, dam_folder, grid, arrays, out_dir, writer,
                 feedback):
        """This tool's rasters in the dam folder against the imported ones (same grid)."""
        names = workflow.FINAL_NAMES
        ours, ref = {}, {}
        for kind in ("depth", "velocity", "arrival", "wse"):
            p = os.path.join(dam_folder, names[kind])
            if os.path.exists(p) and kind in arrays:
                ours[kind] = gdal_io.read_like(p, grid, "near")
                ref[kind] = arrays[kind]
        unit = gdal_io.linear_unit_m(grid.crs_wkt) or 1.0
        cell_ac = (grid.dx * unit) ** 2 / 4046.8564224
        thr = self.parameterAsDouble(parameters, "THRESH_FT", context)
        m = benchmark.compare_fields(ours, ref, cell_ac, thr)
        r = benchmark.write_outputs(os.path.join(out_dir, "benchmark"), m,
                                    benchmark.difference_arrays(ours, ref, thr), writer, grid,
                                    "Benchmark against HEC-RAS")
        feedback.pushInfo(m["verdict"])
        for p in r["rasters"].values():
            self.load(context, p, os.path.basename(p)[:-4])
        return r["csv"], m["verdict"]


HECRAS_ALGORITHMS = (HecRasExportAlgorithm, HecRasImportAlgorithm)
