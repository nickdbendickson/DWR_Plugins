import os

from qgis.core import QgsApplication
from .provider import NDPMPProvider

MENU = "DamSafety PMP"
STREAMSTATS_LABEL = "Get basin data from StreamStats\u2026"


class NDPMPPlugin:
    def __init__(self, iface):
        self.iface = iface
        self.provider = None
        self.action = None
        self.dialog = None

    def initProcessing(self):
        self.provider = NDPMPProvider()
        QgsApplication.processingRegistry().addProvider(self.provider)

    def initGui(self):
        self.initProcessing()
        from qgis.PyQt.QtGui import QIcon
        try:
            from qgis.PyQt.QtGui import QAction          # Qt6
        except ImportError:
            from qgis.PyQt.QtWidgets import QAction      # Qt5
        self.action = QAction(QIcon(os.path.join(os.path.dirname(__file__), "icon.svg")),
                              STREAMSTATS_LABEL, self.iface.mainWindow())
        self.action.setToolTip("Delineate the basin above the dam with USGS StreamStats and fill "
                               "the PMF basin inputs")
        self.action.triggered.connect(self.open_streamstats)
        self.iface.addPluginToMenu(MENU, self.action)

    def open_streamstats(self):
        from .streamstats_dialog import StreamStatsDialog
        if self.dialog is None:
            self.dialog = StreamStatsDialog(self.iface.mainWindow(), self.iface)
        self.dialog.show()
        self.dialog.raise_()
        self.dialog.activateWindow()

    def unload(self):
        if self.provider:
            QgsApplication.processingRegistry().removeProvider(self.provider)
        if self.action is not None:
            self.iface.removePluginMenu(MENU, self.action)
            self.action = None
        if self.dialog is not None:
            self.dialog.close()
            self.dialog = None
