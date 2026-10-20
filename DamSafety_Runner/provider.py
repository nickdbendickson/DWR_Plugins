"""Processing provider "ND Dam Breach Runner" (id ndbreach)."""
import os

from qgis.core import QgsProcessingProvider
from qgis.PyQt.QtGui import QIcon

from .algorithms import (ALGORITHMS as ORIGINAL_ALGORITHMS, PROVIDER_ID, SET_ANUGA, SET_BUILTIN,
                         SET_LISFLOOD, SET_THREADS)
from .core import nd_data
from . import popup_logic as pl
from . import suite_settings_tools as ST


def _tool_name(cls):
    try:
        return cls().name()
    except Exception:                                            # noqa
        return ""


# 3.5: every tool takes its parameter defaults from the suite settings file (suite_settings_tools)
ALGORITHMS = tuple(ST.settings_aware(c, pl.PLUGIN_KEY, pl.settings_group) for c in ORIGINAL_ALGORITHMS)

ICON = os.path.join(os.path.dirname(__file__), "icon.svg")


class Provider(QgsProcessingProvider):
    def id(self):
        return PROVIDER_ID

    def name(self):
        return "DamSafety Runner"

    def longName(self):
        return "DamSafety Runner (LISFLOOD-FP / ANUGA)"

    def icon(self):
        return QIcon(ICON) if os.path.exists(ICON) else QgsProcessingProvider.icon(self)

    def load(self):
        try:
            from processing.core.ProcessingConfig import ProcessingConfig, Setting
            ProcessingConfig.settingIcons[self.name()] = self.icon()
            # first-run defaults from the usual install places (no subprocesses at startup)
            lf = (nd_data.lisflood_candidates() or [""])[0]
            an = (nd_data.anuga_python_candidates() or [""])[0]
            ProcessingConfig.addSetting(Setting(
                self.name(), SET_LISFLOOD, "LISFLOOD-FP executable (built with the "
                "arrival-depth patch)", lf, valuetype=Setting.FILE))
            ProcessingConfig.addSetting(Setting(
                self.name(), SET_ANUGA, "Python interpreter of the ANUGA conda environment "
                "(e.g. ~/miniforge3/envs/anuga/bin/python)", an, valuetype=Setting.FILE))
            ProcessingConfig.addSetting(Setting(
                self.name(), SET_THREADS, "Threads for the models (0 = all cores)", 0,
                valuetype=Setting.INT))
            ProcessingConfig.addSetting(Setting(
                self.name(), SET_BUILTIN, "Use the built-in screening engine when no "
                "LISFLOOD-FP is set (a numpy local-inertial approximation, NOT LISFLOOD-FP; "
                "off by default so a run errors rather than silently using the approximation)",
                False, valuetype=Setting.BOOL))
            ProcessingConfig.readSettings()
        except Exception:   # settings are optional; parameters can carry the paths
            pass
        self.refreshAlgorithms()
        return True

    def unload(self):
        try:
            from processing.core.ProcessingConfig import ProcessingConfig
            for k in (SET_LISFLOOD, SET_ANUGA, SET_THREADS, SET_BUILTIN):
                ProcessingConfig.removeSetting(k)
        except Exception:
            pass

    def loadAlgorithms(self):
        for cls in ALGORITHMS:
            self.addAlgorithm(cls())
