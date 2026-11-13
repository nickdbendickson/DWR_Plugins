# -*- coding: utf-8 -*-
"""DamSafety Core: the one copy of the code the DamSafety plugins share.

    damsuite/   the shared package (dam records, curves, settings, packs, version checks ...)
    ui/         the shared window files (tokens, widgets, suite settings, home, styles)
    alias.py    `<Plugin>.damsuite` -> `DamSafety_Core.damsuite` (one module object per module)

    defaults.json  the shared defaults every tool starts from (damsuite/shared_defaults.py)

The other plugins list this one in plugin_dependencies. It adds an About entry and a Shared
defaults entry under the suite menu and nothing else; importing it has no side effects.
"""


def classFactory(iface):
    """QGIS plugin entry point."""
    return CorePlugin(iface)


MENU = "&Dam safety"


class CorePlugin(object):
    """A library plugin: QGIS needs an object with initGui / unload; the menu entry says what
    is installed, for a user who wonders what 'DamSafety Core' is."""

    def __init__(self, iface):
        self.iface = iface
        self.action = None
        self.defaults_action = None

    def initGui(self):
        try:
            try:
                from qgis.PyQt.QtWidgets import QAction
            except ImportError:                         # Qt6 moved QAction
                from qgis.PyQt.QtGui import QAction
            import os
            from qgis.PyQt.QtGui import QIcon
            icon = QIcon(os.path.join(os.path.dirname(os.path.abspath(__file__)), "icon.svg"))
            self.action = QAction(icon, "About DamSafety Core…", self.iface.mainWindow())
            self.action.setObjectName("DamSafetyCoreAboutAction")
            self.action.triggered.connect(self.about)
            self.iface.addPluginToMenu(MENU, self.action)
            # 1.2.0: the shared defaults every DamSafety tool (and Crest) starts from
            self.defaults_action = QAction(icon, "Shared defaults…", self.iface.mainWindow())
            self.defaults_action.setObjectName("DamSafetySharedDefaultsAction")
            self.defaults_action.triggered.connect(self.shared_defaults)
            self.iface.addPluginToMenu(MENU, self.defaults_action)
        except Exception:                               # noqa: BLE001 - a library never blocks QGIS
            self.action = None

    def unload(self):
        for a in (self.action, self.defaults_action):
            try:
                if a is not None:
                    self.iface.removePluginMenu(MENU, a)
            except Exception:                           # noqa: BLE001
                pass
        self.action = self.defaults_action = None

    def shared_defaults(self):
        """Plugins > Dam safety > Shared defaults… (ui/shared_defaults_dialog.py)."""
        try:
            from .ui import shared_defaults_dialog
            shared_defaults_dialog.show(self.iface.mainWindow())
        except Exception as e:                          # noqa: BLE001
            try:
                from qgis.PyQt.QtWidgets import QMessageBox
                QMessageBox.warning(self.iface.mainWindow(), "Shared defaults",
                                    "The Shared defaults window could not open: {}".format(e))
            except Exception:                           # noqa: BLE001
                pass

    def about(self):
        """Version, what is aliased onto this plugin, and the damsuite self-check."""
        try:
            from qgis.PyQt.QtWidgets import QMessageBox
            from . import alias
            from . import damsuite
            problems = damsuite.selfcheck()
            lines = ["DamSafety Core {} (damsuite {})".format(_version(), damsuite.SUITE_VERSION),
                     "Shared code of the DamSafety plugins; the plugins' damsuite packages are "
                     "this one:",
                     ", ".join(sorted(a.split(".")[0] for a in alias.aliases())) or "none loaded yet",
                     "Self-check: " + ("intact" if not problems else "; ".join(problems))]
            QMessageBox.information(self.iface.mainWindow(), "DamSafety Core", "\n".join(lines))
        except Exception:                               # noqa: BLE001
            pass


def _version():
    import configparser
    import os
    cp = configparser.ConfigParser(interpolation=None, strict=False)
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "metadata.txt"),
                  encoding="utf-8-sig") as f:
            cp.read_file(f)
        return cp.get("general", "version", fallback="?")
    except Exception:                                   # noqa: BLE001
        return "?"
