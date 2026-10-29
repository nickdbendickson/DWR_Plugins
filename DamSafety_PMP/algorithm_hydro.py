"""Thin PyQGIS wrapper: ndpmp:inflow_hydrograph -> core.hydro."""
import glob
import os

from qgis.core import (QgsProcessingAlgorithm, QgsProcessingParameterFile, QgsProcessingParameterFolderDestination,
                       QgsProcessingParameterEnum, QgsProcessingParameterNumber, QgsProcessingParameterBoolean,
                       QgsProcessingParameterString, QgsProcessingException, QgsProcessingOutputFile,
                       QgsProcessingOutputFolder)

from .core.hydro import HydroConfig, run_folder
from .core import streamstats

D = QgsProcessingParameterNumber.Double


class InflowHydrographAlgorithm(QgsProcessingAlgorithm):
    def name(self): return "inflow_hydrograph"
    def displayName(self): return "Inflow hydrograph from PMP hyetographs"
    def createInstance(self): return InflowHydrographAlgorithm()
    def shortHelpString(self):
        return ("Reads nd_pmp hyetograph CSVs and computes inflow hydrographs (SCS CN or initial+constant losses; "
                "SCS or Clark UH; constant or recession baseflow). Output: hydrographs/<storm_id>.csv "
                "(time_hr, flow_cfs, precip_in, excess_in) + inflow_hydrograph_summary.csv. INTERNAL PROTOTYPE.<br><br>"
                "Basin inputs: Plugins ▸ DamSafety PMP ▸ Get basin data from StreamStats… fills the "
                "drainage area, a suggested non-contributing area and Tc / lag from USGS StreamStats, and sets "
                "'StreamStats basin values'. With that file set, the largest peak is compared with the ND 0.2% "
                "AEP regression peak and the Crippen & Bue envelope after the run (a warning in the log, "
                "never a block; streamstats_pmf_check.json). The curve number is never taken from StreamStats.")

    def initAlgorithm(self, config=None):
        A = self.addParameter
        A(QgsProcessingParameterFile("HYETOGRAPHS", "Hyetograph folder (or a single CSV)",
                                     behavior=QgsProcessingParameterFile.Folder))
        A(QgsProcessingParameterString("PATTERN", "File pattern inside folder", "*.csv"))
        A(QgsProcessingParameterFolderDestination("OUTPUT_FOLDER", "Output folder"))
        A(QgsProcessingParameterEnum("LOSS", "Loss method", ["SCS Curve Number", "Initial + constant"], defaultValue=0))
        A(QgsProcessingParameterNumber("CN", "Curve number (AMC II)", D, 80, minValue=1, maxValue=100))
        A(QgsProcessingParameterBoolean("AMC3", "Convert CN from AMC II to AMC III", False))
        A(QgsProcessingParameterNumber("IA_RATIO", "Initial abstraction ratio Ia/S", D, 0.2, minValue=0))
        A(QgsProcessingParameterNumber("INITIAL_IN", "Initial loss (in)", D, 1.0, minValue=0))
        A(QgsProcessingParameterNumber("CONSTANT_IN_HR", "Constant loss rate (in/hr)", D, 0.1, minValue=0))
        A(QgsProcessingParameterEnum("TRANSFORM", "Transform", ["SCS dimensionless UH", "Clark UH"], defaultValue=0))
        A(QgsProcessingParameterNumber("LAG_HR", "SCS lag (hr; 0 = use 0.6*Tc)", D, 0, minValue=0))
        A(QgsProcessingParameterNumber("TC_HR", "Time of concentration Tc (hr)", D, 0, minValue=0))
        A(QgsProcessingParameterNumber("PRF", "SCS peak rate factor", D, 484, minValue=100, maxValue=700))
        A(QgsProcessingParameterNumber("PEAKING_PCT", "PMF peaking adjustment (% increase of UH peak)", D, 0, minValue=0))
        A(QgsProcessingParameterNumber("CLARK_R_HR", "Clark storage coefficient R (hr)", D, 0, minValue=0))
        A(QgsProcessingParameterEnum("BASEFLOW", "Baseflow", ["Constant", "Recession"], defaultValue=0))
        A(QgsProcessingParameterNumber("BASEFLOW_CFS", "Baseflow / initial baseflow (cfs)", D, 0, minValue=0))
        A(QgsProcessingParameterNumber("RECESSION_K", "Recession constant (per day)", D, 0.9, minValue=0.0001, maxValue=1))
        A(QgsProcessingParameterNumber("AREA_MI2", "Drainage area override (mi2; 0 = from CSV header)", D, 0, minValue=0))
        A(QgsProcessingParameterNumber("NONCONTRIB_MI2", "Non-contributing area to subtract (mi2)", D, 0, minValue=0))
        A(QgsProcessingParameterEnum("POTHOLE_MODE", "Pothole storage", ["None", "Fill-and-spill cascade",
                                                                       "Storage abstraction (first X in of excess)"],
                                     defaultValue=0))
        A(QgsProcessingParameterFile("POTHOLE_NETWORK", "pothole_network.json (from pothole_storage)",
                                     extension="json", optional=True))
        A(QgsProcessingParameterNumber("POTHOLE_INITIAL_FULL", "Antecedent pothole fullness (0-1)", D, 0.0,
                                       minValue=0, maxValue=1))
        A(QgsProcessingParameterNumber("POTHOLE_STORAGE_IN", "Abstraction depth override (in; 0 = from network)", D, 0,
                                       minValue=0))
        A(QgsProcessingParameterFile("STREAMSTATS_JSON", "StreamStats basin values (*_streamstats_basin.json; "
                                     "optional: compare the PMF peak)", extension="json", optional=True))
        self.addOutput(QgsProcessingOutputFile("SUMMARY_CSV", "inflow_hydrograph_summary.csv"))
        self.addOutput(QgsProcessingOutputFolder("HYDROGRAPH_FOLDER", "Hydrograph folder"))
        self.addOutput(QgsProcessingOutputFile("STREAMSTATS_CHECK", "streamstats_pmf_check.json"))

    def _files_cfg(self, p, context):
        src = self.parameterAsFile(p, "HYETOGRAPHS", context)
        files = [src] if os.path.isfile(src) else glob.glob(os.path.join(src, self.parameterAsString(p, "PATTERN", context)))
        if not files:
            raise QgsProcessingException("No hyetograph CSVs found in %s" % src)
        num = lambda k: self.parameterAsDouble(p, k, context)
        cfg = HydroConfig(loss=["scs_cn", "init_const"][self.parameterAsEnum(p, "LOSS", context)],
                          cn=num("CN"), amc3=self.parameterAsBool(p, "AMC3", context), ia_ratio=num("IA_RATIO"),
                          initial_in=num("INITIAL_IN"), constant_in_hr=num("CONSTANT_IN_HR"),
                          transform=["scs", "clark"][self.parameterAsEnum(p, "TRANSFORM", context)],
                          lag_hr=num("LAG_HR") or None, tc_hr=num("TC_HR") or None, prf=num("PRF"),
                          peaking_pct=num("PEAKING_PCT"), clark_r_hr=num("CLARK_R_HR") or None,
                          baseflow=["constant", "recession"][self.parameterAsEnum(p, "BASEFLOW", context)],
                          baseflow_cfs=num("BASEFLOW_CFS"), recession_k=num("RECESSION_K"),
                          area_mi2=num("AREA_MI2") or None, noncontrib_mi2=num("NONCONTRIB_MI2"),
                          pothole_mode=["none", "cascade", "abstraction"][self.parameterAsEnum(p, "POTHOLE_MODE", context)],
                          pothole_network=self.parameterAsFile(p, "POTHOLE_NETWORK", context) or None,
                          pothole_initial_full=num("POTHOLE_INITIAL_FULL"),
                          pothole_storage_in=num("POTHOLE_STORAGE_IN") or None)
        return files, cfg

    def processAlgorithm(self, p, context, feedback):
        files, cfg = self._files_cfg(p, context)
        out = self.parameterAsString(p, "OUTPUT_FOLDER", context); os.makedirs(out, exist_ok=True)
        try:
            rows, sp = run_folder(files, out, cfg, log=feedback.pushInfo)
        except Exception as e:
            raise QgsProcessingException(str(e))
        res = {"OUTPUT_FOLDER": out, "SUMMARY_CSV": sp, "HYDROGRAPH_FOLDER": os.path.join(out, "hydrographs")}
        ssj = self.parameterAsFile(p, "STREAMSTATS_JSON", context)
        if ssj:
            try:
                msgs, written = streamstats.check_pmf_run(rows, ssj, out, cfg.area_mi2)
            except Exception as e:  # a check, never a reason to fail the run
                feedback.pushWarning("StreamStats check skipped: %s" % e)
            else:
                for level, text in msgs:
                    (feedback.pushWarning if level == "WARNING" else feedback.pushInfo)("StreamStats check: " + text)
                res["STREAMSTATS_CHECK"] = str(written["check"])
        return res
