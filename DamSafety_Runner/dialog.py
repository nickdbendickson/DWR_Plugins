"""The ND Dam Breach Runner popup window (Plugins > ND Dam Breach Runner).

One window for a dam: the Dams folder, the dam and the DEM at the top, a stage strip
(Dam -> Terrain -> Model run -> Products), "Run this dam", and two tabs: Run (what is in the dam's
folder and what to do next) and Tools (every tool of the plugin, by category). A button opens the
tool's own Processing dialog with the dam's folder (and the DEM chosen here) filled in, so every
value can be seen and changed before anything runs - the window runs nothing itself. The suite
settings grid is behind the gear button.

The logic (folder layout, status, parameters) is popup_logic.py; this file only holds widgets. The
window's choices are saved with the QGIS project. The tools, the wizard and the Processing
provider are unchanged.
"""
import json
import math
import os

from qgis.PyQt.QtCore import QUrl
from qgis.PyQt.QtGui import QDesktopServices
from .ui_fit import fit_window, save_size
from qgis.PyQt.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QGridLayout, QTabWidget, QWidget, QLabel, QLineEdit,
    QPushButton, QComboBox, QTextEdit, QFileDialog, QMessageBox, QDoubleSpinBox, QCheckBox,
    QFrame, QToolButton, QListWidget, QStackedWidget, QScrollArea, QPlainTextEdit, QSizePolicy,
)

from . import popup_logic as pl
from .ui_widgets import Collapsible, DamCard, EngineChip, InfoBanner, StageStrip, ToolRow
from .tokens import TOKENS
from .tool_dialog import exec_tool_dialog

TITLE = "DamSafety Runner"

DEM_SOURCE_TNM = "USGS 3DEP 1 m lidar tiles (native, TNM)"
_MI_M = 1609.344
_CHUNK_PX = 1200                    # Screening solve_gate.TILE_PX: the ImageServer export size


def dem_facts_text(source_i, cell_m, buffer_mi=10.0, scope_i=0):
    """The line under the Runner's 'Download a DEM' box (3.10.88: says how many ImageServer
    chunks a 1 m dam download is, and what the native 1 m tile source writes). Pure: tested."""
    if source_i == 2 and scope_i == 2:
        return ("Native 1 m lidar tiles are not offered for the whole state (thousands of 10 km "
                "tiles): choose Dam or County, or USGS 3DEP at 10 m for the state.")
    if source_i == 2:
        return ("Native 1 m lidar tiles from The National Map: 10 km GeoTIFFs at 1 m, UTM, "
                "elevations in metres, about 270 MB each, kept as they are; the result is "
                "<id>_1m_lidar.vrt (band unit metres). The tool lists the tiles, the total size "
                "and the free space first; tick '1 m tiles: download the listed tiles' and run "
                "again to fetch them (largest download per run: 10 GB by default). Resolution "
                "is ignored (always 1 m). A dam point and {:.0f} mi buffer is a {:.0f} km square "
                "around the dam; for a Runner domain give the tool a box or the dam folder.").format(
                    buffer_mi, 2 * buffer_mi * _MI_M / 1000.0)
    if source_i == 0:
        src = "USGS 3DEP ImageServer"
        native = "native CRS EPSG:5070, native units metres, native file GeoTIFF"
    else:
        # 3.10.89: DEM_Imagine is UTM metres (EPSG:3721 is UTM 14N), as Screening's build_vrts
        # reads it; only ASCII_Grids_ft is feet (State Plane) and it is converted
        src = "ND DWR QL2 LiDAR"
        native = ("native DEM_Imagine: Erdas Imagine .img, UTM metres (EPSG:26914 or EPSG:3721), "
                  "elevations in metres; native ASCII_Grids_ft: ASCII grids in ND State Plane feet "
                  "(EPSG:2265 / 2266), elevations in feet, converted x 0.3048")
    text = ("Local copy: source {src}; CRS EPSG:5070; units metres; file type GeoTIFF float32 "
            "tiled; cell {cell} m. {native}.").format(src=src, cell=cell_m, native=native)
    if source_i == 0 and scope_i == 0:
        side = 2.0 * buffer_mi * _MI_M
        per = math.ceil(side / (_CHUNK_PX * float(cell_m)) - 1e-9)
        n = int(per * per)
        text += (" {n} export requests ({per} x {per}) of {km:.1f} km for a {side:.0f} km square: "
                 "request chunks, not lidar tiles.").format(
                     n=n, per=int(per), km=_CHUNK_PX * float(cell_m) / 1000.0, side=side / 1000.0)
        if cell_m == 1 and n > 100:
            text += " For 1 m use the source '{}' (10 km tiles), or a smaller buffer.".format(
                DEM_SOURCE_TNM)
    return text

SIZE_KEY = "DamSafety_Runner/window"           # QSettings: the window size, saved on close
SCOPE = "NDBreachRunner"          # QgsProject entry scope
ENTRY = "state"
DISCLAIMER = ("Screening / engineering model results. Every threshold and parameter is the "
              "engineer's choice. DamSafety Hazard is the classification authority; the Department "
              "of Water Resources decides the official hazard class.")
# Tools tab: pl.TABS tab name -> the name in the category rail
CATEGORY_TITLES = {"Dam": "Run variants", "Model": "Model runs", "Data": "Terrain & data",
                   "Results": "Post-process", "Checks": "Verify"}
# chips of the dam card: status row key -> chip text
CARD_CHIPS = (("run", "Flood run"), ("baseline", "Baseline"), ("manifest", "Manifest"),
              ("products", "Products"), ("preview", "5.2 preview"), ("handoff", "Hand-off"),
              ("submittal", "Submittal"), ("package", "Package"))
CHIP_STYLE = {"yes": ("✓", TOKENS["success"]), "part": ("◐", TOKENS["warn"]), "no": ("○", TOKENS["off"])}
TOOL_TAG = {"yes": "✓ done", "part": "◐ partial", "no": "not run yet"}
RUN_TIP = "Opens the breach model with this dam, DEM, grid, and hours filled in."
RUN_TIP_NO_DAM = "Set the Dams folder and the dam ID first - the run needs a dam to write into."
MODEL_HINT = "Model paths: Tools > Verify > Model paths."
# the sibling plugins (their QGIS plugin folder names); ND Dam Hazard is retired
HAZARD_PLUGIN = ("DamSafety_Hazard", "DamSafety Hazard")
SCREENING_PLUGIN = ("DamSafety_Screening", "DamSafety Screening")


def _dots(text):
    """One ellipsis character on the buttons."""
    return text.replace("...", "\u2026")


def plugin_version():
    try:
        from .core import damjson
        return damjson.plugin_version()
    except Exception:                                            # noqa
        return ""



def _load_dem_download():
    import importlib
    for name in ("DamSafety_Screening", "nd_dam_screening"):
        try:
            return importlib.import_module(name + ".core.dem_download")
        except Exception:
            continue
    return None

def _vertical_policy(widget, name):
    """Set one widget's vertical size policy by name; a Qt (or test fake) without it is left as is."""
    try:
        sp = widget.sizePolicy()
        sp.setVerticalPolicy(getattr(getattr(QSizePolicy, "Policy", QSizePolicy), name))
        widget.setSizePolicy(sp)
    except Exception:                                            # noqa - sizing never breaks the window
        pass


class NDBreachRunnerDialog(QDialog):
    """The popup window. `iface` is optional (used by the wizard)."""

    def __init__(self, parent=None, iface=None):
        super().__init__(parent)
        self.iface = iface
        self._loading = True
        self.setWindowTitle("{} {}".format(TITLE, plugin_version()).strip())
        self.setMinimumWidth(820)
        self.setMinimumHeight(560)
        self.resize(860, 640)
        self.tool_button = {}
        self.tool_tag = {}
        self.tool_row = {}
        self._pending_dem = ""
        self._build()
        from qgis.PyQt.QtWidgets import QAbstractSpinBox, QComboBox
        for w in self.findChildren(QAbstractSpinBox) + self.findChildren(QComboBox):
            w.installEventFilter(self)
        self._load_state()
        self._loading = False
        try:
            self._load_settings_file(quiet=True)
        except Exception as e:                                   # noqa
            self._log("!!! settings file: {}".format(e))
        self._fill_dem()
        self.refresh()
        self._log_suite_versions()
        try:                                  # after the window is up
            from qgis.PyQt.QtCore import QTimer
            QTimer.singleShot(0, self._refresh_models)
        except Exception:                                        # noqa
            self._refresh_models()

    # ------------------------------------------------------------------ build --
    def _build(self):
        lay = QVBoxLayout()
        lay.setSpacing(6)
        # no in-window title: the window title names the tool (docs/gui-grammar.md)
        self.gear = QToolButton()                 # on the tab bar, as in Hazard and Screening
        self.gear.setText("⚙")
        self.gear.setAutoRaise(True)
        self.gear.setStyleSheet("QToolButton {font-size: 18px; padding: 0 8px 2px 8px;}")
        self.gear.setToolTip("Settings: the suite settings file the tools open with.")
        self.gear.clicked.connect(lambda _c=False: self._open_settings())
        lay.addWidget(InfoBanner(DISCLAIMER))

        self.strip = StageStrip([pl.STAGE_LABELS[k] for k in pl.STAGE_KEYS])
        lay.addWidget(self.strip)

        grid = QGridLayout()
        grid.setColumnStretch(1, 1)
        self.dams_edit = QLineEdit()
        self.dams_edit.setPlaceholderText("not set yet - one folder per dam inside")
        self.dams_edit.setToolTip("The Dams folder: each dam has a folder named after its ID in it. "
                                  "Must be writable. The offline pack is not a Dams folder. "
                                  "(the Runner's own layout).")
        self.dams_edit.editingFinished.connect(self._changed)
        b = QPushButton("Browse…")
        b.clicked.connect(lambda _c=False: self._browse_folder(self.dams_edit))
        grid.addWidget(QLabel("Dams folder:"), 0, 0)
        grid.addWidget(self.dams_edit, 0, 1, 1, 2)
        grid.addWidget(b, 0, 3)

        self.dam_edit = QLineEdit()
        self.dam_edit.setPlaceholderText("dam ID")
        self.dam_edit.setToolTip("The dam ID: its folder is <Dams folder>/<Dam ID>.")
        self.dam_edit.editingFinished.connect(self._changed)
        self.dam_combo = QComboBox()
        self.dam_combo.setToolTip("Dams that already have a folder.")
        self.dam_combo.currentIndexChanged.connect(self._pick_dam)
        grid.addWidget(QLabel("Dam:"), 1, 0)
        grid.addWidget(self.dam_edit, 1, 1)
        grid.addWidget(self.dam_combo, 1, 2, 1, 2)

        self.dem_combo = QComboBox()
        self.dem_combo.setToolTip("The DEM filled into the model tools (a raster layer of the project "
                                  "or a file). Empty: choose it in the tool's dialog.")
        self.dem_combo.currentIndexChanged.connect(self._changed)
        b = QPushButton("File…")
        b.clicked.connect(lambda _c=False: self._browse_dem())
        grid.addWidget(QLabel("DEM:"), 2, 0)
        grid.addWidget(self.dem_combo, 2, 1, 1, 2)
        grid.addWidget(b, 2, 3)
        # 3.10.39 (#107): the dam rows, Download a DEM, Advanced and the Run/Tools tabs sit in a
        # scroll area; the action bar, the Log and Close stay below it. The window has an explicit
        # minimum size (fit_window), so Qt no longer raises it to the layout's minimum; opening
        # "Download a DEM" then squeezed its rows to 6-9 px and drew them on top of each other.
        # In the scroll area every row keeps its own height and the area scrolls when the window
        # is short; opening a section also grows the window up to its screen (_grow_for_body).
        self.body_widget = QWidget()
        body = QVBoxLayout(self.body_widget)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(6)
        body.addLayout(grid)
        self.dem_section = self._dem_download_box()
        body.addWidget(self.dem_section)

        self.grid_m = QDoubleSpinBox()
        self.grid_m.setRange(0.5, 500.0)
        self.grid_m.setDecimals(1)
        self.grid_m.setSingleStep(1.0)
        self.grid_m.setToolTip("The flood grid. 10 m is the starting value. Coarser is faster; "
                               "finer than the DEM does not add terrain that was never surveyed.")
        self.simh = QDoubleSpinBox()
        self.simh.setRange(0.0, 240.0)
        self.simh.setDecimals(1)
        self.simh.setToolTip("How many hours of flood to simulate. 0 = the tool estimates it. "
                             "The starting value is 12.")
        self.simmax = QDoubleSpinBox()
        self.simmax.setRange(1.0, 336.0)
        self.simmax.setDecimals(1)
        self.simmax.setToolTip("The clock is lengthened, up to this many hours, while the flood "
                               "is still moving. The starting value is 96.")
        self.force_anuga = QCheckBox("Force ANUGA (final results from ANUGA)")
        self.force_anuga.setToolTip("Run ANUGA after LISFLOOD-FP finds the extent, and take the "
                                    "final depth, velocity and arrival from ANUGA, even if Models "
                                    "is set to LISFLOOD-FP only.")
        for sp in (self.grid_m, self.simh, self.simmax):
            sp.valueChanged.connect(self._changed)
        self.force_anuga.toggled.connect(self._changed)
        self.carve_combo = QComboBox()                       # v3.10.20
        for key, label in zip(pl.CARVE_CHOICES, ("Off", "Auto", "On")):
            self.carve_combo.addItem(label, key)
        self.carve_combo.setToolTip(
            "Reservoir bottom carved into a copy of the DEM from the dam's storage curve, for "
            "LiDAR that shows a water surface instead of the reservoir bottom. Off: the DEM as it "
            "is. Auto: only when the DEM's pool holds far less than the storage curve. The "
            "source DEM is never changed.")
        self.carve_combo.currentIndexChanged.connect(self._changed)
        self.datum_combo = QComboBox()                       # 3.12.0: ELEV_DATUM
        for key, label in zip(pl.ELEV_DATUM_CHOICES, ("Auto (as stated; else NGVD29)",
                                                      "NAVD88 (use as given)",
                                                      "NGVD29 (convert to NAVD88)")):
            self.datum_combo.addItem(label, key)
        self.datum_combo.setToolTip(pl.ELEV_DATUM_TIP)
        self.datum_combo.currentIndexChanged.connect(self._changed)
        self.datum_combo.currentIndexChanged.connect(self._show_datum)
        self.datum_label = QLabel("")
        self.datum_label.setWordWrap(True)
        self.datum_label.setToolTip(pl.ELEV_DATUM_TIP)
        self.hecras_gmsh = QCheckBox("Refine HEC-RAS mesh with Gmsh points (optional)")  # v3.10.22
        self.hecras_gmsh.setToolTip(pl.HECRAS_GMSH_TIP)
        self.hecras_gmsh.toggled.connect(self._changed)
        self.hecras_crest = QCheckBox("HEC-RAS dam line follows the crest (recommended)")
        self.hecras_crest.setToolTip(pl.HECRAS_CREST_TIP)
        self.hecras_crest.setChecked(True)                  # on by default
        self.hecras_crest.toggled.connect(self._changed)

        # the action bar (added under the tabs): engine state on the left, the primary action at
        # the bottom right in Qt's own look, bold (docs/gui-grammar.md)
        run_row = QHBoxLayout()
        engines = QLabel("Engines:")
        engines.setStyleSheet("color:{};".format(TOKENS["muted"]))
        run_row.addWidget(engines)
        self.lis_chip = EngineChip("LISFLOOD-FP")       # status labels, not buttons
        self.anuga_chip = EngineChip("ANUGA")
        for chip in (self.lis_chip, self.anuga_chip):
            run_row.addWidget(chip)
        run_row.addStretch(1)
        self.run_dam_btn = QPushButton("Run this dam")
        self.run_dam_btn.setDefault(True)
        self.run_dam_btn.setMinimumHeight(34)
        self.run_dam_btn.setStyleSheet("QPushButton {font-weight:bold; padding:4px 16px;}")
        self.run_dam_btn.setToolTip(RUN_TIP_NO_DAM)
        self.run_dam_btn.setEnabled(False)          # refresh() enables it once a dam is chosen
        self.run_dam_btn.clicked.connect(lambda _c=False: self._open_tool("rundam"))
        run_row.addWidget(self.run_dam_btn)

        adv = QWidget()
        grid2 = QGridLayout(adv)
        grid2.setContentsMargins(16, 2, 2, 2)
        grid2.addWidget(QLabel("Grid size (m):"), 0, 0)
        grid2.addWidget(self.grid_m, 0, 1)
        grid2.addWidget(QLabel("Simulated time (h):"), 0, 2)
        grid2.addWidget(self.simh, 0, 3)
        grid2.addWidget(QLabel("Longest simulated time (h):"), 1, 0)
        grid2.addWidget(self.simmax, 1, 1)
        grid2.addWidget(self.force_anuga, 1, 2, 1, 2)
        grid2.addWidget(QLabel("Reservoir carve:"), 2, 0)
        grid2.addWidget(self.carve_combo, 2, 1)
        grid2.addWidget(self.hecras_gmsh, 2, 2, 1, 2)
        grid2.addWidget(self.hecras_crest, 3, 2, 1, 2)
        grid2.addWidget(QLabel("Elevation datum:"), 4, 0)
        grid2.addWidget(self.datum_combo, 4, 1)
        grid2.addWidget(self.datum_label, 4, 2, 1, 3)
        grid2.setColumnStretch(4, 1)
        self.adv_section = Collapsible("Advanced", adv)
        body.addWidget(self.adv_section)

        self.tabs = QTabWidget()
        self.tabs.setMinimumHeight(150)
        # in the scroll area the tabs take the room that is left, not their (tall) size hint
        _vertical_policy(self.tabs, "Ignored")
        self.tabs.addTab(self._build_run_tab(), "Run")
        self.tabs.addTab(self._build_tools_tab(), "Tools")
        self.tabs.setCornerWidget(self.gear)
        body.addWidget(self.tabs, 1)
        self.body_scroll = QScrollArea()
        self.body_scroll.setWidgetResizable(True)
        self.body_scroll.setFrameShape(getattr(QFrame, 'Shape', QFrame).NoFrame)
        self.body_scroll.setWidget(self.body_widget)
        lay.addWidget(self.body_scroll, 1)
        lay.addLayout(run_row)                     # the action bar stays in view under the tabs

        self.settings_dialog = QDialog(self)
        self.settings_dialog.setWindowTitle("{} \u2014 Settings".format(TITLE))
        self.settings_dialog.setModal(False)
        self.settings_dialog.resize(900, 620)
        sd = QVBoxLayout()
        sd.addWidget(self._build_settings_widget(), 1)
        row = QHBoxLayout()
        row.addStretch(1)
        done = QPushButton("Close")
        done.clicked.connect(lambda _c=False: self.settings_dialog.close())
        row.addWidget(done)
        sd.addLayout(row)
        self.settings_dialog.setLayout(sd)

        self.log_edit = QTextEdit()
        self.log_edit.setReadOnly(True)
        self.log_edit.setMaximumHeight(110)
        self.log_section = Collapsible("Log", self.log_edit)
        lay.addWidget(self.log_section)            # outside the scroll area: always just above Close
        for sec in (self.dem_section, self.adv_section, self.log_section):
            sec.header.toggled.connect(self._section_toggled)
            _vertical_policy(sec, "Fixed")   # rows at their own height: never below the size hint
        row = QHBoxLayout()                       # Close in its own row, bottom right
        row.addStretch(1)
        self.close_btn = QPushButton("Close")
        self.close_btn.clicked.connect(self.close)
        row.addWidget(self.close_btn)
        lay.addLayout(row)
        self.setLayout(lay)
        # setDefault() registers the button with its dialog only when the button's own parent
        # changes; register it again now that the whole layout is in this dialog (off then on:
        # setDefault(True) on a default button returns early)
        self.run_dam_btn.setDefault(False)
        self.run_dam_btn.setDefault(True)
        fit_window(self, key=SIZE_KEY)
        self._sync_advanced_title()

    def _section_toggled(self, on):
        if on:
            try:
                from qgis.PyQt.QtCore import QTimer
                QTimer.singleShot(0, self._grow_for_body)
            except Exception:                                    # noqa
                self._grow_for_body()

    def _grow_for_body(self):
        """After a section opens: make the window taller by what the scroll area is short of,
        up to the window's maximum (the screen, set by fit_window), and keep it on its screen.
        Whatever still does not fit scrolls."""
        try:
            need = self.body_widget.minimumSizeHint().height() - self.body_scroll.viewport().height()
            if need <= 0:
                return
            h = min(self.maximumHeight(), self.height() + need)
            if h <= self.height():
                return
            self.resize(self.width(), h)
            from . import ui_fit
            scr = ui_fit._screen_for(self)
            if scr is not None:
                a = scr.availableGeometry()
                g = self.frameGeometry()
                if g.bottom() > a.bottom():
                    self.move(self.x(), max(a.top(), self.y() - (g.bottom() - a.bottom())))
        except Exception:                                        # noqa - sizing never breaks the window
            pass

    def _sync_advanced_title(self, *args):
        """The Advanced header shows the values, so nothing hides behind the closed section."""
        parts = ["grid {:g} m".format(self.grid_m.value()),
                 "{:g} h simulated".format(self.simh.value()) if self.simh.value() else
                 "simulated time estimated",
                 "up to {:g} h".format(self.simmax.value())]
        if self.force_anuga.isChecked():
            parts.append("Force ANUGA")
        carve = self.carve_combo.currentData() if hasattr(self, "carve_combo") else None
        if carve and carve != "off":
            parts.append("reservoir carve " + str(carve))
        if getattr(self, "hecras_gmsh", None) is not None and self.hecras_gmsh.isChecked():
            parts.append("HEC-RAS gmsh points")
        if getattr(self, "hecras_crest", None) is not None and not self.hecras_crest.isChecked():
            parts.append("HEC-RAS straight dam line")
        dm = self.datum_combo.currentData() if hasattr(self, "datum_combo") else None
        if dm and dm != "auto":
            parts.append("elevations " + str(dm))
        self.adv_section.set_title("Advanced  ·  " + " · ".join(parts))

    def _open_settings(self):
        self.settings_dialog.show()
        try:
            self.settings_dialog.raise_()
            self.settings_dialog.activateWindow()
        except Exception:                                        # noqa
            pass

    def _update_gear(self):
        try:
            n = len(self.grid.settings.changed(self.grid.catalog))
        except Exception:                                        # noqa
            n = 0
        self.gear.setText("⚙ {}".format(n) if n else "⚙")
        self.gear.setToolTip("Settings: the suite settings file the tools open with." +
                             ("\n{} value(s) differ from the starting values.".format(n) if n else ""))

    # ---------------------------------------------------------------- the tabs --
    def _build_run_tab(self):
        w = QWidget()
        lay = QVBoxLayout()
        self.card = card = DamCard(CARD_CHIPS)
        # the names the rest of the window (and the tests) use
        self.card_title, self.card_path = card.title_label, card.path_label
        self.card_chips, self.chips_row, self.warn_label = card.chips, card.chips_row, card.warn_label
        self.status_label, self.record_label = card.status_label, card.record_label
        self.suggest_label = card.suggest_label
        self.contents_edit = QPlainTextEdit()
        self.contents_edit.setReadOnly(True)
        self.contents_edit.setMinimumHeight(150)
        self.contents_section = Collapsible("Folder contents (0)", self.contents_edit)
        card.add_section(self.contents_section)
        card.layout_.addStretch(1)
        # the amber warnings above the scrolled card, so they are read without scrolling
        card.layout_.removeWidget(card.warn_label)
        lay.addWidget(card.warn_label)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(getattr(QFrame, 'Shape', QFrame).NoFrame)
        scroll.setWidget(card)
        lay.addWidget(scroll, 1)
        row = QHBoxLayout()
        self.suggest_btn = QPushButton("Open suggested tool")
        self.suggest_btn.clicked.connect(lambda _c=False: self._open_suggested())
        self.folder_btn = QPushButton("Open dam folder")
        self.folder_btn.clicked.connect(lambda _c=False: self._open_dam_folder())
        self.add_btn = QPushButton("Add results to project")
        self.add_btn.setToolTip("The final, baseline and incremental rasters and the flood polygons of "
                                "the dam folder, with the suite's styles (styles/*.qml), in a layer group "
                                "named after the dam.")
        self.add_btn.clicked.connect(lambda _c=False: self._add_results())
        self.wizard_btn = QPushButton("Guided wizard…")
        self.wizard_btn.setToolTip(dict((a, t) for a, _l, t in pl.ALL_TOOLS).get(pl.WIZARD, ""))
        self.wizard_btn.clicked.connect(lambda _c=False: self._open_tool(pl.WIZARD))
        row.addWidget(self.suggest_btn)
        row.addWidget(self.folder_btn)
        row.addWidget(self.add_btn)
        row.addStretch(1)
        row.addWidget(self.wizard_btn)
        lay.addLayout(row)
        w.setLayout(lay)
        return w

    def _build_tools_tab(self):
        w = QWidget()
        h = QHBoxLayout()
        self.rail = QListWidget()
        self.rail.setMaximumWidth(150)
        self.rail.setMinimumWidth(130)
        self.pages = QStackedWidget()
        for tab, tools in pl.TABS:
            self.rail.addItem(CATEGORY_TITLES.get(tab, tab))
            self.pages.addWidget(self._tool_page(tab, tools))
        self.rail.currentRowChanged.connect(self.pages.setCurrentIndex)
        self.rail.setCurrentRow(0)
        h.addWidget(self.rail)
        h.addWidget(self.pages, 1)
        w.setLayout(h)
        return w

    def _tool_row(self, lay, label, tip, slot, alg=None):
        """One tool: a left-aligned button, a status tag (tools with a status row) and its
        description in grey."""
        row = ToolRow(_dots(label), tip)
        row.button.clicked.connect(lambda _c=False: slot())
        if alg in pl.TOOL_STATUS_KEY:
            self.tool_tag[alg] = row.tag
            self.tool_row[alg] = row
        lay.addWidget(row)
        btn = row.button
        return btn

    def _tool_page(self, tab, tools):
        w = QWidget()
        lay = QVBoxLayout()
        for alg, label, tip in tools:
            btn = self._tool_row(lay, label, tip, lambda a=alg: self._open_tool(a), alg)
            btn.setToolTip(tip + ("" if alg == pl.WIZARD else
                                  "\n(Processing: {}:{})".format(pl.PROVIDER, alg)))
            self.tool_button[alg] = btn
        if tab == "Results":
            self.hazard_btn = self._tool_row(
                lay, "Open {}…".format(HAZARD_PLUGIN[1]),
                "The classification authority; its 'Dam suite' group reads the same dam.json records.",
                lambda: self._open_other(*HAZARD_PLUGIN))
            self.screening_btn = self._tool_row(
                lay, "Open {}…".format(SCREENING_PLUGIN[1]),
                "County-wide screening; tool 7b hands dams to this Runner.",
                lambda: self._open_other(*SCREENING_PLUGIN))
        if tab == "Checks":
            self.paths_btn = self._tool_row(lay, "Model paths (Processing options)…",
                                            "Where LISFLOOD-FP and the ANUGA environment are.",
                                            self._model_paths)
            self.help_btn = self._tool_row(lay, "Help (README)", "The plugin's README.", self._open_help)
        lay.addStretch(1)
        w.setLayout(lay)
        scroll = QScrollArea()                 # Post-process has twelve tools
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(getattr(QFrame, 'Shape', QFrame).NoFrame)
        scroll.setWidget(w)
        return scroll

    def _dem_download_box(self):
        try:
            dd = _load_dem_download()
            if dd is None:
                raise ImportError("screening")
        except Exception:
            box = QWidget()
            lay = QVBoxLayout()
            lay.addWidget(QLabel("DEM download needs DamSafety Screening. It could not be imported."))
            box.setLayout(lay)
            return Collapsible("Download a DEM (not available)", box)
        box = QWidget()
        grid = QGridLayout()
        grid.setContentsMargins(16, 2, 2, 2)
        self.dem_source = QComboBox()
        # 3.10.88: the third entry is the Download DEM tool's SOURCE 2 (Screening 0.17.29+), the
        # 10 km 1 m lidar project tiles; "USGS 3DEP" is the ImageServer (1.2 km chunks at 1 m).
        self.dem_source.addItems(["USGS 3DEP", "ND DWR LiDAR (lidar.dwr.nd.gov)",
                                  DEM_SOURCE_TNM])
        self.dem_source.setToolTip(
            "USGS 3DEP: the ImageServer, resampled to 10 m or 1 m and cut into 1,200 px export "
            "chunks (at 1 m a 10 mi buffer is 729 chunks of 1.2 km - not lidar tiles). "
            "{}: the 10 km 1 m lidar tiles themselves (about 270 MB each); the tool lists "
            "them first, tick 'download the listed tiles' to fetch them.".format(DEM_SOURCE_TNM))
        self.dem_ftp = QLineEdit()
        self.dem_ftp.setPlaceholderText("ftp://lidarftp.swc.nd.gov/temp/<id>")
        self.dem_ascii = QCheckBox("ASCII_Grids_ft")
        self.dem_ascii.setChecked(True)
        self.dem_imagine = QCheckBox("DEM_Imagine")
        self.dem_imagine.setChecked(True)
        self.work_crs = QComboBox()
        self.work_crs.addItems(["Keep source CRS", "EPSG:5070 metres", "EPSG:26914 UTM 14N metres",
                                "EPSG:2265 ND North feet", "EPSG:2266 ND South feet"])
        self.work_crs.setToolTip("3DEP is metres in EPSG:5070. QL2 Imagine is metres in UTM. "
                                 "ASCII_Grids_ft is feet. Pick the CRS the model should use; "
                                 "the download is warped to it.")
        self.dem_scope = QComboBox()
        self.dem_scope.addItems(["Dam", "County", "State"])
        self.dem_point = QLineEdit()
        self.dem_point.setPlaceholderText("longitude,latitude")
        self.dem_buffer = QDoubleSpinBox()
        self.dem_buffer.setRange(1.0, 50.0)
        self.dem_buffer.setValue(10.0)
        self.dem_buffer.setSuffix(" mi")
        self.dem_county = QComboBox()
        self.dem_county.addItems(dd.county_names())
        self.dem_res = QComboBox()
        self.dem_res.addItems(["10 m", "1 m"])
        self.dem_res.setToolTip("Local copy cell size. Output is EPSG:5070, metres, tiled GeoTIFF.")
        self.dem_facts = QLabel("")
        self.dem_facts.setWordWrap(True)
        self.dem_source.currentIndexChanged.connect(lambda _i: self._dem_facts())
        self.dem_res.currentIndexChanged.connect(lambda _i: self._dem_facts())
        self.dem_buffer.valueChanged.connect(lambda _v: self._dem_facts())
        self.dem_scope.currentIndexChanged.connect(lambda _i: self._dem_facts())
        self._dem_facts()
        self.dem_out = QLineEdit()
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse_dem_folder)
        counties = QPushButton("Counties on the map")
        counties.clicked.connect(self._show_counties)
        go = QPushButton("Download DEM…")
        go.clicked.connect(self._open_dem_download)
        grid.addWidget(QLabel("Source"), 0, 0)
        grid.addWidget(self.dem_source, 0, 1)
        grid.addWidget(self.dem_ascii, 0, 2)
        grid.addWidget(self.dem_imagine, 0, 3)
        grid.addWidget(QLabel("ND LiDAR path"), 1, 0)
        grid.addWidget(self.dem_ftp, 1, 1, 1, 3)
        grid.addWidget(QLabel("Area"), 2, 0)
        grid.addWidget(self.dem_scope, 2, 1)
        grid.addWidget(QLabel("Dam point"), 3, 0)
        grid.addWidget(self.dem_point, 3, 1)
        grid.addWidget(self.dem_buffer, 3, 2)
        grid.addWidget(QLabel("County"), 4, 0)
        grid.addWidget(self.dem_county, 4, 1)
        grid.addWidget(QLabel("Resolution"), 4, 2)
        grid.addWidget(self.dem_res, 4, 3)
        grid.addWidget(QLabel("Folder"), 5, 0)
        grid.addWidget(self.dem_out, 5, 1)
        grid.addWidget(browse, 5, 2)
        grid.addWidget(go, 5, 3)
        grid.addWidget(QLabel("Work CRS"), 6, 0)
        grid.addWidget(self.work_crs, 6, 1)
        grid.addWidget(counties, 6, 2, 1, 2)
        grid.addWidget(self.dem_facts, 7, 0, 1, 4)
        box.setLayout(grid)
        return Collapsible("Download a DEM (USGS 3DEP, ND DWR LiDAR or 1 m lidar tiles)", box)

    def _dem_facts(self):
        self.dem_facts.setText(dem_facts_text(
            self.dem_source.currentIndex(), 10 if self.dem_res.currentIndex() == 0 else 1,
            float(self.dem_buffer.value()), self.dem_scope.currentIndex()))

    def _browse_dem_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "DEM folder", self.dem_out.text())
        if folder:
            self.dem_out.setText(folder)


    def _work_crs(self):
        text = self.work_crs.currentText()
        return "" if text.startswith("Keep") else text.split()[0]

    def _show_counties(self):
        from qgis.core import (QgsVectorLayer, QgsFeature, QgsGeometry, QgsPointXY,
                               QgsProject, QgsFillSymbol, QgsSingleSymbolRenderer)
        dd = _load_dem_download()
        if dd is None:
            raise ImportError("screening")
        layer = QgsVectorLayer("Polygon?crs=EPSG:4326&field=county:string",
                               "ND counties (download clips)", "memory")
        prov = layer.dataProvider()
        feats = []
        for name, ring in dd.county_features():
            f = QgsFeature(layer.fields())
            f.setAttribute("county", name)
            f.setGeometry(QgsGeometry.fromPolygonXY([[QgsPointXY(x, y) for x, y in ring]]))
            feats.append(f)
        prov.addFeatures(feats)
        layer.updateExtents()
        try:
            sym = QgsFillSymbol.createSimple({"color": "0,80,160,40", "outline_color": "0,60,120",
                                              "outline_width": "0.6"})
            layer.setRenderer(QgsSingleSymbolRenderer(sym))
        except Exception:
            pass
        QgsProject.instance().addMapLayer(layer)
        try:
            layer.selectionChanged.connect(lambda *_a: self._county_from_map(layer))
        except Exception:
            pass
        canvas = getattr(self.iface, "mapCanvas", lambda: None)()
        if canvas is not None:
            canvas.setExtent(layer.extent())
            canvas.refresh()
        self.log_edit.append("County clips are on the map. Select one and the county list follows it.")

    def _county_from_map(self, layer):
        names = [f["county"] for f in layer.selectedFeatures()]
        if not names:
            return
        self.dem_source.setCurrentIndex(1)
        self.dem_scope.setCurrentIndex(1)
        self.dem_county.setCurrentText(names[0])

    def _open_dem_download(self):
        import processing
        folder = self.dem_out.text().strip()
        if not folder:
            QMessageBox.warning(self, TITLE, "Choose a folder for the DEM.")
            return
        if self.dem_scope.currentIndex() == 2 and self.dem_source.currentIndex() == 2:
            # 3.10.89: the tool refuses the native 1 m tiles for the state; say so before asking
            QMessageBox.warning(self, TITLE, dem_facts_text(2, 1, 10.0, 2))
            return
        if self.dem_scope.currentIndex() == 2:
            n = QMessageBox.question(self, TITLE, "A statewide DEM is tiled and can take a long time. Continue?")
            if n != getattr(QMessageBox, 'StandardButton', QMessageBox).Yes:
                return
        # 3.10.89: COUNTY is the Processing enum's index, as Screening's window passes it - the
        # tool dialog turned the county's name into index 0 (Adams) for every county
        dd = _load_dem_download()
        names = list(dd.county_names()) if dd is not None else []
        county = self.dem_county.currentText()
        exec_tool_dialog(processing, "nddam:downloaddem", {
            "SOURCE": self.dem_source.currentIndex(),        # 2 = native 1 m lidar tiles (TNM)
            "SCOPE": self.dem_scope.currentIndex(),
            "DAM_POINT": self.dem_point.text().strip(),
            "BUFFER_MI": float(self.dem_buffer.value()),
            "COUNTY": names.index(county) if county in names else 0,
            "RES": self.dem_res.currentIndex(),
            "CELL": 10.0 if self.dem_res.currentIndex() == 0 else 1.0,
            "DWR_PATH": self.dem_ftp.text().strip(),
            "ASCII_FT": self.dem_ascii.isChecked(),
            "IMAGINE": self.dem_imagine.isChecked(),
            "WORK_CRS": self._work_crs(),
            "OUT": folder,
        }, parent=self)

    # --------------------------------------------------- the settings window --
    def _build_settings_widget(self):
        """3.5: the suite settings file (every engineer-chosen value of the Runner's tools, shared
        with the other two plugins) as a grid - the tools open with its values."""
        from . import suite_settings as SS
        from . import suite_settings_tools as ST
        from .damsuite import settings as DS
        w = QWidget()
        lay = QVBoxLayout()
        row = QHBoxLayout()
        row.addWidget(QLabel("Settings file:"))
        self.settings_file_edit = QLineEdit()
        self.settings_file_edit.setToolTip(
            "The suite settings workbook (damsuite_settings.xlsx, or .csv) shared by the three plugins: "
            "the Runner's tools open with its values. Empty = the tools' own starting values. Edits in "
            "the grid are saved to the file at once.")
        self.settings_file_edit.editingFinished.connect(lambda: self._settings_file_edited())
        b = QPushButton("File…")
        b.clicked.connect(lambda _c=False: self._pick_settings_file())
        n = QPushButton("New…")
        n.setToolTip("Write a new settings file with the current values.")
        n.clicked.connect(lambda _c=False: self._new_settings_file())
        row.addWidget(self.settings_file_edit, 1)
        row.addWidget(b)
        row.addWidget(n)
        lay.addLayout(row)
        try:
            from . import provider as prov
            catalog = ST.full_catalog(prov.ALGORITHMS, pl.PLUGIN_KEY, pl.settings_group, pl.TAB_ORDER)
        except Exception as e:                                   # noqa
            catalog = []
            self._log("!!! settings grid: {}".format(e))
        try:
            from .damsuite import versions as vs
            versions = {pl.PLUGIN_KEY: vs.RELEASE.get(pl.PLUGIN_KEY, "")}
        except Exception:                                        # noqa
            versions = {}
        self.grid = SS.SettingsGrid(pl.PLUGIN_KEY, catalog, DS.Settings(), parent=self,
                                    on_change=lambda _s: self._autosave_settings(),
                                    on_file=self._grid_file, versions=versions, tab_order=pl.TAB_ORDER)
        lay.addWidget(self.grid, 1)
        w.setLayout(lay)
        return w

    def _settings_file_edited(self):
        self._load_settings_file(quiet=False)
        self._changed()

    def _pick_settings_file(self):
        start = self.settings_file_edit.text().strip() or self.dams_edit.text().strip() or ""
        p, _f = QFileDialog.getOpenFileName(self, "Settings file", start,
                                            "Settings workbook (*.xlsx);;Settings table (*.csv);;All files (*)")
        if p:
            self.settings_file_edit.setText(p)
            self._settings_file_edited()

    def _new_settings_file(self):
        from .damsuite import settings as DS
        dams = self.dams_edit.text().strip()
        start = os.path.join(dams, DS.DEFAULT_NAME) if dams else DS.DEFAULT_NAME
        p, _f = QFileDialog.getSaveFileName(self, "New settings file", start,
                                            "Settings workbook (*.xlsx);;Settings table (*.csv)")
        if not p:
            return ""
        if not os.path.splitext(p)[1]:
            p += ".xlsx"
        self.settings_file_edit.setText(p)
        self.grid.settings.path = p
        self.grid.save(p, name=os.path.splitext(os.path.basename(p))[0].replace("_", " "))
        DS.set_current_path(p)
        self._log("Settings file written: {}".format(p))
        self._save_state()
        return p

    def _load_settings_file(self, quiet=True):
        from .damsuite import settings as DS
        p = self.settings_file_edit.text().strip()
        DS.set_current_path(p)
        if not p:
            self.grid.settings = DS.Settings()
            self.grid.fill()
            self._update_gear()
            return False
        if not os.path.isfile(p):
            if not quiet:
                QMessageBox.warning(self, TITLE, "The settings file does not exist: {}".format(p))
            self._log("!!! settings file not found: {}".format(p))
            return False
        ok = self.grid.load(p, quiet=quiet)
        self._update_gear()
        if ok:
            self._log("Settings file: {} ({} value(s) differ from the starting values)".format(
                p, len(self.grid.settings.changed(self.grid.catalog))))
        return ok

    def _grid_file(self, path):
        from .damsuite import settings as DS
        if path and path != self.settings_file_edit.text().strip():
            self.settings_file_edit.setText(path)
            DS.set_current_path(path)
            self._save_state()

    def _autosave_settings(self):
        from .damsuite import settings as DS
        if self._loading:
            return
        p = self.settings_file_edit.text().strip()
        if not p:
            return
        try:
            self.grid.settings.path = p
            self.grid.settings.save(p, catalog=self.grid.catalog, versions=self.grid.versions or None)
            DS.set_current_path(p)
        except DS.SettingsError as e:
            self._log("!!! settings not saved: {}".format(e))
        self._update_gear()

    # ---------------------------------------------------------------- settings --
    def settings(self):
        out = {"dams_folder": self.dams_edit.text().strip(), "dam_id": self.dam_edit.text().strip(),
               "dem": self.dem_combo.currentData() or "",
               "settings_file": self.settings_file_edit.text().strip(),
               "grid_m": float(self.grid_m.value()), "simh": float(self.simh.value()),
               "simmax": float(self.simmax.value()),
               "force_anuga": bool(self.force_anuga.isChecked())}
        carve = self.carve_combo.currentData() or "off"
        if carve != "off":                  # Off (the default) leaves the saved state as before
            out["reservoir_carve"] = carve
        if self.hecras_gmsh.isChecked():    # off (the default) leaves the saved state as before
            out["hecras_gmsh"] = True
        if not self.hecras_crest.isChecked():   # on (the default) leaves the saved state as before
            out["hecras_dam_axis"] = "straight"
        dm = self.datum_combo.currentData() if hasattr(self, "datum_combo") else None
        if dm and dm != "auto":             # auto (the default) leaves the saved state as before
            out["elev_datum"] = dm
        return out

    def _apply(self, s):
        self.dams_edit.setText(s.get("dams_folder", ""))
        self.dam_edit.setText(s.get("dam_id", ""))
        self._pending_dem = s.get("dem", "") or ""
        sf = (s.get("settings_file") or "").strip()
        if not sf:
            try:
                from .damsuite import settings as DS
                sf = DS.current_path() or ""
            except Exception:                                    # noqa
                sf = ""
        self.settings_file_edit.setText(sf)
        cell = s.get("grid_m")
        if cell is None and sf:
            try:
                from .damsuite import settings as DS
                cell = DS.Settings.load(sf).get(pl.PLUGIN_KEY, "rundam", "CELL")
            except Exception:                                    # noqa
                cell = None
        self.grid_m.setValue(float(cell if cell is not None else pl.DEFAULT_SETTINGS["grid_m"]))
        self.simh.setValue(float(s.get("simh", pl.DEFAULT_SETTINGS["simh"])))
        self.simmax.setValue(float(s.get("simmax", pl.DEFAULT_SETTINGS["simmax"])))
        self.force_anuga.setChecked(bool(s.get("force_anuga", False)))
        carve = str(s.get("reservoir_carve") or "off").lower()
        self.carve_combo.setCurrentIndex(pl.CARVE_CHOICES.index(carve)
                                         if carve in pl.CARVE_CHOICES else 0)
        self.hecras_gmsh.setChecked(bool(s.get("hecras_gmsh", False)))
        self.hecras_crest.setChecked(
            str(s.get("hecras_dam_axis") or "crest").strip().lower() != "straight")
        dm = str(s.get("elev_datum") or "auto").strip()
        self.datum_combo.setCurrentIndex(pl.ELEV_DATUM_CHOICES.index(dm)
                                         if dm in pl.ELEV_DATUM_CHOICES else 0)

    def _save_state(self):
        if self._loading:
            return
        try:
            from qgis.core import QgsProject
            QgsProject.instance().writeEntry(SCOPE, ENTRY, json.dumps(self.settings()))
        except Exception:                                        # noqa
            pass

    def _load_state(self):
        s = dict(pl.DEFAULT_SETTINGS)
        s.pop("grid_m")                  # unset: _apply takes the settings file's CELL, else 10
        try:
            from qgis.core import QgsProject
            text, ok = QgsProject.instance().readEntry(SCOPE, ENTRY, "")
            if ok and text:
                loaded = json.loads(text)
                if isinstance(loaded, dict):
                    s.update(loaded)
        except Exception:                                        # noqa
            pass
        self._apply(s)

    def _changed(self, *args):
        self._sync_advanced_title()
        self._save_state()
        self.refresh()

    # --------------------------------------------------------------------- DEM --
    @staticmethod
    def raster_layers():
        """[(name, id)] of the project's raster layers."""
        rows = []
        try:
            from qgis.core import QgsProject, QgsRasterLayer
            for lyr in QgsProject.instance().mapLayers().values():
                if isinstance(lyr, QgsRasterLayer):
                    rows.append((str(lyr.name()), lyr.id()))
        except Exception:                                        # noqa
            return []
        return sorted(rows, key=lambda r: r[0].lower())

    def _fill_dem(self):
        was = self._loading
        self._loading = True
        try:
            keep = self._pending_dem or (self.dem_combo.currentData() or "")
            self.dem_combo.clear()
            self.dem_combo.addItem("(choose in the tool)", "")
            for nm, lid in self.raster_layers():
                self.dem_combo.addItem(nm, lid)
            if keep:
                i = self.dem_combo.findData(keep)
                if i < 0:
                    self.dem_combo.addItem(os.path.basename(keep) or keep, keep)
                    i = self.dem_combo.count() - 1
                self.dem_combo.setCurrentIndex(i)
            else:
                self.dem_combo.setCurrentIndex(0)
            self._pending_dem = ""
        finally:
            self._loading = was

    def _browse_dem(self):
        p, _f = QFileDialog.getOpenFileName(self, "DEM", self.dams_edit.text().strip() or "",
                                            "Raster (*.tif *.tiff *.vrt *.img);;All files (*)")
        if not p:
            return
        i = self.dem_combo.findData(p)
        if i < 0:
            self.dem_combo.addItem(os.path.basename(p), p)
            i = self.dem_combo.count() - 1
        self.dem_combo.setCurrentIndex(i)               # -> _changed

    # ------------------------------------------------------------------ browse --
    def _browse_folder(self, edit):
        d = QFileDialog.getExistingDirectory(self, "Folder", edit.text().strip() or "")
        if d:
            edit.setText(d)
            self._changed()

    def _pick_dam(self, *args):
        if self._loading:
            return
        name = self.dam_combo.currentData()
        if name:
            self.dam_edit.setText(name)
            self._changed()

    # --------------------------------------------------------------------- log --
    def _log(self, msg):
        self.log_edit.append(str(msg))

    def _log_suite_versions(self):
        """Suite problems (a too old DamSafety Hazard / Screening) are also in the message bar."""
        try:
            from .plugin import version_messages
            for level, text in version_messages():
                if level in ("error", "warning"):
                    self._log("{}: {}".format(level.upper(), text))
        except Exception:                                        # noqa
            pass

    # ----------------------------------------------------------------- refresh --
    def folder(self):
        s = self.settings()
        return pl.dam_folder(s["dams_folder"], s["dam_id"])

    def refresh(self, *args):
        """Re-read the Dams folder and the dam's folder."""
        s = self.settings()
        dams = s["dams_folder"]
        have_dams = bool(dams and os.path.isdir(dams))
        # the list of dams that have a folder
        was = self._loading
        self._loading = True
        try:
            keep = self.dam_edit.text().strip()
            self.dam_combo.clear()
            self.dam_combo.addItem("(existing dams)", "")
            for n in pl.list_dams(dams) if have_dams else []:
                self.dam_combo.addItem(n, n)
            self.dam_combo.setCurrentIndex(0)
            self.dam_edit.setText(keep)
        finally:
            self._loading = was
        if dams and ("offline_pack" in dams.lower() or dams.lower().endswith(".sqlite")):
            self._fill_card(dict(s, dam_id="", dem=""), "", False, [], "", False)
            self.status_label.setText("The offline pack is not a Dams folder. Choose a writable folder.")
            self.status_label.setVisible(True)
            self.record_label.setText("")
            self.suggest_label.setText("")
            self._suggested = None
            for b in (self.suggest_btn, self.folder_btn, self.add_btn):
                b.setEnabled(False)
            return
        folder = self.folder()
        have = bool(folder and os.path.isdir(folder))
        rows = pl.dam_status(folder) if have else []
        if have:
            self.status_label.setText("Dam folder {}:\n{}".format(folder, "\n".join(pl.status_lines(rows))))
        elif s["dam_id"] and have_dams:
            self.status_label.setText("No folder for dam {} in the Dams folder yet - the first run "
                                      "creates it.".format(s["dam_id"]))
        else:
            self.status_label.setText("Choose the Dams folder and the dam ID.")
        text, code, has_json = pl.dam_record_info(folder) if have else ("", "", False)
        self.record_label.setText(text)
        tool, why = pl.suggest(rows, code) if have or (s["dam_id"] and have_dams) else (None, "")
        self.suggest_label.setText(("Suggestion: " + why) if why else "")
        self._suggested = tool
        self.suggest_btn.setEnabled(bool(tool))
        self.folder_btn.setEnabled(have)
        self.add_btn.setEnabled(have and bool(pl.result_files(folder)))
        self.status_label.setVisible(not have)           # the chips and Folder contents show it
        self._fill_card(s, folder, have, rows, text, has_json)
        self._show_datum()

    def _show_datum(self, *args):
        """3.12.0: the detected datum of the dam's elevations and the shift at the dam."""
        if not hasattr(self, "datum_label"):
            return
        try:
            self.datum_label.setText(pl.datum_info(self.folder(),
                                                   self.datum_combo.currentData() or "auto"))
        except Exception as e:                                   # noqa
            self.datum_label.setText("Elevation datum: {}".format(e))

    def _fill_card(self, s, folder, have, rows, record, has_json):
        """The dam card, the stage strip and the tool tags from the folder's status rows."""
        st = {k: (v, d) for k, _l, v, d in rows}
        did = s["dam_id"]
        self.card.set_context(did, folder if have else "")
        self.chips_row.setVisible(have)
        warns = pl.package_warnings(folder) if have else []
        self.card.set_warnings(warns)
        self.card.set_rows(rows)
        lines = pl.status_lines(rows)
        self.contents_edit.setPlainText("\n".join(lines))
        self.contents_section.set_title("Folder contents ({})".format(
            sum(1 for _k, _l, v, _d in rows if v != "no")))
        self.contents_section.setVisible(have)
        states = pl.stage_states(rows, have, has_json, s["dem"], did)
        self.strip.set_states([states[k] for k in pl.STAGE_KEYS],
                              [pl.stage_tip(k, states[k], have, has_json) for k in pl.STAGE_KEYS])
        for alg, row in self.tool_row.items():
            row.set_status(pl.tool_status(alg, rows), have)
        ready = bool(s["dams_folder"] and s["dam_id"])
        self.run_dam_btn.setEnabled(ready)
        self.run_dam_btn.setToolTip(RUN_TIP if ready else RUN_TIP_NO_DAM)

    def _refresh_models(self):
        """Which of LISFLOOD-FP / ANUGA are found. Not part of refresh(): looking for a conda
        environment can take a second, and refresh() runs whenever the window is activated."""
        try:
            from . import wizard
            m = wizard._find_models()
            self.lis_chip.set_state(bool(m.get("lisflood")), hint=MODEL_HINT)
            self.anuga_chip.set_state(bool(m.get("anuga")), "" if m.get("anuga") else
                                      "only the ANUGA tier needs it", hint=MODEL_HINT,
                                      optional=True)
        except Exception:                                        # noqa
            self.lis_chip.set_state(None, hint=MODEL_HINT)
            self.anuga_chip.set_state(None, hint=MODEL_HINT)

    def eventFilter(self, obj, event):
        from qgis.PyQt.QtCore import QEvent
        from qgis.PyQt.QtWidgets import QAbstractSpinBox, QComboBox
        wheel = getattr(getattr(QEvent, "Type", QEvent), "Wheel", None)
        if wheel is not None and event.type() == wheel and isinstance(obj, (QAbstractSpinBox, QComboBox)):
            if not obj.hasFocus():
                event.ignore()
                return True
        return super().eventFilter(obj, event)

    def changeEvent(self, event):
        try:
            super().changeEvent(event)
        except Exception:                                        # noqa
            pass
        try:
            if self.isActiveWindow():
                self.refresh()
        except Exception:                                        # noqa
            pass

    # ------------------------------------------------------------------- tools --
    def _open_chain(self, s):
        """3.10.67: the Batch chain window; an empty output folder / DEM from this window."""
        try:
            from .chain_dialog import ChainDialog
            dlg = ChainDialog(self, self.iface)
            if not dlg.out.text().strip() and s.get("dams_folder"):
                dlg.out.setText(s["dams_folder"])
            if not dlg.dem.text().strip() and s.get("dem"):
                dlg.dem.setText(s["dem"])
            self.chain_dialog = dlg
            dlg.show()
            self._log(">>> Opened the Batch chain window")
            return True
        except Exception as e:                                   # noqa
            self._log("!!! The Batch chain window could not be opened: {}".format(e))
            QMessageBox.information(self, TITLE, "The Batch chain window could not be opened "
                                                 "here ({}).".format(e))
            return False

    def _open_tool(self, alg):
        """Open the wizard or the Processing dialog of ndbreach:<alg>, the dam filled in."""
        if alg == pl.WIZARD:
            try:
                from . import wizard
                wizard.run_wizard(self.iface)
                self._log(">>> Opened the guided wizard")
                return True
            except Exception as e:                               # noqa
                self._log("!!! The wizard could not be opened: {}".format(e))
                QMessageBox.information(self, TITLE, "The wizard could not be opened here ({}).".format(e))
                return False
        s = self.settings()
        if alg == pl.CHAIN:
            return self._open_chain(s)
        params = pl.defaults(alg, s["dams_folder"], s["dam_id"], s)
        aid = "{}:{}".format(pl.PROVIDER, alg)
        try:
            try:
                import processing
            except ImportError:
                from qgis import processing
            exec_tool_dialog(processing, aid, params, parent=self)
        except Exception as e:                                   # noqa
            self._log("!!! {} could not be opened: {}".format(aid, e))
            QMessageBox.information(
                self, TITLE,
                "The Processing dialog of {} could not be opened here ({}).\n\nOpen it from the "
                "Processing Toolbox: DamSafety Runner > {}.".format(aid, e, pl.TOOL_LABEL.get(alg, alg)))
            return False
        self._log(">>> Opened {} ({})".format(aid, ", ".join(
            "{}={}".format(k, os.path.basename(str(v)) if os.path.isabs(str(v)) else v)
            for k, v in sorted(params.items())) or "no values filled"))
        self._save_state()
        return True

    def _open_suggested(self):
        if getattr(self, "_suggested", None):
            self._open_tool(self._suggested)

    def _add_results(self):
        """Load the dam folder's result files into the project, styled. Returns the count."""
        folder = self.folder()
        files = pl.result_files(folder) if folder else []
        if not files:
            return 0
        try:
            from qgis.core import QgsProject, QgsRasterLayer, QgsVectorLayer
        except Exception as e:                                   # noqa
            self._log("!!! layers cannot be added here: {}".format(e))
            return 0
        try:
            from . import suite_styles
        except Exception:                                        # noqa
            suite_styles = None
        project = QgsProject.instance()
        group = None
        try:
            root = project.layerTreeRoot()
            name = "Breach: {}".format(os.path.basename(os.path.normpath(folder)))
            group = root.findGroup(name) or root.addGroup(name)
        except Exception:                                        # noqa
            group = None
        added = 0
        for kind, path in files:
            try:
                title = os.path.splitext(os.path.basename(path))[0]
                layer = QgsRasterLayer(path, title) if kind == "raster" else QgsVectorLayer(path, title, "ogr")
                if not layer.isValid():
                    self._log("!!! {} could not be read as a layer".format(path))
                    continue
                if suite_styles is not None:
                    suite_styles.apply(layer, suite_styles.style_for_path(path), log=self._log)
                if group is not None:
                    project.addMapLayer(layer, False)
                    group.addLayer(layer)
                else:
                    project.addMapLayer(layer)
                added += 1
            except Exception as e:                               # noqa
                self._log("!!! {}: {}".format(path, e))
        self._log("Added {} result layer(s) of {} to the project".format(added, folder))
        return added

    def _open_dam_folder(self):
        folder = self.folder()
        if folder and os.path.isdir(folder):
            QDesktopServices.openUrl(QUrl.fromLocalFile(folder))

    def _open_other(self, folder_name, label):
        try:
            from qgis.utils import plugins
            plug = plugins.get(folder_name)
            if plug is None:
                raise KeyError("{} is not installed or not enabled".format(label))
            plug.run_plugin()
        except Exception as e:                                   # noqa
            QMessageBox.information(self, TITLE, "{} could not be opened ({}).".format(label, e))

    def _model_paths(self):
        try:
            try:
                self.iface.showOptionsDialog(self.iface.mainWindow(), currentPage="processingOptions")
            except TypeError:
                self.iface.showOptionsDialog(self.iface.mainWindow())
            self._refresh_models()
        except Exception as e:                                   # noqa
            QMessageBox.information(self, TITLE, "The options could not be opened ({}). Use Settings > "
                                    "Options > Processing > Providers > DamSafety Runner.".format(e))

    def _open_help(self):
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "README.md")
        if os.path.isfile(p):
            QDesktopServices.openUrl(QUrl.fromLocalFile(p))

    def closeEvent(self, event):
        self._save_state()
        save_size(self, SIZE_KEY)
        try:
            event.accept()
        except Exception:                                        # noqa
            pass
