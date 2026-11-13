"""The ND Dam Breach Screening popup window (Plugins > ND Dam Suite > ND Dam Breach Screening).

0.13: a Simple tab (work folder, what to screen, terrain, engine, criterion, settings file, the
steps in plain words, Check before running, Run all, Run one dam), the settings grid (every
engineer-chosen value in one table backed by the shared settings file), a Review tab (the dams
that need a look, with the reviewer's decisions written into the layers), and the results as one
layer group. The All steps tab is the 0.12 window: a button per tool that opens its Processing
dialog with the parameters filled in.

The logic (file layout, step state, parameters, preflight, review) is core/workflow.py,
core/preflight.py and core/review.py; this file only holds the widgets. The window's choices are
saved with the QGIS project; the shareable ones also go to the settings file.

0.18.19 (usability plan): the window is split by tab, as DamSafety Hazard's is - this file holds
the shell, the Run tab (Simple, All steps, Inputs), state and the run-in-order chain; the
Settings window is settings_tab.py, the Review tab review_tab.py, the Results tab
results_tab.py, and the constants they share dialog_consts.py (re-exported here). The methods
were moved, not changed.
"""
import json
import os

from qgis.PyQt.QtCore import QUrl, Qt
from qgis.PyQt.QtGui import QDesktopServices
from .ui_fit import fit_window, wrap_tabs
from qgis.PyQt.QtWidgets import (
    QDialog, QSizePolicy, QVBoxLayout, QHBoxLayout, QGridLayout, QTabWidget, QWidget, QLabel, QLineEdit,
    QPushButton, QComboBox, QTextEdit, QGroupBox, QFileDialog, QMessageBox,
    QCheckBox, QRadioButton, QInputDialog, QListWidget, QListWidgetItem,
    QMenu, QToolButton,
)

from .core import workflow as wf
from .core import handoff as ho
from .core import settings_groups as SG
from .core import preflight as pf
from .damsuite import settings as DS
from .tokens import TOKENS
from .tool_dialog import exec_tool_dialog
from .ui_widgets import CheckPanel, Collapsible, InfoBanner, StageStrip

from .dialog_consts import (  # noqa: F401 - re-exported: tests and the plugin read them here
    TITLE, SCOPE, ENTRY, STATE_TEXT, DISCLAIMER, SCREEN_WHAT, HELP_FILE, PRESETS_DIR,
    REVIEW_COLUMNS, HINT_STYLE,
)
from .settings_tab import SettingsTabMixin
from .review_tab import ReviewTabMixin
from .results_tab import ResultsTabMixin


def plugin_version():
    try:
        from .core import damrecord_io
        return damrecord_io.plugin_version()
    except Exception:                                            # noqa
        return ""


def window_title():
    """'DamSafety Screening 0.17.30': the suite's 'DamSafety <Tool> <version>' (docs/gui-grammar.md)."""
    v = plugin_version()
    return "{} {}".format(TITLE, v) if v else TITLE


# 0.17.23 the pipeline strip: (key, label, the step whose state it shows, tab it jumps to)
PIPELINE = (("data", "Data", "1", "run"), ("dem", "DEM", "2", "run"),
            ("screen", "Screen", "7", "run"), ("report", "Report", None, "results"))
# the strip is the suite's StageStrip (docs/gui-grammar.md): these are its states
PIPE_STAGE = {"done": StageStrip.YES, "ready": StageStrip.ACTIVE, "running": StageStrip.PART,
              "stale": StageStrip.PART, "problem": StageStrip.ERR, "waiting": StageStrip.NO}
STEP_STATE_W, STEP_BTN_W = 110, 270
# docs/gui-grammar.md: the primary button keeps Qt's look; bold says it is the main action
PRIMARY_STYLE = "QPushButton { font-weight: bold; padding: 6px 12px; }"


class QgisEnv(pf.Env):
    """The preflight's environment inside QGIS: project layers as well as files."""

    def __init__(self, read_layer):
        self._read_layer = read_layer

    def layer_info(self, ref):
        info = pf.Env.layer_info(self, ref)
        if info is not None:
            return info
        lyr = self._read_layer(ref)
        if lyr is None:
            return None
        try:
            fields = list(lyr.fields().names())
            n = lyr.featureCount()
            crs = ""
            try:
                crs = lyr.crs().authid()
            except Exception:                                    # noqa
                try:
                    crs = lyr.sourceCrs().authid()
                except Exception:                                # noqa
                    crs = ""
            geom = ""
            try:
                from qgis.core import QgsWkbTypes
                geom = {0: "point", 1: "line", 2: "polygon"}.get(int(QgsWkbTypes.geometryType(lyr.wkbType())), "")
            except Exception:                                    # noqa
                geom = ""
            return {"fields": fields, "count": int(n), "crs": crs or "EPSG:?", "geometry": geom, "name": ref}
        except Exception:                                        # noqa
            return None

    def values(self, ref, field):
        v = pf.Env.values(self, ref, field)
        if v is not None:
            return v
        lyr = self._read_layer(ref)
        if lyr is None or field not in lyr.fields().names():
            return None
        return {str(f[field]).strip() for f in lyr.getFeatures() if f[field] not in (None, "")}

    def rows(self, ref, fields):
        r = pf.Env.rows(self, ref, fields)
        if r is not None:
            return r
        lyr = self._read_layer(ref)
        if lyr is None:
            return None
        names = lyr.fields().names()
        return [tuple(f[x] if x in names else None for x in fields) for f in lyr.getFeatures()]


class NDDamScreeningDialog(QDialog, SettingsTabMixin, ReviewTabMixin, ResultsTabMixin):
    """The popup window. `iface` is optional (used to open the ND Dam Hazard window and the map)."""

    def __init__(self, parent=None, iface=None):
        super().__init__(parent)
        try:            # Core 1.1.0: the suite's display units (SI or US) in this window
            from DamSafety_Core.ui.unit_widgets import watch_window as _watch_units
            _watch_units(self)
        except Exception:                                         # noqa - Core older than 1.1.0
            pass
        self.iface = iface
        self._loading = True
        self.setWindowTitle(window_title())
        self.setMinimumWidth(640)
        self.step_state, self.step_button, self.step_note = {}, {}, {}
        self.simple_state, self.simple_name, self.simple_note = {}, {}, {}
        self.layer_combo = {}
        self.chain = None                 # the run in order that is going (core/workflow.Chain)
        self._task = None
        self._keep = []                   # task / context / feedback kept alive while it runs
        self._one_dam = None              # (dam id, folder) of the one-dam run that is going
        self.review_rows = []
        self.last_checks = []
        self.grid = None
        self._build()
        self._load_state()
        self._prefer_lisflood()
        self._loading = False
        self._fill_layers()
        self._load_settings_file(quiet=True)
        self.refresh()

    # ------------------------------------------------------------------ build --

    def _first_steps(self):
        """The two choices that come right after the work folder."""
        from .core.dem_download import county_names
        box = QWidget()
        grid = QGridLayout()
        grid.addWidget(QLabel("Data folder"), 0, 0)
        self.suite_edit = QLineEdit(getattr(self, "suite_folder", "") or "")
        self.suite_edit.setPlaceholderText("Offline pack or the folder of layer files")
        self.suite_edit.setToolTip("One folder. An offline pack or GeoPackages in it are filled in. Results do not go here.")
        self.suite_browse = browse = QPushButton("Browse\u2026")
        browse.clicked.connect(self._browse_suite)
        grid.addWidget(self.suite_edit, 0, 1)
        grid.addWidget(browse, 0, 2)
        self.data_hint = QLabel("not set yet")
        self.data_hint.setStyleSheet(HINT_STYLE)
        grid.addWidget(self.data_hint, 1, 1)
        self.data_status = QLabel("No data folder yet.")
        self.data_status.setWordWrap(True)
        grid.addWidget(self.data_status, 2, 0, 1, 3)
        grid.addWidget(QLabel("County"), 3, 0)
        self.inventory_county = QComboBox()
        self.inventory_county.addItem("All counties", "")
        for name in county_names():
            self.inventory_county.addItem(name, name)
        self.inventory_county.setToolTip("Only this county is screened. All counties keeps every county.")
        self.inventory_county.currentIndexChanged.connect(self._changed)
        grid.addWidget(self.inventory_county, 3, 1)
        grid.setColumnStretch(1, 1)
        grid.setContentsMargins(0, 0, 0, 0)
        self.storage_choice = QComboBox()
        self.storage_choice.addItem("Max storage", 0)
        self.storage_choice.addItem("Normal storage", 1)
        self.storage_choice.addItem("Larger of the two", 2)
        self.storage_choice.setToolTip("Max storage is the default for a top-of-dam breach. Normal is a sunny-day check.")
        self.storage_choice.currentIndexChanged.connect(self._changed)
        box.setLayout(grid)
        return box


    def _build(self):
        """0.17.23 shell: header + banner, Workspace row, pipeline strip, Run / Review / Results
        tabs, Settings behind the gear, the log folded to one line, Close bottom right."""
        lay = QVBoxLayout()
        lay.setSpacing(6)
        # docs/gui-grammar.md: no in-window title (the window title says it), the shared banner,
        # the gear on the tab bar
        self.disclaimer = InfoBanner(DISCLAIMER)
        lay.addWidget(self.disclaimer)

        ws = QGroupBox("Workspace")
        wsg = QGridLayout()
        wsg.addWidget(QLabel("Work folder"), 0, 0)
        self.work_edit = QLineEdit()
        self.work_edit.setPlaceholderText("One folder per county / per run")
        self.work_edit.setToolTip(
            "One folder for everything this window's steps write (0_data_package.gpkg, "
            "1_dams.gpkg, 2_terrain, …). The window reads it to show which steps are done. "
            "Use a folder per county / per run.")
        self.work_edit.editingFinished.connect(self._changed)
        self.work_browse = QPushButton("Browse\u2026")
        self.work_browse.clicked.connect(lambda _c=False: self._browse_folder(self.work_edit))
        self.work_open = QPushButton("Open folder")
        self.work_open.clicked.connect(self._open_work_folder)
        wsg.addWidget(self.work_edit, 0, 1)
        wsg.addWidget(self.work_browse, 0, 2)
        wsg.addWidget(self.work_open, 0, 3)
        self.work_hint = QLabel("not set yet")
        self.work_hint.setStyleSheet(HINT_STYLE)
        wsg.addWidget(self.work_hint, 1, 1)
        wsg.addWidget(self._first_steps(), 2, 0, 1, 4)
        wsg.setColumnStretch(1, 1)
        ws.setLayout(wsg)
        lay.addWidget(ws)
        lay.addWidget(self._build_pipeline())

        self.settings_page = self._build_settings_tab()
        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_run_tab(), "Run")
        self.tabs.addTab(self._build_review_tab(), "Review")
        self.tabs.addTab(self._build_results_tab(), "Results")
        self.tabs.setCornerWidget(self._build_gear())
        wrap_tabs(self.tabs)
        lay.addWidget(self.tabs, 1)
        lay.addWidget(self._build_action_bar())
        try:
            self.tabs.currentChanged.connect(self._on_tab_changed)
        except Exception:                                        # noqa
            pass
        self._build_settings_window()
        fit_window(self)

        # the status line, then the log folded away, then Close (as in Hazard and the Runner)
        self.log_line = QLabel("Log: -")
        self.log_line.setStyleSheet("color: {};".format(TOKENS["muted"]))
        lay.addWidget(self.log_line)
        self.log_edit = QTextEdit()
        self.log_edit.setReadOnly(True)
        self.log_edit.setMaximumHeight(110)
        self.log_section = Collapsible("Log", self.log_edit)
        self.log_toggle = self.log_section.header
        lay.addWidget(self.log_section)
        row = QHBoxLayout()
        row.addStretch(1)
        self.close_btn = QPushButton("Close")
        self.close_btn.setAutoDefault(False)
        self.close_btn.clicked.connect(self.close)
        row.addWidget(self.close_btn)
        lay.addLayout(row)
        self.setLayout(lay)

    def _build_gear(self):
        """The ⚙ on the tab bar: click = Settings; its menu also holds How it decides."""
        self.settings_btn = QToolButton()
        self.settings_btn.setText("\u2699")
        self.settings_btn.setToolTip("Settings: every engineer-chosen value, the settings file and the "
                                     "run options. The arrow: How it decides.")
        self.settings_btn.setAutoRaise(True)
        self.settings_btn.setStyleSheet("QToolButton {font-size: 18px; padding: 0 8px 2px 8px;}")
        self.settings_btn.clicked.connect(lambda _c=False: self.show_settings())
        menu = QMenu(self.settings_btn)
        self.settings_action = menu.addAction("Settings\u2026")
        self.settings_action.triggered.connect(lambda _c=False: self.show_settings())
        self.help_action = menu.addAction("How it decides\u2026")
        self.help_action.setToolTip("Pictures of the rules: the rim / toe of a pond, the triage envelope, "
                                    "the ND 5.2 / 5.3 decision tree, and what every threshold does.")
        self.help_action.triggered.connect(lambda _c=False: self._open_help())
        self.gear_menu = menu
        self.settings_btn.setMenu(menu)
        try:
            self.settings_btn.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
        except AttributeError:                                   # Qt5 / the test double
            try:
                self.settings_btn.setPopupMode(QToolButton.MenuButtonPopup)
            except Exception:                                    # noqa
                pass
        self.help_btn = self.settings_btn                        # kept name: How it decides is in its menu
        return self.settings_btn

    def _build_action_bar(self):
        """The primary action at the bottom right, beside its siblings (docs/gui-grammar.md);
        shown on the Run tab, where its inputs are."""
        self.action_area = QWidget()
        outer = QVBoxLayout(self.action_area)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(4)
        # 0.18.19 (docs/gui-grammar.md "Checks: CheckPanel, never a message box"): the check
        # list and the "Not ready" / "nothing to run" refusals of Screen this county are the
        # shared panel above the action bar; a red result blocks the primary button
        self.check_panel = CheckPanel(self.preflight_btn, self.run_all_btn, action="screening")
        self.check_panel.errors_fixed.connect(lambda _ok: self.refresh())
        outer.addWidget(self.check_panel)
        bar = QHBoxLayout()
        bar.addWidget(self.next_hint, 1)
        bar.addWidget(self.preflight_btn)
        bar.addWidget(self.one_dam_btn)
        bar.addWidget(self.stop_btn)
        bar.addWidget(self.run_all_btn)
        outer.addLayout(bar)
        return self.action_area

    def _on_tab_changed(self, index):
        self.action_area.setVisible(index == 0)

    # -- 0.17.23 shell pieces
    def _build_pipeline(self):
        """Data -> DEM -> Screen -> Report; each a flat button that jumps to its tab."""
        self.strip = bar = StageStrip([label for _k, label, _s, _t in PIPELINE], clickable=True)
        self.pipe_btn = {key: chip for (key, _l, _s, _t), chip in zip(PIPELINE, bar.chips)}
        bar.clicked.connect(lambda i: self._goto_stage(PIPELINE[i][0]))
        self.pipeline_bar = bar
        # the old prose line, kept (not shown) for anything that reads it
        self.status_line = QLabel("Dams not yet. DEM not yet. Screen not yet.")
        return bar

    def _goto_stage(self, key):
        tab = dict((k, t) for k, _l, _s, t in PIPELINE).get(key, "run")
        self.tabs.setCurrentIndex({"run": 0, "review": 1, "results": 2}[tab])

    def _refresh_pipeline(self, by, busy, have, work):
        """The strip from the same states the steps show (wf.statuses) and the report file."""
        self.pipe_state = {}
        tips = []
        cur = getattr(self.chain, "current", None) if busy else None
        for key, label, step, _tab in PIPELINE:
            if step is None:
                st = "done" if have and os.path.isfile(os.path.join(work, wf.REPORT_INDEX)) else "waiting"
                why = "Report: index.html written by step 10" if st == "done" else "Report: no report yet"
            else:
                st = by.get(step) or "waiting"
                if step == "1":
                    # Data is done once the data folder exists and step 1 can start (or ran)
                    suite = self.suite_edit.text().strip()
                    if have and suite and os.path.isdir(suite) and st in ("ready", "done", "stale"):
                        st = "done"
                    elif st == "done":
                        pass
                    elif have and suite and os.path.isdir(suite):
                        st = "ready"
                if busy and (cur == step or (step == "7" and cur in ("5", "6", "7", "8"))):
                    st = "running"
                why = "{}: step {} - {} ({})".format(label, step, wf.TITLE.get(step, ""), st)
            self.pipe_state[key] = st
            tips.append(why + ". Click to go to the tab.")
        self.strip.set_states([PIPE_STAGE.get(self.pipe_state[k], StageStrip.NO)
                               for k, _l, _s, _t in PIPELINE], tips)

    def _collapsible(self, title, inner, open_=False):
        """The suite's ▸ fold around `inner` (folding hides it; its values still apply)."""
        return Collapsible(title, inner, expanded=bool(open_))

    @staticmethod
    def _fold(box, inner_layout):
        """The suite's ▸ fold (Collapsible, docs/gui-grammar.md) with `box`'s title and tooltip;
        folding only hides the contents, every value in it still applies. Returns the fold."""
        holder = QWidget()
        holder.setLayout(inner_layout)
        fold = Collapsible(box.title(), holder, expanded=bool(box.isChecked()))
        fold.setToolTip(box.toolTip())
        return fold

    def _build_run_tab(self):
        """Simple + All steps: run buttons on the left, the numbered steps and the input
        overrides on the right."""
        w = QWidget()
        row = QHBoxLayout()
        # GUI lint 2026-11-08 (1080p xvfb): the two columns' minimum widths plus the default
        # layout gutters came to 950 px in a 940 px scroll viewport, so the Run page scrolled
        # sideways and hid the right-hand step buttons. Tighten the outer margins and the gap
        # between the columns - the columns keep their own internal padding - so the page fits.
        row.setContentsMargins(2, 2, 2, 2)
        row.setSpacing(6)
        row.addWidget(self._build_simple_tab(), 1)
        right = QGroupBox("Steps")
        v = QVBoxLayout()
        v.addWidget(self._build_workflow_tab())
        self.inputs_page = self._build_inputs_tab()
        self.override_box = self._collapsible("Override inputs (empty = use the data package)",
                                              self.inputs_page)
        v.addWidget(self.override_box)
        right.setLayout(v)
        row.addWidget(right, 2)
        w.setLayout(row)
        return w

    def _build_settings_window(self):
        self.settings_window = QDialog(self)
        self.settings_window.setWindowTitle(TITLE + " \u2014 Settings")
        v = QVBoxLayout()
        v.addWidget(self.settings_page)
        row = QHBoxLayout()
        row.addStretch(1)
        done = QPushButton("Close")
        done.clicked.connect(self.settings_window.close)
        row.addWidget(done)
        v.addLayout(row)
        self.settings_window.setLayout(v)
        try:
            # the Run options tab is a long form: it scrolls, so the window fits a 1080p screen
            # (unscrolled it asked for 1147 px and put Save and Close below the screen)
            # a combo sized to its longest item (802 px) made the page scroll sideways and hid
            # the Browse buttons: combos shrink to the column and show the full text when open
            page = self.grid.tabs.widget(0)
            _adj = getattr(QComboBox, "SizeAdjustPolicy", QComboBox)
            for combo in page.findChildren(QComboBox):
                combo.setSizeAdjustPolicy(_adj.AdjustToMinimumContentsLengthWithIcon)
                combo.setMinimumContentsLength(24)
                combo.setMinimumWidth(200)      # Qt5 keeps the old minimum hint; this overrides it
            wrap_tabs(self.grid.tabs)
            fit_window(self.settings_window, prefer=(1140, 720))   # Run options rows are wide
        except Exception:                                        # noqa
            pass

    def settings_changed_count(self):
        try:
            return len(self.grid.settings.changed(self.grid.catalog))
        except Exception:                                        # noqa
            return 0

    def _update_settings_badge(self):
        n = self.settings_changed_count()
        self.settings_btn.setText("\u2699 {}".format(n) if n else "\u2699")
        self.settings_btn.setToolTip("{} setting(s) differ from the starting values. Opens the settings "
                                     "grid, the settings file and the run options.".format(n))

    def _toggle_log(self):
        self.log_section.set_expanded(not self.log_section.is_expanded())

    # -- Simple
    def _build_simple_tab(self):
        from .algorithms.run_breach_sim import ENGINES
        from .algorithms.hazard_classification import CRIT_LABELS
        w = QWidget()
        lay = QVBoxLayout()
        intro = QLabel("This screens every intact dam in the county and ranks them High, Medium, or Low.")
        intro.setWordWrap(True)
        lay.addWidget(intro)
        run = QHBoxLayout()
        self.run_all_btn = QPushButton("Screen this county")
        self.run_all_btn.setMinimumHeight(36)
        self.run_all_btn.setMinimumWidth(160)
        self.run_all_btn.setStyleSheet(PRIMARY_STYLE)
        self.run_all_btn.setDefault(True)
        self.run_all_btn.setToolTip("Checks the folder, then screens the county. Does not start the Runner.")
        self.run_all_btn.clicked.connect(lambda _c=False: self._run_chain(preflight=True))
        self.one_dam_btn = QPushButton("One dam\u2026")
        self.one_dam_btn.clicked.connect(lambda _c=False: self._run_one_dam())
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.setEnabled(False)
        self.stop_btn.setStyleSheet("QPushButton:enabled {{ color: {}; }}".format(TOKENS["error"]))
        self.stop_btn.clicked.connect(lambda _c=False: self._stop_chain())
        for b in (self.one_dam_btn, self.stop_btn):
            b.setAutoDefault(False)
        lay.addLayout(run)                    # the three run buttons are in the action bar
        grid = QGridLayout()
        r = 0
        grid.addWidget(QLabel("What to screen"), r, 0)
        box = QHBoxLayout()
        self.screen_radio = []
        for i, label in enumerate(SCREEN_WHAT):
            rb = QRadioButton(label)
            rb.toggled.connect(lambda on, idx=i: self._screen_toggled(idx, on))
            self.screen_radio.append(rb)
            box.addWidget(rb)
        box.addStretch(1)
        grid.addLayout(box, r, 1)
        r += 1
        grid.addWidget(QLabel("Data package (step 0)"), r, 0)
        self.package_edit = QLineEdit()
        self.package_edit.setToolTip(
            "Optional: the GeoPackage ND Dam Hazard's 'Export for Screening' writes, or your own "
            "package from step 0. Every input left empty on the Inputs tab is taken from it.")
        self.package_edit.editingFinished.connect(self._changed)
        b = QPushButton("File…")
        b.clicked.connect(lambda _c=False: self._browse_file(self.package_edit, "GeoPackage (*.gpkg)"))
        grid.addWidget(self.package_edit, r, 1)
        grid.addWidget(b, r, 2)
        r += 1
        self.terrain_source = self._combo(["USGS 3DEP service (needs internet)", "Local DEM folder"])
        self.terrain_source.setToolTip("Where the LiDAR comes from: the USGS 3DEP service (once per dam, needs "
                                       "internet) or your own folder of DEM tiles (Settings > Run options).")
        self.terrain_source.currentIndexChanged.connect(self._changed)
        grid.addWidget(QLabel("Terrain"), r, 0)
        grid.addWidget(self.terrain_source, r, 1)
        r += 1
        self.engine = self._combo(ENGINES)
        self.engine.setToolTip("The built-in engine needs no install; LISFLOOD-FP needs its executable "
                               "(Settings > Run options).")
        self.engine.currentIndexChanged.connect(self._changed)
        grid.addWidget(QLabel("Flood engine"), r, 0)
        grid.addWidget(self.engine, r, 1)
        r += 1
        self.crit = self._combo(CRIT_LABELS)
        self.crit.currentIndexChanged.connect(self._changed)
        grid.addWidget(QLabel("Hazard criterion"), r, 0)
        grid.addWidget(self.crit, r, 1)
        r += 1
        more = QGridLayout()
        mr = 0
        more.addWidget(QLabel("Settings file"), mr, 0)
        self.settings_file_edit = QLineEdit()
        self.settings_file_edit.setToolTip(
            "The suite settings workbook (damsuite_settings.xlsx, or .csv): every engineer-chosen value of "
            "the tools, shared by the three plugins. Edit it on the Settings tab or in Excel; the tools open "
            "with its values. Empty = the tools' own starting values.")
        self.settings_file_edit.editingFinished.connect(lambda: self._settings_file_edited())
        b = QPushButton("File…")
        b.clicked.connect(lambda _c=False: self._pick_settings_file())
        self.new_settings_btn = QPushButton("New…")
        self.new_settings_btn.setToolTip("Write a new settings file with the current values (then edit it on the "
                                         "Settings tab).")
        self.new_settings_btn.clicked.connect(lambda _c=False: self._new_settings_file())
        box = QHBoxLayout()
        box.addWidget(self.settings_file_edit, 1)
        box.addWidget(b)
        box.addWidget(self.new_settings_btn)
        more.addLayout(box, mr, 1, 1, 2)
        mr += 1
        more.addWidget(QLabel("Preset"), mr, 0)
        box = QHBoxLayout()
        self.preset_combo = QComboBox()
        self.preset_combo.setToolTip("Named settings files that come with the plugin (and any in the settings "
                                     "file's folder). 'Apply' copies a preset's values into the current settings.")
        self.apply_preset_btn = QPushButton("Apply")
        self.apply_preset_btn.clicked.connect(lambda _c=False: self._apply_preset())
        box.addWidget(self.preset_combo, 1)
        box.addWidget(self.apply_preset_btn)
        more.addLayout(box, mr, 1, 1, 2)
        self._more_grid = more
        self.preflight_btn = QPushButton("Check inputs")      # in the action bar (0.18.19)
        self.preflight_btn.setToolTip("Runs the check list for the steps a county screen would "
                                      "run; a FAIL blocks Screen this county until it is fixed.")
        self.preflight_btn.clicked.connect(lambda _c=False: self._preflight())
        self.continue_btn = QPushButton("Continue")
        self.continue_btn.setEnabled(False)
        self.continue_btn.setToolTip("Opens the Runner or Hazard from this dam's dam.json, not from a depth file name.")
        self.continue_btn.clicked.connect(lambda _c=False: self._continue())
        self.selected_dams_btn = QPushButton("Run selected dams…")
        self.selected_dams_btn.setToolTip("Pick several dams. Runs the county chain for those dams only.")
        self.selected_dams_btn.clicked.connect(lambda _c=False: self._run_selected_dams())
        self.to_runner_btn = QPushButton("Send High and Medium to the Runner")
        self.to_runner_btn.setToolTip("Opens step 7b. Leave Dams to send on the default.")
        self.to_runner_btn.clicked.connect(lambda _c=False: self._open_step("7b"))
        self.handoff_runner_btn = QPushButton("Open this dam in the Runner")
        self.handoff_runner_btn.clicked.connect(lambda _c=False: self._handoff_runner())
        self.handoff_hazard_btn = QPushButton("Open this dam in Hazard")
        self.handoff_hazard_btn.clicked.connect(lambda _c=False: self._handoff_hazard())
        run.addWidget(self.continue_btn)
        self.continue_btn.setVisible(False)
        self.next_hint = QLabel("Choose a work folder, a data folder, and a county.")
        self.next_hint.setWordWrap(True)
        self.next_hint.setStyleSheet("color: {};".format(TOKENS["muted"]))   # in the action bar
        # 0.17.22: the Runner engine step 7b will use, always visible on the Run tab
        self.engine_status_label = QLabel(ho.engine_status(0))
        self.engine_status_label.setWordWrap(True)
        lay.addWidget(self.engine_status_label)
        lay.addWidget(self._county_dem_box())

        for key in SG.MAIN_STEPS:
            if key == "17":
                continue
            st = QLabel("")
            nm = QLabel(SG.plain(key, wf.TITLE[key]))
            nt = QLabel("")
            self.simple_state[key], self.simple_name[key], self.simple_note[key] = st, nm, nt

        self.preflight_label = QLabel("")
        self.preflight_label.setWordWrap(True)
        self.preflight_text = QTextEdit()
        self.preflight_text.setReadOnly(True)
        self.preflight_text.setMaximumHeight(150)
        self.simple_hint = QLabel("")
        self.simple_hint.setWordWrap(True)
        more_box = QGroupBox("Advanced options (inputs, engine, storage, hand-offs)")
        more_box.setCheckable(True)
        more_box.setChecked(False)
        more_box.setToolTip("Data package, terrain source, flood engine, hazard criterion, storage, settings "
                            "file and presets, the preflight check and the Runner / Hazard hand-offs. "
                            "The defaults are fine for a normal county screen.")
        inner = QVBoxLayout()
        inner.addLayout(grid)
        store = QHBoxLayout()
        store.addWidget(QLabel("Storage at top of dam"))
        store.addWidget(self.storage_choice, 1)
        inner.addLayout(store)
        inner.addLayout(self._more_grid)
        extra = QHBoxLayout()
        for b in (self.selected_dams_btn, self.to_runner_btn,
                  self.handoff_runner_btn, self.handoff_hazard_btn):
            extra.addWidget(b)
        inner.addLayout(extra)
        inner.addWidget(self.preflight_label)
        inner.addWidget(self.preflight_text)
        inner.addWidget(self.simple_hint)
        lay.addWidget(self._fold(more_box, inner))
        lay.addStretch(1)
        w.setLayout(lay)
        return w

    # -- All steps (the 0.12 window)
    def _build_workflow_tab(self):
        w = QWidget()
        lay = QVBoxLayout()
        intro = QLabel("Each button opens the step's Processing dialog, pre-filled. \u24d8")
        intro.setToolTip(
            "Each button opens the tool's Processing dialog with the work folder's files, the settings "
            "file's values and the choices of this window filled in. Check the values, run it there, "
            "then come back: the list updates when this window is activated. Every step can also be run "
            "from the Processing Toolbox (DamSafety Screening > Screening steps / Advanced).")
        lay.addWidget(intro)
        dem_btn = QPushButton("Download county DEM…")
        dem_btn.setToolTip("Opens Download DEM with the Inventory county, 10 m or 1 m, and the suite folder filled in.")
        dem_btn.clicked.connect(lambda _c=False: self._open_dem_download())
        lay.addWidget(dem_btn)
        off_btn = QPushButton("Download offline layers for this county…")
        off_btn.setToolTip("Opens 0b with the county dropdown set from this window. Saves a GeoPackage of dams, roads, structures, and addresses.")
        off_btn.clicked.connect(lambda _c=False: self._open_offline_layers())
        lay.addWidget(off_btn)
        grid = QGridLayout()
        for i, key in enumerate(wf.KEYS):
            state = QLabel("")
            btn = QPushButton("{} - {}\u2026".format(key, wf.TITLE[key]))
            btn.setToolTip("{}\n(Processing: {})".format(wf.HINT[key], wf.algorithm_id(key)))
            btn.clicked.connect(lambda _c=False, k=key: self._open_step(k))
            note = QLabel("")
            note.setWordWrap(True)
            state.setFixedWidth(STEP_STATE_W)
            btn.setFixedWidth(STEP_BTN_W)
            note.setSizePolicy(getattr(QSizePolicy, 'Policy', QSizePolicy).Ignored,
                               getattr(QSizePolicy, 'Policy', QSizePolicy).Preferred)
            self.step_state[key], self.step_button[key], self.step_note[key] = state, btn, note
            grid.addWidget(state, i, 0)
            grid.addWidget(btn, i, 1)
            grid.addWidget(note, i, 2)
        grid.setColumnStretch(2, 1)
        lay.addLayout(grid)
        grp = QGroupBox("Run the steps in order")
        g = QVBoxLayout()
        txt = QLabel(
            "Runs every step that is not done (or out of date) one after the other, in the "
            "background, with the values this window would fill in - no dialogs. It stops at the "
            "first step that fails or lacks an input, and checks each step wrote its output. "
            "Step 7b (DamSafety Runner) is never part of it. Screen this county, on this Run tab, "
            "is the same run with the check list first.")
        txt.setWordWrap(True)
        g.addWidget(txt)
        row = QGridLayout()                # 0.17.23: two columns (was one long row)
        self.chain_check = {}
        for key, label in (("0", "also build the data package (0)"), ("6", "also the quick screen (6)"),
                           ("9", "also the sensitivity (9)"),
                           ("16", "ponds and dugouts (16)"), ("18", "merge dams and ponds (18)"),
                           ("19", "a proposed dam (19)")):
            c = QCheckBox(label)
            c.toggled.connect(self._chain_check_toggled)
            self.chain_check[key] = c
            row.addWidget(c, (len(self.chain_check) - 1) // 2,
                          (len(self.chain_check) - 1) % 2)
        g.addLayout(row)
        row = QHBoxLayout()
        self.chain_btn = QPushButton("Run the steps in order")
        self.chain_btn.clicked.connect(lambda _c=False: self._run_chain())
        self.chain_stop_btn = QPushButton("Stop")
        self.chain_stop_btn.setEnabled(False)
        self.chain_stop_btn.clicked.connect(lambda _c=False: self._stop_chain())
        self.export_btn = QPushButton("Export settings (JSON)…")
        self.export_btn.setToolTip("Writes this window's choices as the JSON the headless tool (15) also "
                                   "reads; the suite settings file (.xlsx) is the usual way now.")
        self.export_btn.clicked.connect(lambda _c=False: self._export_settings())
        row.addWidget(self.chain_btn)
        row.addWidget(self.chain_stop_btn)
        row.addWidget(self.export_btn)
        row.addStretch(1)
        g.addLayout(row)
        grp.setLayout(g)
        lay.addWidget(grp)
        row = QHBoxLayout()
        self.next_btn = QPushButton("Open the next step")
        self.next_btn.setToolTip("The first step that is not done and whose earlier steps are "
                                 "done; optional steps are skipped.")
        self.next_btn.clicked.connect(self._open_next)
        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.clicked.connect(lambda _c=False: self.refresh())
        row.addWidget(self.next_btn)
        row.addWidget(self.refresh_btn)
        row.addStretch(1)
        lay.addLayout(row)
        self.hint_label = QLabel("")
        self.hint_label.setWordWrap(True)
        lay.addWidget(self.hint_label)
        lay.addStretch(1)
        w.setLayout(lay)
        return w

    # -- Inputs
    def _build_inputs_tab(self):
        w = QWidget()
        lay = QVBoxLayout()
        info = QLabel(
            "The data the tools read. A layer chosen here wins; left empty, the tools use the "
            "layer of the same meaning in the data package that step 0 wrote (structures, roads, "
            "railroads, flowlines, waterbodies, dam lines, lifelines). Pick project layers or "
            "'File\u2026'.")
        info.setWordWrap(True)
        lay.addWidget(info)
        grp = QGroupBox("Layers")
        grid = QGridLayout()
        slots = list(wf.LAYER_SLOTS) + [("clip", "Clip to (step 0; e.g. the ND boundary)", None)]
        for i, (slot, label, _pkg) in enumerate(slots):
            combo = QComboBox()
            combo.currentIndexChanged.connect(self._changed)
            btn = QPushButton("File…")
            btn.clicked.connect(lambda _c=False, s=slot: self._browse_layer(s))
            self.layer_combo[slot] = combo
            grid.addWidget(QLabel(label), i, 0)
            grid.addWidget(combo, i, 1)
            grid.addWidget(btn, i, 2)
        grp.setLayout(grid)
        lay.addWidget(grp)
        self.layers_btn = QPushButton("Refresh the layer lists")
        self.layers_btn.clicked.connect(lambda _c=False: self._fill_layers())
        lay.addWidget(self.layers_btn)
        self.suite_btn = QPushButton("Fill empty layers from suite folder…")
        self.suite_btn.setToolTip(
            "Pick the suite data folder. Empty slots are filled from the GeoPackage "
            "layer whose name and fields match (DWR dams, dam lines, NSI). A layer "
            "already chosen is left alone. Unknown structures and dugouts are not "
            "the dam inventory: the DWR dugouts in the same folder are read by tools 1 "
            "and 16 by themselves (a DWR dugout is not screened as a dam; a found pond "
            "that is one is classed a dugout).")
        self.suite_btn.clicked.connect(self._fill_from_suite)
        lay.addWidget(self.suite_btn)
        self.suite_note = QLabel("")
        self.suite_note.setWordWrap(True)
        lay.addWidget(self.suite_note)
        lay.addStretch(1)
        w.setLayout(lay)
        return w

    @staticmethod
    def _combo(items):
        c = QComboBox()
        for i, t in enumerate(items):
            c.addItem(t, i)
        return c

    # ---------------------------------------------------------------- settings --
    _EDITS = ("local_dem", "exe", "chart_file", "runner_dem", "dams_folder", "ref_folder", "old_inventory")
    _COMBOS = ("terrain_source", "larger_clips", "engine", "scenario", "crit", "buildings", "ref_units", "runner_tier")
    _CHECKS = ("baseline", "incremental", "runner_refine", "runner_water_mask")
    _SPINS = ("baseline_cfs", "building_raise_ft", "building_n", "dem_cell", "grid_m", "simh", "simmax")

    def settings(self):
        """The window's choices as the dict core/workflow.py takes."""
        s = wf.default_settings()
        s["work"] = self.work_edit.text().strip()
        s["package"] = self.package_edit.text().strip()
        for k in self._EDITS:
            s[k] = getattr(self, k).text().strip()
        for k in self._COMBOS:
            d = getattr(self, k).currentData()
            s[k] = int(d) if d is not None else 0
        s["workers"] = int(self.workers.value())
        for k in self._CHECKS:
            s[k] = bool(getattr(self, k).isChecked())
        for k in self._SPINS:
            s[k] = float(getattr(self, k).value())
        s["chain"] = [k for k, c in self.chain_check.items() if c.isChecked()]
        s["screen_what"] = self.screen_what()
        s["inventory_county"] = self.inventory_county.currentData() or ""
        s["storage_choice"] = int(self.storage_choice.currentData() or 0) if hasattr(self, "storage_choice") else 0
        s["settings_file"] = self.settings_file_edit.text().strip()
        s["reviewer"] = self.reviewer_edit.text().strip()
        s["suite_folder"] = getattr(self, "suite_folder", "") or ""
        for slot, combo in self.layer_combo.items():
            s[slot] = combo.currentData() or ""
        return s

    def screen_what(self):
        for i, rb in enumerate(self.screen_radio):
            if rb.isChecked():
                return i
        return 0

    def _apply(self, s):
        # 0.17.22: an old 'force ANUGA' choice is the engine choice now
        if s and s.get("runner_force_anuga"):
            s = dict(s, runner_tier=1, runner_force_anuga=False)
        self.work_edit.setText(s.get("work", ""))
        self.package_edit.setText(s.get("package", ""))
        self.inventory_county.setCurrentText(s.get("inventory_county") or "All counties")
        if hasattr(self, "storage_choice"):
            i = self.storage_choice.findData(int(s.get("storage_choice", 0) or 0))
            self.storage_choice.setCurrentIndex(i if i >= 0 else 0)
        for k in self._EDITS:
            getattr(self, k).setText(s.get(k, ""))
        for k in self._COMBOS:
            combo = getattr(self, k)
            i = combo.findData(int(s.get(k, 0) or 0))
            combo.setCurrentIndex(i if i >= 0 else 0)
        self.workers.setValue(int(s.get("workers", 0) or 0))
        for k in self._CHECKS:
            getattr(self, k).setChecked(bool(s.get(k, False)))
        for k in self._SPINS:
            try:
                getattr(self, k).setValue(float(s.get(k, wf.DEFAULT_SETTINGS[k])))
            except (TypeError, ValueError):
                getattr(self, k).setValue(float(wf.DEFAULT_SETTINGS[k]))
        for k, c in self.chain_check.items():
            c.setChecked(k in (s.get("chain") or []))
        what = int(s.get("screen_what", 0) or 0)
        for i, rb in enumerate(self.screen_radio):
            rb.setChecked(i == what)
        self.settings_file_edit.setText(s.get("settings_file", "") or "")
        self.reviewer_edit.setText(s.get("reviewer", "") or "")
        self.suite_folder = s.get("suite_folder", "") or ""
        self._pending_layers = {slot: s.get(slot, "") for slot in self.layer_combo}

    def _save_state(self):
        if self._loading:
            return
        try:
            from qgis.core import QgsProject
            QgsProject.instance().writeEntry(SCOPE, ENTRY, json.dumps(self.settings()))
        except Exception:                                        # noqa
            pass

    def _load_state(self):
        s = wf.default_settings()
        try:
            from qgis.core import QgsProject
            text, ok = QgsProject.instance().readEntry(SCOPE, ENTRY, "")
            if ok and text:
                loaded = json.loads(text)
                if isinstance(loaded, dict):
                    self._saved_engine = "engine" in loaded
                    s.update(loaded)
        except Exception:                                        # noqa
            pass
        if not (s.get("settings_file") or "").strip():
            s["settings_file"] = DS.current_path() or ""
        self._apply(s)

    def _prefer_lisflood(self):
        """Use LISFLOOD-FP when it is installed and the user has not saved another engine."""
        if getattr(self, "_saved_engine", False):
            return
        import shutil
        if shutil.which("lisflood") or shutil.which("lisflood.exe"):
            self.engine.setCurrentIndex(0)

    def _ensure_layout(self, work):
        """Standard empty folders in the work folder (damsuite/layout.py): never deletes,
        moves or overwrites; nothing inside the Dams folder."""
        try:
            from .damsuite import layout as LY
            made = LY.ensure_layout(work, wf.dams_folder(work, self.settings()))
        except Exception as e:                    # LayoutRefused / OSError: say it, go on
            self._log("Standard folders not created: {}".format(e))
            return
        if made:
            self._log("Created the standard work-folder layout ({} item(s)) in {}".format(len(made), work))

    def _changed(self, *args):
        self._save_state()
        work = self.work_edit.text().strip()
        if work and os.path.isdir(work) and work != getattr(self, "_layout_for", None):
            self._layout_for = work
            self._ensure_layout(work)
        self._autosave_settings()
        self.refresh()

    def _screen_toggled(self, idx, on):
        """The Simple tab's radios drive the chain's 16 / 18 boxes."""
        if not on or self._loading:
            return
        for i, rb in enumerate(self.screen_radio):
            if i != idx and rb.isChecked():
                rb.setChecked(False)
        was = self._loading
        self._loading = True
        try:
            self.chain_check["16"].setChecked(idx in (1, 2))
            self.chain_check["18"].setChecked(idx == 2)
        finally:
            self._loading = was
        self._changed()

    def _chain_check_toggled(self, *args):
        if self._loading:
            return
        was = self._loading
        self._loading = True
        try:
            c16, c18 = self.chain_check["16"].isChecked(), self.chain_check["18"].isChecked()
            what = 2 if (c16 and c18) else (1 if c16 else 0)
            for i, rb in enumerate(self.screen_radio):
                rb.setChecked(i == what)
        finally:
            self._loading = was
        self._changed()

    # ------------------------------------------------------------------ layers --
    @staticmethod
    def project_layers():
        """[(name, id)] of the layers in the project (empty when there is no project)."""
        rows = []
        try:
            from qgis.core import QgsProject
            for lyr in QgsProject.instance().mapLayers().values():
                nm = lyr.name() if callable(lyr.name) else lyr.name
                rows.append((str(nm), lyr.id()))
        except Exception:                                        # noqa
            return []
        return sorted(rows, key=lambda r: r[0].lower())

    def _read_layer(self, ref):
        """A QgsVectorLayer for a project layer id or a file ('path' / 'path|layername=x'); None
        when it cannot be opened."""
        if not ref:
            return None
        try:
            from qgis.core import QgsProject, QgsVectorLayer
        except Exception:                                        # noqa
            return None
        try:
            lyr = QgsProject.instance().mapLayers().get(ref)
            if lyr is not None:
                return lyr
            lyr = QgsVectorLayer(ref, os.path.basename(str(ref).split("|")[0]), "ogr")
            return lyr if lyr.isValid() else None
        except Exception:                                        # noqa
            return None

    def _read_rows(self, ref, fields):
        lyr = self._read_layer(ref)
        if lyr is None:
            return None
        names = lyr.fields().names()
        return [tuple(f[x] if x in names else None for x in fields) for f in lyr.getFeatures()]

    def _fill_layers(self):
        """Fill every layer list with the project's layers, keeping what is chosen (also a file
        chosen with 'File...' or a layer that is no longer in the project)."""
        was_loading = self._loading
        self._loading = True
        try:
            layers = self.project_layers()
            pending = getattr(self, "_pending_layers", {})
            for slot, combo in self.layer_combo.items():
                keep = pending.get(slot) if slot in pending else (combo.currentData() or "")
                names = {}
                combo.clear()
                combo.addItem("(the package's layer / none)", "")
                for nm, lid in layers:
                    combo.addItem(nm, lid)
                    names[lid] = nm
                if keep:
                    i = combo.findData(keep)
                    if i < 0:
                        combo.addItem(os.path.basename(keep.split("|")[0]) or keep, keep)
                        i = combo.count() - 1
                    combo.setCurrentIndex(i)
                else:
                    combo.setCurrentIndex(0)
            self._pending_layers = {}
        finally:
            self._loading = was_loading

    def _fill_from_suite(self, *args):
        folder = QFileDialog.getExistingDirectory(
            self, "Suite data folder", self.work_edit.text().strip() or "")
        if not folder:
            return
        from .damsuite.suite_fill import scan
        found, notes = scan(folder)
        self.suite_folder = folder
        filled = []
        for slot, combo in self.layer_combo.items():
            uri = found.get(slot)
            if not uri:
                continue
            combo.blockSignals(True)
            i = combo.findData(uri)
            if i < 0:
                combo.addItem(os.path.basename(uri.split("|")[0]) + ":" + uri.split("=")[-1], uri)
                i = combo.count() - 1
            combo.setCurrentIndex(i)
            combo.blockSignals(False)
            filled.append(slot)
        if found.get("pack") and hasattr(self, "package_edit") and not self.package_edit.text().strip():
            self.package_edit.setText(found["pack"])
        missing = [s for s in self.layer_combo if s not in filled]
        self.suite_note.setText(
            "Filled {} from {}. Still empty: {}.".format(
                ", ".join(filled) or "nothing", folder,
                ", ".join(missing) or "none"))
        for n in notes:
            self._log("suite: " + n)
        self._changed()

    def _browse_layer(self, slot):
        path, _f = QFileDialog.getOpenFileName(
            self, "Layer file", self.work_edit.text().strip() or "",
            "Data (*.gpkg *.shp *.geojson *.json *.tif *.tiff *.vrt);;All files (*)")
        if not path:
            return
        combo = self.layer_combo[slot]
        i = combo.findData(path)
        if i < 0:
            combo.addItem(os.path.basename(path), path)
            i = combo.count() - 1
        combo.setCurrentIndex(i)            # -> _changed

    # ------------------------------------------------------------------ browse --
    def _browse_folder(self, edit):
        d = QFileDialog.getExistingDirectory(self, "Folder", edit.text().strip() or "")
        if d:
            edit.setText(d)
            self._changed()

    def _browse_file(self, edit, flt="All files (*)"):
        p, _f = QFileDialog.getOpenFileName(self, "File", edit.text().strip() or "", flt)
        if p:
            edit.setText(p)
            self._changed()
            if edit is self.package_edit:
                self._connect_package(p)

    def _browse_for(self, edit, kind):
        if kind == "dir":
            self._browse_folder(edit)
        else:
            flt = {"json": "JSON (*.json)", "raster": "Raster (*.tif *.tiff *.vrt)",
                   "file": "All files (*)"}.get(kind, "All files (*)")
            self._browse_file(edit, flt)

    # --------------------------------------------------------------------- log --
    def _log(self, msg):
        self.log_edit.append(str(msg))
        line = getattr(self, "log_line", None)
        if line is not None:
            text = str(msg).strip().splitlines()
            line.setText("Log: " + (text[-1][:120] if text else "-"))

    # ----------------------------------------------------------------- refresh --
    def refresh(self, *args):
        """Re-read the work folder: the state of every step and the results."""
        work = self.work_edit.text().strip()
        s = self.settings()
        have = bool(work and os.path.isdir(work))
        rows = wf.statuses(work, s) if have else [
            (k, wf.TITLE[k], "optional" if k in wf.OPTIONAL else "waiting", "") for k in wf.KEYS]
        what = s.get("screen_what", 0)
        if hasattr(self, "engine_status_label"):
            try:
                cul = wf.slot_source("culverts", work, s) if have else ""
            except Exception:                                    # noqa
                cul = ""
            txt = ho.engine_status(s.get("runner_tier"), cul)
            self.engine_status_label.setText(txt)
            self.engine_status_label.setStyleSheet("font-weight: bold;" if ho.effective_engine(
                s.get("runner_tier")) else "")
        for key, _title, state, note in rows:
            self.step_state[key].setText(STATE_TEXT.get(state, state))
            self.step_note[key].setText(self._short_note(note))
            self.step_note[key].setToolTip(note)
            # 0.17.23: a step still waiting for an earlier one is shown greyed (its button stays usable)
            self.step_state[key].setEnabled(state != "waiting")
            self.step_note[key].setEnabled(state != "waiting")
            if key in self.simple_state:
                self.simple_state[key].setText(STATE_TEXT.get(state, state))
                self.simple_note[key].setText(note)
                # the steps the chosen path does not use are shown greyed, not removed
                used = not ((key in wf.POND_COVERS and what == 1) or (key == "16" and what == 0) or
                            (key == "18" and what != 2))
                self.simple_name[key].setEnabled(used)
                self.simple_state[key].setEnabled(used)
        nxt = wf.next_step(work, s) if have else None
        if have:
            self._fill_results_table(work)
        busy_now = self.chain is not None and self.chain.active
        cur = getattr(self.chain, "current", None) if busy_now else None
        if cur and cur in getattr(self, "simple_note", {}):
            clock = getattr(self, "_step_clock", {})
            if clock.get("step") != cur:
                self._step_clock = {"step": cur, "t": __import__("time").time()}
            elapsed = int(__import__("time").time() - self._step_clock["t"])
            extra = "running {} min {:02d} s".format(elapsed // 60, elapsed % 60)
            if cur == "7":
                sim = os.path.join(work, "7_sim")
                n = len([fn for fn in os.listdir(sim) if fn.endswith("_depth.tif")]) if os.path.isdir(sim) else 0
                extra += "; depth rasters written: {}".format(n)
            self.simple_note[cur].setText((self.simple_note[cur].text() + " | " + extra).strip(" |"))
        hint = getattr(self, "next_hint", None)
        line = getattr(self, "status_line", None)
        by = {k: state for k, _t, state, _n in rows}

        def _word(st):
            return {"done": "ready", "waiting": "not yet", "running": "running",
                    "optional": "optional", "problem": "needs a look"}.get(st, st or "not yet")

        self._refresh_pipeline(by, busy_now, have, work)
        self.work_hint.setText("" if have else ("not set yet" if not work else "folder not found yet"))
        self.work_hint.setVisible(not have)
        data_set = bool(self.suite_edit.text().strip())
        self.data_hint.setVisible(not data_set)
        self.data_status.setVisible(data_set and self.data_status.text() != "No data folder yet.")
        self._update_settings_badge()
        if line is not None:
            screen = "running" if busy_now else _word(by.get("7"))
            line.setText("Dams {}. DEM {}. Screen {}.".format(
                _word(by.get("1")), _word(by.get("2")), screen))
        if hint is not None:
            if not have:
                hint.setText("Choose a work folder, a data folder, and a county.")
            elif busy_now:
                hint.setText("Screen running.")
            elif os.path.isfile(os.path.join(work, "7_results.gpkg")) or (have and wf.one_dam_runs(work)):
                counts = wf.summary_counts(work) or {}
                high, med = counts.get("High", 0), counts.get("Medium", 0)
                if high or med:
                    hint.setText("{} High, {} Medium. Open the Runner for those dams.".format(high, med))
                else:
                    hint.setText("Screen done. Continue follows this dam's dam.json.")
            else:
                hint.setText("Ready to screen.")

        busy = self.chain is not None and self.chain.active
        for k in wf.KEYS:
            self.step_button[k].setEnabled(have and not busy)
        self.next_btn.setEnabled(bool(nxt) and not busy)
        for b in (self.chain_btn, self.run_all_btn, self.one_dam_btn, self.preflight_btn):
            b.setEnabled(have and not busy)
        panel = getattr(self, "check_panel", None)
        if panel is not None and panel.blocked:          # 0.18.19: red check results block Screen
            self.run_all_btn.setEnabled(False)
        self.chain_stop_btn.setEnabled(busy)
        self.stop_btn.setEnabled(busy)
        cont = getattr(self, "continue_btn", None)
        if cont is not None:
            plugin, label, why = ("", "Continue", "")
            if have and not busy:
                plugin, label, why = wf.continue_target(work, self.settings(), self._selected_dam())
            cont.setText(label or "Continue")
            cont.setToolTip(why)
            cont.setEnabled(bool(plugin) and not busy)
            cont.setVisible(bool(plugin) and not busy)
        text = ("Choose or create a work folder first." if not have else
                ("Next: {} - {}. {}".format(nxt, wf.TITLE[nxt], wf.HINT[nxt]) if nxt else
                 "Nothing required is left: every non-optional step that can start is done."))
        self.hint_label.setText(text)
        self.simple_hint.setText(text if not have else
                                 ("Next: {}. {}".format(SG.plain(nxt, wf.TITLE[nxt]), wf.HINT[nxt]) if nxt
                                  else "Everything required is done. Results tab: report and layers."))
        self.counts_label.setText(wf.counts_text(wf.summary_counts(work)))
        self.record_label.setText(wf.dam_record_text(wf.dams_folder(work, s)) if have else "")
        self.report_btn.setEnabled(have and os.path.isfile(os.path.join(work, wf.REPORT_INDEX)))
        self.add_btn.setEnabled(have and bool(wf.result_layers(work)))
        runs = wf.one_dam_runs(work) if have else []
        self.one_dam_label.setText("One-dam runs: {}".format(", ".join(d for d, _f in runs) if runs else "none yet"))
        cur = self.one_dam_combo.currentData()
        self.one_dam_combo.clear()
        for d, folder in runs:
            self.one_dam_combo.addItem(d, folder)
        if cur:
            i = self.one_dam_combo.findData(cur)
            if i >= 0:
                self.one_dam_combo.setCurrentIndex(i)
        self.one_dam_report_btn.setEnabled(bool(runs))
        if have:
            self.refresh_review()

    @staticmethod
    def _short_note(note, limit=40):
        """0.17.23: one short line next to a step button; the full text is the tooltip."""
        note = (note or "").strip()
        if len(note) <= limit:
            return note
        cut = note[:limit].rsplit(" ", 1)[0].rstrip(" ;,(")
        return cut + " \u2026"

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
    def _open_step(self, key):
        work = self.work_edit.text().strip()
        if not work:
            QMessageBox.information(self, TITLE, "Choose a work folder first.")
            return False
        if not os.path.isdir(work):
            try:
                os.makedirs(work)
                self._log("Created the work folder {}".format(work))
            except OSError as e:
                QMessageBox.warning(self, TITLE, "The work folder cannot be created: {}".format(e))
                return False
        self._ensure_layout(work)
        params = wf.defaults(key, work, self.settings())
        aid = wf.algorithm_id(key)
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
                "Processing Toolbox: DamSafety Screening > {}.".format(aid, e, wf.TITLE[key]))
            return False
        self._log(">>> Opened {} ({})".format(aid, ", ".join(
            "{}={}".format(k, os.path.basename(str(v)) if os.path.isabs(str(v).split("|")[0]) else v)
            for k, v in sorted(params.items())) or "no values filled"))
        self._save_state()
        return True

    def _open_next(self):
        work = self.work_edit.text().strip()
        nxt = wf.next_step(work, self.settings()) if work and os.path.isdir(work) else None
        if nxt:
            self._open_step(nxt)

    def _open_help(self):
        if os.path.isfile(HELP_FILE):
            QDesktopServices.openUrl(QUrl.fromLocalFile(HELP_FILE))
            return True
        QMessageBox.information(self, TITLE, "The help page is missing from this install ({}).".format(HELP_FILE))
        return False

    # --------------------------------------------------------------- preflight --
    def _preflight(self, steps=None, settings=None, work=None):
        """Run the checks and show them on the Simple tab. Returns the checks."""
        work = work or self.work_edit.text().strip()
        S = settings or self.settings()
        include = tuple(k for k in wf.CHAIN_OPTIONAL if k in (S.get("chain") or []))
        if steps is None and work and os.path.isdir(work):
            steps = wf.chain_plan(work, S, include)
        checks = pf.run_checks(work, S, steps=steps, env=QgisEnv(self._read_layer))
        self.last_checks = checks
        lines = pf.report_lines(checks)
        self.preflight_text.setPlainText("\n".join(lines))
        self.preflight_label.setText("Check: " + pf.summary(checks))
        for line in lines[:1]:
            self._log(line)
        self._show_checks(checks)
        return checks

    def _show_checks(self, checks, extra_errors=()):
        """The check list in the shared CheckPanel: FAIL -> red (Screen this county blocked),
        WARN -> amber rows, the rest folded into "N checks passed". `extra_errors`: refusals
        of the run itself (no work folder, nothing to run), reported the same way."""
        from .ui_widgets import ERROR, WARNING, OK
        level = {pf.FAIL: ERROR, pf.WARN: WARNING}
        results = [(ERROR, e) for e in extra_errors]
        for c in checks or []:
            lvl = level.get(c.get("status"), OK)
            text = "{}: {}".format(c.get("title", ""), c.get("detail", ""))
            if c.get("fix") and lvl != OK:
                text += " \u2192 " + c["fix"]
            results.append((lvl, text))
        self.check_panel.set_results(results)

    def _refuse_run(self, text):
        """A county run that cannot start: the reason in the red panel (never a pop-up), the
        log, and the Run tab in front so it is seen."""
        self._show_checks([], extra_errors=[text])
        self._log("!!! " + text)
        try:
            self.tabs.setCurrentIndex(0)
        except Exception:                                        # noqa
            pass
        return False

    # ------------------------------------------------------------ run in order --
    def _yes(self):
        sb = getattr(QMessageBox, "StandardButton", QMessageBox)
        return sb.Yes

    def _definitions(self, aid):
        """[(name, description, optional, has_default, is_destination)] of a tool's parameters
        (core/workflow.required_missing). A hook: the tests replace it."""
        from qgis.core import QgsApplication, QgsProcessingParameterDefinition as D
        reg = QgsApplication.processingRegistry()
        alg = reg.createAlgorithmById(aid)
        try:                                  # QGIS 4 / PyQt6: scoped enum
            flag = D.Flag.FlagOptional
        except AttributeError:                # QGIS 3.x
            flag = D.FlagOptional
        out = []
        for d in alg.parameterDefinitions():
            out.append((d.name(), d.description(), bool(d.flags() & flag),
                        d.defaultValue() is not None, bool(d.isDestination())))
        return out

    def _chain_problems(self, step, aid, params):
        try:
            return ["{} is empty".format(m) for m in wf.required_missing(self._definitions(aid), params)]
        except Exception as e:                                   # noqa
            self._log("(parameters of {} could not be checked: {})".format(aid, e))
            return []

    def _start_algorithm(self, step, aid, params, on_done):
        """Run a tool in a background task and call on_done(ok, message) when it ends."""
        from qgis.core import (QgsApplication, QgsProcessingAlgRunnerTask, QgsProcessingContext,
                               QgsProcessingFeedback, QgsProject)
        alg = QgsApplication.processingRegistry().createAlgorithmById(aid)
        if alg is None:
            return on_done(False, "{} is not available".format(aid))
        ctx = QgsProcessingContext()
        ctx.setProject(QgsProject.instance())
        dlg = self

        class _Feedback(QgsProcessingFeedback):
            def pushInfo(self, info):
                dlg._bridge_log(info)
                super().pushInfo(info)

            def setProgressText(self, text):
                dlg._bridge_log(text)
                super().setProgressText(text)

            def setProgress(self, progress):
                dlg._bridge_progress(progress)
                super().setProgress(progress)

            def pushWarning(self, warning):
                dlg._bridge_log("WARNING: " + str(warning))
                super().pushWarning(warning)

            def reportError(self, error, fatalError=False):
                dlg._bridge_log("ERROR: " + str(error))
                super().reportError(error, fatalError)

        fb = _Feedback()
        ok, why = alg.checkParameterValues(params, ctx)
        if not ok:
            return on_done(False, why or "the values were refused")
        task = QgsProcessingAlgRunnerTask(alg, params, ctx, fb)

        def finished(successful, results=None):
            self._keep = []
            self._task = None
            on_done(bool(successful), "" if successful else "see the log above and the Processing log")

        task.executed.connect(finished)
        self._keep = [alg, ctx, fb, task]
        self._task = task
        QgsApplication.taskManager().addTask(task)

    def _bridge_progress(self, progress):
        try:
            from qgis.PyQt.QtCore import QTimer
            QTimer.singleShot(0, lambda p=float(progress): self.hint_label.setText(
                "Cache terrain is running: {:.0f}%".format(p)))
        except Exception:
            pass

    def _bridge_log(self, text):
        """Feedback arrives on the task's thread: hand it to the window's thread."""
        try:
            from qgis.PyQt.QtCore import QTimer
            QTimer.singleShot(0, lambda t=str(text): self._log("    " + t))
        except Exception:                                        # noqa
            pass

    def _run_chain(self, preflight=False):
        """Run the steps in order; with `preflight` (the Simple tab's Run all) the check list runs
        first and a FAIL stops it."""
        work = self.work_edit.text().strip()
        if not work or not os.path.isdir(work):
            return self._refuse_run("Choose a work folder first.")
        if self.chain is not None and self.chain.active:
            return False
        S = self.settings()
        include = tuple(k for k in wf.CHAIN_OPTIONAL if k in S.get("chain", []))
        # what would run, and which data it would run without
        first, problem = wf.chain_next(work, S, include, ())
        if first is None and problem:
            return self._refuse_run("Nothing can run: " + problem + ".")
        steps_planned = wf.chain_plan(work, S, include)
        if not steps_planned:
            return self._refuse_run("Nothing to run: every required step is done.")
        if preflight:
            checks = self._preflight(steps=steps_planned, settings=S)
            if not pf.ready(checks):
                self._log("!!! Not ready: " + pf.summary(checks))
                return False                   # the red panel says what to fix
        plan = ["{} - {}".format(st, wf.TITLE[st]) for st in steps_planned]
        notes = []
        for st in steps_planned:
            notes += wf.chain_summary(st, work, S)
        text = "Run these steps one after the other?\n\n" + "\n".join(plan)
        if notes:
            text += "\n\nData the steps will use:\n" + "\n".join(notes)
        if QMessageBox.question(self, TITLE, text) != self._yes():
            return False
        self._save_state()
        try:
            wf.write_run_settings(work, S, self._log)
        except Exception as e:                                   # noqa
            self._log("settings record not written: {}".format(e))
        self._one_dam = None
        self.chain = wf.Chain(work, self.settings, include, self._start_algorithm, log=self._log,
                              problems=self._chain_problems, on_end=self._chain_ended)
        self.check_panel.clear()
        self.chain.begin()
        self.refresh()
        return True

    def _export_settings(self, path=None):
        """The settings JSON for the headless chain tool (algorithms/run_chain.load_settings)."""
        if path is None:
            work = self.work_edit.text().strip()
            path, _f = QFileDialog.getSaveFileName(
                self, "Settings JSON", os.path.join(work, "screening_settings.json") if work else "",
                "JSON (*.json)")
        if not path:
            return ""
        s = {k: v for k, v in self.settings().items() if k != "work"}
        try:
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(s, fh, indent=1, sort_keys=True)
        except OSError as e:
            QMessageBox.warning(self, TITLE, "Settings not written: {}".format(e))
            return ""
        self._log("Settings written to {} (for 15 - Run the steps in order (headless) / qgis_process "
                  "nddam:runchain --WORK=<work folder> --SETTINGS={})".format(path, path))
        return path

    def _stop_chain(self):
        if self.chain is not None and self.chain.active:
            self.chain.cancel()
            self._log("Stopping after the step that is running\u2026")
            task = self._task
            try:
                if task is not None:
                    task.cancel()
            except Exception:                                    # noqa
                pass

    def _chain_ended(self, ok, text):
        one = self._one_dam
        self._one_dam = None
        self.refresh()
        if not ok:
            # 0.18.19: the step that failed, in the red panel next to the Run button (the log
            # has the detail); it clears on the next check or run
            self._show_checks([], extra_errors=[text])
            self._log("!!! " + text)
            return
        if one:
            dam_id, folder = one
            self._log("One-dam run finished: {} -> {}".format(dam_id, folder))
            report = os.path.join(folder, wf.REPORT_INDEX)
            if os.path.isfile(report):
                QDesktopServices.openUrl(QUrl.fromLocalFile(report))
            self._add_results(work=folder, title="DamSafety Screening - {}".format(dam_id))

    # ----------------------------------------------------------------- one dam --
    def _run_one_dam(self, dam_id=None):
        work = self.work_edit.text().strip()
        if not work or not os.path.isdir(work):
            return self._refuse_run("Choose a work folder first.")
        if self.chain is not None and self.chain.active:
            return False
        S = self.settings()
        if dam_id is None:
            choices = wf.dam_choices(work, S, self._read_rows)
            if choices:
                labels = [lab for _d, lab in choices]
                picked, ok = QInputDialog.getItem(self, TITLE, "Which dam or pond? (type to search)", labels, 0, True)
                if not ok or not picked:
                    return False
                dam_id = dict((lab, d) for d, lab in choices).get(picked, picked.split()[0] if picked.strip() else "")
            else:
                dam_id, ok = QInputDialog.getText(self, TITLE, "Dam ID (DWR structure ID such as 5289, DWR-5289, "
                                                               "or a pond ID / water-body ID):")
                if not ok or not str(dam_id).strip():
                    return False
        dam_id = str(dam_id).strip()
        folder = wf.one_dam_work(work, dam_id)
        try:
            os.makedirs(folder, exist_ok=True)
        except OSError as e:
            return self._refuse_run("The one-dam folder cannot be created: {}".format(e))
        S1 = wf.one_dam_settings(S, work, dam_id)
        what = S.get("screen_what", 0)
        include = tuple(k for k in ("16", "18") if (k == "16" and what in (1, 2)) or (k == "18" and what == 2))
        if "0" in (S.get("chain") or []) and not wf.package_path(work):
            include = ("0",) + include
        plan = wf.chain_plan(folder, S1, include)
        if not plan:
            return self._refuse_run("Nothing to run for {}: every step is done in {}.".format(dam_id, folder))
        text = "Run the whole chain for {} alone?\n\nFolder: {}\nSteps: {}\n\nThe quick screen (6) is skipped so " \
               "the flood is simulated; the result is a screening of one dam, not the batch.".format(
                   dam_id, folder, ", ".join(SG.plain(k, wf.TITLE[k]) for k in plan))
        if QMessageBox.question(self, TITLE, text) != self._yes():
            return False
        self._save_state()
        try:
            wf.write_run_settings(folder, S1, self._log)
        except Exception:                                        # noqa
            pass
        self._one_dam = (dam_id, folder)
        self.chain = wf.Chain(folder, lambda: wf.one_dam_settings(self.settings(), work, dam_id), include,
                              self._start_algorithm, log=self._log, problems=self._chain_problems,
                              on_end=self._chain_ended)
        self.chain.begin()
        self.refresh()
        return True

    def _run_selected_dams(self):
        """Pick several dams and run the county chain for those dams only."""
        work = self.work_edit.text().strip()
        if not work or not os.path.isdir(work):
            return self._refuse_run("Choose a work folder first.")
        if self.chain is not None and self.chain.active:
            return False
        S = self.settings()
        choices = wf.dam_choices(work, S, self._read_rows)
        if not choices:
            return self._refuse_run("No dams to pick. Run step 1 first, or fill the suite folder.")
        dlg = QDialog(self)
        dlg.setWindowTitle(TITLE)
        dlg.setMinimumWidth(420)
        lay = QVBoxLayout(dlg)
        lay.addWidget(QLabel("Check the dams to screen. The county chain runs for these dams only."))
        lst = QListWidget()
        lst.setSelectionMode(getattr(QListWidget, 'SelectionMode', QListWidget).MultiSelection)
        for did, lab in choices:
            item = QListWidgetItem(lab)
            item.setData(getattr(Qt, "ItemDataRole", Qt).UserRole, did)
            item.setFlags(item.flags() | getattr(Qt, 'ItemFlag', Qt).ItemIsUserCheckable)
            item.setCheckState(getattr(Qt, 'CheckState', Qt).Unchecked)
            lst.addItem(item)
        lay.addWidget(lst)
        btns = QHBoxLayout()
        all_btn = QPushButton("Check all")
        none_btn = QPushButton("Check none")
        ok_btn = QPushButton("Run these dams")
        cancel_btn = QPushButton("Cancel")
        btns.addWidget(all_btn)
        btns.addWidget(none_btn)
        btns.addStretch()
        btns.addWidget(ok_btn)
        btns.addWidget(cancel_btn)
        lay.addLayout(btns)
        all_btn.clicked.connect(lambda: [lst.item(i).setCheckState(getattr(Qt, 'CheckState', Qt).Checked) for i in range(lst.count())])
        none_btn.clicked.connect(lambda: [lst.item(i).setCheckState(getattr(Qt, 'CheckState', Qt).Unchecked) for i in range(lst.count())])
        ok_btn.clicked.connect(dlg.accept)
        cancel_btn.clicked.connect(dlg.reject)
        if (dlg.exec() if hasattr(dlg, 'exec') else dlg.exec_()) != getattr(QDialog, 'DialogCode', QDialog).Accepted:
            return False
        ids = [lst.item(i).data(getattr(Qt, "ItemDataRole", Qt).UserRole) for i in range(lst.count()) if lst.item(i).checkState() == getattr(Qt, 'CheckState', Qt).Checked]
        if not ids:
            return self._refuse_run("No dams checked.")
        S1 = dict(S)
        S1["only_ids"] = ",".join(ids)
        what = S.get("screen_what", 0)
        include = tuple(k for k in ("16", "18") if (k == "16" and what in (1, 2)) or (k == "18" and what == 2))
        if "0" in (S.get("chain") or []) and not wf.package_path(work):
            include = ("0",) + include
        plan = wf.chain_plan(work, S1, include)
        if not plan:
            return self._refuse_run("Nothing to run: every step is done.")
        text = "Run the county chain for {} dams?\n\n{}\n\nSteps: {}".format(
            len(ids), ", ".join(ids[:8]) + ("\u2026" if len(ids) > 8 else ""),
            ", ".join(SG.plain(k, wf.TITLE[k]) for k in plan))
        if QMessageBox.question(self, TITLE, text) != self._yes():
            return False
        self._save_state()
        try:
            wf.write_run_settings(work, S1, self._log)
        except Exception:
            pass
        self.chain = wf.Chain(work, lambda: S1, include, self._start_algorithm, log=self._log,
                              problems=self._chain_problems, on_end=self._chain_ended)
        self.chain.begin()
        self.refresh()
        return True

    def _open_one_dam_report(self):
        folder = self.one_dam_combo.currentData()
        if folder:
            p = os.path.join(folder, wf.REPORT_INDEX)
            if os.path.isfile(p):
                QDesktopServices.openUrl(QUrl.fromLocalFile(p))
            else:
                QMessageBox.information(self, TITLE, "No report yet in {}.".format(folder))

    def show_settings(self):
        """Open the Settings window (the gear; also the suite menu's 'Suite settings...')."""
        try:
            self._update_settings_badge()
            self.settings_window.show()
            self.settings_window.raise_()
        except Exception:                                        # noqa
            pass

    def closeEvent(self, event):
        self._save_state()
        try:
            event.accept()
        except Exception:                                        # noqa
            pass


    def _connect_package(self, path):
        """Selecting the compiled package adds its layers and the raster servers."""
        try:
            from .core.offline import connect_package
            added = connect_package(path)
            self._log("Package connected: " + ", ".join(added))
        except Exception as e:
            self._log("Package not connected: {}".format(e))
