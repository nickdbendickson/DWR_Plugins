# -*- coding: utf-8 -*-
"""DamSafety Core: the one copy of the code the DamSafety plugins share.

    damsuite/   the shared package (dam records, curves, settings, packs, version checks ...)
    ui/         the shared window files (tokens, widgets, suite settings, home, styles)
    alias.py    `<Plugin>.damsuite` -> `DamSafety_Core.damsuite` (one module object per module)

The other plugins list this one in plugin_dependencies. It adds one About entry under the
suite menu and nothing else; importing it has no side effects.
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
        except Exception:                               # noqa: BLE001 - a library never blocks QGIS
            self.action = None

    def unload(self):
        try:
            if self.action is not None:
                self.iface.removePluginMenu(MENU, self.action)
        except Exception:                               # noqa: BLE001
            pass
        self.action = None

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
