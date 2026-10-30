# -*- coding: utf-8 -*-
"""2.21.0: 'Add model results to a dam' window (opened from the Classify view). The work is
adddam.add_results, shared with the Processing tool ndhazard:adddamresults."""

import os

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (QApplication, QCheckBox, QComboBox, QCompleter, QDialog,
                                 QFileDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
                                 QPushButton, QTextEdit, QVBoxLayout, QWidget)

from . import adddam as AD
from .tokens import TOKENS

_WAIT = getattr(Qt, "WaitCursor", None) or Qt.CursorShape.WaitCursor
_CASE_INSENSITIVE = getattr(Qt, "CaseInsensitive", None) or Qt.CaseSensitivity.CaseInsensitive
_CONTAINS = getattr(Qt, "MatchContains", None) or Qt.MatchFlag.MatchContains


def _row(*widgets):
    w = QWidget()
    lay = QHBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 0)
    for i, x in enumerate(widgets):
        lay.addWidget(x, 3 if i == 0 else 0)
    return w


class AddResultsDialog(QDialog):
    """Name a dam, choose its model results; they go to the dam's folder (made when the
    Dams folder has none). `on_use(folder)` is called by 'Use this dam folder'."""

    def __init__(self, parent=None, on_use=None):
        super().__init__(parent)
        self.setWindowTitle("DamSafety Hazard \u2014 Add model results to a dam")
        self.on_use = on_use
        self.last = None
        lay = QVBoxLayout(self)
        intro = QLabel(
            "Name the dam and choose its model results. They are put in the dam's folder in "
            "the Dams folder (a new folder with a dam.json when the dam has none), under "
            "Hazard_inputs, where the batch classification finds them. The results "
            "themselves are only read, and nothing already there is overwritten.")
        intro.setWordWrap(True)
        lay.addWidget(intro)
        form = QFormLayout()
        self.root_edit = QLineEdit()
        try:
            self.root_edit.setText(AD.dams_root("")[0])
        except AD.AddResultsError:
            pass
        self.root_btn = QPushButton("Browse\u2026")
        self.root_btn.clicked.connect(self._browse_root)
        form.addRow("Dams folder:", _row(self.root_edit, self.root_btn))
        self.dam_edit = QLineEdit()
        self.dam_edit.setPlaceholderText("Dam ID (DWR-2801, ND00199) or name")
        self.completer = QCompleter([], self)
        self.completer.setCaseSensitivity(_CASE_INSENSITIVE)
        try:
            self.completer.setFilterMode(_CONTAINS)
        except Exception:                                        # noqa
            pass
        self.dam_edit.setCompleter(self.completer)
        form.addRow("Dam name or ID:", self.dam_edit)
        self.match_label = QLabel("")
        self.match_label.setWordWrap(True)
        self.match_label.setStyleSheet("color:{};".format(TOKENS["muted"]))
        form.addRow("", self.match_label)
        self.model_combo = QComboBox()
        for key, label in AD.MODELS:
            self.model_combo.addItem(label, key)
        form.addRow("Model:", self.model_combo)
        self.src_edit = QLineEdit()
        self.src_edit.setPlaceholderText("DSS-WISE zip, RAS Mapper rasters, .p##.hdf plan, "
                                         "LISFLOOD-FP .max \u2026")
        self.src_file_btn = QPushButton("File\u2026")
        self.src_file_btn.clicked.connect(self._browse_file)
        self.src_dir_btn = QPushButton("Folder\u2026")
        self.src_dir_btn.clicked.connect(self._browse_folder)
        form.addRow("Model results:", _row(self.src_edit, self.src_file_btn, self.src_dir_btn))
        self.scen_combo = QComboBox()
        for key, label in AD.SCENARIOS:
            self.scen_combo.addItem(label, key)
        form.addRow("Scenario:", self.scen_combo)
        self.crs_edit = QLineEdit()
        self.crs_edit.setPlaceholderText("LISFLOOD-FP only, when its grids have no .prj - "
                                         "e.g. EPSG:26914")
        form.addRow("Coordinate system:", self.crs_edit)
        self.terrain_edit = QLineEdit()
        self.terrain_edit.setPlaceholderText("HEC-RAS plan only, optional - terrain for the "
                                             "depth")
        self.terrain_btn = QPushButton("Browse\u2026")
        self.terrain_btn.clicked.connect(self._browse_terrain)
        form.addRow("Terrain:", _row(self.terrain_edit, self.terrain_btn))
        self.assumed_check = QCheckBox("DSS-WISE: accept US units where a file name states "
                                       "none")
        form.addRow("", self.assumed_check)
        lay.addLayout(form)
        self.log = QTextEdit()
        self.log.setReadOnly(True)
        lay.addWidget(self.log, 1)
        bar = QHBoxLayout()
        self.add_btn = QPushButton("Add results")
        self.add_btn.setDefault(True)
        self.add_btn.clicked.connect(self.run)
        self.use_btn = QPushButton("Use this dam folder")
        self.use_btn.setToolTip("Put the dam folder in the Setup box of the Classify view")
        self.use_btn.setEnabled(False)
        self.use_btn.clicked.connect(self._use)
        self.close_btn = QPushButton("Close")
        self.close_btn.clicked.connect(self.reject)
        bar.addWidget(self.add_btn)
        bar.addWidget(self.use_btn)
        bar.addStretch()
        bar.addWidget(self.close_btn)
        lay.addLayout(bar)
        self.root_edit.textChanged.connect(self._refresh_names)
        self.dam_edit.textChanged.connect(self._show_match)
        self._refresh_names()
        self.resize(760, 560)

    # ---------------------------------------------------------------- pickers --
    def _browse_root(self, *args):
        d = QFileDialog.getExistingDirectory(self, "Dams folder", self.root_edit.text())
        if d:
            self.root_edit.setText(d)

    def _start(self):
        t = self.src_edit.text().strip()
        return t if os.path.isdir(t) else os.path.dirname(t)

    def _browse_file(self, *args):
        f, _ = QFileDialog.getOpenFileName(
            self, "Model results file", self._start(),
            "Model results (*.zip *.hdf *.max *.maxVc *.Vmax *.inittm *.mxe *.tif *.tiff "
            "*.vrt *.asc *.img *.flt);;All files (*)")
        if f:
            self.src_edit.setText(f)

    def _browse_folder(self, *args):
        d = QFileDialog.getExistingDirectory(self, "Folder of model results", self._start())
        if d:
            self.src_edit.setText(d)

    def _browse_terrain(self, *args):
        f, _ = QFileDialog.getOpenFileName(self, "Terrain raster", self._start(),
                                           "Rasters (*.tif *.tiff *.vrt *.img);;All files (*)")
        if f:
            self.terrain_edit.setText(f)

    # ---------------------------------------------------------------- state ----
    def _refresh_names(self, *args):
        root = self.root_edit.text().strip()
        names = AD.completions(root) if root and os.path.isdir(root) else []
        try:
            from qgis.PyQt.QtCore import QStringListModel
            self.completer.setModel(QStringListModel(names, self.completer))
        except Exception:                                        # noqa
            pass
        self._show_match()

    def match_text(self):
        root, dam = self.root_edit.text().strip(), self.dam_edit.text().strip()
        if not dam:
            return ""
        try:
            hit = AD.match_dam(root, dam) if root and os.path.isdir(root) else None
            if hit:
                return "Existing dam folder: {}".format(hit)
            return "No folder for this dam yet: {} will be made".format(
                os.path.join(root or "?", AD.new_folder_name(dam)))
        except AD.AddResultsError as e:
            return str(e)

    def _show_match(self, *args):
        self.match_label.setText(self.match_text())

    # ---------------------------------------------------------------- run ------
    def run(self, *args):
        self.log.clear()
        self.add_btn.setEnabled(False)
        QApplication.setOverrideCursor(_WAIT)

        def progress(pct, msg=""):
            if msg:
                self.add_btn.setText("Adding\u2026 {:.0f}%".format(pct))
            QApplication.processEvents()
        try:
            res = AD.add_results(self.dam_edit.text().strip(), self.src_edit.text().strip(),
                                 self.model_combo.currentData(), self.scen_combo.currentData(),
                                 dams_folder=self.root_edit.text().strip(),
                                 crs=self.crs_edit.text().strip(),
                                 terrain=self.terrain_edit.text().strip(),
                                 accept_assumed=self.assumed_check.isChecked(),
                                 progress=progress)
        except AD.AddResultsError as e:
            self.log.setPlainText("Not added: {}".format(e))
            return None
        except Exception as e:                                   # noqa - never a crash
            import traceback
            self.log.setPlainText("Not added (unexpected error): {!r}\n\n{}".format(
                e, traceback.format_exc()))
            return None
        finally:
            QApplication.restoreOverrideCursor()
            self.add_btn.setText("Add results")
            self.add_btn.setEnabled(True)
        self.last = res
        self.log.setPlainText("\n".join(AD.summary(res)))
        self.use_btn.setEnabled(True)
        self._refresh_names()
        return res

    def _use(self, *args):
        if self.last and self.on_use:
            self.on_use(self.last["dam_folder"])
            self.accept()
