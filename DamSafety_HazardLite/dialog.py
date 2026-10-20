# -*- coding: utf-8 -*-
"""DamSafety Hazard Lite window: the shared GUI grammar (docs/gui-grammar.md) over core.run().

The banner, the ⚙ gear, the CheckPanel, the bold primary button beside Check inputs, and the
▸ Log fold are the same widgets the other DamSafety windows use (shared/ui). One window, no tabs;
the work is one tool. The Processing algorithm (plugin.py) stays, so the tool also runs headless.
"""
import os
import traceback


from qgis.PyQt.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel,
                                 QLineEdit, QPushButton, QCheckBox, QFileDialog, QWidget,
                                 QPlainTextEdit, QToolButton, QMenu, QMessageBox)

from .ui_fit import fit_window
from .ui_widgets import InfoBanner, CheckPanel, Collapsible, StageStrip, ERROR, OK
from .tokens import TOKENS as T
from . import core

def _enum(cls, scope, name):
    """cls.<name> on Qt5, cls.<scope>.<name> on Qt6 (the flags are scoped there)."""
    return getattr(getattr(cls, scope, cls), name, None) or getattr(cls, name)


DISCLAIMER = ("Screening result under ND Dam Safety Standards REG_05.2026 Section 5.2. "
              "The engineer of record classifies the dam. hmax / vmax are read from the "
              "breach rasters; sole-access roads, lifelines, future development and dams in "
              "series are listed for review, not screened.")


def _row(grid, r, label, widget, button=None):
    grid.addWidget(QLabel(label), r, 0)
    grid.addWidget(widget, r, 1)
    if button is not None:
        grid.addWidget(button, r, 2)


class HazardLiteDialog(QDialog):
    def __init__(self, parent=None, iface=None):
        super().__init__(parent)
        self.iface = iface
        self.setWindowTitle("DamSafety Hazard Lite {}".format(core.VERSION))
        root = QVBoxLayout(self)
        root.addWidget(InfoBanner(DISCLAIMER))

        self.stage = StageStrip(["Rasters", "Pack", "Classify"])
        root.addWidget(self.stage)

        grid = QGridLayout()
        grid.setColumnStretch(1, 1)
        self.depth = QLineEdit()
        self.vel = QLineEdit()
        self.pack = QLineEdit(self._default_pack())
        self.out = QLineEdit()
        self.dam = QLineEdit()
        _row(grid, 0, "Max depth raster", self.depth, self._browse(self.depth, "raster"))
        _row(grid, 1, "Max velocity raster", self.vel, self._browse(self.vel, "raster"))
        _row(grid, 2, "Offline pack folder", self.pack, self._browse(self.pack, "dir"))
        _row(grid, 3, "Output folder", self.out, self._browse(self.out, "dir"))
        _row(grid, 4, "Dam name", self.dam)
        root.addLayout(grid)

        self.metres = QCheckBox("Rasters are in metres and m/s")
        self.judgment = QCheckBox("Count the judgment zone as High (5.3 borderline rule)")
        self.judgment.setChecked(True)
        self.exclude_ag = QCheckBox("Exclude FEMA agricultural out-buildings (5.2.1.1)")
        self.exclude_ag.setChecked(True)
        root.addWidget(self.metres)
        root.addWidget(self.judgment)
        root.addWidget(self.exclude_ag)

        self.check_btn = QPushButton("Check inputs")
        self.classify_btn = QPushButton("Classify")
        f = self.classify_btn.font()
        f.setBold(True)
        self.classify_btn.setFont(f)
        self.check_panel = CheckPanel(self.check_btn, self.classify_btn, action="classifying")
        root.addWidget(self.check_panel)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        root.addWidget(Collapsible("Log", self.log))

        bar = QHBoxLayout()
        self.gear = QToolButton()
        self.gear.setText("⚙")
        self.gear.setPopupMode(_enum(QToolButton, "ToolButtonPopupMode", "InstantPopup"))
        menu = QMenu(self.gear)
        menu.addAction("About Hazard Lite…", self._about)
        self.gear.setMenu(menu)
        bar.addWidget(self.gear)
        bar.addStretch(1)
        bar.addWidget(self.check_btn)
        bar.addWidget(self.classify_btn)
        self.close_btn = QPushButton("Close")
        bar.addWidget(self.close_btn)
        root.addLayout(bar)

        for w in (self.depth, self.vel, self.pack, self.out):
            w.textChanged.connect(self._changed)
        self.check_btn.clicked.connect(lambda: self.check_panel.set_results(self._problems()))
        self.classify_btn.clicked.connect(self._run)
        self.close_btn.clicked.connect(self.reject)
        self._changed()
        fit_window(self, prefer=(720, 560), key="HazardLite/size")

    # -- helpers --------------------------------------------------------------
    def _default_pack(self):
        try:
            from qgis.core import QgsSettings
            return QgsSettings().value("ndsuite/pack_path", os.environ.get("NDSUITE_PACK", "")) or ""
        except Exception:                                                     # noqa
            return os.environ.get("NDSUITE_PACK", "")

    def _browse(self, line, kind):
        b = QPushButton("Browse…")
        if kind == "dir":
            b.clicked.connect(lambda: self._pick(line, QFileDialog.getExistingDirectory(self, "Folder")))
        else:
            b.clicked.connect(lambda: self._pick(line, QFileDialog.getOpenFileName(self, "Raster")[0]))
        return b

    def _pick(self, line, value):
        if value:
            line.setText(value)

    def _changed(self, *a):
        self.check_panel.clear()
        have_r = bool(self.depth.text() and self.vel.text())
        have_p = bool(self.pack.text())
        self.stage.set_states([self.stage.YES if have_r else self.stage.NO,
                               self.stage.YES if have_p else self.stage.NO,
                               self.stage.ACTIVE if have_r and have_p else self.stage.NO])

    def _problems(self):
        res = []
        for label, path in (("Max depth raster", self.depth.text()),
                            ("Max velocity raster", self.vel.text())):
            if not path:
                res.append((ERROR, "{} is required".format(label)))
            elif not os.path.exists(path):
                res.append((ERROR, "{} not found: {}".format(label, path)))
        pack = self.pack.text()
        if not pack:
            res.append((ERROR, "Offline pack folder is required"))
        else:
            missing = [v for v in core.PACK_FILES.values() if not os.path.exists(os.path.join(pack, v))]
            if missing:
                res.append((ERROR, "Pack folder is missing {} layer file(s), e.g. {}".format(
                    len(missing), missing[0])))
        if not self.out.text():
            res.append((ERROR, "Output folder is required"))
        if not res:
            res.append((OK, "Rasters, pack and output folder are set"))
        return res

    def _run(self):
        self.check_panel.set_results(self._problems())
        if self.check_panel.blocked:
            return
        self.log.clear()
        try:
            res = core.run(self.depth.text(), self.vel.text(), self.out.text(),
                           core.pack_paths(self.pack.text()), metres=self.metres.isChecked(),
                           judgment_high=self.judgment.isChecked(),
                           exclude_ag=self.exclude_ag.isChecked(), dam_name=self.dam.text(),
                           log=self.log.appendPlainText)
        except Exception as e:                                                # noqa
            self.log.appendPlainText(traceback.format_exc())
            QMessageBox.critical(self, "Hazard Lite", "The run failed: {}".format(e))
            return
        for cls, sec, why in res["reasons"]:
            self.log.appendPlainText("{} [{}] {}".format(cls, sec, why))
        if self.iface is not None:
            for lyr in ("structures", "road_spots", "rail_spots"):
                v = self.iface.addVectorLayer("{}|layername={}".format(res["gpkg"], lyr), lyr, "ogr")
        QMessageBox.information(self, "Hazard Lite", "ND hazard class: {}\n\nSummary written to {}".format(
            res["class"], os.path.join(self.out.text(), "hazard_summary.md")))

    def _about(self):
        QMessageBox.information(self, "DamSafety Hazard Lite {}".format(core.VERSION),
                                DISCLAIMER + "\n\nOne Processing tool (ndlite:classify) and this window.")
