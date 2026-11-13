# -*- coding: utf-8 -*-
"""The Dam suite home window - one view of a Dams folder for the three plugins.

One copy, in the DamSafety Core plugin (DamSafety_Core/ui/suite_home.py); Hazard, Runner and
Screening carry a three-line shim onto it (Core 1.0.0). Each plugin opens it
from its menu ("Dam suite home..."); whichever plugin opened it, the window is the same: every
dam folder of a Dams folder with its dam.json state (screened / modelled / classified / reviewed),
its class, the suite's next action (damsuite.suite_home / damsuite.dam_record.next_action) and the
plugin that does it, and buttons that open that plugin's window with the dam filled in where the
window has a field for it (the Runner: Dams folder + dam ID; Screening: the Dams folder; ND Dam
Hazard: the dam folder).

Core 1.3.0: "Try the sample dam" puts the suite's sample dam (damsuite.sample_dam) in the Dams
folder, from the offline pack when the pack has it and from the USGS elevation service when it
does not, and opens the Runner with the dam and its terrain filled in. It is the one thing in
this window that writes a file.

Logic is damsuite.suite_home (pure Python, tested); this file holds only the widgets. Built
offscreen in headless QGIS 3.34; not yet exercised by hand in an interactive QGIS.
"""
import os

from qgis.PyQt.QtCore import QUrl, Qt
from qgis.PyQt.QtGui import QDesktopServices
from qgis.PyQt.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton, QTableWidget,
    QTableWidgetItem, QFileDialog, QMessageBox,
)

from ..damsuite import sample_dam as SD
from ..damsuite import suite_config as SC
from ..damsuite import suite_home as SH
from ..damsuite import versions as VS
from .ui_fit import fit_window

TITLE = "Dam suite home"
SCOPE = "NDDamSuite"
ENTRY = "home_dams_folder"
# v2.18.13 / 2.31.1 (Heim 1.6/1.7): the Dams folder is a per-computer setting; a project that
# already stores one (older releases) keeps it as an override, so existing projects open as before
DAMS_KEY = SCOPE + "/" + ENTRY
HEADER_KEY = SCOPE + "/home_table_header"
FOLDER_ROLE = getattr(Qt, "UserRole", 256)
RECENT_ERRORS = []          # the last few problems shown to the user, for "Copy diagnostics"
MAX_ERRORS = 20
SAMPLE_BUTTON = "Try the sample dam\u2026"


def note_error(text):
    """Remember a problem for the diagnostics text (never raises)."""
    try:
        t = str(text or "").strip()
        if t:
            RECENT_ERRORS.append(t)
            del RECENT_ERRORS[:-MAX_ERRORS]
    except Exception:                                            # noqa
        pass


def resolve_dams_folder(project_value, computer_value, fallback=""):
    """The Dams folder to show: the project's (an override kept from older releases), else this
    computer's setting, else `fallback`."""
    for v in (project_value, computer_value, fallback):
        v = (v or "").strip() if isinstance(v, str) else ""
        if v:
            return v
    return ""


def diagnostics_text(info):
    """Plain text for "Copy diagnostics" from a dict (pure, tested). Missing keys print '?'."""
    def g(k):
        v = info.get(k)
        return "?" if v in (None, "") else str(v)
    lines = ["Dam suite diagnostics",
             "Plugin: {} {}".format(g("plugin"), g("plugin_version")),
             "QGIS: {}".format(g("qgis")),
             "Python: {}".format(g("python")),
             "OS: {}".format(g("os")),
             "Settings file: {}".format(g("settings_file")),
             "Dams folder: {}".format(g("dams_folder"))]
    inst = info.get("installed") or {}
    if inst:
        lines.append("Installed: " + "; ".join("{} {}".format(k, v or "not found")
                                               for k, v in inst.items()))
    errs = list(info.get("errors") or [])
    lines.append("Recent problems: " + ("none recorded" if not errs else str(len(errs))))
    lines.extend("  - " + e for e in errs[-MAX_ERRORS:])
    return "\n".join(lines) + "\n"
COLUMNS = ("Dam ID", "Name", "Class", "Existing", "Screened", "Modelled", "Classified", "Reviewed",
           "Next", "Plugin", "Notes")
# plugin key -> (plugin folder name, how to point its window at the dam)
PLUGIN_FOLDER = VS.PLUGINS


class SuiteHomeDialog(QDialog):
    def __init__(self, parent=None, iface=None, opened_from=""):
        super().__init__(parent)
        self.iface = iface
        self.opened_from = opened_from
        self.rows = []
        self.setWindowTitle(TITLE)
        self._build()
        # GUI check 2026-11-04: a fixed 900 px minimum was wider than an 800 x 600 screen. The
        # window now opens at 960 x 600 where the screen has room and may shrink to the screen
        # (fit_window: the table scrolls and the buttons keep their own rows).
        fit_window(self, prefer=(960, 600))
        try:                                                     # never cut a button row short
            need = min(self.layout().minimumSize().width(), self.maximumWidth())
            if need > self.minimumWidth():
                self.setMinimumWidth(need)
        except Exception:                                        # noqa - sizing never breaks the window
            pass
        self._load()
        self.refresh()

    # ------------------------------------------------------------------ build --
    def _build(self):
        lay = QVBoxLayout()
        info = QLabel(
            "Every dam folder of the Dams folder with its dam.json state and the next action the suite "
            "suggests. Classes are DamSafety Hazard's (the Runner's / Screening's previews are not counted). "
            "Nothing here changes a file, except \"Try the sample dam\", which adds the sample dam's "
            "folder to the Dams folder.")
        info.setWordWrap(True)
        lay.addWidget(info)
        row = QHBoxLayout()
        row.addWidget(QLabel("Dams folder:"))
        self.dams_edit = QLineEdit()
        self.dams_edit.editingFinished.connect(self._changed)
        b = QPushButton("Browse\u2026")
        b.clicked.connect(lambda _c=False: self._browse())
        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.clicked.connect(lambda _c=False: self.refresh())
        row.addWidget(self.dams_edit, 1)
        row.addWidget(b)
        row.addWidget(self.refresh_btn)
        lay.addLayout(row)
        self.summary_label = QLabel("")
        self.summary_label.setWordWrap(True)
        lay.addWidget(self.summary_label)
        self.table = QTableWidget()
        self.table.setColumnCount(len(COLUMNS))
        try:
            self.table.setHorizontalHeaderLabels(list(COLUMNS))
            self.table.setSortingEnabled(True)
        except Exception:                                        # noqa
            pass
        self.table.cellDoubleClicked.connect(lambda r, c: self._open_for_row(r))
        self._header_restored = self._restore_table_state()
        try:
            hdr = self.table.horizontalHeader()
            hdr.sectionResized.connect(lambda *_a: self._save_table_state())
            hdr.sortIndicatorChanged.connect(lambda *_a: self._save_table_state())
        except Exception:                                        # noqa
            pass
        lay.addWidget(self.table, 1)
        row = QHBoxLayout()
        self.open_btn = QPushButton("Open the plugin for the selected dam")
        self.open_btn.clicked.connect(lambda _c=False: self._open_for_row(self.table.currentRow()))
        self.folder_btn = QPushButton("Open the dam folder")
        self.folder_btn.clicked.connect(lambda _c=False: self._open_folder(self.table.currentRow()))
        row.addWidget(self.open_btn)
        row.addWidget(self.folder_btn)
        self.diag_btn = QPushButton("Copy diagnostics")
        self.diag_btn.setToolTip("Copy plugin, QGIS and OS versions, the settings file, the Dams "
                                 "folder and recent problems to the clipboard, to paste into a "
                                 "support message.")
        self.diag_btn.clicked.connect(lambda _c=False: self.copy_diagnostics())
        row.addWidget(self.diag_btn)
        row.addStretch(1)
        lay.addLayout(row)
        # a second row: six buttons on one line are cut off below about 1400 px
        plugins = QHBoxLayout()
        self._sample_job = None
        self._sample_timer = None
        self.sample_btn = QPushButton(SAMPLE_BUTTON)
        self.sample_btn.setToolTip(
            "New here? This puts a ready-made dam (its record and 10 m terrain) in the Dams "
            "folder and opens the breach Runner with it filled in. From the offline pack when "
            "the pack has it; otherwise the terrain is downloaded from the USGS.")
        self.sample_btn.clicked.connect(lambda _c=False: self.try_sample())
        plugins.addWidget(self.sample_btn)
        plugins.addStretch(1)
        for key in ("ndhazard", "ndbreach", "ndscreen"):
            btn = QPushButton(VS.LABELS.get(key, key) + "\u2026")
            btn.clicked.connect(lambda _c=False, k=key: self.open_plugin(k))
            setattr(self, "btn_" + key, btn)
            plugins.addWidget(btn)
        lay.addLayout(plugins)
        self.plugins_label = QLabel("")
        self.plugins_label.setWordWrap(True)
        lay.addWidget(self.plugins_label)
        self.setLayout(lay)

    # --------------------------------------------------------------- settings --
    def _load(self):
        project, computer = "", ""
        try:
            from qgis.core import QgsProject
            text, ok = QgsProject.instance().readEntry(SCOPE, ENTRY, "")
            project = text if ok else ""
        except Exception:                                        # noqa
            pass
        try:
            from qgis.core import QgsSettings
            computer = QgsSettings().value(DAMS_KEY, "") or ""
        except Exception:                                        # noqa
            pass
        self._project_override = bool((project or "").strip())
        text = resolve_dams_folder(project, computer)
        if text:
            self.dams_edit.setText(text)

    def _save(self):
        value = self.dams_edit.text().strip()
        try:
            from qgis.core import QgsSettings
            QgsSettings().setValue(DAMS_KEY, value)
        except Exception as e:                                   # noqa
            note_error("Dams folder not remembered: {}".format(e))
        if getattr(self, "_project_override", False):
            # an older project stores its own Dams folder: keep it in step so it does not mask
            # the choice just made
            try:
                from qgis.core import QgsProject
                QgsProject.instance().writeEntry(SCOPE, ENTRY, value)
            except Exception:                                    # noqa
                pass

    def _changed(self, *args):
        self._save()
        self.refresh()

    def _browse(self):
        d = QFileDialog.getExistingDirectory(self, "Dams folder", self.dams_edit.text().strip() or "")
        if d:
            self.dams_edit.setText(d)
            self._changed()

    # ---------------------------------------------------------------- refresh --
    def refresh(self, *args):
        dams = self.dams_edit.text().strip()
        self.rows = SH.scan(dams) if dams and os.path.isdir(dams) else []
        s = SH.summary(self.rows)
        text = SH.summary_text(s) if dams else "Choose the Dams folder."
        if dams and not self.rows:                # an empty Dams folder: where a new user starts
            text += " New here? Click \"{}\" below.".format(SAMPLE_BUTTON.rstrip("\u2026"))
        self.summary_label.setText(text)
        try:
            self.table.setSortingEnabled(False)
        except Exception:                                        # noqa
            pass
        self.table.setRowCount(len(self.rows))
        for i, r in enumerate(self.rows):
            notes = []
            if r["disputed"]:
                notes.append("{} disputed value(s)".format(r["disputed"]))
            if r["problem"]:
                notes.append(r["problem"])
            if not r["has_record"]:
                notes.append("no dam.json")
            vals = (r["dam_id"], r["name"], r["class"], r["existing_class"], r["screened"], r["modelled"],
                    r["classified"], r["reviewed"], "{}: {}".format(r["next"], r["next_text"]),
                    VS.LABELS.get(r["plugin"], r["plugin"]), "; ".join(notes))
            for j, v in enumerate(vals):
                it = QTableWidgetItem(str(v))
                if j == 0:
                    it.setData(FOLDER_ROLE, r.get("folder") or "")
                self.table.setItem(i, j, it)
        try:
            self.table.setSortingEnabled(True)
            if not getattr(self, "_header_restored", False):
                self.table.resizeColumnsToContents()
        except Exception:                                        # noqa
            pass
        inst = SH.installed_plugins()
        self.plugins_label.setText("Installed: " + "; ".join(
            "{} {}".format(VS.LABELS[k], v or "not found") for k, v in inst.items()) +
            ". Opened from {}.".format(VS.LABELS.get(self.opened_from, self.opened_from or "?")))
        have = bool(self.rows)
        self.open_btn.setEnabled(have)
        self.folder_btn.setEnabled(have)

    def row_at(self, index):
        """The scan row shown at table row `index` (the table may be sorted). Found by the dam
        folder stored on the first cell (two folders can share a Dam ID); the ID text and then the
        position are the fallbacks."""
        if index is None or index < 0:
            return None
        folder, did = None, None
        try:
            item = self.table.item(index, 0)
            if item is not None:
                folder = item.data(FOLDER_ROLE)
                did = item.text()
        except Exception:                                        # noqa
            pass
        if folder:
            hit = {r.get("folder"): r for r in self.rows}.get(folder)
            if hit is not None:
                return hit
        for r in self.rows:
            if r["dam_id"] == did:
                return r
        return self.rows[index] if 0 <= index < len(self.rows) else None

    # ------------------------------------------------------- table state --
    def _restore_table_state(self):
        """Column widths and sort order from the last session; True when restored."""
        try:
            from qgis.core import QgsSettings
            st = QgsSettings().value(HEADER_KEY, None)
            if st:
                return bool(self.table.horizontalHeader().restoreState(st))
        except Exception:                                        # noqa
            pass
        return False

    def _save_table_state(self):
        try:
            from qgis.core import QgsSettings
            QgsSettings().setValue(HEADER_KEY, self.table.horizontalHeader().saveState())
        except Exception:                                        # noqa
            pass

    # ------------------------------------------------------- diagnostics --
    def diagnostics_info(self):
        import platform
        import sys
        info = {"dams_folder": self.dams_edit.text().strip(), "errors": list(RECENT_ERRORS),
                "python": sys.version.split()[0], "os": platform.platform()}
        # Core 1.0.0: this module lives in DamSafety Core, so the plugin that opened the window
        # is `opened_from` (its suite key), not the package this file sits in
        key = self.opened_from or ""
        info["plugin"] = VS.LABELS.get(key, key or (__package__ or "?"))
        try:
            v = VS.installed(key) if key else None
            if v:
                info["plugin_version"] = v
            elif key in VS.RELEASE:
                info["plugin_version"] = VS.RELEASE[key] + " (release set)"
        except Exception:                                        # noqa
            pass
        try:
            from qgis.core import Qgis
            info["qgis"] = Qgis.QGIS_VERSION
        except Exception:                                        # noqa
            pass
        try:
            from ..damsuite import settings as DS
            info["settings_file"] = DS.current_path() or "(none)"
        except Exception:                                        # noqa
            pass
        try:
            info["installed"] = {VS.LABELS.get(k, k): v for k, v in SH.installed_plugins().items()}
        except Exception:                                        # noqa
            pass
        return info

    def copy_diagnostics(self):
        text = diagnostics_text(self.diagnostics_info())
        try:
            from qgis.PyQt.QtWidgets import QApplication
            QApplication.clipboard().setText(text)
            QMessageBox.information(self, TITLE, "Diagnostics copied to the clipboard - paste them "
                                                 "into your support message.")
        except Exception as e:                                   # noqa
            QMessageBox.warning(self, TITLE, "Could not use the clipboard ({}). The text:\n\n{}"
                                .format(e, text))
        return text

    # ------------------------------------------------------------------ open --
    def _open_folder(self, index):
        r = self.row_at(index)
        if r and os.path.isdir(r["folder"]):
            QDesktopServices.openUrl(QUrl.fromLocalFile(r["folder"]))

    def _open_for_row(self, index):
        r = self.row_at(index)
        if r is None:
            return False
        return self.open_plugin(r["plugin"], r)

    def _plugin_object(self, key):
        names = {
            "ndscreen": ("DamSafety_Screening", "nd_dam_screening"),
            "ndbreach": ("DamSafety_Runner", "nd_breach_runner"),
            "ndhazard": ("DamSafety_Hazard", "ND_Dam_Hazard_Helper"),
        }.get(key, (PLUGIN_FOLDER.get(key, key),))
        try:
            from qgis.utils import plugins, loadPlugin, startPlugin
        except Exception:                                        # noqa
            return None
        for name in names:
            plug = plugins.get(name)
            if plug is not None and hasattr(plug, "run_plugin"):
                return plug
        for name in names:
            try:
                loadPlugin(name)
                startPlugin(name)
            except Exception:
                pass
            plug = plugins.get(name)
            if plug is not None and hasattr(plug, "run_plugin"):
                return plug
        return None

    # ------------------------------------------------------------ sample dam --
    def _ask(self, text, yes="Yes", no="No"):
        """A two-button question; True for the first button. (A method so a test can answer.)"""
        box = QMessageBox(self)
        box.setWindowTitle(TITLE)
        box.setText(text)
        roles = getattr(QMessageBox, "ButtonRole", QMessageBox)   # Qt6 / Qt5
        first = box.addButton(yes, roles.AcceptRole)
        box.addButton(no, roles.RejectRole)
        box.exec_() if hasattr(box, "exec_") else box.exec()
        return box.clickedButton() is first

    def _tell(self, text):
        QMessageBox.information(self, TITLE, text)

    def try_sample(self):
        """Put the sample dam in the Dams folder and open the Runner on it. While its terrain
        downloads, the same button cancels the download. Returns "opened", "downloading",
        "cancelling" or "" (nothing done)."""
        if self._sample_job is not None:
            self._sample_job.cancel()
            self.sample_btn.setText("Cancelling the download\u2026")
            self.sample_btn.setEnabled(False)
            return "cancelling"
        try:
            sm = SD.get()
        except SD.SampleError as e:
            self._tell(str(e))
            return ""
        dams = self.dams_edit.text().strip()
        if not dams:
            dams = SC.safe_dams_folder()
            if not self._ask("No Dams folder is chosen yet. Use {} ?\n\nThe Dams folder holds one "
                             "folder per dam; the sample dam goes there.".format(dams),
                             "Use this folder", "Cancel"):
                return ""
            self.dams_edit.setText(dams)
            self._save()
        if SC.in_suite_pack(dams):
            self._tell("The Dams folder is inside the offline pack, which the suite never writes "
                       "to. Choose another Dams folder first.")
            return ""
        try:
            os.makedirs(dams, exist_ok=True)
        except OSError as e:
            note_error("Sample dam: {}".format(e))
            self._tell("The Dams folder {} cannot be made ({}).".format(dams, e))
            return ""
        have = SD.installed(dams)
        if have:
            return self._sample_ready(have, "It is already in the Dams folder.")
        source = SD.find(*SD.pack_roots())
        if source:
            try:
                done = SD.install(source, dams)
            except SD.SampleError as e:
                note_error("Sample dam: {}".format(e))
                self._tell("The sample dam could not be set up: {}.".format(e))
                return ""
            return self._sample_ready(done["folder"], "Copied from the offline pack.")
        if not self._ask(
                "The sample dam is {} ({}), {}\n\nIts terrain is not in an offline pack on this "
                "computer, so it has to be downloaded from the USGS elevation service: about "
                "{} MB, usually a minute or two. It is written to {}.".format(
                    sm.name, sm.dam_id, sm.about, SD.download_mb(),
                    os.path.join(dams, sm.dam_id)),
                "Download", "Cancel"):
            return ""
        self._sample_job = SD.Job(folder=os.path.join(dams, sm.dam_id)).start()
        self.sample_btn.setText("Downloading the sample dam\u2026 (click to cancel)")
        # imported here, not at the top: the home window must still load where QTimer is missing
        from qgis.PyQt.QtCore import QTimer
        self._sample_timer = QTimer(self)
        self._sample_timer.timeout.connect(self._sample_poll)
        self._sample_timer.start(400)
        return "downloading"

    def _sample_poll(self):
        """Called by the timer while the terrain downloads; finishes the button's job when the
        download ends. Returns what try_sample would ("" while it still runs)."""
        job = self._sample_job
        if job is None:
            return ""
        if not job.done:
            if job.messages and not job.cancelled:
                self.summary_label.setText(job.messages[-1] + "\u2026")
            return ""
        self._sample_job = None
        if self._sample_timer is not None:
            self._sample_timer.stop()
            self._sample_timer = None
        self.sample_btn.setText(SAMPLE_BUTTON)
        self.sample_btn.setEnabled(True)
        if job.error:
            self.refresh()
            if job.cancelled:
                self._tell("The sample dam's download was cancelled. The parts already "
                           "downloaded are kept: the button goes on from there.")
            else:
                note_error("Sample dam: {}".format(job.error))
                self._tell("The sample dam was not set up: {}".format(job.error))
            return ""
        return self._sample_ready(job.folder, "Its terrain was downloaded from the USGS.")

    def _sample_ready(self, folder, how):
        sm = SD.get()
        self.refresh()
        self._select_folder(folder)
        quick = self._ask(
            "The sample dam, {} ({}), is in\n{}\n{}\n\nThe breach Runner opens next with the "
            "dam and its terrain filled in. This dam is {}\n\nIt is a sample to learn the tools "
            "on. A run of it is not a hazard determination for this dam.".format(
                sm.name, sm.dam_id, folder, how, sm.about),
            "Quick first try: {:g} m cell, first {:g} hours".format(SD.FIRST_TRY_CELL_M,
                                                                     SD.FIRST_TRY_HOURS),
            "Keep the Runner's settings")
        row = {"folder": folder, "dem": os.path.join(folder, SD.dem_name())}
        if quick:
            row["first_try"] = dict(SD.FIRST_TRY)
        opened = self.open_plugin("ndbreach", row)
        if opened and quick:
            changed = SD.first_try_lines(row.get("first_try_before"))
            missing = [SD.FIRST_TRY_LABELS[k] for k in row.get("first_try_missing") or []]
            self._tell(
                "For a quick look at the sample, these settings of the Runner window were "
                "changed:\n\n{}\n\nThe run maps the first {:g} hours of the flood and takes a "
                "few minutes. Its report will say the flood is still advancing: that is "
                "expected, the flood on this plain moves for days.\n\nThe Runner window "
                "remembers these values. Set them back before you model a dam for real.{}".format(
                    "\n".join("  " + ln for ln in changed) or "  (none: they were set already)",
                    SD.FIRST_TRY_HOURS,
                    "\n\nThis Runner window has no field for: {}. Without it the run is not "
                    "quick.".format(", ".join(missing)) if missing else ""))
        return "opened" if opened else ""

    def _select_folder(self, folder):
        try:
            for i in range(self.table.rowCount()):
                item = self.table.item(i, 0)
                if item is not None and item.data(FOLDER_ROLE) == folder:
                    self.table.setCurrentCell(i, 0)
                    return True
        except Exception:                                        # noqa
            pass
        return False

    def open_plugin(self, key, row=None):
        """Open plugin `key`'s window, pointed at the dam of `row` when it has a field for it.
        Returns True when the window opened."""
        plug = self._plugin_object(key)
        if plug is None or not hasattr(plug, "run_plugin"):
            QMessageBox.information(self, TITLE,
                "{} is not enabled in this QGIS. Enable DamSafety Screening, DamSafety Runner "
                "and DamSafety Hazard (or the ND copies) under Plugins, then restart QGIS.".format(
                    VS.LABELS.get(key, key)))
            return False
        try:
            plug.run_plugin()
        except Exception as e:                                   # noqa
            QMessageBox.information(self, TITLE, "{} could not be opened ({}).".format(VS.LABELS.get(key, key), e))
            return False
        if row:
            point_window_at(getattr(plug, "dialog", None), key, self.dams_edit.text().strip(), row)
        return True


def point_window_at(dialog, key, dams_folder, row):
    """Fill the other plugin's window with the dam (duck typing on the fields the three windows
    have; anything missing is skipped). Returns what was set."""
    done = []
    if dialog is None:
        return done
    try:
        if key == "ndbreach":
            if hasattr(dialog, "dams_edit"):
                dialog.dams_edit.setText(dams_folder)
                done.append("dams_folder")
            if hasattr(dialog, "dam_edit"):
                dialog.dam_edit.setText(os.path.basename(os.path.normpath(row["folder"])))
                done.append("dam_id")
            dem = row.get("dem") or ""
            if dem and hasattr(dialog, "dem_combo"):       # the sample dam's terrain file
                i = dialog.dem_combo.findData(dem)
                if i < 0:
                    dialog.dem_combo.addItem(os.path.basename(dem), dem)
                    i = dialog.dem_combo.count() - 1
                dialog.dem_combo.setCurrentIndex(i)
                done.append("dem")
            if row.get("grid_m") and hasattr(dialog, "grid_m"):
                dialog.grid_m.setValue(float(row["grid_m"]))
                done.append("grid_m")
            if row.get("first_try"):
                # the sample dam's quick first try: every value that makes it quick, and what
                # the window held before, so the user can be told what to set back
                before, missing = {}, []
                for name, value in row["first_try"].items():
                    w = getattr(dialog, name, None)
                    try:
                        if isinstance(value, bool):
                            before[name] = bool(w.isChecked())
                            w.setChecked(value)
                        else:
                            before[name] = float(w.value())
                            w.setValue(float(value))
                        done.append(name)
                    except Exception:                            # noqa: an older Runner window
                        before.pop(name, None)
                        missing.append(name)
                row["first_try_before"], row["first_try_missing"] = before, missing
        elif key == "ndscreen":
            if hasattr(dialog, "dams_folder"):
                dialog.dams_folder.setText(dams_folder)
                done.append("dams_folder")
        elif key == "ndhazard":
            if hasattr(dialog, "dam_folder_edit"):
                dialog.dam_folder_edit.setText(row["folder"])
                done.append("dam_folder")
        for name in ("_changed", "refresh"):
            fn = getattr(dialog, name, None)
            if callable(fn):
                fn()
                break
    except Exception:                                            # noqa
        pass
    return done


# ------------------------------------------------------------ the suite menu --
# 2026-11-08 (usability plan, change 4): one Plugins menu. Every DamSafety plugin puts its
# entries in "Dam safety"; the shared entries (the home) go there too, added once.
SUITE_MENU = "Dam safety"
HOME_ACTION = ("Dam suite home\u2026", "NDSuiteHome")   # (label, object name) of the one entry


def _suite_menu_actions(iface):
    """The actions already in Plugins > ND Dam Suite (another suite plugin may have added them)."""
    out = {}
    try:
        from qgis.PyQt.QtWidgets import QMenu
        for menu in iface.pluginMenu().findChildren(QMenu):
            if menu.title().replace("&", "") == SUITE_MENU:
                for a in menu.actions():
                    out[a.objectName()] = a
    except Exception:                                            # noqa - no such menu yet / a test double
        pass
    return out


def add_home_action(plugin, iface):
    """The one "Dam suite home…" entry of Plugins > Dam safety, added by whichever of the three
    plugins loads first (2026-11-08: until then no menu and no window opened the home at all).
    Appended to plugin.suite_actions so unload removes it; returns the QAction or None."""
    label, name = HOME_ACTION
    for a in getattr(plugin, "suite_actions", None) or []:       # this plugin added it already
        try:
            if a.objectName() == name:
                return None
        except Exception:                                        # noqa
            pass
    action = add_suite_action(iface, label, name, plugin.run_home)
    if action is not None:
        plugin.suite_actions.append(action)
    return action


def add_suite_action(iface, label, object_name, slot):
    """Add an entry to the shared Plugins > Dam safety menu (1.4.0: one menu for the three
    plugins). An entry with the same object name that another plugin already added is not added
    again. Returns the QAction, or None when it exists already or cannot be made."""
    if iface is None:
        return None
    if object_name in _suite_menu_actions(iface):
        return None
    try:
        try:                              # Qt6 (QGIS 4): QAction lives in QtGui
            from qgis.PyQt.QtGui import QAction
        except ImportError:               # Qt5 (QGIS 3.x): QtWidgets
            from qgis.PyQt.QtWidgets import QAction
        action = QAction(label, iface.mainWindow())
        action.setObjectName(object_name)
        action.triggered.connect(slot)
        iface.addPluginToMenu(SUITE_MENU, action)
        return action
    except Exception:                                            # noqa
        return None


def remove_suite_actions(iface, actions):
    for a in actions or []:
        try:
            iface.removePluginMenu(SUITE_MENU, a)
        except Exception:                                        # noqa
            pass
        try:
            a.deleteLater()
        except Exception:                                        # noqa
            pass


def _shared():
    """A registry shared by the three plugins' copies of this module (each plugin imports its
    own copy, so a module global is not shared): one home window per QGIS."""
    import sys
    import types
    reg = sys.modules.get("_damsuite_shared_windows")
    if reg is None:
        reg = types.ModuleType("_damsuite_shared_windows")
        reg.home = None
        sys.modules["_damsuite_shared_windows"] = reg
    return reg


def _alive(dlg):
    if dlg is None:
        return False
    try:
        try:
            from qgis.PyQt import sip
        except ImportError:               # pragma: no cover
            import sip
        return not sip.isdeleted(dlg)
    except Exception:                                            # noqa
        return False


def open_home(plugin, iface, opened_from, parent=None):
    """One Dam suite home window per QGIS, whichever plugin opens it (it used to be one per
    plugin, so three identical homes could be open). Re-created when Qt destroyed it."""
    reg = _shared()
    dlg = reg.home if _alive(reg.home) else None
    if dlg is None:
        dlg = getattr(plugin, "home_dialog", None)
        dlg = dlg if _alive(dlg) else None
    if dlg is None:
        dlg = SuiteHomeDialog(parent=parent, iface=iface, opened_from=opened_from)
    reg.home = dlg
    plugin.home_dialog = dlg
    dlg.show()
    dlg.raise_()
    dlg.activateWindow()
    return dlg
