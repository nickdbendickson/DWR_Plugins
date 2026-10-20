# -*- coding: utf-8 -*-
"""DamSafety Setup: four pages in front of the other DamSafety plugins.

A chosen value is never locked. The last page opens DamSafety Runner with the
answers filled in, and Advanced holds the grid, engine, scenario, run length
and EAP switch.
"""
import html
import os

from qgis.PyQt.QtCore import QTimer, Qt
from qgis.PyQt.QtWidgets import (
    QAction, QWizard, QWizardPage, QFormLayout, QDialog, QLineEdit, QPushButton, QLabel,
    QVBoxLayout, QHBoxLayout, QFileDialog, QComboBox, QDoubleSpinBox, QCheckBox,
    QGroupBox, QWidget,
)

MENU = "&Dam safety"

ND_COUNTIES = (
    "Adams", "Barnes", "Benson", "Billings", "Bottineau", "Bowman", "Burke",
    "Burleigh", "Cass", "Cavalier", "Dickey", "Divide", "Dunn", "Eddy",
    "Emmons", "Foster", "Golden Valley", "Grand Forks", "Grant", "Griggs",
    "Hettinger", "Kidder", "LaMoure", "Logan", "McHenry", "McIntosh",
    "McKenzie", "McLean", "Mercer", "Morton", "Mountrail", "Nelson", "Oliver",
    "Pembina", "Pierce", "Ramsey", "Ransom", "Renville", "Richland", "Rolette",
    "Sargent", "Sheridan", "Sioux", "Slope", "Stark", "Steele", "Stutsman",
    "Towner", "Traill", "Walsh", "Ward", "Wells", "Williams",
)
ALG = "ndbreach:rundam"
from .runner_params import DEFAULT_MAX_EXTENT_MI  # noqa: E402  (no Qt in it)
from . import nid_fill  # noqa: E402  (no Qt in it)

# page ids (the order addPage is called in)
FOLDER_PAGE, DAM_PAGE, DEM_PAGE, RUN_PAGE = range(4)
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


def _with_label(widget, label):
    """widget with a label to its right, as one form field."""
    box = QWidget()
    row = QHBoxLayout(box)
    row.setContentsMargins(0, 0, 0, 0)
    row.addWidget(widget)
    row.addWidget(label, 1)
    return box


def source_text(source):
    """'from NID ND00182' for the label next to a value ('' for none)."""
    if not source:
        return ""
    if source == "typed in":
        return "typed in"
    if source.startswith("NID "):
        return "from NID {}".format(source.split()[1])
    if source.startswith("estimated from DEM"):
        return "estimated from DEM (screening only)"
    if source.startswith("DWR normal pool"):
        return "from DWR (normal pool volume)"
    return "from {}".format(source.split(" (")[0])


def values_text(dam):
    """'height 18 ft (from DWR) | storage 452 ac-ft (from NID ND00182)'."""
    out = []
    for key, label, unit in (("height", "height", "ft"), ("storage", "storage", "ac-ft")):
        val = nid_fill.number((dam or {}).get(key))
        src = source_text((dam or {}).get(key + "_source"))
        out.append("{} {}".format(label, "{:g} {}".format(val, unit) if val else "blank") +
                   (" ({})".format(src) if val and src else ""))
    return " | ".join(out)


def run_task(work, done, progress):
    """Run work(progress) in a QgsTask (not on the window's thread); done(result, '') or
    done(None, error) back on the window's thread. progress(pct) follows the task."""
    from qgis.core import QgsApplication, QgsTask

    def fn(task):
        return work(lambda pct: task.setProgress(float(pct)))

    def finished(exc, result=None):
        _TASKS.discard(task)
        if exc is not None:
            done(None, str(exc) or exc.__class__.__name__)
        elif result is None:
            done(None, "cancelled")
        else:
            done(result, "")
    task = QgsTask.fromFunction("DamSafety Setup: estimate the dam from the DEM", fn,
                                on_finished=finished)
    task.progressChanged.connect(progress)
    _TASKS.add(task)
    QgsApplication.taskManager().addTask(task)
    return task


_TASKS = set()


def nid_get(url, done):
    """GET url through QGIS's network manager (its proxy and SSL settings), without
    blocking the window; done(text, '') or done('', error). Times out after
    nid_fill.TIMEOUT_S seconds."""
    from qgis.core import QgsNetworkAccessManager
    from qgis.PyQt.QtCore import QUrl
    from qgis.PyQt.QtNetwork import QNetworkRequest, QNetworkReply
    req = QNetworkRequest(QUrl(url))
    req.setRawHeader(b"Accept", b"application/json")
    if hasattr(req, "setTransferTimeout"):
        req.setTransferTimeout(int(nid_fill.TIMEOUT_S * 1000))
    reply = QgsNetworkAccessManager.instance().get(req)
    _REPLIES.add(reply)

    def finished():
        _REPLIES.discard(reply)
        status = reply.attribute(QNetworkRequest.HttpStatusCodeAttribute)
        body = bytes(reply.readAll()).decode("utf-8", "replace")
        if reply.error() == QNetworkReply.NoError:
            done(body, "")
        elif status == 404:
            done("{}", "")                 # not in the NID: an answer, not a failure
        else:
            done("", reply.errorString() or "HTTP {}".format(status))
        reply.deleteLater()
    reply.finished.connect(finished)
    return reply


_REPLIES = set()


def scan_marks_html(found):
    """The folder scan as coloured marks: a green check for each layer found, a circle
    for each missing one (red for the DWR dams, which Next needs; grey otherwise)."""
    rows = []
    for key, label in (("dwr", "DWR dams"), ("dam_lines", "Dam lines"),
                       ("structures", "NSI structures"), ("roads", "Roads"), ("rail", "Rail")):
        if key in (found or {}):
            mark, color = "\u2713", GREEN
        else:
            mark, color = "\u25cb", RED if key == "dwr" else GREY
        rows.append("<span style='color:{}'>{}</span> {}{}".format(
            color, mark, html.escape(label), "" if key in (found or {}) else " (missing)"))
    return "<br>".join(rows)


def dam_problem(row, height, storage):
    """Why the picked dam cannot go to the Runner, or '' (the Runner needs a height and a
    storage above 0: the wizard used to hand over 0 ft / 0 ac-ft)."""
    if not row or row.get("error"):
        return "Pick a dam from the list (choose a county or type a name)."
    if not height or height <= 0:
        return "Height is 0 ft: enter the dam height."
    if not storage or storage <= 0:
        return "Max storage is 0 ac-ft: enter the storage."
    return ""


def dem_target(folder, dam_id, point, res_index):
    """Where 'Download DEM' writes for this dam: (folder, file name). The name follows
    runner_params.downloaded_dem: <DAM_ID>_10m_5070.tif, else dam_<lon>_<lat>_..."""
    from .runner_params import dem_stem, DEM_SUFFIXES
    suffix = DEM_SUFFIXES[1] if res_index == 1 else DEM_SUFFIXES[0]
    stem = dem_stem(dam_id)
    if not stem:
        raw = (point or "").split("[")[0].strip()
        if "," in raw:
            stem = "dam_{}_{}".format(*[p.strip() for p in raw.split(",")[:2]])
    return folder, (stem + suffix) if stem else "(pick a dam first)"



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
            self.iface.addDockWidget(1, self.dock)
        self.dock.refresh()
        self.dock.show()

    def run(self):
        # One per-dam wizard: the Runner's. This plugin keeps suite setup.
        try:
            import importlib
            from qgis.utils import plugins
            for name in ("DamSafety_Runner", "nd_breach_runner"):
                if plugins.get(name) is None:
                    continue
                importlib.import_module(name + ".wizard").run_wizard(self.iface)
                return
        except Exception:
            pass
        wiz = SetupWizard(self.iface.mainWindow())
        run = getattr(wiz, "exec_", None) or getattr(wiz, "exec")
        if not run():
            return
        try:
            import processing
            params = wiz.params()
            try:
                from qgis.core import QgsMessageLog, Qgis
                QgsMessageLog.logMessage("{}: height from {}, storage from {}".format(
                    params.get("DAM_ID") or "dam", params.get("HEIGHT_SOURCE") or "?",
                    params.get("STORAGE_SOURCE") or "?"), "DamSafety Setup", Qgis.Info)
            except Exception:
                pass
            processing.execAlgorithmDialog(ALG, params)
        except Exception as e:
            from qgis.PyQt.QtWidgets import QMessageBox
            QMessageBox.warning(self.iface.mainWindow(), "DamSafety Setup",
                                "The runner dialog did not open ({}). Install DamSafety Runner "
                                "and run again.\n\n{}".format(ALG, e))


class DamSafetySetupPlugin(SetupPlugin):
    pass


class SetupWizard(QWizard):
    def __init__(self, parent=None):
        super(SetupWizard, self).__init__(parent)
        self.setWindowTitle("DamSafety Setup \u2014 Set up one dam")
        self.found = {}
        self.addPage(FolderPage(self))
        self.addPage(DamPage(self))
        self.addPage(DemPage(self))
        self.addPage(RunPage(self))

    def selected_dam(self):
        """The dam picked on the dam page with the page's edits applied ({} when none):
        the one place the DEM and Run pages read it from."""
        return self.page(DAM_PAGE).selected() or {}

    def params(self):
        from .runner_params import build_params
        return build_params(self.answers())

    def answers(self):
        return getattr(self, "_answers", {})


class FolderPage(QWizardPage):
    def __init__(self, wiz):
        super(FolderPage, self).__init__()
        self.wiz = wiz
        self.setTitle("Suite data folder")
        lay = QVBoxLayout()
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
        self.note = QLabel("Pick the suite data folder. Results do not go here.")
        self.note.setWordWrap(True)
        lay.addWidget(self.note)
        self.setLayout(lay)
        self._scanned = None             # the folder the marks belong to
        self._timer = QTimer(self)       # scan shortly after typing stops
        self._timer.setSingleShot(True)
        self._timer.setInterval(600)
        self._timer.timeout.connect(self._scan)
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
        return not self.problem()

    def validatePage(self):
        if self._scanned != self.path.text().strip():
            self._scan()
        return self.isComplete()


class DamPage(QWizardPage):
    def __init__(self, wiz):
        super(DamPage, self).__init__()
        self.wiz = wiz
        self.setTitle("Pick the dam")
        lay = QVBoxLayout()
        self.county = QComboBox()
        self.county.addItem("All counties", "")
        for name in ND_COUNTIES:
            self.county.addItem(name, name)
        self.county.currentIndexChanged.connect(self._find)
        self.county.setToolTip("North Dakota county. Choosing one lists that county's dams.")
        self.search = QLineEdit()
        self.search.setPlaceholderText("Type a dam name, then press Enter")
        self.search.returnPressed.connect(self._find)
        self.combo = QComboBox()
        self.combo.currentIndexChanged.connect(self._show)
        self.count = QLabel("")
        self.count.setStyleSheet("font-weight: bold;")
        lay.addWidget(QLabel("County"))
        lay.addWidget(self.county)
        lay.addWidget(QLabel("Dam name"))
        lay.addWidget(self.search)
        lay.addWidget(self.combo)
        lay.addWidget(self.count)
        # records that are not a dam to model (status Breached / Removed / Not Built /
        # Submerged, remarks 'VOID ...', 'No Dam Visible') stay off the list unless ticked
        self.show_all = QCheckBox("Show all records")
        self.show_all.setToolTip("Also list the DWR records that are not a dam to model: "
                                 "status Removed, Breached, Not Built or Submerged, or remarks "
                                 "such as 'VOID' or 'No Dam Visible'. Each shows why.")
        self.show_all.toggled.connect(self._find)
        lay.addWidget(self.show_all)
        self.note = QLabel("Pick a county to list its dams, or type a name.")
        self.note.setWordWrap(True)
        lay.addWidget(self.note)
        form = QFormLayout()
        self.height = QDoubleSpinBox()
        self.height.setRange(0, 1000)
        self.height.setDecimals(1)
        self.height.setSuffix(" ft")
        self.storage = QDoubleSpinBox()
        self.storage.setRange(0, 50000000)    # Lake Sakakawea is 26 million ac-ft
        self.storage.setDecimals(1)
        self.storage.setSuffix(" ac-ft")
        self.area = QDoubleSpinBox()
        self.area.setRange(0, 500000)
        self.area.setDecimals(1)
        self.area.setSuffix(" ac")
        self.crest = QDoubleSpinBox()
        self.crest.setRange(0, 20000)
        self.crest.setDecimals(1)
        self.crest.setSuffix(" ft")
        self.nid = QLineEdit()
        self.point = QLineEdit()
        self.point.setPlaceholderText("longitude, latitude")
        # where each number came from ('from DWR', 'from NID ND00182', 'typed in')
        self.height_src = QLabel("")
        self.storage_src = QLabel("")
        for lbl in (self.height_src, self.storage_src):
            lbl.setStyleSheet("color: {};".format(GREY))
        form.addRow("Height", _with_label(self.height, self.height_src))
        form.addRow("Max storage", _with_label(self.storage, self.storage_src))
        form.addRow("Surface area at top", self.area)
        form.addRow("Crest elevation", self.crest)
        form.addRow("NID", self.nid)
        form.addRow("Dam point", self.point)
        lay.addLayout(form)
        # The NID web service is asked only when this button is clicked (no silent
        # network calls); the NID layer in the suite data folder fills without it.
        self.nid_button = QPushButton("Fill from NID (online)")
        self.nid_button.setToolTip(
            "Ask the National Inventory of Dams web service ({}) for the height and storage "
            "this dam's record is missing. Only blanks are filled; DWR values and typed "
            "values are kept.".format(nid_fill.NID_API.split("/api")[0]))
        self.nid_button.clicked.connect(self._fill_online)
        self.nid_status = QLabel("")
        self.nid_status.setWordWrap(True)
        nrow = QHBoxLayout()
        nrow.addWidget(self.nid_button)
        nrow.addWidget(self.nid_status, 1)
        lay.addLayout(nrow)
        # Estimate from the DEM: screening values for a record that has no height / storage
        self.est_button = QPushButton("Estimate from DEM")
        self.est_button.setToolTip(
            "Screening estimate from the DEM: height = crest along the dam line minus the "
            "lowest ground just downstream; storage = DEM volume below the crest in the "
            "basin upstream. Fills only blank values; a typed value always wins.")
        self.est_button.clicked.connect(self._estimate)
        self.est_status = QLabel("")
        self.est_status.setWordWrap(True)
        erow = QHBoxLayout()
        erow.addWidget(self.est_button)
        erow.addWidget(self.est_status, 1)
        lay.addLayout(erow)
        self.reason = _reason_label()
        lay.addWidget(self.reason)
        self.setLayout(lay)
        self.rows = []
        self.nid_filled = 0
        self.fetch = nid_get             # (url, done(text, error)); tests replace it
        self.run_task = run_task         # (fn(progress), done(result, error), progress(pct))
        self._busy = False
        self._est_busy = False
        for spin in (self.height, self.storage):
            spin.valueChanged.connect(self._changed)

    def initializePage(self):
        self.combo.clear()
        self.rows = []
        self.note.setText("Pick a county to list its dams, or type a name.")
        self._find()

    def _find(self, *_a):
        county = self.county.currentData() or ""
        found = self.wiz.found
        hidden = []
        self.rows = _dams(found.get("dwr"), self.search.text().strip(), county,
                          lines_uri=found.get("dam_lines"),
                          hidden=None if self.show_all.isChecked() else hidden)
        self.hidden = hidden
        self.nid_filled = nid_fill.fill_rows(self.rows, found.get("nid"))
        self.combo.blockSignals(True)
        self.combo.clear()
        for row in self.rows:
            text = "{} ({})".format(row["name"], row.get("county") or county)
            if row.get("hidden"):
                text += " \u2013 not a dam? {}".format(row["hidden"].split(":")[0])
            self.combo.addItem(text, row)
        self.combo.blockSignals(False)
        text = match_count_text(self.rows, county, self.search.text().strip())
        if hidden:
            text += "; {} record{} hidden (not a dam: void, removed, breached\u2026)".format(
                len(hidden), "" if len(hidden) == 1 else "s")
        self.count.setText(text)
        self._show()

    def _show(self, *_a):
        row = self.combo.currentData() or {}
        self.height.setValue(float(row.get("height") or 0))
        self.storage.setValue(float(row.get("storage") or 0))
        self.area.setValue(float(row.get("area") or 0))
        self.crest.setValue(float(row.get("crest") or 0))
        self.nid.setText(str(row.get("nid") or ""))
        self.point.setText(str(row.get("point") or ""))
        self.note.setText("{} | county {} | crest line {}{}".format(
            row.get("name") or "No dam", row.get("county") or "blank",
            "found" if row.get("from_line") or self.wiz.found.get("dam_lines") else "not found",
            " | point from the dam line" if row.get("from_line") else "") +
            ("\nNot a dam to model? {}".format(row["hidden"]) if row.get("hidden") else ""))
        self.nid_status.setText("")
        self.est_status.setText("")
        self._changed()

    def _changed(self, *_a):
        for key, lbl in (("height", self.height_src), ("storage", self.storage_src)):
            src = self.source(key)
            lbl.setText(source_text(src))
            lbl.setToolTip(src)            # the NID field and how the dam was matched
        row = self.combo.currentData() or {}
        blank = not self.height.value() or not self.storage.value()
        self.nid_button.setEnabled(bool(row) and not row.get("error") and blank and
                                   not self._busy and bool(nid_fill.online_urls(row)))
        dem = self.dem_for_estimate()
        self.est_button.setText("Estimate from DEM" if dem else "Choose DEM and estimate\u2026")
        self.est_button.setEnabled(bool(row) and not row.get("error") and blank and
                                   not self._est_busy and bool(row.get("point")))
        _set_reason(self.reason, self.problem())
        self.completeChanged.emit()

    def dem_for_estimate(self):
        """The DEM to estimate from: the one on the DEM page when it is a readable file,
        else one already downloaded for this dam in the Dams folder; '' when none."""
        from .runner_params import dem_problem, downloaded_dem
        page = self.wiz.page(DEM_PAGE)
        text = page.dem.text().strip()
        if text and not dem_problem(text):
            return text
        row = self.combo.currentData() or {}
        if not row or row.get("error"):
            return ""
        point = (row.get("point") or "").split("[")[0].strip()
        try:
            return downloaded_dem(page._dem_folder(), row.get("id"), point)
        except Exception:
            return ""

    def _estimate(self, *_a):
        row = self.combo.currentData()
        index = self.combo.currentIndex()
        if not row or row.get("error") or self._est_busy:
            return
        dem = self.dem_for_estimate()
        if not dem:
            path, _f = QFileDialog.getOpenFileName(self, "DEM for the estimate", "",
                                                   "Raster (*.tif *.tiff *.vrt *.img)")
            if not path:
                return
            self.wiz.page(DEM_PAGE).dem.setText(path)    # the DEM page starts with it
            dem = path
        page = self.wiz.page(DEM_PAGE)
        zchoice = page.zunit.currentData() if getattr(page, "visited", False) else "meters"
        lines_uri = self.wiz.found.get("dam_lines")
        sid = row.get("structure_id")
        point = nid_fill.parse_point(row.get("point"))
        self._est_busy = True
        self.est_button.setEnabled(False)
        self.est_status.setText("Estimating from {}\u2026 0 %".format(os.path.basename(dem)))

        def work(progress):
            from . import dem_estimate as DE
            line = DE.dam_line(lines_uri, sid)
            return DE.estimate(dem, point, line, zchoice,
                               progress=lambda pct, text="": progress(pct))

        def show_progress(pct):
            if self._est_busy:
                self.est_status.setText("Estimating from {}\u2026 {:.0f} %".format(
                    os.path.basename(dem), pct))

        def done(res, error):
            from . import dem_estimate as DE
            self._est_busy = False
            now = self.combo.currentData() or {}
            if self.combo.currentIndex() != index or now.get("id") != row.get("id"):
                self._changed()
                return
            if error:
                self.est_status.setText("No estimate: {}".format(error))
                self._changed()
                return
            filled = []
            for key, spin, val, minimum in (
                    ("height", self.height, res.get("height_ft"), 0.5),
                    ("storage", self.storage, res.get("storage_acft"), 0.1)):
                if val is None or val < minimum or spin.value():
                    continue                       # typed / record values are kept
                row[key] = round(float(val), 1)
                row[key + "_source"] = DE.source(key, res)
                filled.append(key)
            if filled:
                self.combo.setItemData(index, row)
                if 0 <= index < len(self.rows):
                    self.rows[index] = row
                for key in filled:
                    (self.height if key == "height" else self.storage).setValue(row[key])
            msg = ("Estimated {} from the DEM (screening only)".format(" and ".join(filled))
                   if filled else "The DEM gave no usable value")
            self.est_status.setText("{}. {}".format(msg, " ".join(res.get("notes", [])[1:])))
            self.est_status.setToolTip(" ".join(res.get("notes", [])))
            self._changed()
        self.run_task(work, done, show_progress)

    def source(self, key):
        """Where the shown height / storage comes from: the record's source while the box
        still holds the record's value, 'typed in' once it was changed, '' when 0."""
        spin = self.height if key == "height" else self.storage
        val = spin.value()
        if not val:
            return ""
        row = self.combo.currentData() or {}
        rec = nid_fill.number(row.get(key))
        if rec is not None and abs(rec - val) < 0.05:
            return row.get(key + "_source") or "DWR"
        return "typed in"

    def _fill_online(self, *_a):
        row = self.combo.currentData()       # a copy: written back with setItemData
        index = self.combo.currentIndex()
        if not row or row.get("error") or self._busy:
            return
        work = dict(row)
        work["height"] = self.height.value() or None      # a typed value is kept
        work["storage"] = self.storage.value() or None
        urls = nid_fill.online_urls(work)
        if not urls:
            self.nid_status.setText("No NID id or dam point to look up.")
            return
        self._busy = True
        self.nid_button.setEnabled(False)
        self.nid_status.setText("Asking the NID\u2026")
        answers, errors = [], []

        def step(i):
            if i >= len(urls):
                return finish()
            kind, url = urls[i]

            def done(text, error):
                if error:
                    errors.append(error)
                else:
                    answers.append((kind, text))
                    probe = dict(work)
                    if nid_fill.fill_online(probe, [(kind, text)])[0]:
                        return finish()           # filled: no second request
                step(i + 1)
            self.fetch(url, done)

        def finish():
            self._busy = False
            now = self.combo.currentData() or {}
            if self.combo.currentIndex() != index or now.get("id") != row.get("id"):
                self._changed()                           # the engineer picked another dam
                return
            filled, msg = nid_fill.fill_online(work, answers)
            if not answers and errors:
                msg = "The NID did not answer ({}): type the values in.".format(errors[0])
            for key in filled:
                row[key] = work[key]
                row[key + "_source"] = work[key + "_source"]
            if filled:
                if work.get("nid") and not row.get("nid"):
                    row["nid"] = work["nid"]
                self.combo.setItemData(index, row)
                if 0 <= index < len(self.rows):
                    self.rows[index] = row
                for key in filled:
                    (self.height if key == "height" else self.storage).setValue(float(row[key]))
                if row.get("nid") and not self.nid.text().strip():
                    self.nid.setText(row["nid"])
            self.nid_status.setText(msg)
            self._changed()
        step(0)

    def problem(self):
        return dam_problem(self.combo.currentData(), self.height.value(), self.storage.value())

    def isComplete(self):
        return not self.problem()

    def selected(self):
        """The picked dam with the edits of this page, or None."""
        row = self.combo.currentData()
        if not row or row.get("error"):
            return None
        return self._apply_edits()

    def _apply_edits(self):
        row = self.combo.currentData()
        if not row:
            return None
        for key in ("height", "storage"):
            row[key + "_source"] = self.source(key) or row.get(key + "_source") or ""
        row["height"] = self.height.value() or row.get("height")
        row["storage"] = self.storage.value() or row.get("storage")
        row["area"] = self.area.value() or row.get("area")
        row["crest"] = self.crest.value() or row.get("crest")
        row["nid"] = self.nid.text().strip() or row.get("nid")
        row["point"] = self.point.text().strip() or row.get("point")
        return row

    def validatePage(self):
        return self.isComplete() and self._apply_edits() is not None


class DemPage(QWizardPage):
    def __init__(self, wiz):
        super(DemPage, self).__init__()
        self.wiz = wiz
        self.setTitle("DEM, crest and inflow")
        form = QFormLayout()
        row = QHBoxLayout()
        self.dem = QLineEdit()
        browse = QPushButton("Browse\u2026")
        browse.clicked.connect(self._browse)
        row.addWidget(self.dem)
        row.addWidget(browse)
        wrap = QWidget()
        wrap.setLayout(row)
        self.top = QDoubleSpinBox()
        self.top.setRange(0, 20000)
        self.top.setDecimals(1)
        self.drop = QDoubleSpinBox()
        self.drop.setRange(0, 500)
        self.drop.setDecimals(1)
        self.zunit = QComboBox()
        self.zunit.addItem("Feet (QL2 ASCII)", "feet")
        self.zunit.addItem("Metres (USGS IMG)", "meters")
        form.addRow("DEM", wrap)
        self.res = QComboBox()
        self.res.addItem("10 m", 0)
        self.res.addItem("1 m", 1)
        download = QPushButton("Download DEM for this dam")
        download.clicked.connect(self._download)
        self.save_to = QLabel("")
        self.save_to.setWordWrap(True)
        self.save_to.setTextInteractionFlags(getattr(Qt, 'TextInteractionFlag', Qt).TextSelectableByMouse)
        drow = QHBoxLayout()
        drow.addWidget(download)
        drow.addWidget(self.save_to, 1)
        dwrap = QWidget()
        dwrap.setLayout(drow)
        self.res.currentIndexChanged.connect(self._show_target)
        self.dem.textChanged.connect(self._changed)
        self.reason = _reason_label()
        form.addRow("Local DEM", self.res)
        form.addRow("", dwrap)
        form.addRow("", self.reason)
        form.addRow("Elevation unit", self.zunit)
        form.addRow("Top of dam (ft)", self.top)
        form.addRow("Inflow drop (ft)", self.drop)
        self.note = QLabel("Download writes a GeoTIFF in EPSG:5070, metres, for the selected dam. "
                           "Top of dam is filled from the dam record. Both numbers can still be changed.")
        self.note.setWordWrap(True)
        lay = QVBoxLayout()
        lay.addLayout(form)
        lay.addWidget(self.note)
        self.setLayout(lay)

    def initializePage(self):
        dam = self.wiz.selected_dam()
        self.visited = True
        self._show_target()
        self._changed()
        if dam.get("crest") and self.top.value() == 0:
            self.top.setValue(float(dam["crest"]))
        self.zunit.setCurrentIndex(1)
        self.note.setText("Dam {} | {} | point {}".format(
            dam.get("name") or "none", values_text(dam), dam.get("point") or "blank"))

    def _dem_folder(self):
        # Never into the suite data folder (it may be the read-only ND_Dam_Suite_Pack).
        from .damsuite.suite_config import safe_dams_folder
        return os.path.join(safe_dams_folder(), "terrain")

    def _show_target(self, *_a):
        dam = self.wiz.selected_dam()
        point = (dam.get("point") or "").split("[")[0].strip()
        folder, name = dem_target(self._dem_folder(), dam.get("id"), point,
                                  self.res.currentIndex())
        self.save_to.setText("Saves to {}\nas {}".format(folder, name))
        self.save_to.setToolTip(os.path.join(folder, name))

    def _changed(self, *_a):
        _set_reason(self.reason, self.problem())
        self.completeChanged.emit()

    def problem(self):
        from .runner_params import dem_problem
        return dem_problem(self.dem.text())

    def isComplete(self):
        return not self.problem()

    def _browse(self):
        path, _f = QFileDialog.getOpenFileName(self, "DEM", "", "Raster (*.tif *.tiff *.vrt)")
        if path:
            self.dem.setText(path)

    def _download(self):
        import processing
        from qgis.core import QgsApplication
        dam = self.wiz.selected_dam()
        county = self.wiz.page(DAM_PAGE).county.currentData() or dam.get("county") or ""
        point = (dam.get("point") or "").split("[")[0].strip()
        from .damsuite.suite_config import dem_buffer_mi
        out = self._dem_folder()
        names = list(ND_COUNTIES)
        params = {
            "SOURCE": 0,
            "RES": self.res.currentData(),
            "SCOPE": 0,
            "DAM_POINT": point,
            "BUFFER_MI": dem_buffer_mi(self.wiz.page(RUN_PAGE).max_extent.value()),   # half the max extent
            "COUNTY": names.index(county) if county in names else 0,
            "OUT": out,
            "DAM_ID": str(dam.get("id") or ""),     # names the file <DAM_ID>_10m_5070.tif
        }
        alg = "nddam:downloaddem"
        if QgsApplication.processingRegistry().algorithmById(alg) is None:
            alg = "ndbreach:downloaddem"
        self.note.setText("Opening {} for {}.".format(alg, dam.get("name") or county or "this dam"))
        processing.execAlgorithmDialog(alg, params)
        from .runner_params import downloaded_dem
        path = downloaded_dem(out, dam.get("id"), point)
        if path:
            self.dem.setText(path)

    def validatePage(self):
        why = self.problem()
        _set_reason(self.reason, why)         # shown under the DEM; empty when it is fine
        return not why


class RunPage(QWizardPage):
    def __init__(self, wiz):
        super(RunPage, self).__init__()
        self.wiz = wiz
        self.setTitle("Run")
        lay = QVBoxLayout()
        self.summary = QLabel("")
        self.summary.setWordWrap(True)
        lay.addWidget(self.summary)
        # Max extent is a main setting, not Advanced: it caps how far the flood is followed.
        main = QFormLayout()
        self.max_extent = QDoubleSpinBox()
        self.max_extent.setRange(1, 200)
        self.max_extent.setDecimals(1)
        self.max_extent.setValue(DEFAULT_MAX_EXTENT_MI)
        self.max_extent.setSuffix(" mi")
        self.max_extent.setToolTip("Largest side of the model domain. The run stops growing the "
                                   "domain at this size.")
        main.addRow("Max extent (largest domain side)", self.max_extent)
        lay.addLayout(main)
        # Ticking the box makes its values editable. Unticked they are NOT reset: the values
        # shown are the ones the Runner gets (as in the Runner's own dialog, where Advanced
        # only folds the parameters away), and the summary above states them.
        box = QGroupBox("Advanced settings")
        box.setCheckable(True)
        box.setChecked(False)
        self.advanced = box
        form = QFormLayout()
        self.cell = QDoubleSpinBox()
        self.cell.setRange(10, 500)
        self.cell.setValue(10)
        self.engine = QComboBox()
        self.engine.addItem("Built-in (no install)", 2)
        self.hours = QDoubleSpinBox()
        self.hours.setRange(1, 96)
        self.hours.setValue(24)
        self.eap = QCheckBox("Write the EAP report")
        form.addRow("Grid (m)", self.cell)
        form.addRow("Engine", self.engine)
        form.addRow("Run length (h)", self.hours)
        form.addRow("", self.eap)
        box.setLayout(form)
        lay.addWidget(box)
        self.setLayout(lay)
        for sig in (self.cell.valueChanged, self.hours.valueChanged, self.max_extent.valueChanged,
                    self.engine.currentIndexChanged, self.eap.toggled, box.toggled):
            sig.connect(self._summarise)

    def initializePage(self):
        self._summarise()

    def _summarise(self, *_a):
        dam = self.wiz.selected_dam()
        self.summary.setText(
            "{}\n{}\n{} engine, {:g} m grid, {:g} h run, {}, max extent {:g} mi{}.\n"
            "The runner dialog opens next and these values can still be changed.".format(
                dam.get("name") or "No dam", values_text(dam),
                (self.engine.currentText() or "Built-in").split(" (")[0], self.cell.value(),
                self.hours.value(), "EAP report" if self.eap.isChecked() else "no EAP report",
                self.max_extent.value(),
                "" if self.advanced.isChecked() else " (tick Advanced settings to change them)"))

    def validatePage(self):
        dam = self.wiz.selected_dam()
        dem = self.wiz.page(DEM_PAGE)
        self.wiz._answers = {
            "folder": self.wiz.page(FOLDER_PAGE).path.text().strip(),
            "dam_id": dam.get("id"), "dam_name": dam.get("name"),
            "point": dam.get("point"),
            "lines": self.wiz.found.get("dam_lines"),
            "structure_id": dam.get("structure_id"),
            "height": dam.get("height"), "storage": dam.get("storage"),
            "height_source": dam.get("height_source") or "",
            "storage_source": dam.get("storage_source") or "",
            "dem": dem.dem.text().strip(), "dem_z": dem.zunit.currentData(),
            "top": dem.top.value(), "drop": dem.drop.value(),
            "cell": self.cell.value(), "engine": self.engine.currentData(),
            "hours": self.hours.value(), "eap": self.eap.isChecked(),
            "max_extent_mi": self.max_extent.value(),
        }
        return True


DAM_LIMIT = 400


def match_count_text(rows, county="", name="", limit=None):
    """'12 dams match (Cass, "lake")' for the dam page; says when the list is cut off."""
    limit = DAM_LIMIT if limit is None else limit
    rows = [r for r in rows or [] if not r.get("error")]
    where = ", ".join(x for x in (county, '"{}"'.format(name) if name else "") if x) or \
        "all counties"
    if not rows:
        return "No dam matches ({}).".format(where)
    n = len(rows)
    return "{} dam{} match{} ({}){}".format(
        n, "" if n == 1 else "s", "es" if n == 1 else "", where,
        " - only the first {} are listed; narrow the search".format(limit) if n >= limit else "")


def _dams(uri, name="", county="", limit=DAM_LIMIT, lines_uri=None, hidden=None):
    """Dams in one county. Read the GeoPackage directly so a QGIS filter cannot
    return an empty list, and do not scan every dam line before the list shows.
    Height, storage and area of 0 are blank (DWR stores "not recorded" as 0). A dam
    without a point gets the midpoint of its dam line (lines_uri, else a dwr_dam_lines
    table in the same file), only for the listed dams.
    hidden: a list -> records that are not a dam to model (dam_filter: status Breached,
    remarks 'VOID ...') go there instead of the result, each with row['hidden'] = the
    reason. None (default) lists every record, with the reason on the hidden ones."""
    from . import dam_filter
    rows = []
    if not uri:
        return rows
    path, layer = uri, None
    if "|layername=" in uri:
        path, layer = uri.split("|layername=", 1)
    try:
        import sqlite3
        con = sqlite3.connect("file:{}?mode=ro".format(path), uri=True)
        tables = [r[0] for r in con.execute("SELECT table_name FROM gpkg_contents")]
        table = layer if layer in tables else ("dwr_dams" if "dwr_dams" in tables else None)
        if table:
            cols = [r[1] for r in con.execute('PRAGMA table_info("{}")'.format(table))]
            low = {c.lower(): c for c in cols}
            def col(*keys):
                for key in keys:
                    if key.lower() in low:
                        return low[key.lower()]
                return None
            where, args = [], []
            ccol = col("county")
            if county and ccol:
                where.append('lower("{}") = ?'.format(ccol))
                args.append(county.lower())
            ncol = col("dam_name")
            if name and ncol:
                where.append('lower("{}") LIKE ?'.format(ncol))
                args.append("%{}%".format(name.lower()))
            sql = 'SELECT * FROM "{}"'.format(table)
            if where:
                sql += " WHERE " + " AND ".join(where)
            if hidden is None:
                sql += " LIMIT {}".format(int(limit))
            for rec in con.execute(sql, args):
                if len(rows) >= limit:
                    break
                data = dict(zip(cols, rec))
                def pick(*keys):
                    for key in keys:
                        c = col(key)
                        if c and data.get(c) not in (None, ""):
                            return data.get(c)
                    return None

                def amount(*keys):          # a number above 0, else None
                    for key in keys:
                        val = _positive(pick(key))
                        if val is not None:
                            return val
                    return None
                sid = pick("structure_id", "structure_")
                lon, lat = _coord(pick("longitude")), _coord(pick("latitude"))
                rows.append({
                    "name": str(pick("dam_name") or sid or ""),
                    "id": str(sid or ""),
                    "structure_id": "" if sid is None else str(sid),
                    "nid": str(pick("national_id", "national_i") or ""),
                    "height": amount("max_dam_height", "max_dam_he"),
                    # max pool volume, else the DWR normal pool volume (as before 1.0.10);
                    # the NID only fills what the DWR record has neither of
                    "storage": amount("max_pool_vol", "max_pool_v"),
                    "normal_vol": amount("normal_vol"),
                    "area": amount("max_pool_area", "normal_area", "surface_area", "max_pool_a"),
                    "point": "{},{} [EPSG:4326]".format(lon, lat) if lon and lat else "",
                    "county": str(pick("county") or county or ""),
                    "status": str(pick("status") or ""),
                })
                why = dam_filter.reason(pick("status"), pick("remarks"))
                if why:
                    rows[-1]["hidden"] = why
                    if hidden is not None:
                        hidden.append(rows.pop())
            same = next((t for t in tables if t.lower() == "dwr_dam_lines"), None)
            con.close()
            points_from_lines(rows, lines_uri or (
                "{}|layername={}".format(path, same) if same else ""))
            rows.sort(key=lambda r: r["name"].lower())
            return rows
        con.close()
    except Exception:
        pass
    try:
        from qgis.core import QgsVectorLayer
        lyr = QgsVectorLayer(uri, "dwr", "ogr")
        if not lyr.isValid():
            return [{"name": "Dam layer did not open", "id": "", "point": "", "error": True}]
        want = (county or "").lower()
        needle = (name or "").lower()
        for feat in lyr.getFeatures():
            names = feat.fields().names()
            low = {n.lower(): n for n in names}
            got = str(feat[low["county"]] or "") if "county" in low else ""
            dam_name = str(feat[low["dam_name"]] or "") if "dam_name" in low else ""
            if want and got.lower() != want:
                continue
            if needle and needle not in dam_name.lower():
                continue
            sid = feat[low["structure_id"]] if "structure_id" in low else feat[low.get("structure_", "")] if "structure_" in low else ""
            def pick(*keys):
                for key in keys:
                    if key in low:
                        val = feat[low[key]]
                        if val not in (None, ""):
                            return val
                return None
            point = ""
            geom = feat.geometry()
            if geom is not None and not geom.isEmpty():
                p = geom.centroid().asPoint()
                point = "{:.6f},{:.6f}".format(p.x(), p.y())
            rows.append({"name": dam_name or str(sid), "id": str(sid or ""),
                         "structure_id": str(sid or ""),
                         "nid": str(pick("national_id", "nidid", "nid_id") or ""),
                         "height": _positive(pick("max_dam_height", "dam_height", "height")),
                         "storage": _positive(pick("max_pool_vol", "max_storage")),
                         "normal_vol": _positive(pick("normal_vol", "normal_pool_vol")),
                         "area": _positive(pick("max_pool_area", "normal_area", "surface_area")),
                         "crest": pick("crest_elev", "top_of_dam", "crest", "elev_crest"),
                         "point": point, "county": got,
                         "status": str(pick("status") or "")})
            why = dam_filter.reason(pick("status"), pick("remarks"))
            if why:
                rows[-1]["hidden"] = why
                if hidden is not None:
                    hidden.append(rows.pop())
            if len(rows) >= limit:
                break
    except Exception as e:
        return [{"name": "Could not read dams ({})".format(e), "id": "", "point": "",
                 "error": True}]
    rows.sort(key=lambda r: r["name"].lower())
    return rows


def _positive(value):
    """A number above 0, else None (DWR stores "not recorded" as 0)."""
    from .nid_fill import number
    return number(value)


def _coord(value):
    """A coordinate, or None for blank or 0 (three DWR dams have 0, 0 for no point)."""
    try:
        val = float(value)
    except (TypeError, ValueError):
        return None
    return val if val else None


def points_from_lines(rows, lines_uri):
    """A listed dam without a point gets the midpoint of its dam line (the crest), in
    EPSG:4326 like the inventory points, and row['from_line'] = True. A dam that has a
    point keeps it. Only the lines of those dams are read."""
    want = [r for r in rows if not r.get("point") and r.get("structure_id")]
    if not want or not lines_uri:
        return 0
    try:
        from qgis.core import (QgsVectorLayer, QgsCoordinateReferenceSystem,
                               QgsCoordinateTransform, QgsProject)
        lines = QgsVectorLayer(lines_uri, "lines", "ogr")
        if not lines.isValid() or "structure_id" not in lines.fields().names():
            return 0
        ids = ",".join("'{}'".format(str(r["structure_id"]).replace("'", "''")) for r in want)
        lines.setSubsetString('"structure_id" IN ({})'.format(ids))
        wgs = QgsCoordinateReferenceSystem("EPSG:4326")
        xform = None
        if lines.crs().isValid() and lines.crs() != wgs:
            xform = QgsCoordinateTransform(lines.crs(), wgs, QgsProject.instance())
        by_id = {}
        for feat in lines.getFeatures():
            g = feat.geometry()
            if g is None or g.isEmpty():
                continue
            parts = g.asMultiPolyline() if g.isMultipart() else [g.asPolyline()]
            parts = [pl for pl in parts if len(pl) >= 2]
            if not parts:
                continue
            mid = _midpoint(max(parts, key=_length))
            if xform is not None:
                mid = xform.transform(mid)
            by_id.setdefault(str(feat["structure_id"]), mid)
        n = 0
        for row in want:
            mid = by_id.get(str(row["structure_id"]))
            if mid is not None:
                row["point"] = "{:.6f},{:.6f} [EPSG:4326]".format(mid.x(), mid.y())
                row["from_line"] = True
                n += 1
        return n
    except Exception:
        return 0


def _length(part):
    return sum(((b.x() - a.x()) ** 2 + (b.y() - a.y()) ** 2) ** 0.5
               for a, b in zip(part, part[1:]))


def _midpoint(part):
    if len(part) < 2:
        return part[0]
    if len(part) == 2:
        return type(part[0])((part[0].x() + part[1].x()) / 2.0, (part[0].y() + part[1].y()) / 2.0)
    lengths = [0.0]
    for a, b in zip(part, part[1:]):
        lengths.append(lengths[-1] + ((b.x() - a.x()) ** 2 + (b.y() - a.y()) ** 2) ** 0.5)
    half = lengths[-1] / 2.0
    for i in range(1, len(lengths)):
        if lengths[i] >= half:
            span = lengths[i] - lengths[i - 1] or 1.0
            t = (half - lengths[i - 1]) / span
            a, b = part[i - 1], part[i]
            return type(a)(a.x() + t * (b.x() - a.x()), a.y() + t * (b.y() - a.y()))
    return part[len(part) // 2]


def ensure_dams_folder(path):
    """Create the chosen Dams folder (only that folder; damsuite/layout.py). Never raises."""
    try:
        from .damsuite import layout as LY
        return LY.ensure_dams_folder(path) if path else []
    except Exception as e:
        try:
            from qgis.core import QgsMessageLog, Qgis
            QgsMessageLog.logMessage("Dams folder not created: {}".format(e), "DamSafety Setup", getattr(Qgis, 'MessageLevel', Qgis).Warning)
        except Exception:
            pass
        return []
