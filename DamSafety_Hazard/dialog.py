# -*- coding: utf-8 -*-
"""Dialog UI and orchestration of DamSafety Hazard.

2.18.34: three views - Classify (setup, imports, data and output, the classification
options, check panel, Classify), Results (the classes and the tools for the last run) and
Review (structures; roads & facilities) - and a gear menu (suite settings, documentation,
self-test, data sources). Until 2.18.33 these were nine tabs."""

import os
import html
import time
import traceback

from .ui_fit import fit_window, wrap_tabs
from .ui_widgets import CheckPanel, Collapsible, InfoBanner
from qgis.PyQt.QtWidgets import (
    QApplication, QProgressDialog, QDialog, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QLineEdit, QFileDialog, QMessageBox, QGroupBox,
    QFormLayout, QTabWidget, QTextEdit, QWidget, QComboBox, QMenu, QToolButton,
)

from qgis.core import QgsProject, QgsRasterLayer

from .compat import MSG_INFO, MSG_WARNING, MSG_CRITICAL, WINDOW_MODAL
from .helpers import log_message, CancelledByUser, plugin_group, dam_display_name

from .constants import (
    PLUGIN_NAME, PLUGIN_VERSION, OUTPUT_FOLDER_NAME,
    BTN_ACTIVE_STYLE, BTN_INACTIVE_STYLE, README_TEXT, PROGRESS_MIN_INTERVAL_S, DISCLAIMER,
)
from .extent import make_extent_clip
from .classify_tab import ClassificationTabMixin
from .extras_tab import ExtrasTabMixin
from .extras290_tab import Extras290TabMixin
from .suite_tab import SuiteTabMixin
from .review_tab import ReviewTabMixin
from .dsswise_tab import DssWiseTabMixin
from .rasimport_tab import RasImportTabMixin
from .data_tab import DataTabMixin
from .datafolder_tab import DataFolderTabMixin
from .facility_review_tab import FacilityReviewTabMixin
from .suite_settings_tab import SuiteSettingsTabMixin
from . import facility_review_tab as FT
from .settings import save_state, load_state, widget_value, set_widget_value

# the raster box: what an unticked box means (its fields are greyed out then)
RASTER_GROUP_TIP = ("Unticked: the boxes are filled from the dam folder (Find rasters, or "
                    "Classify when Depth is empty). Tick to choose the files yourself. Depth "
                    "is always needed and Velocity for the 2D criterion; Arrival and "
                    "Depth x Velocity are optional.")


def _enum(cls, scope, name):
    """cls.<name> on Qt5, cls.<scope>.<name> on Qt6."""
    return getattr(getattr(cls, scope, cls), name, None) or getattr(cls, name)

class NDDamHazardDialog(QDialog, DataTabMixin, DataFolderTabMixin, ClassificationTabMixin,
                        ExtrasTabMixin, Extras290TabMixin, SuiteTabMixin, ReviewTabMixin, SuiteSettingsTabMixin,
                        FacilityReviewTabMixin,
                        DssWiseTabMixin, RasImportTabMixin):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.config_dam_folder = ""
        self.config_output_folder = ""
        self._rasters = {}
        self._progress = None
        self._last_tick = 0.0
        self._last_pct = 0.0
        self._busy = False
        self._last_run = None
        self._chain = None              # (lo, hi) progress range of a nested sync step
        self._task = None               # v2.6: the background task while it runs
        self._finishing = False         # main-thread part of a background run
        self._project_gen = 0           # bumped when another project is opened
        self._restoring = False         # settings being restored: no raster search
        self.setup_ui()
        self.dam_folder_edit.textChanged.connect(self._on_dam_folder_changed)
        for e in (self.depth_edit, self.vel_edit, self.arr_edit, self.dv_edit):
            e.textChanged.connect(self._update_button_states)
            e.textChanged.connect(self.check_panel.clear)   # 2.18.32: old results are stale
        try:
            self.model_combo.currentIndexChanged.connect(self._update_button_states)
            self.model_combo.currentIndexChanged.connect(self.check_panel.clear)
        except Exception:
            pass
        self._defaults = self._current_state()
        self._restore_settings()
        self._update_button_states()
        # the dialog outlives a Project > Open: re-read that project's settings
        try:
            QgsProject.instance().readProject.connect(self._on_project_changed)
            QgsProject.instance().cleared.connect(self._on_project_changed)
        except Exception:
            pass
        try:
            QgsProject.instance().writeProject.connect(self._on_write_project)
        except Exception:
            pass

    # ------------------------------------------------------------------ UI --
    # 2.18.34: three views (Classify / Results / Review) and a gear menu instead of nine tabs
    VIEWS = ("Classify", "Results", "Review")

    def setup_ui(self):
        self.setWindowTitle("{} {}".format(PLUGIN_NAME, PLUGIN_VERSION))
        self.setObjectName("DamSafetyHazardDialog")
        self.setMinimumSize(640, 420)
        main_layout = QVBoxLayout(self)
        main_layout.setSpacing(6)
        main_layout.addWidget(InfoBanner(DISCLAIMER))

        self.tab_widget = QTabWidget()
        self._dss_tab = self._build_dsswise_tab()
        self._ras_tab = self._build_ras_tab()
        self.tab_widget.addTab(self._build_classify_view(), "Classify")
        self.tab_widget.addTab(self._build_results_panel(), "Results")
        self.tab_widget.addTab(self._build_review_view(), "Review")
        self.tab_widget.setCornerWidget(self._build_gear())
        self.tab_widget.setCurrentIndex(0)
        self.tab_widget.currentChanged.connect(self._on_tab_changed)
        main_layout.addWidget(self.tab_widget, 1)
        main_layout.addWidget(self.action_area)   # Classify view only (_on_tab_changed)

        self.status_label = QLabel("Ready \u2014 pick the dam folder")
        self.status_label.setWordWrap(True)
        self.status_label.setStyleSheet("padding: 6px; background-color: #f5f5f5;")
        main_layout.addWidget(self.status_label)

        self.log_text = QTextEdit()
        self.log_text.setMaximumHeight(150)
        self.log_text.setReadOnly(True)
        self.log_section = Collapsible("Log", self.log_text)
        main_layout.addWidget(self.log_section)
        row = QHBoxLayout()                       # Close in its own row, bottom right
        row.addStretch(1)
        self.close_btn = QPushButton("Close")
        self.close_btn.setAutoDefault(False)
        self.close_btn.clicked.connect(self.close)
        row.addWidget(self.close_btn)
        main_layout.addLayout(row)

        self._build_settings_dialog()             # hidden; the gear menu shows them
        self._build_readme_dialog()
        wrap_tabs(self.tab_widget)                # after every widget is in place
        self.classify_scroll = self.tab_widget.widget(self._view_index("Classify"))
        fit_window(self, (1000, 720))
        self._refresh_layer_combos()              # once, the data and option lists exist now

    # -- Classify view
    def _build_classify_view(self):
        """Setup (dam folder, rasters), the two imports, Data and output, the
        classification options and the dam suite group (the page wrap_tabs scrolls). The
        check panel and the action bar (self.action_area) sit under the tabs, so the red
        banner and Classify are always in sight on this view."""
        body = QWidget()
        lay = QVBoxLayout(body)
        lay.setContentsMargins(6, 6, 6, 6)

        dam_group = QGroupBox("Setup \u2014 dam folder")
        dam_layout = QHBoxLayout()
        self.dam_folder_edit = QLineEdit()
        self.dam_folder_edit.setPlaceholderText("Browse or paste the dam project folder")
        self.dam_browse_btn = QPushButton("Browse\u2026")
        self.dam_browse_btn.setToolTip("Pick the dam project folder")
        self.dam_browse_btn.clicked.connect(self.browse_dam_folder)
        self.find_rasters_btn = QPushButton("Find rasters")
        self.find_rasters_btn.setToolTip(
            "Look through the dam folder for HEC-RAS (RAS Mapper) / DSS-WISE exports: "
            "Depth (Max), Velocity (Max), Arrival Time and D * V (Max). Done automatically "
            "when the raster boxes are empty.")
        self.find_rasters_btn.clicked.connect(lambda *a: self._auto_fill_rasters(force=True))
        dam_layout.addWidget(self.dam_folder_edit, stretch=3)
        dam_layout.addWidget(self.dam_browse_btn)
        dam_layout.addWidget(self.find_rasters_btn)
        dam_group.setLayout(dam_layout)
        lay.addWidget(dam_group)

        raster_group = QGroupBox("Rasters \u2014 tick to choose them yourself")
        raster_group.setCheckable(True)
        raster_group.setChecked(False)
        raster_group.setToolTip(RASTER_GROUP_TIP)
        self.raster_group = raster_group     # _set_busy: its children follow its tick
        raster_layout = QFormLayout()
        self.raster_hint = QLabel(RASTER_GROUP_TIP)
        self.raster_hint.setWordWrap(True)
        self.raster_hint.setStyleSheet("color:#666;")
        self.depth_edit = QLineEdit()
        self.depth_browse_btn = QPushButton("Browse\u2026")
        self.depth_browse_btn.clicked.connect(self.browse_depth)
        raster_layout.addRow("Depth (Hmax):",
                             self._inline_row(self.depth_edit, self.depth_browse_btn))
        self.vel_edit = QLineEdit()
        self.vel_browse_btn = QPushButton("Browse\u2026")
        self.vel_browse_btn.clicked.connect(self.browse_velocity)
        raster_layout.addRow("Velocity (Vmax):",
                             self._inline_row(self.vel_edit, self.vel_browse_btn))
        self.arr_edit = QLineEdit()
        self.arr_edit.setPlaceholderText("optional - without it there are no warning times")
        self.arr_browse_btn = QPushButton("Browse\u2026")
        self.arr_browse_btn.clicked.connect(self.browse_arrival)
        raster_layout.addRow("Arrival Time:",
                             self._inline_row(self.arr_edit, self.arr_browse_btn))
        self.dv_edit = QLineEdit()
        self.dv_edit.setPlaceholderText("optional - RAS Mapper 'Depth x Velocity' (Max), "
                                        "DSS-WISE DV; flood severity for the loss-of-life "
                                        "estimate (ACER zones use Dmax / Vmax)")
        self.dv_browse_btn = QPushButton("Browse\u2026")
        self.dv_browse_btn.clicked.connect(
            lambda: self._browse_raster(self.dv_edit, "Select Depth x Velocity (max) raster"))
        raster_layout.addRow("Depth x Velocity (ft\u00b2/s):",
                             self._inline_row(self.dv_edit, self.dv_browse_btn))
        # the breach model (was in the classification tab's Hydraulics group)
        self.model_combo = QComboBox()
        self.model_combo.addItem("2D model - ACER depth-velocity curves, Dmax / Vmax "
                                 "(ND Figs 5.1/5.2)", "2d")
        self.model_combo.addItem("1D model - habitable structure depth > 2 ft", "1d")
        self.model_combo.setToolTip("Set automatically by the DSS-WISE and HEC-RAS imports.")
        self.model_combo.setSizeAdjustPolicy(
            _enum(QComboBox, "SizeAdjustPolicy", "AdjustToMinimumContentsLengthWithIcon"))
        self.model_combo.setMinimumContentsLength(24)
        raster_layout.addRow("Breach model:", self.model_combo)
        raster_group.setLayout(raster_layout)
        lay.addWidget(raster_group)
        lay.addWidget(self.raster_hint)       # outside the box: readable while it is off

        self.dss_section = Collapsible("Import DSS-WISE Lite results", self._dss_tab)
        self.ras_section = Collapsible("Import HEC-RAS plan results", self._ras_tab)
        self.data_section = Collapsible("Data and output", self._build_data_tab(scroll=False))
        for sec in (self.dss_section, self.ras_section, self.data_section):
            lay.addWidget(sec)

        head = QLabel("<b>Classification options</b> \u2014 the ND defaults are preset; "
                      "open a section to change it.")
        head.setWordWrap(True)
        head.setStyleSheet("padding-top: 6px;")
        lay.addWidget(head)
        self.option_sections = {}
        for title, grp, expanded in self._build_classification_options():
            sec = Collapsible(title, grp, expanded=expanded)
            self.option_sections[title] = sec
            lay.addWidget(sec)
        if getattr(self, "suite_group", None) is not None:
            lay.addWidget(self.suite_group)       # always shown (v2.10)
        lay.addStretch(1)

        # 2.18.32: check results by severity (ui_widgets.CheckPanel); under the tabs, so the
        # red banner is seen next to the button it blocks
        self.action_area = QWidget()
        outer = QVBoxLayout(self.action_area)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(4)
        self.check_btn = QPushButton("Check inputs")
        self.check_btn.setToolTip("Checks the rasters (units, water-surface raster given as "
                                  "depth, arrival in minutes, missing velocity, coordinate "
                                  "systems), the outline file and the download services.")
        self.check_btn.setAutoDefault(False)
        self.check_btn.clicked.connect(self._on_check_inputs)
        self.classify_btn = QPushButton("Classify")
        self.classify_btn.setMinimumHeight(36)
        self.classify_btn.setMinimumWidth(160)
        self.classify_btn.setStyleSheet("font-size: 14px; font-weight: bold;")
        self.classify_btn.setToolTip(
            "Finds the depth, velocity and arrival rasters when the boxes are empty, checks "
            "them, saves the data sets first when 'Data and output' asks for it, and "
            "classifies. The result opens on the Results tab.")
        self.classify_btn.setDefault(True)
        self.classify_btn.clicked.connect(self._classify_simple)
        self.check_panel = CheckPanel(self.check_btn, self.classify_btn, action="classifying")
        self.check_panel.errors_fixed.connect(lambda _ok: self._update_button_states())
        outer.addWidget(self.check_panel)
        bar = QHBoxLayout()
        self.next_label = QLabel("Classify \u2192 the classes and the report on Results \u2192 "
                                 "your decisions on Review")
        self.next_label.setToolTip(
            "Classify finds the habitable structures, overtopped roads (AADT) and inundated "
            "railroads, estimates the consequences, classifies the dam (REG_05.2026 Sec. 5.2, "
            "ACER TM 11) and writes the report.")
        self.next_label.setWordWrap(True)
        self.next_label.setStyleSheet("color:#666;")
        bar.addWidget(self.next_label, 1)
        bar.addWidget(self.check_btn)
        bar.addWidget(self.classify_btn)
        outer.addLayout(bar)
        return body

    # -- Review view
    def _build_review_view(self):
        """The two review tables as sub-tabs. They stay two tables: each one's decisions
        are tied to its own row list (_review_rows / _facrev_rows)."""
        self.review_tabs = QTabWidget()
        self.review_tabs.addTab(self._build_review_tab(), "Structures")
        self.review_tabs.addTab(self._build_facility_review_tab(),
                                FT.TAB_TITLE.replace("Review: ", "").replace("&", "&&")
                                .capitalize())
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(4, 4, 4, 4)
        v.addWidget(self.review_tabs)
        return page

    # -- gear menu, settings and README windows
    def _build_gear(self):
        self.gear_btn = QToolButton()
        self.gear_btn.setText("\u2699")
        self.gear_btn.setToolTip("Suite settings, documentation, self-test, data sources")
        self.gear_btn.setPopupMode(_enum(QToolButton, "ToolButtonPopupMode", "InstantPopup"))
        self.gear_btn.setAutoRaise(True)
        self.gear_btn.setStyleSheet("QToolButton {font-size: 18px; padding: 0 8px 2px 8px;}")
        menu = QMenu(self.gear_btn)
        self.settings_action = menu.addAction("Suite settings\u2026")
        self.settings_action.triggered.connect(lambda _c=False: self._open_suite_settings())
        self.readme_action = menu.addAction("Documentation (README)\u2026")
        self.readme_action.triggered.connect(lambda _c=False: self._open_readme())
        menu.addSeparator()
        self.selftest_action = menu.addAction("Run self-test")
        self.selftest_action.setToolTip("Runs the full pipeline on synthetic data in a "
                                        "temporary folder. Your project is not changed.")
        self.selftest_action.triggered.connect(lambda _c=False: self._on_run_selftest())
        self.sources_action = menu.addAction("Edit data sources\u2026")
        self.sources_action.triggered.connect(lambda _c=False: self._on_edit_sources())
        self.gear_menu = menu
        self.gear_btn.setMenu(menu)
        return self.gear_btn

    def _build_settings_dialog(self):
        """The suite settings grid in its own non-modal window (was a tab). Built once,
        hidden; a grid that cannot be built never stops the window (v2.13 guard)."""
        self.suite_settings_dialog = None
        try:
            page = self._build_suite_settings_tab()
        except Exception as e:                                   # noqa - the grid must never stop the window
            log_message("Suite settings window not built: {}".format(e))
            self.suite_settings_error = str(e) or repr(e)
            return
        dlg = QDialog(self)
        dlg.setWindowTitle("{} \u2014 Suite settings".format(PLUGIN_NAME))
        dlg.setModal(False)
        v = QVBoxLayout(dlg)
        v.addWidget(page, 1)
        row = QHBoxLayout()
        row.addStretch(1)
        done = QPushButton("Close")
        done.clicked.connect(dlg.close)
        row.addWidget(done)
        v.addLayout(row)
        dlg.resize(940, 640)
        self.suite_settings_dialog = dlg

    def _build_readme_dialog(self):
        """The README with the self-test and data-source buttons (was the Readme tab).
        Built once with the window, so _set_busy reaches its buttons before it is opened."""
        dlg = QDialog(self)
        dlg.setWindowTitle("{} \u2014 Documentation".format(PLUGIN_NAME))
        dlg.setModal(False)
        v = QVBoxLayout(dlg)
        readme_text = QTextEdit()
        readme_text.setReadOnly(True)
        readme_text.setPlainText(README_TEXT)
        readme_text.setStyleSheet(
            "font-family: 'Menlo', 'Courier New', monospace; font-size: 12px;")
        v.addWidget(readme_text, 1)
        row = QHBoxLayout()
        self.selftest_btn = QPushButton("Run self-test")
        self.selftest_btn.setToolTip("Runs the full pipeline on synthetic data in a "
                                     "temporary folder. Your project is not changed.")
        self.selftest_btn.clicked.connect(lambda _c=False: self._on_run_selftest())
        row.addWidget(self.selftest_btn)
        self.sources_btn = QPushButton("Edit data sources\u2026")
        self.sources_btn.setToolTip("Opens the file with every download address (NSI, ND GIS "
                                    "Hub, NID, HIFLD, NBI ...). Change an address there when "
                                    "a service moves, then restart QGIS.")
        self.sources_btn.clicked.connect(self._on_edit_sources)
        row.addWidget(self.sources_btn)
        row.addStretch(1)
        done = QPushButton("Close")
        done.clicked.connect(dlg.close)
        row.addWidget(done)
        v.addLayout(row)
        dlg.resize(820, 640)
        self.readme_text = readme_text
        self.readme_dialog = dlg

    @staticmethod
    def _raise(dlg):
        dlg.show()
        try:
            dlg.raise_()
            dlg.activateWindow()
        except Exception:                                        # noqa
            pass

    def _open_suite_settings(self):
        dlg = getattr(self, "suite_settings_dialog", None)
        if dlg is None:
            QMessageBox.warning(self, "Suite settings", "The suite settings grid could not be "
                                "built: {}".format(getattr(self, "suite_settings_error", "?")))
            return None
        self._raise(dlg)
        return dlg

    def _open_readme(self):
        self._raise(self.readme_dialog)
        return self.readme_dialog

    # -- the views
    def _view_index(self, name):
        """Index of a view by its tab text (wrap_tabs replaces the page widgets, so the
        text is the stable key)."""
        for i in range(self.tab_widget.count()):
            if self.tab_widget.tabText(i).replace("&&", "&") == name:
                return i
        return -1

    def _show_view(self, name):
        i = self._view_index(name)
        if i >= 0:
            self.tab_widget.setCurrentIndex(i)
        return i

    def _show_input_errors(self, errors):
        """Input errors in the check panel (red banner, Classify blocked) on the Classify
        view - since 2.18.34 instead of a pop-up."""
        from .checks import ERROR
        self.check_panel.set_results([(ERROR, e) for e in errors])
        self._update_status("Inputs need fixing: {} error(s) - see the red box".format(
            len(errors)), False)
        for e in errors:
            self._log(">>> INPUT ERROR: {}".format(e))
        self._show_view("Classify")
        self._update_button_states()

    @property
    def _checks_block(self):
        """True while the check panel shows errors: Classify stays off."""
        panel = self.__dict__.get("check_panel")
        return bool(panel is not None and panel.blocked)

    def _on_edit_sources(self, *args):
        from .constants import write_sources_file, SOURCE_OVERRIDES
        from qgis.PyQt.QtCore import QUrl
        from qgis.PyQt.QtGui import QDesktopServices
        try:
            path = write_sources_file()
        except OSError as e:
            QMessageBox.warning(self, "Data sources", "Could not write the file: {}".format(e))
            return
        self._log(">>> Data-source file: {}{}".format(
            path, " (in use: {} change(s))".format(len(SOURCE_OVERRIDES))
            if SOURCE_OVERRIDES else ""))
        QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def _on_tab_changed(self, index):
        # the Classify view holds the layer lists (data and options): list the project's
        # current layers whenever it is shown again
        classify = self.tab_widget.tabText(index).replace("&&", "&") == "Classify"
        area = self.__dict__.get("action_area")
        if area is not None:
            area.setVisible(classify)
        if classify:
            self._refresh_layer_combos()

    def _inline_row(self, edit, btn):
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        h.addWidget(edit, stretch=3)
        h.addWidget(btn)
        return w

    # ------------------------------------------------------------ inputs ----
    def _raster_paths(self):
        return {"depth": self.depth_edit.text().strip(),
                "vel": self.vel_edit.text().strip(),
                "arr": self.arr_edit.text().strip()}

    def _dv_path(self):
        p = self.dv_edit.text().strip()
        return p if p and os.path.isfile(p) else None

    def _one_d_selected(self):
        combo = getattr(self, "model_combo", None)
        return combo is not None and combo.currentData() == "1d"

    def _required_rasters(self):
        """Depth is always needed; velocity too (ACER zones from Dmax / Vmax)
        except for the 1D (depth-only) criterion. Arrival is optional
        (arrival fields and warning times are then left empty)."""
        keys = ["depth"]
        if not self._one_d_selected():
            keys.append("vel")
        return keys

    def _inputs_valid_for_main(self):
        paths = self._raster_paths()
        return (bool(self.config_dam_folder) and os.path.isdir(self.config_dam_folder)
                and all(paths[k] and os.path.isfile(paths[k]) for k in self._required_rasters())
                and (not paths["vel"] or os.path.isfile(paths["vel"])))

    def _update_button_states(self, *args):
        busy = self._busy
        ready = self._inputs_valid_for_main() and not busy
        if hasattr(self, "run_all_btn"):
            self.run_all_btn.setEnabled(ready)
            self.run_all_btn.setStyleSheet(BTN_ACTIVE_STYLE if ready else BTN_INACTIVE_STYLE)
            if ready:
                self.run_all_btn.setToolTip("All inputs selected - ready to run")
            elif busy:
                self.run_all_btn.setToolTip("A run is in progress")
            else:
                missing = []
                if not self.config_dam_folder or not os.path.isdir(self.config_dam_folder):
                    missing.append("Dam folder")
                labels = {"depth": "Depth raster", "vel": "Velocity raster",
                          "arr": "Arrival raster"}
                for key, path in self._raster_paths().items():
                    if key in self._required_rasters() and (not path or not os.path.isfile(path)):
                        missing.append(labels[key])
                self.run_all_btn.setToolTip("Missing: {}".format(", ".join(missing)))
        self._classification_button_state()

    # ------------------------------------------------------------ BROWSERS --
    def browse_dam_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Select Dam Project Folder",
                                                  self.config_dam_folder or "")
        if folder:
            self.dam_folder_edit.setText(folder)   # -> _on_dam_folder_changed
        self._update_button_states()

    def _on_dam_folder_changed(self, text):
        folder = (text or "").strip()
        if folder in ("/", "", os.path.sep) or (folder and not os.path.isdir(folder)):
            folder = ""
            self.dam_folder_edit.setText("")
            self.status_label.setText("Dam folder must be a real directory, not / or empty.")
        self.config_dam_folder = folder
        self.config_output_folder = os.path.join(folder, OUTPUT_FOLDER_NAME) if folder else ""
        self.output_folder_edit.setText(self.config_output_folder)
        self.check_panel.clear()                       # 2.18.34: results of another dam
        if not self._restoring:
            n = self._auto_fill_rasters(force=False)
            if folder:
                self._update_status("Dam folder: {} - {}".format(
                    os.path.basename(folder.rstrip("/\\")) or folder,
                    "{} raster(s) found; Check inputs, then Classify".format(n) if n else
                    "check the rasters, then Classify"), True)
        self._update_button_states()
        self._refresh_dam_record()                     # v2.10: dam.json status line

    def _auto_fill_rasters(self, force=False):
        """Fill the raster boxes from the files found in the dam folder: all
        of them with force (the "Find rasters" button), otherwise only when
        every raster box is still empty. Returns the number filled."""
        folder = self.config_dam_folder
        if not folder or folder in ("/", os.path.sep) or not os.path.isdir(folder):
            return 0
        edits = [("depth", self.depth_edit), ("vel", self.vel_edit), ("arr", self.arr_edit),
                 ("dv", self.dv_edit)]
        base = []          # v2.5.1: no no-breach baseline / time steps in the dialog
        if not force and any(e.text().strip() for _k, e in edits):
            return 0
        from .autodetect import find_rasters, describe
        found = find_rasters(folder)
        n = 0
        for key, edit in edits + base:
            if edit is None or not found.get(key):
                continue
            if force or not edit.text().strip():
                edit.setText(found[key])
                n += 1
        self._log(">>> Rasters found in the dam folder: {}".format(describe(found, folder)))
        for note in found["notes"]:
            self._log("    " + note + " - change it on the Setup tab if needed")
        self._update_button_states()
        return n

    def _browse_raster(self, edit, title):
        start = os.path.dirname(edit.text().strip()) or self.config_dam_folder or ""
        path, _ = QFileDialog.getOpenFileName(
            self, title, start,
            "Raster Files (*.tif *.tiff *.TIF *.TIFF *.img *.asc *.vrt *.flt *.bil);;"
            "All Files (*)")
        if path:
            edit.setText(path)
        self._update_button_states()

    def browse_depth(self):
        self._browse_raster(self.depth_edit, "Select Depth Raster")

    def browse_velocity(self):
        self._browse_raster(self.vel_edit, "Select Velocity Raster")

    def browse_arrival(self):
        self._browse_raster(self.arr_edit, "Select Arrival Raster")

    def browse_output(self):
        folder = QFileDialog.getExistingDirectory(self, "Select Output Folder",
                                                  self.config_output_folder or "")
        if folder:
            self.config_output_folder = folder
            self.output_folder_edit.setText(folder)
        self._update_button_states()

    # ------------------------------------------------------------ VALIDATION
    def validate_all_inputs(self):
        errors = []
        if not self.config_dam_folder or not os.path.isdir(self.config_dam_folder):
            errors.append("Dam folder not selected (or folder missing)")
        labels = {"depth": "Depth", "vel": "Velocity", "arr": "Arrival"}
        for key, path in self._raster_paths().items():
            if key in self._required_rasters() and (not path or not os.path.isfile(path)):
                errors.append("{} raster not selected (or file missing)".format(labels[key]))
            elif path and not os.path.isfile(path):
                errors.append("{} raster file missing".format(labels[key]))
        if hasattr(self, "scenario_table"):
            errors.extend(self._scenario_problems())     # v2.7: the scenario table
        if hasattr(self, "sens_check"):
            errors.extend(self._extras_problems())       # v2.8: the extra analyses
            errors.extend(self._extras290_problems())    # v2.9
        return len(errors) == 0, errors

    # ------------------------------------------------------------ STATUS/LOG
    def _update_status(self, message, is_success=True):
        color = "#2e7d32" if is_success else "#c62828"
        self.status_label.setText(message)
        self.status_label.setStyleSheet(
            "padding: 8px; background-color: #f5f5f5; color: {};".format(color))

    def _log(self, message):
        self.log_text.append(str(message))
        sb = self.log_text.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _load_rasters(self):
        """(Re)load the three rasters from the current paths."""
        try:
            specs = [("depth", "Depth"), ("vel", "Velocity"), ("arr", "Arrival")]
            paths = self._raster_paths()
            rasters, bad = {}, []
            for key, name in specs:
                if not paths[key] and key not in self._required_rasters():
                    rasters[key] = None
                    self._log(">>> {} raster not given - {}".format(
                        name, "arrival times and warning times are left empty"
                        if key == "arr" else "depth-only (1D) criterion"))
                    continue
                lyr = QgsRasterLayer(paths[key], name)
                rasters[key] = lyr
                if not lyr.isValid():
                    bad.append(name)
                elif not lyr.crs().isValid():
                    self._log(">>> WARNING: {} raster has no CRS - coordinates are "
                              "assumed to match the point layers".format(name))
            self._rasters = rasters
            if bad:
                return False, "These rasters failed to load: {}".format(", ".join(bad))
            return True, "OK"
        except Exception as e:
            return False, str(e)

    def _study_area(self, ctx):
        """ExtentClip for the loaded depth raster (None: no clipping) and
        the keep-dry choice; logs both."""
        clip = make_extent_clip(self._rasters.get("depth"), self.extent_spin.value(), ctx)
        keep_dry = self.keep_dry_check.isChecked()
        if clip is not None:
            self._log(">>> Study area: {} (features without flood values {})".format(
                clip.describe(), "kept" if keep_dry else "dropped"))
        else:
            self._log(">>> WARNING: study area unavailable - features limited to raster "
                      "coverage")
        return clip, keep_dry

    # ------------------------------------------------------------- PROGRESS
    def _make_progress(self, title, modal=True):
        if self._chain is not None and self._progress is not None:
            self._progress.setLabelText(title)          # one bar for a chained step
            return self._progress
        prog = QProgressDialog(title, "Cancel", 0, 100, self)
        prog.setWindowTitle(PLUGIN_NAME)
        # Modal (synchronous steps): blocks the plugin dialog and QGIS while
        # the step pumps events. Background runs (v2.6) use a non-modal bar:
        # QGIS stays usable, the dialog's buttons are disabled meanwhile.
        if modal:
            prog.setWindowModality(WINDOW_MODAL)
        prog.setMinimumDuration(0)
        prog.setAutoClose(False)
        prog.setAutoReset(False)
        prog.setValue(0)
        prog.show()
        self._last_tick = 0.0
        self._last_pct = 0.0
        QApplication.processEvents()
        return prog

    def _progress_tick(self, pct, message):
        if self._progress is None:
            return
        pct = min(max(float(pct), 0.0), 100.0)
        if self._chain is not None:
            lo, hi = self._chain
            pct = lo + (hi - lo) * pct / 100.0
        pct = max(self._last_pct, pct)
        now = time.monotonic()
        # Refreshing the UI is comparatively expensive; do it at most every
        # PROGRESS_MIN_INTERVAL_S unless the run is finishing.
        if pct < 100.0 and now - self._last_tick < PROGRESS_MIN_INTERVAL_S:
            return
        self._last_tick = now
        self._last_pct = pct
        self._progress.setValue(int(pct))
        self._progress.setLabelText(message)
        QApplication.processEvents()
        # A click on Cancel after the last step has finished is ignored
        if pct < 100.0 and self._progress.wasCanceled():
            raise CancelledByUser()

    def _finish_progress(self, force=False):
        if self._chain is not None and not force:
            return
        if self._progress:
            self._progress.close()
            self._progress.deleteLater()
            self._progress = None
        QApplication.processEvents()

    # ------------------------------------------------------- BACKGROUND RUNS
    def _start_background(self, title, work, on_done, project_bound=True):
        """v2.6: run work(progress, log) in a QGIS task - QGIS stays usable,
        the dialog is busy. on_done(outcome) runs on the main thread when it
        has finished (outcome.result / .error / .cancelled). Without a task
        manager (tests, headless) the work runs at once, same callbacks.
        project_bound=False (v2.6.2): the work does not belong to the current
        project (offline pack build) - on_done runs even when another
        project was opened meanwhile."""
        from .runner import start_task
        self._set_busy(True)
        self._chain = None
        self._progress = self._make_progress(title, modal=False)
        try:                                     # the bar's Cancel stops the task
            self._progress.canceled.connect(self._cancel_task)
        except Exception:
            pass

        gen = self._project_gen

        def done(outcome):
            self._task = None
            try:
                if project_bound and outcome.ok and gen != self._project_gen:
                    self._log(">>> The project changed while the run was working - the "
                              "outputs are written in {} but not added to this project"
                              .format(self.config_output_folder))
                    self._update_status("Finished - project changed during the run; outputs "
                                        "in the outputs folder", False)
                    return
                on_done(outcome)
            finally:
                self._finishing = False
                self._finish_progress(force=True)
                self._set_busy(False)
        try:
            self._task = start_task(title, work, self._task_progress, self._log, done)
        except Exception as e:                   # the task could not even be started
            tb = traceback.format_exc()
            self._log(tb)
            log_message(tb, MSG_CRITICAL)
            self._finish_progress(force=True)
            self._set_busy(False)
            self._update_status("FAILED: {}".format(e), False)
            QMessageBox.critical(self, "Error", str(e) or repr(e))

    def _cancel_task(self, *args):
        if self._task is not None:
            try:
                self._task.cancel()
            except Exception:
                pass

    def _task_progress(self, pct, message):
        """Progress from the worker (main thread slot) - or from the work
        itself when it runs synchronously."""
        if self._progress is None or self._finishing:
            return                       # (a late worker signal: the finish bar is up)
        pct = min(max(float(pct), 0.0), 100.0)
        pct = max(self._last_pct, pct)
        now = time.monotonic()
        if pct < 100.0 and now - self._last_tick < PROGRESS_MIN_INTERVAL_S:
            return
        self._last_tick = now
        self._last_pct = pct
        self._progress.setValue(int(pct))
        self._progress.setLabelText(message)
        if self._task is None:
            QApplication.processEvents()         # synchronous fallback
        if pct < 100.0 and self._progress.wasCanceled():
            if self._task is not None:
                self._task.cancel()
            else:
                raise CancelledByUser()

    def _finish_deferred(self, res, title):
        """Main-thread part of a background run (layers, map book, report)
        behind a modal progress bar (modality cannot change on a shown bar,
        so a new one replaces the task's)."""
        from .pipeline import finish_run
        self._finishing = True
        self._finish_progress(force=True)
        self._chain = None
        self._progress = self._make_progress(title, modal=True)
        return finish_run(res, progress=self._progress_tick, log=self._log)

    def _set_busy(self, busy):
        self._busy = busy
        for name in ("check_btn", "run_all_btn", "find_rasters_btn", "dam_browse_btn",
                     "depth_browse_btn", "vel_browse_btn", "arr_browse_btn",
                     "output_browse_btn", "selftest_btn", "refresh_layers_btn",
                     # 2.18.34: the gear menu's run actions and the Results tools
                     "selftest_action", "sources_action", "submittal_btn", "watch_btn",
                     "dv_browse_btn", "review_zoom_btn", "review_webmap_btn", "cache_clear_btn",
                     "review_save_btn", "review_rerun_btn", "sources_btn",
                     "review_export_btn", "review_merge_btn", "series_btn",
                     "dsw_scan_btn", "dsw_import_btn", "ras_scan_btn", "ras_import_btn",
                     "data_download_btn", "data_sources_btn", "open_data_btn",
                     # v2.9
                     "datafolder_edit", "datafolder_browse_btn", "datafolder_search_btn",
                     "datafolder_hifld_btn", "datafolder_use_check",
                     *Extras290TabMixin.BUSY_WIDGETS_290,
                     *SuiteTabMixin.BUSY_WIDGETS_2100,
                     "facrev_kind_combo", "facrev_scenario_combo", "facrev_show_all_check",
                     "facrev_zoom_btn", "facrev_save_btn", "facrev_rerun_btn",
                     # v2.6.1: the pack must not change under a running task
                     "pack_path_edit", "pack_browse_btn", "pack_clear_btn",
                     "offline_only_check", "pack_build_btn"):
            w = getattr(self, name, None)
            if w is not None:
                w.setEnabled(not busy)
        if not busy and hasattr(self, "dsw_import_btn"):
            self.dsw_import_btn.setEnabled(bool(getattr(self, "_dsw_items", None)))
        group = getattr(self, "raster_group", None)
        if not busy and group is not None and group.isCheckable():
            # 2.18.19: an unticked raster box keeps its browse buttons off (Qt disabled them)
            for name in ("depth_browse_btn", "vel_browse_btn", "arr_browse_btn",
                         "dv_browse_btn"):
                w = getattr(self, name, None)
                if w is not None:
                    w.setEnabled(group.isChecked())
        self._update_button_states()

    def _group(self, category):
        """Layer-tree group path of this dam for a category."""
        return plugin_group(dam_display_name(self.config_dam_folder), category)

    # -------------------------------------------------- CHECK / RUN EVERYTHING
    def _check_paths(self):
        paths = dict(self._raster_paths())
        paths["dv"] = self.dv_edit.text().strip() or None
        return paths

    def _check_cfg(self):
        cfg = dict(self._data_cfg())         # the Data & Output tab choices
        cfg["one_d"] = self._one_d_selected()
        return cfg

    def _run_checks(self, services):
        from .checks import check_all
        return check_all(self._check_paths(), self.config_output_folder,
                         self.config_dam_folder, self._check_cfg(), services=services,
                         progress=self._progress_tick if services else None)

    @staticmethod
    def _checks_html(results):
        colors = {"ERROR": "#c62828", "WARNING": "#e65100", "OK": "#2e7d32"}
        return "<br>".join("<span style='color:{}'><b>{}</b></span> {}".format(
            colors.get(lvl, "#000"), lvl, html.escape(t)) for lvl, t in results)

    def _classify_simple(self, *args):
        """Find the Runner rasters if the boxes are empty, then check and classify."""
        if not self.depth_edit.text().strip():
            self._auto_fill_rasters(force=True)
        self._on_run_all()

    def _on_check_inputs(self, *args):
        if self._busy:
            return
        from .checks import summary
        self._set_busy(True)
        try:
            self._progress = self._make_progress("Checking inputs and data services...")
            results = self._run_checks(services=True)
        except CancelledByUser:
            self._log(">>> Input check cancelled")
            return
        except Exception as e:
            tb = traceback.format_exc()
            self._log(tb)
            log_message(tb, MSG_CRITICAL)
            self._update_status("Input check FAILED: {}".format(e), False)
            QMessageBox.critical(self, "Check Inputs Error",
                                 "{}\n\nFull details are in the log area below.".format(e))
            return
        finally:
            self._finish_progress()
            self._set_busy(False)
        for lvl, t in results:
            self._log(">>> CHECK {}: {}".format(lvl, t))
        n_err, n_warn = summary(results)
        self._update_status("Input check: {} error(s), {} warning(s)".format(n_err, n_warn),
                            n_err == 0)
        # 2.18.32: the results are shown in the panel under the buttons (no pop-up)
        self.check_panel.set_results(results)
        return results

    def _on_run_all(self, *args):
        """Check inputs, then (v2.6, one background task) save the data sets
        when asked and run the classification."""
        if self._busy:
            return
        valid, errors = self.validate_all_inputs()
        if not valid:
            self._show_input_errors(errors)       # 2.18.34: the red box, not a pop-up
            return
        from .checks import summary
        results = self._run_checks(services=False)
        for lvl, t in results:
            if lvl != "OK":
                self._log(">>> CHECK {}: {}".format(lvl, t))
        self.check_panel.set_results(results)
        n_err, n_warn = summary(results)
        if n_err:
            # 2.18.34: the red box on the Classify view says what to fix (was a pop-up)
            self._update_status("Inputs need fixing: {} error(s), {} warning(s) - see the red "
                                "box".format(n_err, n_warn), False)
            self._show_view("Classify")
            self._update_button_states()
            return
        download_first = self.data_save_check.isChecked()
        if download_first:
            try:
                self._download_job()
            except Exception as e:
                # optional step: the classification downloads what it needs
                self._log(">>> WARNING: the data sets cannot be saved to outputs/Data ({}) - "
                          "continuing without".format(e))
                download_first = False
        self._launch_classification(download_first=download_first, everything=True)

    # ------------------------------------------------------------ SELF-TEST
    def _on_run_selftest(self):
        if self._busy:
            return
        from .selftest import run_self_test
        self._set_busy(True)
        self._progress = self._make_progress("Running self-test...")
        try:
            ok, lines = run_self_test(progress_cb=self._progress_tick)
        except CancelledByUser:
            self._update_status("Self-test cancelled", False)
            return
        except Exception:
            ok, lines = False, ["Self-test crashed:"] + traceback.format_exc().splitlines()
        finally:
            self._finish_progress()
            self._set_busy(False)
        for line in lines:
            self._log(line)
            log_message(line, MSG_INFO if ok else MSG_WARNING)
        self._update_status("Self-test {}".format("PASSED" if ok else "FAILED"), ok)
        box = QMessageBox.information if ok else QMessageBox.warning
        box(self, "Self-Test", "<br>".join(html.escape(line) for line in lines))

    # ------------------------------------------------------------ SETTINGS --
    _SETTING_WIDGETS = (
        "dam_folder_edit", "output_folder_edit", "depth_edit", "vel_edit", "arr_edit",
        "dv_edit",
        "extent_spin", "keep_dry_check",
        "nsi_download_check", "addr_download_check", "usa_download_check", "model_combo",
        "judgment_check", "near_spin", "borderline_check", "ppd_spin", "min_area_spin",
        "occ_spin", "speed_spin", "lol_check", "warn_spin", "understanding_combo",
        "severity_combo", "population_combo", "access_check", "access_spin",
        "access_medium_check", "report_check", "pages_spin", "road_combo", "aadt_combo",
        "rail_combo", "crossing_combo", "outline_combo", "dsw_path_edit",
        "dams_download_check", "dam_layer_combo", "subject_nid_edit",
        "lifelines_download_check", "lifeline_combo", "lifeline_medium_check",
        "boundary_combo", "eap_check",
        "crossings_download_check", "spreadsheets_check",
        # v2.5
        "camps_download_check", "parcel_combo", "parcel_acres_spin", "compare_check",
        "eap_list_check", "eap_mapbook_check", "mapbook_scale_combo", "eap_travel_check",
        "centerline_combo", "flowlines_download_check", "station_spin",
        "ras_plan_edit", "ras_dem_edit", "ras_breach_spin", "ras_arrival_spin", "ras_cell_spin",
        # v2.5.1
        "data_area_combo", "flood_buffer_spin", "data_save_check",
        # v2.6
        "zone_rasters_check", "sampled_cells_check", "reuse_check",
        # v2.7
        "main_name_edit", "damage_curves_edit", "hcom_edit", "review_aerial_check",
        # v2.7.1
        "sample_area_combo",
        # v2.8 (the population layer before its field pickers: choosing the layer fills them)
        "sens_check", "sens_factors_edit", "sens_split_check", "term_check", "term_rise_spin",
        "term_depth_spin", "term_confirm_spin", "pop_source_combo", "pop_field_combo",
        "pop_day_combo", "pop_housing_combo", "pop_period_combo", "pop_units_combo",
        "pop_method_combo", "pop_mindepth_spin", "evac_check", "evac_prep_spin",
        "evac_passable_spin", "evac_include_combo", "evac_safe_spin", "cache_check",
        "cache_age_spin", "cache_refresh_check", "quick_check", "watch_workers_spin",
        # v2.9
        "datafolder_edit", "datafolder_use_check", "datafolder_picks_edit"
    ) + Extras290TabMixin.SETTING_WIDGETS_290 + SuiteTabMixin.SETTING_WIDGETS_2100

    # 2.18.34: saved by older versions, no widget now ('Show advanced options': every option
    # group is its own section). A saved value is ignored; a profile with it is not flagged.
    _RETIRED_SETTINGS = ("advanced_check",)

    def _current_state(self):
        state = {}
        for name in self._SETTING_WIDGETS:
            w = getattr(self, name, None)
            if w is not None:
                v = widget_value(w)
                if v is not None:
                    state[name] = v
        if hasattr(self, "scenario_table"):
            state.update(self._scenario_state())            # v2.7: scenario table
        return state

    def _save_settings(self):
        state = self._current_state()
        # write only real changes (writing marks the project as modified)
        if state != load_state() and (state != self._defaults or load_state()):
            save_state(state)

    def _apply_state(self, state):
        self._restoring = True          # no raster search while settings are restored
        self._apply_failed = []         # SE.2: keys whose value was not accepted
        try:
            for name in self._SETTING_WIDGETS:
                if name in state and getattr(self, name, None) is not None:
                    if set_widget_value(getattr(self, name), state[name]) is False:
                        self._apply_failed.append(name)
            if hasattr(self, "scenario_table"):
                self._apply_scenario_state(state)
            if hasattr(self, "_restore_parcel_fields290"):
                self._restore_parcel_fields290(state)
        finally:
            self._restoring = False
        # an output folder chosen by the user (after the dam folder, which
        # resets it to <dam>/outputs); the review decisions live there
        out = (state.get("output_folder_edit") or "").strip()
        if out and self.config_dam_folder:
            self.config_output_folder = out
            self.output_folder_edit.setText(out)
            self._update_button_states()

    def _restore_settings(self):
        state = load_state()
        if not state:
            return
        self._apply_state(state)
        self._log(">>> Settings restored from the QGIS project")

    def _on_project_changed(self, *args):
        """Another project was opened (or the project was closed): show that
        project's settings instead of the previous project's."""
        self._project_gen += 1          # a running task must not finish into it
        if self._busy:
            return
        self._apply_state(dict(self._defaults))
        self._last_run = None
        # nothing from the previous project's run may be edited or opened
        if hasattr(self, "review_table"):
            self._review_rows = []
            self.review_table.setRowCount(0)
            self.review_status.setText("Run Hazard Classification first")
        if hasattr(self, "_facrev_clear"):
            self._facrev_clear()
        if hasattr(self, "open_report_btn"):
            self.open_report_btn.setEnabled(False)
        self._clear_results()                 # 2.18.34: the Results view is per project
        # the layer choices of the new project can only be restored once the
        # combos list the new project's layers
        if hasattr(self, "_refresh_layer_combos"):
            try:
                self._refresh_layer_combos()
            except Exception as e:
                log_message("Layer lists not refreshed: {}".format(e))
        self._restore_settings()
        self._update_button_states()

    def _clear_results(self):
        """The Results view back to 'no run yet' (another project was opened)."""
        if not hasattr(self, "result_table"):
            return
        self.result_label.setText("No classification run yet")
        self.result_hint.setVisible(True)
        self.result_table.setRowCount(0)
        self._result_rows = []
        self.scenario_result_table.setRowCount(0)
        self.scenario_result_table.setVisible(False)
        self.class_result_text.clear()
        self.result_tool_rows["open_report_btn"].set_status("no", show=False)

    def _on_write_project(self, *args):
        """Project > Save: store the dialog settings with it."""
        if self._busy:
            return
        try:
            self._save_settings()
        except Exception as e:
            log_message("Settings not saved in the project: {}".format(e))

    def hideEvent(self, event):
        # Esc / the window's close box hide the dialog: keep its settings
        if not self._busy:
            try:
                self._save_settings()
            except Exception:
                pass
        super().hideEvent(event)

    def closeEvent(self, event):
        if self._busy:
            event.ignore()
            return
        try:
            self._save_settings()
        except Exception:
            pass
        super().closeEvent(event)
