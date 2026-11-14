# -*- coding: utf-8 -*-
"""2.25.3: Processing algorithm ndhazard:creepschedule - "Schedule the hazard creep watch"
(watch_schedule.py). Registered by processing_provider.Provider. Puts ndhazard:creepwatch
on the computer's own scheduler (launchd, a systemd timer, the Windows Task Scheduler) for
one folder of dams, or removes that schedule. Enum spellings come from processing_provider
(compat-safe for QGIS 3.28 - 4.x)."""

import os

from qgis.core import (
    QgsProcessingException, QgsProcessingParameterFile, QgsProcessingParameterEnum,
    QgsProcessingParameterNumber, QgsProcessingParameterBoolean, QgsProcessingOutputFile,
    QgsProcessingOutputString,
)

from .processing_provider import _Base, FILE_FOLDER, NUM_INTEGER, WT_TIMEOUT_MIN

ACTIONS = ["Schedule it", "Only write the script and show the command", "Remove the schedule"]
EVERY_LABELS = ["Once a year", "Every 3 months", "Every month"]


def session_profile_dir():
    """The profile folder of this QGIS session ('' outside QGIS)."""
    try:
        from qgis.core import QgsApplication
        return QgsApplication.qgisSettingsDirPath() or ""
    except Exception:                                             # noqa: BLE001
        return ""


def pack_env():
    """The offline data pack choice of this session, for the scheduled run (which may open
    QGIS without it)."""
    try:
        from .download import offline_config, ENV_PACK, ENV_ONLY
        path, only = offline_config()
        return {ENV_PACK: path or "", ENV_ONLY: "1" if only else "0"} if path else {}
    except Exception:                                             # noqa: BLE001
        return {}


class CreepScheduleAlgorithm(_Base):
    """Puts the hazard creep watch of one folder on the computer's scheduler."""

    def name(self):
        return "creepschedule"

    def displayName(self):
        return "Schedule the hazard creep watch"

    def shortHelpString(self):
        return ("Runs 'Hazard creep watch' for this folder of dams by itself: once a year (or "
                "every 3 months, or every month), on the day and hour you choose. Each run "
                "re-classifies every dam with fresh data and writes "
                "Hazard_Creep_Changed_Dams_<date>.html in the folder: one page with only the "
                "dams that changed. Nothing is decided for you; it is the list of dams to "
                "look at.\n\n"
                "The schedule belongs to this computer and this user: a launchd agent on "
                "macOS, a systemd timer on Linux, a task in the Task Scheduler on Windows. "
                "The computer has to be on (macOS runs a missed watch when it wakes, Linux at "
                "the next start). The watch opens the QGIS profile this tool was run from, so "
                "run it from the profile that has the suite. A small script and the log of "
                "every run are kept in <folder>/_creep_watch/.\n\n"
                "Run the tool again to change the schedule, or choose 'Remove the schedule'. "
                "'Only write the script' schedules nothing and shows the command, for an "
                "administrator to register. One schedule per folder.")

    def initAlgorithm(self, config=None):
        P = self.addParameter
        P(QgsProcessingParameterFile("PARENT", "Folder containing the dam folders",
                                     behavior=FILE_FOLDER))
        P(QgsProcessingParameterEnum("ACTION", "What to do", options=list(ACTIONS),
                                     defaultValue=0))
        P(QgsProcessingParameterEnum("EVERY", "How often", options=list(EVERY_LABELS),
                                     defaultValue=0))
        P(QgsProcessingParameterNumber("MONTH", "Month of the (first) run (1 = January)",
                                       type=NUM_INTEGER, defaultValue=1, minValue=1,
                                       maxValue=12))
        P(QgsProcessingParameterNumber("DAY", "Day of the month (1 to 28)", type=NUM_INTEGER,
                                       defaultValue=15, minValue=1, maxValue=28))
        P(QgsProcessingParameterNumber("HOUR", "Hour of the day (0 to 23)", type=NUM_INTEGER,
                                       defaultValue=6, minValue=0, maxValue=23))
        P(QgsProcessingParameterBoolean("DOWNLOADS", "Use the internet downloads (or the "
                                        "offline data pack, if one is chosen)",
                                        defaultValue=True))
        P(QgsProcessingParameterNumber("WORKERS", "Dams classified at the same time",
                                       type=NUM_INTEGER, defaultValue=2, minValue=1,
                                       maxValue=16))
        P(QgsProcessingParameterNumber("TIMEOUT_MIN", "Time limit per dam (minutes, 0 = none)",
                                       type=NUM_INTEGER, defaultValue=int(WT_TIMEOUT_MIN),
                                       minValue=0, optional=True))
        self.addOutput(QgsProcessingOutputFile("SCRIPT", "The script the scheduler starts"))
        self.addOutput(QgsProcessingOutputFile("LOG", "Log of every scheduled run"))
        self.addOutput(QgsProcessingOutputString("WHEN", "When the watch runs"))
        self.addOutput(QgsProcessingOutputString("SUMMARY", "What was done"))

    def processAlgorithm(self, parameters, context, feedback):
        from . import watch as WT
        from . import watch_schedule as WS
        parent = self.parameterAsFile(parameters, "PARENT", context)
        if not parent or not os.path.isdir(parent):
            raise QgsProcessingException("Folder not found: {}".format(parent or "(none)"))
        action = self._int(parameters, "ACTION", context, 0)
        try:
            if action == 2:
                lines = WS.remove(parent)
                for ln in lines:
                    feedback.pushInfo(ln)
                return {"SCRIPT": "", "LOG": os.path.join(WS.work_folder(parent), WS.LOG_NAME),
                        "WHEN": "", "SUMMARY": lines[0]}
            exe, why = WT.find_qgis_process()
            if not exe:
                raise WS.ScheduleError(
                    "qgis_process was not found ({}). Set {} to its full path.".format(
                        why or "not in this QGIS installation", WT.ENV_QGIS_PROCESS))
            every = WS.EVERY[max(0, min(len(WS.EVERY) - 1,
                                        self._int(parameters, "EVERY", context, 0)))]
            p = WS.plan(parent, exe, settings_dir=session_profile_dir(), every=every,
                        month=self._int(parameters, "MONTH", context, 1),
                        day=self._int(parameters, "DAY", context, 15),
                        hour=self._int(parameters, "HOUR", context, 6),
                        workers=self._int(parameters, "WORKERS", context, 2),
                        timeout_min=self._int(parameters, "TIMEOUT_MIN", context,
                                              WT_TIMEOUT_MIN),
                        downloads=self.parameterAsBool(parameters, "DOWNLOADS", context),
                        env=pack_env())
            lines = WS.apply(p, register=(action == 0))
        except WS.ScheduleError as e:
            raise QgsProcessingException(str(e)) from e
        n = len(WT.dam_folders(parent))
        feedback.pushInfo("{} dam folder(s) in {} today.".format(n, parent))
        for ln in lines:
            feedback.pushInfo(ln)
        done = "scheduled" if action == 0 and not p["manual"] else "script written, not scheduled"
        return {"SCRIPT": p["wrapper"], "LOG": p["log"], "WHEN": p["when"],
                "SUMMARY": "{}: {}".format(done, p["when"])}
