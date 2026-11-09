# -*- coding: utf-8 -*-
"""The Settings window of the DamSafety Screening popup: the Run options page of the settings grid, the county DEM download box, the settings file, the presets.

0.18.19 (usability plan, change 5/8-style clean-up): split out of dialog.py mechanically - the
methods are moved, not changed - so the window reads tab by tab, as DamSafety Hazard's does.
Dialog contract: the host is NDDamScreeningDialog (dialog.py); these methods use its widgets,
settings(), _log() and refresh()."""
import os

from qgis.PyQt.QtWidgets import (
    QVBoxLayout, QGridLayout, QWidget, QLabel, QLineEdit, QPushButton, QComboBox, QSpinBox,
    QDoubleSpinBox, QGroupBox, QFileDialog, QMessageBox, QCheckBox,
)

from .core import workflow as wf
from .core import handoff as ho
from .core import settings_groups as SG
from .damsuite import settings as DS
from .tool_dialog import exec_tool_dialog
from .dialog_consts import TITLE, PRESETS_DIR, HINT_STYLE


class SettingsTabMixin(object):
    """Run options, the county DEM box, the settings grid and file, presets."""

    # -- Settings: run options + the grid
    def _build_run_options(self):
        from .algorithms.run_breach_sim import TERRAIN, SCENARIOS
        w = QWidget()
        lay = QVBoxLayout()
        info = QLabel("Choices of this window that the tools' dialogs are filled with (files of this computer, "
                      "engine options). Every one can still be changed in the dialog. The parameters of the "
                      "tools themselves are on the other tabs.")
        info.setWordWrap(True)
        lay.addWidget(info)
        grid = QGridLayout()
        self.local_dem = QLineEdit()
        self.larger_clips = self._combo(TERRAIN)
        self.exe = QLineEdit()
        self.scenario = self._combo(SCENARIOS)
        self.workers = QSpinBox()
        self.workers.setRange(0, 64)
        self.dem_cell = QDoubleSpinBox()
        self.dem_cell.setRange(0.5, 100.0)
        self.dem_cell.setDecimals(1)
        self.dem_cell.setSingleStep(1.0)
        self.dem_cell.setToolTip("Step 2: the cell size of the cached DEM. 10 m is the starting value. "
                                 "The flood does not have to use this size.")
        self.grid_m = QDoubleSpinBox()
        self.grid_m.setRange(0.0, 500.0)
        self.grid_m.setDecimals(1)
        self.grid_m.setSingleStep(1.0)
        self.grid_m.setToolTip("Step 7: the flood grid. 0 = chosen from the reservoir size. "
                               "It can be coarser than the DEM. The starting value is 20 m.")
        self.simh = QDoubleSpinBox()
        self.simh.setRange(0.0, 240.0)
        self.simh.setDecimals(1)
        self.simh.setToolTip("Step 7: how many hours of flood to simulate. 0 = the tool estimates it. "
                             "The starting value is 4.")
        self.simmax = QDoubleSpinBox()
        self.simmax.setRange(1.0, 240.0)
        self.simmax.setDecimals(1)
        self.simmax.setToolTip("Step 7: the clock is lengthened, up to this many hours, while the flood is "
                               "still spreading. The starting value is 12.")
        self.chart_file = QLineEdit()
        self.runner_dem = QLineEdit()
        self.dams_folder = QLineEdit()
        for e in (self.local_dem, self.exe, self.chart_file, self.runner_dem, self.dams_folder):
            e.editingFinished.connect(self._changed)
        self.workers.valueChanged.connect(self._changed)
        self.workers.setToolTip("0 = leave the tool's own default (CPU cores - 1).")
        self.chart_file.setToolTip("The digitized ACER TM 11 chart file (.json). Empty = the tools' "
                                   "own default. ND Dam Hazard's 'Chart sign-off' writes one.")
        self.dams_folder.setToolTip("The folder of dam folders (one per dam) that tool 7b writes and "
                                    "ND Dam Breach Runner reads; tool 8 adds a 'screened' status to "
                                    "dam.json there. Empty = <work folder>/Dams.")
        # step 7b: the Runner's models / solver (defaults = the Runner's own)
        self.runner_tier = self._combo(list(ho.ENGINE_LABELS))
        self.runner_tier.setToolTip("Step 7b: the Runner's engine (one choice; 'then ANUGA' is the old "
                                    "'force ANUGA'). Only ANUGA models the culvert / bridge layer "
                                    "(Inputs: Culverts / bridges). The default sends nothing extra.")
        self.runner_refine = QCheckBox("Step 7b: near-dam refine (finer FV1 grid near the dam; slower)")
        self.runner_refine.toggled.connect(self._changed)
        # 0.17.46: the Inputs water bodies as the Runner's report mask (rundam WATER_MASK)
        self.runner_water_mask = QCheckBox("Step 7b: mask existing lakes and ponds in the Runner's report")
        self.runner_water_mask.setToolTip(
            "Off (default): the Runner does what it always does. On: step 7b gives the Runner the "
            "'NHD waterbodies' layer from the Inputs tab, and the Runner's run report then shows the "
            "flooded area and speeds twice - with and without the lakes and ponds that were "
            "already there before the breach. Only the report changes: the flood model, its maps "
            "and the hazard class are the same either way. Needs that layer saved in a "
            "file (Inputs tab) and ND Dam Breach Runner 3.10.27 or later.")
        self.runner_water_mask.toggled.connect(self._changed)
        self.runner_tier.currentIndexChanged.connect(self._changed)
        self.baseline = QCheckBox("Step 7: also run a no-breach baseline (increment rasters)")
        self.baseline.setToolTip("A second simulation of each dam with only the steady ambient flow, no "
                                 "breach; the increment (breach minus baseline) is written too. A screening "
                                 "stand-in for ND's flood-scenario comparison; the real PMF routing is the "
                                 "Runner's.")
        self.baseline_cfs = QDoubleSpinBox()
        self.baseline_cfs.setRange(0.0, 1e7)
        self.baseline_cfs.setDecimals(1)
        self.baseline_cfs.setToolTip("Steady ambient flow through the dam site for the baseline (cfs; 0 = dry). "
                                     "YOUR value - the tool does not know the spillway flow.")
        self.buildings = self._combo(["Not represented (bare terrain)", "Raise the DEM at building footprints",
                                      "Manning's n at building footprints"])
        self.buildings.setToolTip("Step 7: how polygon footprints of the structures layer enter the terrain. "
                                  "Points are ignored. Neither treatment is right everywhere - YOUR choice.")
        self.building_raise_ft = QDoubleSpinBox()
        self.building_raise_ft.setRange(0.1, 200.0)
        self.building_raise_ft.setDecimals(1)
        self.building_n = QDoubleSpinBox()
        self.building_n.setRange(0.01, 5.0)
        self.building_n.setDecimals(3)
        self.incremental = QCheckBox("Step 8: count impacts from the incremental depth")
        self.incremental.setToolTip("Needs step 7's baseline. A structure counts as flooded by the BREACH only "
                                    "where breach depth minus baseline depth meets the threshold.")
        self.ref_folder = QLineEdit()
        self.ref_folder.setToolTip("Step 11: HEC-RAS / DSS-WISE reference rasters, in per-dam sub-folders or "
                                   "files named by dam ID.")
        self.ref_units = self._combo(["US customary (ft, ft/s, hours)", "Metric (m, m/s, hours)"])
        self.old_inventory = QLineEdit()
        self.old_inventory.setToolTip("Step 13: the previous inventory (a tool-1 GeoPackage or DWR export) "
                                      "to compare the current 1_dams.gpkg with.")
        self.reviewer_edit = QLineEdit()
        self.reviewer_edit.setToolTip("Your name or initials, written with every decision on the Review tab.")
        for e in (self.ref_folder, self.old_inventory, self.reviewer_edit):
            e.editingFinished.connect(self._changed)
        for c in (self.baseline, self.incremental):
            c.toggled.connect(self._changed)
        for sp in (self.baseline_cfs, self.building_raise_ft, self.building_n,
                   self.dem_cell, self.grid_m, self.simh, self.simmax):
            sp.valueChanged.connect(self._changed)
        self.buildings.currentIndexChanged.connect(self._changed)
        self.ref_units.currentIndexChanged.connect(self._changed)
        self.larger_clips.currentIndexChanged.connect(self._changed)
        self.scenario.currentIndexChanged.connect(self._changed)
        rows = (
            ("Local DEM folder (LiDAR tiles)", self.local_dem, "dir"),
            ("Step 7 terrain for larger clips", self.larger_clips, None),
            ("LISFLOOD-FP executable", self.exe, "file"),
            ("Step 7 scenario", self.scenario, None),
            ("DEM cell size (m)", self.dem_cell, None),
            ("Grid size (m)", self.grid_m, None),
            ("Simulated time (h)", self.simh, None),
            ("Longest simulated time (h)", self.simmax, None),
            ("Dams at the same time (0 = tool default)", self.workers, None),
            ("ACER TM 11 chart file (.json)", self.chart_file, "json"),
            ("Step 7b DEM for the Runner", self.runner_dem, "raster"),
            ("Dams folder (7b, dam.json)", self.dams_folder, "dir"),
            ("Step 7b Runner engine", self.runner_tier, None),
            ("", self.runner_refine, None),
            ("", self.runner_water_mask, None),
            ("", self.baseline, None),
            ("Baseline ambient flow (cfs)", self.baseline_cfs, None),
            ("Step 7 buildings in the terrain", self.buildings, None),
            ("Buildings: raise the DEM by (ft)", self.building_raise_ft, None),
            ("Buildings: Manning's n", self.building_n, None),
            ("", self.incremental, None),
            ("Step 11 reference rasters folder", self.ref_folder, "dir"),
            ("Step 11 reference units", self.ref_units, None),
            ("Step 13 previous inventory", self.old_inventory, "file"),
            ("Reviewer (Review tab)", self.reviewer_edit, None),
        )
        for i, (label, widget, kind) in enumerate(rows):
            grid.addWidget(QLabel(label), i, 0)
            grid.addWidget(widget, i, 1)
            if kind:
                b = QPushButton("Browse…")
                b.clicked.connect(lambda _c=False, e=widget, k=kind: self._browse_for(e, k))
                grid.addWidget(b, i, 2)
        lay.addLayout(grid)
        lay.addStretch(1)
        w.setLayout(lay)
        return w



    def _county_dem_box(self):
        """0.18.19 (usability plan, change 7): one button, "Download DEM for this county…", with the
        resolution beside it. The box used to repeat the Download DEM tool's whole form (source,
        area, county, folder, dam point, buffer, FTP path, format ticks) and then open the tool's
        own dialog anyway; now the dialog opens with the county (the one chosen above), the
        resolution and the folder filled in, and every other choice is made there. The DEM folder
        (dem_out, <work>/terrain) is still filled from the suite folder for tool 2."""
        box = QGroupBox("Download the county DEM")
        box.setToolTip("Download the county DEM, if you do not already have one.")
        box.setCheckable(True)
        box.setChecked(False)
        grid = QGridLayout()
        grid.addWidget(QLabel("Resolution"), 0, 0)
        self.dem_res = QComboBox()
        self.dem_res.addItems(["10 m", "1 m"])
        self.dem_res.setToolTip("Local file is EPSG:5070, metres, tiled GeoTIFF. 10 m is the county working copy.")
        grid.addWidget(self.dem_res, 0, 1)
        go = QPushButton("Download DEM for this county…")
        go.setToolTip("Opens Download DEM with the county chosen above, the resolution and the DEM "
                      "folder filled in. USGS 3DEP by default; ND DWR LiDAR, the native 1 m tiles, "
                      "one dam or the state are chosen in the tool.")
        go.clicked.connect(self._open_dem_download)
        self.dem_download_btn = go
        grid.addWidget(go, 0, 2)
        self.dem_out = QLineEdit()                      # the DEM folder tool 2 reads (kept)
        self.dem_out.setPlaceholderText("Where Burleigh_10m_5070.tif is written (empty = <work folder>/terrain)")
        self.dem_out.setToolTip("The folder the county DEM is written to and tool 2 reads from.")
        browse = QPushButton("Browse…")
        browse.clicked.connect(lambda _c=False: self._browse_for(self.dem_out, "dir"))
        grid.addWidget(QLabel("DEM folder"), 1, 0)
        grid.addWidget(self.dem_out, 1, 1)
        grid.addWidget(browse, 1, 2)
        # 0.19.1 (Nick, 2026-11-09): the short ND DWR units note is back on the window; the
        # Download DEM tool's help has the long form.
        self.dem_facts = QLabel("USGS 3DEP: EPSG:5070, metres, GeoTIFF; a county at 10 m is a few "
                                "minutes, at 1 m hours. "
                                "ND DWR LiDAR: DEM_Imagine .img in UTM metres; ASCII_Grids_ft in State Plane feet, converted.")
        self.dem_facts.setWordWrap(True)
        self.dem_facts.setStyleSheet(HINT_STYLE)
        grid.addWidget(self.dem_facts, 2, 0, 1, 3)
        off = QPushButton("Download offline layers for this county…")
        off.setToolTip("County dropdown above. Writes dams, flowlines, waterbodies, roads, rail, structures, and address points to a GeoPackage.")
        off.clicked.connect(self._open_offline_layers)
        grid.addWidget(off, 3, 0, 1, 3)
        grid.setColumnStretch(1, 1)
        return self._fold(box, grid)

    def _download_county(self):
        """The county the DEM and offline-layer downloads are for: the inventory county (0.18.19:
        the download box no longer has its own county list)."""
        text = self.inventory_county.currentText()
        return "" if text == "All counties" else text

    def _browse_suite(self):
        folder = QFileDialog.getExistingDirectory(self, "Suite data folder", self.suite_edit.text() or self.work_edit.text())
        if folder:
            self.suite_edit.setText(folder)
            self.suite_folder = folder
            self._fill_suite_edit()

    def _fill_suite_edit(self):
        folder = self.suite_edit.text().strip()
        if not folder:
            self._fill_from_suite()
            if getattr(self, "suite_folder", ""):
                self.suite_edit.setText(self.suite_folder)
            return
        self.suite_folder = folder
        from .damsuite.suite_fill import scan
        found, notes = scan(folder)
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
        if not self.dem_out.text().strip():
            self.dem_out.setText(os.path.join(self.work_edit.text().strip() or folder, "terrain"))
        if hasattr(self, "local_dem") and not self.local_dem.text().strip():
            self.local_dem.setText(self.dem_out.text().strip())
        self.suite_note.setText("Filled {} from {}.".format(", ".join(filled) or "nothing", folder))
        found_names = {
            "dwr": "DWR dams", "dam_lines": "dam lines", "structures": "structures",
            "roads": "roads", "rail": "rail", "address_points": "address points",
            "nid": "NID", "streams": "flowlines", "waterbodies": "waterbodies",
        }
        shown = [found_names[k] for k in found_names if k in found]
        status = "Found " + ", ".join(shown) + "." if shown else "Nothing matched in that folder."
        try:
            from .damsuite.suite_fill import find_pack
            if find_pack(folder):
                status = "Offline pack. " + status
        except Exception:
            pass
        if hasattr(self, "data_status"):
            self.data_status.setText(status)
        for n in notes:
            self._log("suite: " + n)
        self._log("Suite folder {} autofilled {}. DEM folder {}.".format(folder, ", ".join(filled) or "nothing", self.dem_out.text()))
        self._changed()

    def _open_dem_download(self):
        """The Download DEM tool's own dialog for the county chosen above (0.18.19: the window
        passes the county, the resolution and the folder; source, a dam point or the state are
        chosen in the tool)."""
        try:
            import processing
        except ImportError:                                      # the test doubles
            from qgis import processing
        folder = self.dem_out.text().strip()
        if not folder:
            folder = os.path.join(self.work_edit.text().strip() or os.path.expanduser("~"), "terrain")
            self.dem_out.setText(folder)
        self.terrain_source.setCurrentIndex(1)
        from .core.dem_download import county_names
        county = self._download_county()
        names = county_names()
        res = self.dem_res.currentIndex()
        self._log("Terrain set to the local DEM folder. Download DEM: {} | {} | {}".format(
            self.dem_res.currentText(), county or "(county not chosen - pick it in the tool)", folder))
        exec_tool_dialog(processing, "nddam:downloaddem", {
            "SOURCE": 0,
            "RES": res,
            "SCOPE": 1,
            "COUNTY": names.index(county) if county in names else 0,
            "CELL": 10.0 if res == 0 else 1.0,
            "OUT": folder,
        }, parent=self)


    def _open_offline_layers(self):
        try:
            import processing
        except ImportError:                                      # the test doubles
            from qgis import processing
        from .core.dem_download import county_names
        county = self._download_county() or "Burleigh"
        names = county_names()
        folder = self.work_edit.text().strip() or os.path.expanduser("~")
        out = os.path.join(folder, "{}_offline.gpkg".format(county.replace(" ", "_")))
        self._log("Offline layers: county {} -> {}".format(county, out))
        exec_tool_dialog(processing, "nddam:downloadlayers", {
            "COUNTY": names.index(county) if county in names else 0,
            "OUT": out,
        }, parent=self)

    def _build_settings_tab(self):
        from . import suite_settings as SS
        from .algorithms import _settings as ST
        try:
            from . import provider as prov
            catalog = ST.full_catalog(prov.ALGORITHMS)
        except Exception as e:                                   # noqa - a tool that cannot be built here
            catalog = []
            self._log("!!! settings grid: {}".format(e))
        try:
            from .damsuite import versions as vs
            versions = {SG.PLUGIN: vs.RELEASE.get(SG.PLUGIN, "")}
        except Exception:                                        # noqa
            versions = {}
        self.grid = SS.SettingsGrid(SG.PLUGIN, catalog, DS.Settings(), parent=self,
                                    on_change=self._grid_changed, on_file=self._grid_file,
                                    versions=versions, tab_order=SG.TAB_ORDER,
                                    extra_tabs=[("Run options", self._build_run_options())])
        return self.grid


    # ------------------------------------------------------------ settings file --
    def _settings_file_edited(self):
        self._load_settings_file(quiet=False)
        self._changed()

    def _pick_settings_file(self):
        start = self.settings_file_edit.text().strip() or self.work_edit.text().strip() or ""
        p, _f = QFileDialog.getOpenFileName(self, "Settings file", start,
                                            "Settings workbook (*.xlsx);;Settings table (*.csv);;All files (*)")
        if p:
            self.settings_file_edit.setText(p)
            self._settings_file_edited()

    def _new_settings_file(self):
        work = self.work_edit.text().strip()
        start = os.path.join(work, DS.DEFAULT_NAME) if work else DS.DEFAULT_NAME
        p, _f = QFileDialog.getSaveFileName(self, "New settings file", start,
                                            "Settings workbook (*.xlsx);;Settings table (*.csv)")
        if not p:
            return ""
        if not os.path.splitext(p)[1]:
            p += ".xlsx"
        self.settings_file_edit.setText(p)
        if self.grid is not None:
            wf.window_settings_to(self.grid.settings, self.settings())
            self.grid.settings.path = p
            self.grid.save(p, name=os.path.splitext(os.path.basename(p))[0].replace("_", " "))
        DS.set_current_path(p)
        self._log("Settings file written: {}".format(p))
        self._fill_presets()
        self._save_state()
        self.refresh()
        return p

    def _load_settings_file(self, quiet=True):
        """Read the settings file named on the Simple tab into the grid and the window."""
        p = self.settings_file_edit.text().strip()
        DS.set_current_path(p)
        if self.grid is None:
            return False
        if not p:
            self.grid.settings = DS.Settings()
            self.grid.fill()
            self._fill_presets()
            return False
        if not os.path.isfile(p):
            if not quiet:
                QMessageBox.warning(self, TITLE, "The settings file does not exist: {}".format(p))
            self._log("!!! settings file not found: {}".format(p))
            self._fill_presets()
            return False
        ok = self.grid.load(p, quiet=quiet)
        if ok:
            win = wf.window_settings_from(self.grid.settings)
            if win:
                was = self._loading
                self._loading = True
                try:
                    s = self.settings()
                    s.update(win)
                    self._apply(s)
                    self._fill_layers()
                finally:
                    self._loading = was
            self._log("Settings file: {} ({} value(s) differ from the starting values)".format(
                p, len(self.grid.settings.changed(self.grid.catalog))))
        self._fill_presets()
        return ok

    def _grid_changed(self, settings):
        """An edit in the grid: the file is saved at once when there is one (the tools read it)."""
        self._autosave_settings()
        if hasattr(self, "settings_btn"):
            self._update_settings_badge()

    def _grid_file(self, path):
        if path and path != self.settings_file_edit.text().strip():
            self.settings_file_edit.setText(path)
            DS.set_current_path(path)
            self._save_state()
        self._fill_presets()

    def _autosave_settings(self):
        if self._loading or self.grid is None:
            return
        p = self.settings_file_edit.text().strip()
        if not p:
            return
        try:
            wf.window_settings_to(self.grid.settings, self.settings(), defaults=not os.path.isfile(p))
            self.grid.settings.path = p
            self.grid.settings.save(p, catalog=self.grid.catalog, versions=self.grid.versions or None)
            DS.set_current_path(p)
        except DS.SettingsError as e:
            self._log("!!! settings not saved: {}".format(e))

    def _fill_presets(self):
        folders = [PRESETS_DIR]
        p = self.settings_file_edit.text().strip()
        if p:
            folders.append(os.path.dirname(p))
        self.presets = DS.list_presets(folders)
        self.preset_combo.clear()
        self.preset_combo.addItem("(choose a preset)", "")
        for name, path in self.presets:
            if p and os.path.abspath(path) == os.path.abspath(p):
                continue
            self.preset_combo.addItem(name, path)

    def _apply_preset(self):
        path = self.preset_combo.currentData()
        if not path:
            return False
        try:
            preset = DS.Settings.load(path)
        except DS.SettingsError as e:
            QMessageBox.warning(self, TITLE, str(e))
            return False
        n = len([k for k in preset.values if k[0] == SG.PLUGIN])
        if QMessageBox.question(self, TITLE, "Copy the {} value(s) of the preset '{}' into the current settings?"
                                "\n{}".format(n, self.preset_combo.currentText(), preset.meta.get("note", ""))) \
                != self._yes():
            return False
        if self.grid is not None:
            for (pl, tool, key), v in preset.values.items():
                if pl == SG.PLUGIN:
                    self.grid.settings.set(pl, tool, key, v)
            self.grid.fill()
            win = wf.window_settings_from(self.grid.settings)
            if win:
                s = self.settings()
                s.update(win)
                was = self._loading
                self._loading = True
                try:
                    self._apply(s)
                    self._fill_layers()
                finally:
                    self._loading = was
        self._log("Preset applied: {} ({})".format(self.preset_combo.currentText(), path))
        self._changed()
        return True

