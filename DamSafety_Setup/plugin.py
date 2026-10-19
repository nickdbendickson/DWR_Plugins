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

# page ids (the order addPage is called in)
FOLDER_PAGE, DAM_PAGE, DEM_PAGE, RUN_PAGE = range(4)
RED, GREEN, GREY = "#c62828", "#2e7d32", "#888888"


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


class SetupPlugin(object):
    def __init__(self, iface):
        self.iface = iface
        self.action = None

    def initGui(self):
        self.action = QAction("Start here\u2026", self.iface.mainWindow())
        self.action.triggered.connect(self.run)
        self.iface.addPluginToMenu(MENU, self.action)
        self.dock = None

    def unload(self):
        if self.action is not None:
            self.iface.removePluginMenu(MENU, self.action)
        if getattr(self, "dock_action", None) is not None:
            self.iface.removePluginMenu(MENU, self.dock_action)
        if getattr(self, "dock", None) is not None:
            self.iface.removeDockWidget(self.dock)

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
            processing.execAlgorithmDialog(ALG, wiz.params())
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
        self.setWindowTitle("Set up one dam")
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
        browse = QPushButton("Browse")
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
        self.note = QLabel("Pick a county to list its dams, or type a name.")
        self.note.setWordWrap(True)
        lay.addWidget(self.note)
        form = QFormLayout()
        self.height = QDoubleSpinBox()
        self.height.setRange(0, 1000)
        self.height.setDecimals(1)
        self.height.setSuffix(" ft")
        self.storage = QDoubleSpinBox()
        self.storage.setRange(0, 5000000)
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
        form.addRow("Height", self.height)
        form.addRow("Max storage", self.storage)
        form.addRow("Surface area at top", self.area)
        form.addRow("Crest elevation", self.crest)
        form.addRow("NID", self.nid)
        form.addRow("Dam point", self.point)
        lay.addLayout(form)
        self.reason = _reason_label()
        lay.addWidget(self.reason)
        self.setLayout(lay)
        self.rows = []
        for spin in (self.height, self.storage):
            spin.valueChanged.connect(self._changed)

    def initializePage(self):
        self.combo.clear()
        self.rows = []
        self.note.setText("Pick a county to list its dams, or type a name.")
        self._find()

    def _find(self, *_a):
        county = self.county.currentData() or ""
        self.rows = _dams(self.wiz.found.get("dwr"), self.search.text().strip(), county)
        self.combo.blockSignals(True)
        self.combo.clear()
        for row in self.rows:
            self.combo.addItem("{} ({})".format(row["name"], row.get("county") or county), row)
        self.combo.blockSignals(False)
        self.count.setText(match_count_text(self.rows, county, self.search.text().strip()))
        self._show()

    def _show(self, *_a):
        row = self.combo.currentData() or {}
        self.height.setValue(float(row.get("height") or 0))
        self.storage.setValue(float(row.get("storage") or 0))
        self.area.setValue(float(row.get("area") or 0))
        self.crest.setValue(float(row.get("crest") or 0))
        self.nid.setText(str(row.get("nid") or ""))
        self.point.setText(str(row.get("point") or ""))
        self.note.setText("{} | county {} | crest line {}".format(
            row.get("name") or "No dam", row.get("county") or "blank",
            "found" if row.get("from_line") or self.wiz.found.get("dam_lines") else "not found"))
        self._changed()

    def _changed(self, *_a):
        _set_reason(self.reason, self.problem())
        self.completeChanged.emit()

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
        browse = QPushButton("Browse")
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
        self._show_target()
        self._changed()
        if dam.get("crest") and self.top.value() == 0:
            self.top.setValue(float(dam["crest"]))
        self.zunit.setCurrentIndex(1)
        self.note.setText("Dam {} | height {} ft | storage {} ac-ft | point {}".format(
            dam.get("name") or "none", dam.get("height") or "blank",
            dam.get("storage") or "blank", dam.get("point") or "blank"))

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
            "{}\n{} engine, {:g} m grid, {:g} h run, {}, max extent {:g} mi{}.\n"
            "The runner dialog opens next and these values can still be changed.".format(
                dam.get("name") or "No dam",
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


def _dams(uri, name="", county="", limit=DAM_LIMIT):
    """Dams in one county. Read the GeoPackage directly so a QGIS filter cannot
    return an empty list, and do not scan every dam line before the list shows."""
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
            sql += " LIMIT {}".format(int(limit))
            for rec in con.execute(sql, args):
                data = dict(zip(cols, rec))
                def pick(*keys):
                    for key in keys:
                        c = col(key)
                        if c and data.get(c) not in (None, ""):
                            return data.get(c)
                    return None
                sid = pick("structure_id", "structure_")
                lon, lat = pick("longitude"), pick("latitude")
                rows.append({
                    "name": str(pick("dam_name") or sid or ""),
                    "id": str(sid or ""),
                    "structure_id": "" if sid is None else str(sid),
                    "nid": str(pick("national_id", "national_i") or ""),
                    "height": pick("max_dam_height", "max_dam_he"),
                    "storage": pick("max_pool_vol", "normal_vol", "max_pool_v"),
                    "area": pick("max_pool_area", "normal_area", "surface_area", "max_pool_a"),
                    "point": "{},{} [EPSG:4326]".format(lon, lat) if lon and lat else "",
                    "county": str(pick("county") or county or ""),
                })
            if "dwr_dam_lines" in tables:
                ids = [r["structure_id"] for r in rows if r.get("structure_id")]
                if ids:
                    q = ",".join("?" for _ in ids)
                    mids = {}
                    for sid, geom in con.execute(
                            'SELECT structure_id, geom FROM dwr_dam_lines WHERE structure_id IN ({})'.format(q), ids):
                        mids[str(sid)] = geom
                    for row in rows:
                        if row.get("structure_id") in mids and not row.get("point"):
                            row["from_line"] = False
            con.close()
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
                         "height": pick("max_dam_height", "dam_height", "height"),
                         "storage": pick("max_pool_vol", "max_storage", "normal_pool_vol"),
                         "area": pick("max_pool_area", "normal_area", "surface_area"),
                         "crest": pick("crest_elev", "top_of_dam", "crest", "elev_crest"),
                         "point": point, "county": got})
            if len(rows) >= limit:
                break
    except Exception as e:
        return [{"name": "Could not read dams ({})".format(e), "id": "", "point": "",
                 "error": True}]
    rows.sort(key=lambda r: r["name"].lower())
    return rows


def _line_midpoints(dam_uri, rows):
    """Replace the inventory point with the dam-line midpoint. The line is the crest."""
    if not dam_uri or "dwr_dams" not in dam_uri:
        return
    line_uri = dam_uri.replace("dwr_dams", "dwr_dam_lines")
    try:
        from qgis.core import QgsVectorLayer, QgsGeometry
        lines = QgsVectorLayer(line_uri, "lines", "ogr")
        if not lines.isValid():
            return
        by_id = {}
        for feat in lines.getFeatures():
            sid = str(feat["structure_id"]) if "structure_id" in feat.fields().names() else ""
            g = feat.geometry()
            if not sid or g is None or g.isEmpty():
                continue
            parts = g.asMultiPolyline() if g.isMultipart() else [g.asPolyline()]
            part = max(parts, key=lambda pl: len(pl))
            if len(part) < 2:
                continue
            mid = _midpoint(part)
            by_id[sid] = (mid.x(), mid.y(), lines.crs().authid() or "EPSG:4326")
        for row in rows:
            mid = by_id.get(row.get("structure_id") or "")
            if mid:
                row["point"] = "{},{} [{}]".format(mid[0], mid[1], mid[2])
                row["from_line"] = True
    except Exception:
        return


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
