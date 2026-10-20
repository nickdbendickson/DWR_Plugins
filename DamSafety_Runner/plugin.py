"""QGIS plugin entry: registers the Processing provider and adds a menu.

v3.3.0: Plugins > ND Dam Breach Runner > "Open the Runner window..." opens a popup window
(dialog.py): the Dams folder and dam, what is in the dam's folder, and every tool below on tabs.
The menu entries stay.

v3.2.0: once QGIS has loaded its plugins (a zero-delay timer after initGui), the versions of
the other ND dam suite plugins are checked (damsuite.versions.check("ndbreach")); problems go
to the message bar and the "ND Dam Breach Runner" log. The plugin loads and works without ND
Dam Hazard or ND Dam Breach Screening installed."""
from qgis.core import QgsApplication

from .provider import Provider

MENU = "&Dam safety"
LOG_TAG = "ND Dam Breach Runner"


def version_messages(versions=None):
    """[(level, text)] of damsuite.versions.check("ndbreach") ("error" below a hard minimum,
    "warning" below a soft one), plus the one-line summary per suite plugin at level "info".
    `versions` ({key: version}) replaces the QGIS lookup (tests). Never raises."""
    try:
        from .damsuite import versions as dv
        out = list(dv.check("ndbreach", versions))
        out += [("info", ln) for ln in dv.report_lines("ndbreach", versions)]
        return out
    except Exception as e:           # a version check must never stop the plugin loading
        return [("info", "Suite version check skipped: {}".format(e))]



def _plugin_icon():
    """The plugin's icon.svg for its menu entry (every DamSafety plugin has one)."""
    import os
    from qgis.PyQt.QtGui import QIcon
    return QIcon(os.path.join(os.path.dirname(os.path.abspath(__file__)), "icon.svg"))

class NDBreachRunnerPlugin:
    def __init__(self, iface):
        self.iface = iface
        self.provider = None
        self.actions = []
        self.suite_actions = []           # 3.5: entries in the shared Plugins > ND Dam Suite menu
        self.action = None
        self.dialog = None
        self.home_action = None
        self.home_dialog = None

    def initProcessing(self):
        """Register the Processing provider once. QGIS / qgis_process call this for a
        hasProcessingProvider=yes plugin and initGui calls it too: 3.4.1 keeps the first
        provider (a second one was refused by the registry but replaced self.provider, so
        unload removed the wrong object and left the first registered - a crash risk on
        plugin reload)."""
        if self.provider is not None:
            return
        self.provider = Provider()
        QgsApplication.processingRegistry().addProvider(self.provider)

    def initGui(self):
        self.initProcessing()
        self._schedule_version_check()
        try:
            from qgis.PyQt.QtWidgets import QAction
        except ImportError:  # Qt6 moved QAction
            from qgis.PyQt.QtGui import QAction
        self.action = QAction(_plugin_icon(), "Run a dam\u2026", self.iface.mainWindow())
        self.action.setObjectName("NDBreachRunnerAction")
        self.action.triggered.connect(self.run_plugin)
        self.iface.addPluginToMenu(MENU, self.action)
        self.actions.append(self.action)

    def run_settings(self, *args):
        """Open the window and its Settings window (the suite settings grid)."""
        self.run_plugin()
        try:
            self.dialog._open_settings()
        except Exception:                                        # noqa
            pass

    def _schedule_version_check(self):
        """After QGIS has loaded every plugin (the event loop runs the timer), not now: ND
        Dam Hazard may load after this plugin."""
        try:
            from qgis.PyQt.QtCore import QTimer
            QTimer.singleShot(0, self.check_versions)
        except Exception:
            self.check_versions()

    def check_versions(self, versions=None):
        """Show the suite version problems in the message bar and the log. Returns them."""
        msgs = version_messages(versions)
        try:
            from qgis.core import QgsMessageLog
            from .compat import MSG_INFO, MSG_WARNING, MSG_CRITICAL
        except Exception:
            return msgs
        level = {"error": MSG_CRITICAL, "warning": MSG_WARNING, "info": MSG_INFO}
        for kind, text in msgs:
            try:
                QgsMessageLog.logMessage(text, LOG_TAG, level.get(kind, MSG_INFO))
            except Exception:
                pass
            if kind in ("error", "warning"):
                try:
                    self.iface.messageBar().pushMessage(LOG_TAG, text, level[kind],
                                                        0 if kind == "error" else 15)
                except Exception:
                    pass
        return msgs

    def _dialog_alive(self):
        if self.dialog is None:
            return False
        try:
            try:
                from qgis.PyQt import sip
            except ImportError:           # pragma: no cover - very old builds
                import sip
            return not sip.isdeleted(self.dialog)
        except Exception:
            return False

    def run_home(self, *args):
        """The Dam suite home window (suite_home.py, the same in all three plugins)."""
        from . import suite_home
        return suite_home.open_home(self, self.iface, "ndbreach", self.iface.mainWindow())

    def run_plugin(self, *args):
        """Open the window; one instance, re-created if Qt has destroyed it."""
        if not self._dialog_alive():
            # imported here so a problem in the window cannot stop the plugin from loading
            from .dialog import NDBreachRunnerDialog
            self.dialog = NDBreachRunnerDialog(parent=self.iface.mainWindow(), iface=self.iface)
        self.dialog.show()
        self.dialog.raise_()
        self.dialog.activateWindow()
        return self.dialog

    def _open(self, alg):
        if alg == "wizard:":
            from . import wizard
            wizard.run_wizard(self.iface)
            return
        import processing
        processing.execAlgorithmDialog(alg)

    def _settings(self):
        try:
            self.iface.showOptionsDialog(self.iface.mainWindow(), currentPage="processingOptions")
        except TypeError:
            self.iface.showOptionsDialog(self.iface.mainWindow())

    def unload(self):
        for a in self.actions:
            self.iface.removePluginMenu(MENU, a)
        self.actions = []
        try:
            from . import suite_home
            suite_home.remove_suite_actions(self.iface, self.suite_actions)
        except Exception:                                        # noqa
            pass
        self.suite_actions = []
        if self.action is not None:
            try:
                self.action.triggered.disconnect(self.run_plugin)
            except (RuntimeError, TypeError, AttributeError):
                pass
            self.action = None
        if self._dialog_alive():
            try:
                self.dialog.close()
                self.dialog.deleteLater()
            except RuntimeError:
                pass
        self.dialog = None
        if self.home_action is not None:
            try:
                self.home_action.triggered.disconnect(self.run_home)
            except (RuntimeError, TypeError, AttributeError):
                pass
            self.home_action = None
        if self.home_dialog is not None:
            try:
                self.home_dialog.close()
                self.home_dialog.deleteLater()
            except Exception:
                pass
            self.home_dialog = None
        if self.provider is not None:
            QgsApplication.processingRegistry().removeProvider(self.provider)
            self.provider = None
