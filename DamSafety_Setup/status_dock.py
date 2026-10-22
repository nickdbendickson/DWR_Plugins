# -*- coding: utf-8 -*-
"""Always-available suite status dock. One place for folders, models, and pack conversion."""
import os
import subprocess

from qgis.PyQt.QtWidgets import (
    QDockWidget, QWidget, QVBoxLayout, QLabel, QPushButton, QHBoxLayout, QFileDialog,
)


class SuiteStatusDock(QDockWidget):
    def __init__(self, iface):
        super(SuiteStatusDock, self).__init__("DamSafety Setup \u2014 Suite status", iface.mainWindow())
        self.iface = iface
        self.setObjectName("DamSafetySuiteStatus")
        body = QWidget()
        self.lay = QVBoxLayout(body)
        self.rows = QLabel("")
        self.rows.setWordWrap(True)
        self.lay.addWidget(self.rows)
        row = QHBoxLayout()
        for label, fn, tip in (
            ("Change folders\u2026", self._change,
             "Pick the suite data folder, then the Dams folder. Results never go in the data folder."),
            ("Build LISFLOOD-FP\u2026", self._build_lisflood,
             "Build LISFLOOD-FP and remember the executable."),
            ("Create ANUGA env\u2026", self._build_anuga,
             "Create the optional ANUGA conda environment."),
            ("Convert old pack\u2026", self._convert,
             "Convert an older offline pack into the current layout."),
        ):
            b = QPushButton(label)
            b.setToolTip(tip)
            b.clicked.connect(fn)
            row.addWidget(b)
        self.lay.addLayout(row)
        self.setWidget(body)
        self.refresh()

    def refresh(self):
        from .suite_config import get, dams_folder, data_folder
        lines = [
            "Suite data folder: " + (data_folder() or "not set"),
            "Dams folder: " + (dams_folder() or "not set"),
            "LISFLOOD-FP: " + (get("lisflood_exe") or "not set"),
            "ANUGA python: " + (get("anuga_python") or "optional, not set"),
            "Settings: " + (get("settings_file") or "built-in starting values"),
        ]
        self.rows.setText("\n".join(lines))

    def _change(self):
        from .suite_config import get, set, dams_folder
        folder = QFileDialog.getExistingDirectory(self, "Suite data folder", get("data_folder") or "")
        if folder:
            set("data_folder", folder)
        dams = QFileDialog.getExistingDirectory(self, "Dams folder (results)", dams_folder())
        if dams:
            set("dams_folder", dams)
            from .plugin import ensure_dams_folder
            ensure_dams_folder(dams)
        self.refresh()

    def _build_lisflood(self):
        script = os.path.join(os.path.dirname(__file__), "scripts", "build_lisflood_linux.sh")
        self._run("Build LISFLOOD-FP", script, "Applies the arrival-depth patch (scripts/patch_lisflood.py) and builds the LISFLOOD-FP source in LISFLOOD_SRC (default ~/lisflood-fp), then set the executable in this dock.")

    def _build_anuga(self):
        script = os.path.join(os.path.dirname(__file__), "scripts", "create_anuga_env.sh")
        self._run("Create ANUGA environment", script, "Creates a conda env named anuga. Optional.")

    def _run(self, title, script, hint):
        from qgis.PyQt.QtWidgets import QMessageBox
        if not os.path.isfile(script):
            QMessageBox.information(self, title, hint + "\nScript not in this plugin: " + script)
            return
        try:
            subprocess.Popen(["bash", script])
            QMessageBox.information(self, title, "Started " + script + "\n" + hint)
        except Exception as e:
            QMessageBox.warning(self, title, str(e))

    def _convert(self):
        from .pack_convert import convert_pack
        from .suite_config import set
        src, _f = QFileDialog.getOpenFileName(self, "Old pack", "", "Pack (*.sqlite *.zip)")
        if not src:
            return
        dest = QFileDialog.getExistingDirectory(self, "Suite data folder to write")
        if not dest:
            return
        note = convert_pack(src, dest)
        set("data_folder", dest)
        self.refresh()
        from qgis.PyQt.QtWidgets import QMessageBox
        QMessageBox.information(self, "Convert old pack", note)
