"""ndpmp:export_hms -> core.hms (same loss/transform parameters as ndpmp:inflow_hydrograph)."""
import os
from qgis.core import (QgsProcessingParameterBoolean, QgsProcessingParameterFile, QgsProcessingParameterString,
                       QgsProcessingException, QgsProcessingOutputFile, QgsSettings)
from .algorithm_hydro import InflowHydrographAlgorithm

SETTING_HMS = "nd_pmp/hms_home"


class ExportHMSAlgorithm(InflowHydrographAlgorithm):
    def name(self): return "export_hms"
    def displayName(self): return "Export HEC-HMS project (optionally run it headless)"
    def createInstance(self): return ExportHMSAlgorithm()
    def shortHelpString(self):
        return ("Writes a HEC-HMS 4.13 project (.hms/.basin/.met/.control/.gage/.run + run_hms.py) with one subbasin "
                "-> sink and one met/control/run per hyetograph. run_hms.py (Jython, hec-hms.sh -s) writes the "
                "precipitation to the project DSS7 file, computes all runs and writes hms_peaks.csv. Exported: SCS CN / "
                "Initial+Constant, SCS UH (STANDARD/PRFnnn) / Clark, no baseflow; pothole abstraction -> Simple Surface "
                "(different ordering, see README); cascade not exported. INTERNAL PROTOTYPE.")

    def initAlgorithm(self, config=None):
        super().initAlgorithm(config)
        self.addParameter(QgsProcessingParameterString("PROJECT", "HMS project name", "NDPMP"))
        self.addParameter(QgsProcessingParameterBoolean("RUN", "Run with HEC-HMS now", False))
        self.addParameter(QgsProcessingParameterFile("HMS_HOME", "HEC-HMS folder (contains hec-hms.sh)",
                                                     QgsProcessingParameterFile.Folder, optional=True,
                                                     defaultValue=QgsSettings().value(SETTING_HMS, "")))
        self.addOutput(QgsProcessingOutputFile("HMS_PROJECT", ".hms file"))
        self.addOutput(QgsProcessingOutputFile("HMS_PEAKS", "hms_peaks.csv"))

    def processAlgorithm(self, p, context, feedback):
        from .core.hms import export_hms, run_hms
        files, cfg = self._files_cfg(p, context)
        out = self.parameterAsString(p, "OUTPUT_FOLDER", context)
        try:
            ex = export_hms(files, cfg, out, project=self.parameterAsString(p, "PROJECT", context) or "NDPMP",
                            log=feedback.pushWarning)
        except Exception as e:
            raise QgsProcessingException(str(e))
        res = {"OUTPUT_FOLDER": out, "HMS_PROJECT": ex["project"], "HMS_PEAKS": ""}
        if self.parameterAsBool(p, "RUN", context):
            home = self.parameterAsFile(p, "HMS_HOME", context)
            if not home or not os.path.exists(os.path.join(home, "hec-hms.sh")):
                raise QgsProcessingException("HMS_HOME must contain hec-hms.sh")
            QgsSettings().setValue(SETTING_HMS, home)
            rc, so, se, peaks = run_hms(out, home)
            for r in peaks:
                feedback.pushInfo("%s: HMS peak %s cfs at %s h (%s)" % (r["storm_id"], r["hms_peak_cfs"],
                                                                        r["hms_time_to_peak_hr"], r["status"]))
            if rc != 0:
                feedback.reportError("hec-hms.sh exit %s: %s" % (rc, se[-800:]))
            res["HMS_PEAKS"] = os.path.join(out, "hms_peaks.csv")
        return res
