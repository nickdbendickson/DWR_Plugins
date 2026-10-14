"""HEC-RAS interchange algorithms: export a breach to HEC-RAS, import 2D results as rasters.

Register with `HECRAS_ALGORITHMS` (see the end of the file)."""
import os

import numpy as np
from qgis.core import (QgsProcessingException, QgsProcessingOutputFile,
                       QgsProcessingOutputFolder, QgsProcessingOutputString,
                       QgsProcessingParameterBoolean, QgsProcessingParameterEnum,
                       QgsProcessingParameterExtent, QgsProcessingParameterRasterLayer)

from .algorithms_tools import _ToolBase
from .core import benchmark, gdal_io, hecras, workflow

STEPS = [(hecras.ras_interval_label(m), m) for m in hecras.RAS_INTERVALS_MIN]
STEP_DEFAULT = [m for _, m in STEPS].index(5)
IMPORT_NAMES = {"depth": "HECRAS Depth (Max).tif", "velocity": "HECRAS Velocity (Max).tif",
                "arrival": "HECRAS Arrival Time.tif", "wse": "HECRAS WSE (Max).tif",
                "time_of_max": "HECRAS Time of Max WSE.tif"}
US_SI = ["Detect from the file (US customary when it says nothing)", "US customary (ft)",
         "SI metric (m)"]


class HecRasExportAlgorithm(_ToolBase):
    def name(self):
        return "hecrasexport"

    def displayName(self):
        return "Export the breach to HEC-RAS (hydrograph and breach table)"

    def shortHelpString(self):
        return ("Reads a finished run's breach hydrograph and parameters from its dam folder "
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
        return {"HYDROGRAPH_TXT": r["hydrograph_txt"], "HYDROGRAPH_CSV": r["hydrograph_csv"],
                "BREACH_TABLE": r["table_csv"], "BREACH_TABLE_MD": r["table_md"],
                "RESULT": msg}


class HecRasImportAlgorithm(_ToolBase):
    def name(self):
        return "hecrasimport"

    def displayName(self):
        return "Import HEC-RAS 2D results as rasters"

    def shortHelpString(self):
        return ("Reads the maximum results of a HEC-RAS 2D plan HDF (.p##.hdf) and writes "
                "GeoTIFFs 'HECRAS Depth (Max).tif', 'HECRAS Velocity (Max).tif', 'HECRAS "
                "Arrival Time.tif' and 'HECRAS WSE (Max).tif' in ft, ft/s, hours and ft. Depth "
                "is the maximum water surface minus the cell's minimum elevation. Velocity is "
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
        self._num("ARRIVAL_FT", "Arrival depth (ft; starting value 1 is the engineer's, a "
                  "screening value you choose)", 1.0, minimum=0.01)
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
            arrays = hecras.rasterise(mesh, x0, ytop, dx, nx, ny)
        except (hecras.HecRasError, OSError, ValueError, RuntimeError) as e:
            raise QgsProcessingException(str(e))
        crs = (like_grid.crs_wkt if like_grid is not None else None) or mesh.crs_wkt
        if not crs:
            raise QgsProcessingException("The HDF file records no projection; give a raster to "
                                         "match so the rasters can be georeferenced.")
        from .core.grid import Grid
        grid = Grid(np.zeros((ny, nx)), x0, ytop, dx, crs_wkt=crs)
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
