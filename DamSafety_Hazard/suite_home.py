# -*- coding: utf-8 -*-
"""The Dam suite home window - one view of a Dams folder for the three plugins.

Byte-identical in ND Dam Hazard Classification Helper, ND Dam Breach Runner and ND Dam Breach
Screening (written by suite/suite_shared/make_styles.py; edit it there). Each plugin opens it
from its menu ("Dam suite home..."); whichever plugin opened it, the window is the same: every
dam folder of a Dams folder with its dam.json state (screened / modelled / classified / reviewed),
its class, the suite's next action (damsuite.suite_home / damsuite.dam_record.next_action) and the
plugin that does it, and buttons that open that plugin's window with the dam filled in where the
window has a field for it (the Runner: Dams folder + dam ID; Screening: the Dams folder; ND Dam
Hazard: the dam folder).

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

from .damsuite import suite_home as SH
from .damsuite import versions as VS

TITLE = "Dam suite home"
SCOPE = "NDDamSuite"
ENTRY = "home_dams_folder"
# v2.18.13 / 2.31.1 (Lumo 1.6/1.7): the Dams folder is a per-computer setting; a project that
# already stores one (older releases) keeps it as an override, so existing projects open as before
DAMS_KEY = SCOPE + "/" + ENTRY
HEADER_KEY = SCOPE + "/home_table_header"
FOLDER_ROLE = getattr(Qt, "UserRole", 256)
RECENT_ERRORS = []          # the last few problems shown to the user, for "Copy diagnostics"
MAX_ERRORS = 20


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
        self.setMinimumWidth(900)
        self._build()
        self._load()
        self.refresh()

    # ------------------------------------------------------------------ build --
    def _build(self):
        lay = QVBoxLayout()
        info = QLabel(
            "Every dam folder of the Dams folder with its dam.json state and the next action the suite "
            "suggests. Classes are ND Dam Hazard's (the Runner's / Screening's previews are not counted). "
            "Nothing here changes a file.")
        info.setWordWrap(True)
        lay.addWidget(info)
        row = QHBoxLayout()
        row.addWidget(QLabel("Dams folder:"))
        self.dams_edit = QLineEdit()
        self.dams_edit.editingFinished.connect(self._changed)
        b = QPushButton("Browse...")
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
        for key in ("ndhazard", "ndbreach", "ndscreen"):
            btn = QPushButton(VS.LABELS.get(key, key) + "...")
            btn.clicked.connect(lambda _c=False, k=key: self.open_plugin(k))
            setattr(self, "btn_" + key, btn)
            row.addWidget(btn)
        lay.addLayout(row)
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
        self.summary_label.setText(SH.summary_text(s) if dams else "Choose the Dams folder.")
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
        try:
            from .constants import PLUGIN_NAME, PLUGIN_VERSION
            info["plugin"], info["plugin_version"] = PLUGIN_NAME, PLUGIN_VERSION
        except Exception:                                        # noqa
            info["plugin"] = __package__ or "?"
        try:
            from qgis.core import Qgis
            info["qgis"] = Qgis.QGIS_VERSION
        except Exception:                                        # noqa
            pass
        try:
            from .damsuite import settings as DS
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
SUITE_MENU = "ND Dam Suite"


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


def add_suite_action(iface, label, object_name, slot):
    """Add an entry to the shared Plugins > ND Dam Suite menu (1.4.0: one menu for the three
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
