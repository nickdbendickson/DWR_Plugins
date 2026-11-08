# -*- coding: utf-8 -*-
"""Always-available suite status dock. One place for folders, models, and pack conversion.

1.0.21 (2026-11-07, audit U6): "Build LISFLOOD-FP…" and "Create ANUGA env…" run their script
through QProcess with stdout / stderr streamed into a ▸ Log fold-out in the dock (before, a bare
Popen and a "Started…" message box: no result ever reached the user). When the LISFLOOD-FP build
ends with the executable in place, the dock writes it to the suite store's `lisflood_exe`
(QgsSettings `ndsuite/lisflood_exe`), which is the key the Runner's find_lisflood reads first
(the Oct-15 audit's M15 mismatch was the Runner never reading that key; the Runner has read it
since, so Setup keeps writing the suite key and NOT the provider's own ProcessingConfig key).
The build scripts are bash, so the two buttons are off on Windows with a tooltip that says why
(docs/gui-grammar.md: a disabled control says why)."""
import os
import sys

from qgis.PyQt.QtCore import QProcess
from qgis.PyQt.QtWidgets import (
    QDockWidget, QWidget, QVBoxLayout, QLabel, QPushButton, QGridLayout, QFileDialog,
    QPlainTextEdit,
)

from .ui_widgets import Collapsible

# Where build_lisflood_linux.sh puts the executable when it is given no install folder
# ($HOME/lisflood-fp/lisflood); the dock passes the folder explicitly so it knows where to look.
LISFLOOD_OUT = os.path.join(os.path.expanduser("~"), "lisflood-fp")
WINDOWS_WHY = ("Not on Windows: the build script needs bash. Use the LISFLOOD-FP that the "
               "DamSafety Runner release zip carries (bin/windows-x64/lisflood.exe), or build "
               "it with MinGW-w64 as docs/CI.md describes.")
ANUGA_WINDOWS_WHY = ("Not on Windows: the script needs bash and conda. Create the env in a conda "
                     "prompt: conda create -n anuga python=3.12 && conda run -n anuga pip install "
                     "anuga, then set the interpreter in Processing > Options > Providers > "
                     "DamSafety Runner.")


def _is_windows():
    return os.name == "nt" or sys.platform.startswith("win")


def _process_state_text(exit_code, exit_status_normal):
    """One line for the log when a build script ends."""
    if not exit_status_normal:
        return "The script crashed or was killed."
    return "Finished (exit code {}).".format(exit_code)


class SuiteStatusDock(QDockWidget):
    def __init__(self, iface):
        super(SuiteStatusDock, self).__init__("DamSafety Setup — Suite status", iface.mainWindow())
        self.iface = iface
        self.setObjectName("DamSafetySuiteStatus")
        self.proc = None               # the running QProcess, one at a time
        self._job = None               # ("lisflood" | "anuga", expected output path or "")
        body = QWidget()
        self.lay = QVBoxLayout(body)
        self.rows = QLabel("")
        self.rows.setWordWrap(True)
        self.lay.addWidget(self.rows)
        # 1.0.20 (GUI check 2026-11-04): four buttons in one row needed 500 px, wider than a
        # 420 px dock; they now sit two to a row
        row = QGridLayout()
        self.buttons = {}
        for i, (key, label, fn, tip) in enumerate((
            ("folders", "Change folders…", self._change,
             "Pick the suite data folder, then the Dams folder. Results never go in the data folder."),
            ("lisflood", "Build LISFLOOD-FP…", self._build_lisflood,
             "Build LISFLOOD-FP 8.x (source in LISFLOOD_SRC or ~/lisflood-fp) with the arrival-depth "
             "patch and remember the executable. Output streams into the Log below."),
            ("anuga", "Create ANUGA env…", self._build_anuga,
             "Create the optional ANUGA conda environment. Output streams into the Log below."),
            ("convert", "Convert old pack…", self._convert,
             "Convert an older offline pack into the current layout."),
        )):
            b = QPushButton(label)
            b.setToolTip(tip)
            b.clicked.connect(fn)
            row.addWidget(b, i // 2, i % 2)
            self.buttons[key] = b
        if _is_windows():
            # docs/gui-grammar.md: a disabled control says why (in its tooltip)
            self.buttons["lisflood"].setEnabled(False)
            self.buttons["lisflood"].setToolTip(WINDOWS_WHY)
            self.buttons["anuga"].setEnabled(False)
            self.buttons["anuga"].setToolTip(ANUGA_WINDOWS_WHY)
        self.lay.addLayout(row)
        # ▸ Log fold-out (docs/gui-grammar.md): the build scripts' output, line by line
        self.log_edit = QPlainTextEdit()
        self.log_edit.setReadOnly(True)
        self.log_edit.setMaximumBlockCount(5000)
        self.log_edit.setMaximumHeight(160)
        self.log_section = Collapsible("Log", self.log_edit)
        self.lay.addWidget(self.log_section)
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

    # -- the two build scripts -------------------------------------------------------------
    def _build_lisflood(self):
        script = os.path.join(os.path.dirname(__file__), "scripts", "build_lisflood_linux.sh")
        src = os.environ.get("LISFLOOD_SRC") or os.path.join(os.path.expanduser("~"), "lisflood-fp")
        self._run("lisflood", script, [src, LISFLOOD_OUT],
                  "Applies the arrival-depth patch (scripts/patch_lisflood.py) and builds the "
                  "LISFLOOD-FP source in {} (set LISFLOOD_SRC to use another folder) into {}. "
                  "On success the executable is remembered for the Runner.".format(src, LISFLOOD_OUT),
                  expect=os.path.join(LISFLOOD_OUT, "lisflood"))

    def _build_anuga(self):
        script = os.path.join(os.path.dirname(__file__), "scripts", "create_anuga_env.sh")
        self._run("anuga", script, [], "Creates a conda env named anuga. Optional.")

    def _log(self, text):
        self.log_edit.appendPlainText(text.rstrip("\n"))
        self.log_edit.verticalScrollBar().setValue(self.log_edit.verticalScrollBar().maximum())

    def _run(self, job, script, args, hint, expect=""):
        if not os.path.isfile(script):
            self._log("Script not in this plugin: " + script + "\n" + hint)
            self.log_section.set_expanded(True)
            return
        if self.proc is not None:
            self._log("A build is already running; wait for it to finish.")
            return
        self.log_section.set_expanded(True)
        self._log("$ bash {} {}\n{}".format(script, " ".join(args), hint))
        proc = QProcess(self)
        proc.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels
                                   if hasattr(QProcess, "ProcessChannelMode")
                                   else QProcess.MergedChannels)  # qt6-ok
        proc.readyReadStandardOutput.connect(self._read_output)
        proc.finished.connect(self._finished)
        proc.errorOccurred.connect(self._proc_error)
        self.proc, self._job = proc, (job, expect)
        for key in ("lisflood", "anuga"):
            self.buttons[key].setEnabled(False)
            self.buttons[key].setToolTip("Running {}; the output is in the Log below."
                                         .format(os.path.basename(script)))
        proc.start("bash", [script] + list(args))

    def _read_output(self):
        if self.proc is None:
            return
        data = bytes(self.proc.readAllStandardOutput())
        text = data.decode("utf-8", errors="replace")
        if text.strip():
            self._log(text)

    def _proc_error(self, _err):
        if self.proc is not None:
            self._log("Could not run bash: " + str(self.proc.errorString()))

    def _finished(self, exit_code, exit_status):
        self._read_output()
        job, expect = self._job or ("", "")
        normal = int(exit_status) == 0            # QProcess.NormalExit
        self._log(_process_state_text(int(exit_code), normal))
        if job == "lisflood" and normal and int(exit_code) == 0 and expect and os.path.isfile(expect):
            self._remember_lisflood(expect)
        elif job == "lisflood":
            self._log("LISFLOOD-FP was not set: no executable at {} (see the output above)."
                      .format(expect))
        self.proc, self._job = None, None
        self._reset_buttons()
        self.refresh()

    def _remember_lisflood(self, exe):
        # The suite store (QgsSettings ndsuite/lisflood_exe): the key the Runner's
        # algorithms.find_lisflood reads before its own provider setting (M15).
        from .suite_config import set
        set("lisflood_exe", exe)
        self._log("LISFLOOD-FP executable set for the Runner: " + exe)

    def _reset_buttons(self):
        for key, tip in (("lisflood", "Build LISFLOOD-FP 8.x (source in LISFLOOD_SRC or "
                                      "~/lisflood-fp) with the arrival-depth patch and remember "
                                      "the executable. Output streams into the Log below."),
                         ("anuga", "Create the optional ANUGA conda environment. Output "
                                   "streams into the Log below.")):
            self.buttons[key].setEnabled(not _is_windows())
            self.buttons[key].setToolTip(tip if not _is_windows() else
                                         (WINDOWS_WHY if key == "lisflood" else ANUGA_WINDOWS_WHY))

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
