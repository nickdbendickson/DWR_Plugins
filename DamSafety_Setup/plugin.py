# -*- coding: utf-8 -*-
"""DamSafety Setup: the suite data folder, in front of the other DamSafety plugins.

Start here\u2026 asks for the suite data folder (one page, scanned as you type) and then
continues into DamSafety Runner's dam wizard with the folder and the scan carried over: the
Runner's wizard is the suite's one per-dam wizard (docs/gui-grammar.md).
"""
import html
import os

from qgis.PyQt.QtCore import QTimer, Qt
from qgis.PyQt.QtWidgets import (
    QAction, QWizard, QWizardPage, QFormLayout, QDialog, QLineEdit, QPushButton, QLabel,
    QVBoxLayout, QHBoxLayout, QFileDialog,
)

MENU = "&Dam safety"

# 1.0.18: the status dock's area as the Qt enum (Qt6 / PyQt6 refuses the bare int 1 that
# addDockWidget was given; QGIS 4 fulltest 2026-10-30)
try:
    LEFT_DOCK = Qt.DockWidgetArea.LeftDockWidgetArea
except AttributeError:                 # pragma: no cover - older PyQt5
    LEFT_DOCK = Qt.LeftDockWidgetArea

# page ids (the order addPage is called in)
FOLDER_PAGE = 0
from .tokens import TOKENS  # noqa: E402
RED, GREEN, GREY = TOKENS["error"], TOKENS["success"], TOKENS["off"]


def _reason_label():
    """The red line under a control that says why Next is off ('' hides it)."""
    lbl = QLabel("")
    lbl.setWordWrap(True)
    lbl.setStyleSheet("color: {};".format(RED))
    lbl.setVisible(False)
    return lbl


def _set_reason(lbl, text):
    lbl.setText(text or "")
    lbl.setVisible(bool(text))


# Style A (2026-11-02): each layer of the folder scan with what it is for, so a missing optional
# layer reads as "less detail", not as an error. The notes are user-tested wording: keep them.
SCAN_LAYERS = (
    ("dwr", "DWR dams", "required \u2014 the dam list comes from here"),
    ("dam_lines", "Dam lines", "optional \u2014 locates crests precisely"),
    ("structures", "NSI structures", "optional \u2014 improves receptor mapping"),
    ("roads", "Roads", "optional \u2014 road-crossing impacts"),
    ("rail", "Rail", "optional \u2014 rail-crossing impacts"),
)
SCAN_ENOUGH = "\u2713 Enough to continue \u2014 missing layers only reduce detail."

# The steps of Start here\u2026 end to end: this page, then DamSafety Runner's dam wizard
# (DamSafety_Runner/wizard.py WIZARD_STEPS, the same names), then the run tool's dialog.
WIZARD_STEPS = ("Data folder", "Dam", "Terrain", "Scenario", "Models", "Review", "Run tool")


def steps_html(current, folder_done):
    """The Start here\u2026 steps on one wrapping line, the mark first (docs/gui-grammar.md):
    done = green check, this page = blue triangle in bold, later = grey circle. The Runner's
    wizard has the same function (DamSafety_Runner/wizard.py)."""
    parts = []
    for i, name in enumerate(WIZARD_STEPS):
        if i == current:
            parts.append("<b style='color:{}; white-space:nowrap'>\u25b6 {}</b>".format(TOKENS["link"], name))
        elif i < current or (i == 0 and folder_done):
            parts.append("<span style='color:{}; white-space:nowrap'>\u2713 {}</span>".format(GREEN, name))
        else:
            parts.append("<span style='color:{}; white-space:nowrap'>\u25cb {}</span>".format(GREY, name))
    return " \u2192 ".join(parts)


def scan_marks_html(found):
    """The folder scan as a per-layer status list: a green check for each layer found, a red
    cross and what the layer is for when it is missing, then a green line when the DWR dams
    (the one layer Next needs) are there."""
    found = found or {}
    rows = []
    for key, label, note in SCAN_LAYERS:
        if key in found:
            rows.append("<span style='color:{}'>\u2713 {}</span> \u2014 found".format(
                GREEN, html.escape(label)))
        else:
            rows.append("<span style='color:{}'>\u2715 {}</span> \u2014 {}".format(
                RED, html.escape(label), html.escape(note)))
    if "dwr" in found:
        rows.append("<br><span style='color:{}'>{}</span>".format(GREEN, html.escape(SCAN_ENOUGH)))
    return "<br>".join(rows)


def _log(text, level="Warning"):
    """Write to the QGIS message log (DamSafety Setup tab). Never raises."""
    try:
        from qgis.core import QgsMessageLog, Qgis
        QgsMessageLog.logMessage(text, "DamSafety Setup",
                                 getattr(getattr(Qgis, "MessageLevel", Qgis), level))
    except Exception:                                            # noqa - headless: stderr
        import sys
        sys.stderr.write("DamSafety Setup: {}\n".format(text))


def _msg(text, level="Warning"):
    """A message the user sees: the QGIS message bar, else the message log. Never raises."""
    _log(text, level)
    try:
        from qgis.utils import iface
        from qgis.core import Qgis
        iface.messageBar().pushMessage("Dam safety", text,
                                       level=getattr(getattr(Qgis, "MessageLevel", Qgis), level),
                                       duration=8)
    except Exception:                                            # noqa - no main window (tests)
        return


def _plugin_icon():
    """The plugin's icon.svg for its menu entry (every DamSafety plugin has one)."""
    import os
    from qgis.PyQt.QtGui import QIcon
    return QIcon(os.path.join(os.path.dirname(os.path.abspath(__file__)), "icon.svg"))

class SetupPlugin(object):
    def __init__(self, iface):
        self.iface = iface
        self.action = None

    def initGui(self):
        self.action = QAction(_plugin_icon(), "Start here\u2026", self.iface.mainWindow())
        self.action.setObjectName("DamSafetySetupStartAction")
        self.action.triggered.connect(self.run)
        self.iface.addPluginToMenu(MENU, self.action)
        # Suite-configuration surface. Without these the setup/status dialog (open_status)
        # and the always-on status dock (status_dock.SuiteStatusDock, via show_dock) were
        # dead code: reachable from no menu, so the suite's folders, models and pack
        # conversion could not be opened from QGIS.
        self.setup_action = QAction("Suite setup / status…", self.iface.mainWindow())
        self.setup_action.setObjectName("DamSafetySetupConfigAction")
        self.setup_action.triggered.connect(self.open_status)
        self.iface.addPluginToMenu(MENU, self.setup_action)
        self.dock_action = QAction("Suite status dock…", self.iface.mainWindow())
        self.dock_action.setObjectName("DamSafetySetupStatusDockAction")
        self.dock_action.triggered.connect(self.show_dock)
        self.iface.addPluginToMenu(MENU, self.dock_action)
        self.dock = None

    def unload(self):
        if self.action is not None:
            self.iface.removePluginMenu(MENU, self.action)
        if getattr(self, "setup_action", None) is not None:
            self.iface.removePluginMenu(MENU, self.setup_action)
        if getattr(self, "dock_action", None) is not None:
            self.iface.removePluginMenu(MENU, self.dock_action)
        if getattr(self, "dock", None) is not None:
            self.iface.removeDockWidget(self.dock)
            # removeDockWidget only detaches the dock from the main window; without releasing
            # it the hidden SuiteStatusDock stays parented to the window and leaks on every
            # 'Reload plugin'. Detach and schedule deletion, then drop our own reference.
            try:
                self.dock.setParent(None)
                self.dock.deleteLater()
            except (RuntimeError, AttributeError):
                pass
            self.dock = None

    def open_status(self):
        from .damsuite.suite_config import get, set, dams_folder
        dlg = QDialog(self.iface.mainWindow())
        dlg.setWindowTitle("Suite setup / status")
        lay = QFormLayout(dlg)
        data = QLineEdit(get("data_folder"))
        dams = QLineEdit(get("dams_folder") or dams_folder())
        flood = QLineEdit(get("lisflood_exe"))
        lay.addRow("Suite data folder", data)
        lay.addRow("Dams folder (results)", dams)
        lay.addRow("LISFLOOD-FP", flood)
        note = QLabel("Choose each folder once. Results never go in the suite data folder.")
        note.setWordWrap(True)
        lay.addRow(note)
        save = QPushButton("Save")
        def _save():
            set("data_folder", data.text().strip())
            set("dams_folder", dams.text().strip())
            ensure_dams_folder(dams.text().strip())
            set("lisflood_exe", flood.text().strip())
            dlg.accept()
        save.clicked.connect(_save)
        lay.addRow(save)
        dlg.exec() if hasattr(dlg, 'exec') else dlg.exec_()

    def show_dock(self):
        if self.dock is None:
            from .status_dock import SuiteStatusDock
            self.dock = SuiteStatusDock(self.iface)
            self.iface.addDockWidget(LEFT_DOCK, self.dock)
        self.dock.refresh()
        self.dock.show()

    def run(self):
        """Start here: the suite data folder, then the Runner's dam wizard with the scan."""
        runner = runner_wizard_module()
        wiz = SetupWizard(self.iface.mainWindow(), runner=runner)
        run = getattr(wiz, "exec_", None) or getattr(wiz, "exec")
        if not run() or runner is None:
            return
        try:
            runner.run_wizard(self.iface, found=dict(wiz.found), data_folder=wiz.data_folder())
        except TypeError:                # a Runner older than the hand-off: its own wizard
            runner.run_wizard(self.iface)


class DamSafetySetupPlugin(SetupPlugin):
    pass


RUNNER_PLUGINS = ("DamSafety_Runner", "nd_breach_runner")
NO_RUNNER = ("DamSafety Runner is not installed \u2014 it provides the modelling tools. "
             "Install it, then Start here\u2026 again.")
NEXT_LINE = "The Runner's dam wizard opens next; your folder choice carries over."


def runner_wizard_module():
    """The Runner's wizard module when DamSafety Runner is installed and loaded, else None."""
    try:
        import importlib
        from qgis.utils import plugins
    except Exception:                                            # noqa - outside QGIS: no Runner
        return None
    for name in RUNNER_PLUGINS:
        if plugins.get(name) is None:
            continue
        try:
            return importlib.import_module(name + ".wizard")
        except Exception as e:                                   # noqa - tell, then try the next
            _msg("{} is installed but its dam wizard did not load ({}). Reinstall it, then "
                 "Start here\u2026 again.".format(name, e))
            continue
    return None


class SetupWizard(QWizard):
    """One page, the suite data folder, then the Runner's dam wizard (docs/gui-grammar.md:
    one wizard per job). The scan and the folder carry over."""

    def __init__(self, parent=None, runner=None):
        super(SetupWizard, self).__init__(parent)
        self.setWindowTitle("DamSafety Setup \u2014 Suite data folder")
        self.found = {}
        self.runner = runner                 # the Runner's wizard module, None = not installed
        self.addPage(FolderPage(self))
        try:
            finish = getattr(QWizard, "FinishButton", None) or QWizard.WizardButton.FinishButton
            self.setButtonText(finish, "Continue\u2026")
        except Exception as e:                                   # noqa
            _log("Continue\u2026 label not set: {}".format(e))

    def data_folder(self):
        return self.page(FOLDER_PAGE).path.text().strip()


class FolderPage(QWizardPage):
    def __init__(self, wiz):
        super(FolderPage, self).__init__()
        self.wiz = wiz
        self.setTitle("Suite data folder")
        self.setSubTitle("The offline pack or the folder of layer files. Results never go here.")
        lay = QVBoxLayout()
        self.steps = QLabel(steps_html(0, False))  # where this page sits in Start here\u2026
        self.steps.setWordWrap(True)                # wraps in a narrow window, never widens it
        self.steps.setTextFormat(getattr(Qt, 'TextFormat', Qt).RichText)
        lay.addWidget(self.steps)
        row = QHBoxLayout()
        self.path = QLineEdit()
        browse = QPushButton("Browse\u2026")
        browse.clicked.connect(self._browse)
        self.path.editingFinished.connect(self._scan)
        self.path.textChanged.connect(self._edited)
        row.addWidget(self.path)
        row.addWidget(browse)
        lay.addLayout(row)
        self.reason = _reason_label()
        lay.addWidget(self.reason)
        self.note = QLabel("")
        self.note.setWordWrap(True)
        lay.addWidget(self.note)
        lay.addStretch(1)
        from .ui_widgets import InfoBanner
        self.next_banner = InfoBanner(NEXT_LINE)
        lay.addWidget(self.next_banner)
        self.runner_line = _reason_label()   # red, beside Continue: why it is off
        lay.addWidget(self.runner_line)
        self.setLayout(lay)
        self._scanned = None             # the folder the marks belong to
        self._timer = QTimer(self)       # scan shortly after typing stops
        self._timer.setSingleShot(True)
        self._timer.setInterval(600)
        self._timer.timeout.connect(self._scan)
        _set_reason(self.runner_line, "" if wiz.runner is not None else NO_RUNNER)
        self.next_banner.setVisible(wiz.runner is not None)
        try:
            from .damsuite.suite_config import get
            saved = get("data_folder") or ""
        except Exception as e:                                   # noqa
            _log("Last suite data folder not read: {}".format(e))
            saved = ""
        if saved and os.path.isdir(saved):
            self.path.setText(saved)     # the folder chosen last time, checked now
            self._scan()
        _set_reason(self.reason, self.problem())

    def _browse(self):
        folder = QFileDialog.getExistingDirectory(self, "Suite data folder", self.path.text())
        if not folder:
            return
        self.path.setText(folder)
        self._scan()

    def _edited(self, _text=""):
        """Typing makes the last scan stale: Next goes off until this folder is scanned."""
        self.wiz.found = {}
        self._scanned = None
        self._timer.start()
        _set_reason(self.reason, self.problem())
        self.completeChanged.emit()

    def _scan(self):
        self._timer.stop()
        folder = self.path.text().strip()
        if not folder or not os.path.isdir(folder):
            self.wiz.found = {}
            self._scanned = None
            self.note.setText("")
            _set_reason(self.reason, self.problem())
            self.completeChanged.emit()
            return
        from .suite_fill import scan
        from .damsuite.suite_config import set as save
        found, notes = scan(folder)
        self.wiz.found = found
        self._scanned = folder
        if found.get("dwr"):
            save("data_folder", folder)  # only a folder that holds the dams
        self.note.setTextFormat(getattr(Qt, 'TextFormat', Qt).RichText)
        self.note.setText(scan_marks_html(found))
        _set_reason(self.reason, self.problem())
        self.completeChanged.emit()

    def problem(self):
        folder = self.path.text().strip()
        if not folder:
            return "Pick the suite data folder (Browse)."
        if not os.path.isdir(folder):
            return "That folder was not found: {}".format(folder)
        if self._scanned != folder:
            return "Checking the folder\u2026 (press Enter to check now)"
        if not self.wiz.found.get("dwr"):
            return "No DWR dams in this folder: choose the suite data folder that holds them."
        return ""

    def isComplete(self):
        return not self.problem() and self.wiz.runner is not None

    def validatePage(self):
        if self._scanned != self.path.text().strip():
            self._scan()
        return self.isComplete()



def ensure_dams_folder(path):
    """Create the chosen Dams folder (only that folder; damsuite/layout.py). Never raises; a
    failure is logged and shown in the message bar (Style A: no silent failures)."""
    try:
        from .damsuite import layout as LY
        return LY.ensure_dams_folder(path) if path else []
    except Exception as e:
        _msg("The Dams folder was not created ({}). Pick a folder you can write to in "
             "Suite setup / status.".format(e))
        return []
