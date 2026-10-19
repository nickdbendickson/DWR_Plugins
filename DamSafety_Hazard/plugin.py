# -*- coding: utf-8 -*-
"""
Plugin registration: Toolbar/menu wiring and dialog lifecycle.
This file connects QGIS to the NDDamHazardDialog.
"""

import os
from qgis.PyQt.QtCore import QObject
from qgis.PyQt.QtGui import QIcon

try:                                    # Qt6 (QGIS 4): QAction lives in QtGui
    from qgis.PyQt.QtGui import QAction
except ImportError:                     # Qt5 (QGIS 3.x): QtWidgets
    from qgis.PyQt.QtWidgets import QAction

try:
    from qgis.PyQt import sip
except ImportError:  # pragma: no cover - very old builds
    import sip

from .constants import PLUGIN_NAME


class NDdamHazardHelperPlugin(QObject):
    """Main plugin class registered by __init__.py."""

    def __init__(self, iface):
        """
        Initialize the plugin.
        :param iface: QGIS interface object.
        """
        super().__init__()
        self.iface = iface
        self.action = None
        self.dialog = None
        self.home_action = None
        self.home_dialog = None
        self.provider = None
        self._versions_checked = False

    def initProcessing(self):
        """Register the Processing provider (toolbox, models, qgis_process).
        A failure here must not stop the plugin's menu entry. Called by QGIS
        (hasProcessingProvider=yes) and by initGui - registered only once."""
        if self.provider is not None:
            return
        try:
            from qgis.core import QgsApplication
            from .processing_provider import Provider
            self.provider = Provider()
            QgsApplication.processingRegistry().addProvider(self.provider)
        except Exception as e:           # pragma: no cover - depends on QGIS build
            self.provider = None
            try:
                from qgis.core import QgsMessageLog
                QgsMessageLog.logMessage("Processing provider not available: {}".format(e),
                                         PLUGIN_NAME)
            except Exception:
                pass

    def initGui(self):
        """
        Create the action that will be displayed in the menu/toolbar.
        Called when the plugin is enabled.
        """
        self.initProcessing()
        # Try to load an icon; if missing, use a blank icon
        icon_path = os.path.join(os.path.dirname(__file__), "icon.png")
        icon = QIcon(icon_path) if os.path.exists(icon_path) else QIcon()

        self.action = QAction(icon, "Classify\u2026", self.iface.mainWindow())
        self.action.setObjectName("NDDamHazardHelperAction")
        self.action.triggered.connect(self.run_plugin)

        # Add to Plugins Menu
        self.iface.addPluginToMenu("Dam safety", self.action)
        # v2.12: the Dam suite home (the same window in all three plugins)
        self.home_action = QAction(icon, "Dam suite home...", self.iface.mainWindow())
        self.home_action.setObjectName("NDDamHazardSuiteHomeAction")
        self.home_action.triggered.connect(self.run_home)
        self.iface.addPluginToMenu("Dam safety", self.home_action)

        # Optional: Add to a custom toolbar (uncomment if you want a dedicated toolbar)
        # self.iface.addToolBarIcon(self.action)

        # v2.10: the other dam-suite plugins' versions, once QGIS has loaded every plugin
        try:
            self.iface.initializationCompleted.connect(self.check_versions)
        except Exception:
            pass
        try:
            from qgis.PyQt.QtCore import QTimer
            QTimer.singleShot(0, self.check_versions)
        except Exception:
            pass

    def check_versions(self, versions=None):
        """v2.10: warn (message log and bar) when ND Dam Breach Runner or ND Dam Breach
        Screening is installed in a version this release does not work with (damsuite
        versions table). Runs once; never raises. Returns the messages."""
        if self._versions_checked and versions is None:
            return []
        self._versions_checked = True
        try:
            from .damsuite import versions as VS
            msgs = VS.check("ndhazard", versions=versions)
            lines = VS.report_lines("ndhazard", versions=versions)
        except Exception as e:
            try:
                from .helpers import log_message
                log_message("Suite version check skipped: the version table could not be "
                            "loaded ({})".format(e))
            except Exception:
                pass
            return []
        try:
            from .helpers import log_message
            from .compat import MSG_INFO, MSG_WARNING, MSG_CRITICAL
            for line in lines:
                log_message("Dam suite: " + line, MSG_INFO)
            for level, text in msgs:
                log_message(text, MSG_CRITICAL if level == "error" else MSG_WARNING)
            bar = self.iface.messageBar() if self.iface is not None else None
            for level, text in msgs:
                if bar is not None:
                    bar.pushMessage(PLUGIN_NAME, text,
                                    level=MSG_CRITICAL if level == "error" else MSG_WARNING,
                                    duration=0 if level == "error" else 15)
        except Exception:
            pass
        return msgs

    def _dialog_alive(self):
        if self.dialog is None:
            return False
        try:
            return not sip.isdeleted(self.dialog)
        except Exception:
            return False

    def run_settings(self, *args):
        """Open the window and its Suite settings window (v2.13 a tab; 2.18.34 the gear
        menu's non-modal window)."""
        self.run_plugin()
        try:
            self.dialog._open_suite_settings()
        except Exception:                                        # noqa
            pass

    def run_home(self, *args):
        """v2.12: the Dam suite home window (suite_home.py, the same in all three plugins)."""
        from . import suite_home
        return suite_home.open_home(self, self.iface, "ndhazard", self.iface.mainWindow())

    def run_plugin(self):
        """
        Open the main dialog when the action is triggered.
        Ensures only one instance of the dialog is open at a time, and
        recreates it if Qt has already destroyed it.
        """
        if not self._dialog_alive():
            # Imported lazily so a missing optional dependency cannot stop
            # QGIS from loading the plugin menu entry.
            from .dialog import NDDamHazardDialog
            self.dialog = NDDamHazardDialog(parent=self.iface.mainWindow())

        self.dialog.show()
        self.dialog.raise_()
        self.dialog.activateWindow()
        return self.dialog

    def unload(self):
        """
        Remove the plugin action from the menu/toolbar and free the dialog.
        Called when the plugin is disabled, reloaded, or QGIS closes.
        Safe to call more than once.
        """
        try:
            self.iface.initializationCompleted.disconnect(self.check_versions)
        except Exception:
            pass
        try:                                                    # v2.13: the shared suite menu
            from . import suite_home
            suite_home.remove_suite_actions(self.iface, getattr(self, "suite_actions", []))
        except Exception:
            pass
        self.suite_actions = []
        # v2.6.1: requests of a cancelled download may still be running -
        # wait for them before Python lets their thread pools go
        try:
            from .download import wait_for_parked_pools
            wait_for_parked_pools()
        except Exception:
            pass
        if self.action is not None:
            try:
                self.iface.removePluginMenu("Dam safety", self.action)
                # self.iface.removeToolBarIcon(self.action) # Uncomment if toolbar icon added
                self.action.triggered.disconnect(self.run_plugin)
            except (RuntimeError, TypeError):
                pass
            self.action.deleteLater()
            self.action = None
        if self.home_action is not None:
            try:
                self.iface.removePluginMenu("Dam safety", self.home_action)
                self.home_action.triggered.disconnect(self.run_home)
            except (RuntimeError, TypeError, AttributeError):
                pass
            self.home_action.deleteLater()
            self.home_action = None
        if self.home_dialog is not None:
            try:
                self.home_dialog.close()
                self.home_dialog.deleteLater()
            except Exception:
                pass
            self.home_dialog = None

        if self.provider is not None:
            try:
                from qgis.core import QgsApplication
                QgsApplication.processingRegistry().removeProvider(self.provider)
            except Exception:
                pass
            self.provider = None

        if self._dialog_alive():
            if getattr(self.dialog, "_busy", False):
                # v2.3.1: defer deletion until run completes
                self.dialog.hide()
                from qgis.PyQt.QtCore import QTimer
                QTimer.singleShot(5000, self.unload)
                return
            try:
                self.dialog.close()
                self.dialog.deleteLater()
            except RuntimeError:
                pass
        self.dialog = None
