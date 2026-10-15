# -*- coding: utf-8 -*-
"""v2.13: the "Suite settings" tab of the dialog - the suite settings file (every engineer-chosen
value of this plugin's Processing tools, shared with ND Dam Breach Runner and ND Dam Breach
Screening) as a grid. The tools open with the file's values; the dialog's own classification
options stay on their tabs and in the settings profiles (settings.py).

Logic: damsuite.settings and suite_settings_tools (tested); the grid widget is suite_settings.py
(the same file in the three plugins). Built offscreen in headless QGIS 3.34; not
yet exercised by hand in an interactive QGIS.
"""
import os

from qgis.PyQt.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
                                 QFileDialog, QMessageBox)

from .helpers import log_message

PLUGIN_KEY = "ndhazard"
FILE_FILTER = "Settings workbook (*.xlsx);;Settings table (*.csv);;All files (*)"


class SuiteSettingsTabMixin(object):

    def _build_suite_settings_tab(self):
        from . import suite_settings as SS
        from . import suite_settings_tools as ST
        from .damsuite import settings as DS
        w = QWidget()
        lay = QVBoxLayout(w)
        info = QLabel("The suite settings file: one table of the engineer-chosen values of the three "
                      "plugins' Processing tools (this plugin's are on the tabs below). The tools open "
                      "with these values; every one can still be changed in a tool's dialog. Edits here "
                      "are saved to the file at once. The classification options of this window live on "
                      "their own tabs and in the settings profiles, not here.")
        info.setWordWrap(True)
        lay.addWidget(info)
        row = QHBoxLayout()
        row.addWidget(QLabel("Settings file:"))
        self.suite_settings_file_edit = QLineEdit()
        self.suite_settings_file_edit.setText(DS.current_path() or "")
        self.suite_settings_file_edit.editingFinished.connect(lambda: self._suite_settings_file_edited())
        b = QPushButton("File...")
        b.clicked.connect(lambda _c=False: self._pick_suite_settings_file())
        n = QPushButton("New...")
        n.clicked.connect(lambda _c=False: self._new_suite_settings_file())
        row.addWidget(self.suite_settings_file_edit, 1)
        row.addWidget(b)
        row.addWidget(n)
        lay.addLayout(row)
        # status area: save failures, a refused file, a catalog that did not load (Lumo 2.7.1/2.7.3/2.7.5)
        self.suite_settings_status = QLabel("")
        self.suite_settings_status.setWordWrap(True)
        try:
            self.suite_settings_status.setStyleSheet("color: #b42318;")
        except Exception:                                        # noqa
            pass
        lay.addWidget(self.suite_settings_status)
        catalog_problem = ""
        try:
            from .processing_provider import Provider, settings_group, SETTINGS_TABS
            catalog = ST.full_catalog(Provider.original_classes(), PLUGIN_KEY, settings_group, SETTINGS_TABS)
            tabs = SETTINGS_TABS
        except Exception as e:                                   # noqa
            catalog, tabs = [], ()
            catalog_problem = "The settings list could not be loaded, so the grid is empty: {}".format(e)
            log_message("Suite settings grid: {}".format(e))
        try:
            from .damsuite import versions as vs
            versions = {PLUGIN_KEY: vs.RELEASE.get(PLUGIN_KEY, "")}
        except Exception:                                        # noqa
            versions = {}
        self.suite_grid = SS.SettingsGrid(PLUGIN_KEY, catalog, DS.Settings(), parent=w,
                                          on_change=lambda _s: self._autosave_suite_settings(),
                                          on_file=self._suite_grid_file, versions=versions, tab_order=tabs)
        lay.addWidget(self.suite_grid, 1)
        if catalog_problem:
            self._suite_status(catalog_problem)
        p = self.suite_settings_file_edit.text().strip()
        # the path the grid's values belong to; autosave writes only while the box shows it.
        # A path that does not exist yet starts from the starting values (the first edit creates it).
        self._suite_loaded_path = p if (p and not os.path.isfile(p)) else ""
        if p and os.path.isfile(p):
            self._suite_loaded_path = p           # set first: load() fires on_change (autosave)
            try:
                if not self.suite_grid.load(p, quiet=True):
                    self._suite_loaded_path = ""
            except Exception as e:                               # noqa
                self._suite_loaded_path = ""
                log_message("Suite settings file: {}".format(e))
                self._suite_status("Suite settings file {} not loaded: {}".format(p, e))
        return w

    def _suite_status(self, text, error=True):
        lbl = getattr(self, "suite_settings_status", None)
        if lbl is not None:
            lbl.setText(text or "")
        if text and error:
            try:
                from .suite_home import note_error
                note_error(text)
            except Exception:                                    # noqa
                pass

    def _suite_settings_file_edited(self):
        from .damsuite import settings as DS
        p = self.suite_settings_file_edit.text().strip()
        old = getattr(self, "_suite_loaded_path", "") or ""
        if p == old:
            return True
        if not p:
            DS.set_current_path("")
            self.suite_grid.settings = DS.Settings()
            self.suite_grid.fill()
            self._suite_loaded_path = ""
            self._suite_status("")
            return False
        if not os.path.isfile(p):
            # Lumo 2.7.5: never make a missing file current - the grid still holds the old file's
            # values and the next edit would autosave them into the typed path
            self.suite_settings_file_edit.setText(old)
            msg = "The settings file does not exist: {}. Still using {}.".format(p, old or "(none)")
            self._suite_status(msg)
            QMessageBox.warning(self, "Suite settings", msg)
            return False
        self._suite_loaded_path = p               # set first: load() fires on_change (autosave)
        if not self.suite_grid.load(p):
            self._suite_loaded_path = old
            self.suite_settings_file_edit.setText(old)
            self._suite_status("{} could not be loaded. Still using {}.".format(p, old or "(none)"))
            return False
        DS.set_current_path(p)
        self._suite_loaded_path = p
        self._suite_status("", error=False)
        return True

    def _pick_suite_settings_file(self):
        start = self.suite_settings_file_edit.text().strip() or ""
        p, _f = QFileDialog.getOpenFileName(self, "Settings file", start, FILE_FILTER)
        if not p:
            return False
        self.suite_settings_file_edit.setText(p)
        return self._suite_settings_file_edited()

    def _new_suite_settings_file(self):
        from .damsuite import settings as DS
        p, _f = QFileDialog.getSaveFileName(self, "New settings file", DS.DEFAULT_NAME,
                                            "Settings workbook (*.xlsx);;Settings table (*.csv)")
        if not p:
            return ""
        if not os.path.splitext(p)[1]:
            p += ".xlsx"
        self.suite_grid.settings.path = p
        if not self.suite_grid.save(p, name=os.path.splitext(os.path.basename(p))[0].replace("_", " ")):
            self._suite_status("The new settings file {} was not written.".format(p))
            return ""
        self.suite_settings_file_edit.setText(p)
        DS.set_current_path(p)
        self._suite_loaded_path = p
        self._suite_status("", error=False)
        return p

    def _suite_grid_file(self, path):
        from .damsuite import settings as DS
        if path and path != self.suite_settings_file_edit.text().strip():
            self.suite_settings_file_edit.setText(path)
        DS.set_current_path(path)
        if path:
            self._suite_loaded_path = path

    def _autosave_suite_settings(self):
        from .damsuite import settings as DS
        p = self.suite_settings_file_edit.text().strip()
        if not p:
            return False
        if p != (getattr(self, "_suite_loaded_path", None) or ""):
            # Lumo 2.7.5: the box and the grid disagree - never write one file's values into another
            self._suite_status("Not saved: the grid shows {} but the box says {}. Press Enter in the "
                               "box or choose the file again.".format(
                                   getattr(self, "_suite_loaded_path", "") or "(none)", p))
            return False
        try:
            self.suite_grid.settings.path = p
            self.suite_grid.settings.save(p, catalog=self.suite_grid.catalog,
                                          versions=self.suite_grid.versions or None)
            DS.set_current_path(p)
        except DS.SettingsError as e:
            log_message("Suite settings not saved: {}".format(e))
            self._suite_status("Suite settings not saved: {}".format(e))
            return False
        except Exception as e:                                   # noqa - never escape a Qt slot
            log_message("Suite settings not saved ({}: {})".format(type(e).__name__, e))
            self._suite_status("Suite settings not saved ({}: {})".format(type(e).__name__, e))
            return False
        if getattr(self, "suite_settings_status", None) is not None and \
                self.suite_settings_status.text().startswith(("Suite settings not saved", "Not saved")):
            self._suite_status("", error=False)
        return True
