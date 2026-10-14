# -*- coding: utf-8 -*-
"""v2.9: "Search a folder for data sets" on the Data & Output tab.

The engineer points at a folder on this computer (or a work server) that holds the
data sets of groups 3-6 (structures, roads / rail / crossings, dams / lifelines /
other layers, channel). The folder is searched in the background (datafolder.py):
every layer is read and matched to what the plugin needs by its name and its fields.
The pick for each data set is shown and can be changed. Order of use in a run:

    1. the layer found in this folder,
    2. the layer in the QGIS layer tree (auto-detect / the list of the group),
    3. the download from the internet.

A layer that the engineer picks by hand in one of the lists of groups 3-6 is never
replaced by the folder. Nothing is written into the folder.

HIFLD: the plugin can no longer download HIFLD (the HIFLD Open service was closed and
its copy at NASA NCCS does not answer). Download the layers from the Homeland Security
Digital Library, https://www.hsdl.org/hifld/, put them in a folder and search it here.
"""

import json
import os

from qgis.PyQt.QtWidgets import (
    QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QGroupBox, QCheckBox, QComboBox,
    QLineEdit, QTableWidget, QTableWidgetItem, QWidget, QMessageBox, QFileDialog,
)

from .helpers import log_message
from . import datafolder as DF

# data-folder role -> (list on the Data & Output tab, is "(none)" a decision?)
# a list that holds a layer of the project overrides the folder for that role
_ROLE_COMBOS = {
    "roads": ("road_combo", True), "aadt": ("aadt_combo", False),
    "rail": ("rail_combo", True), "crossings": ("crossing_combo", True),
    "outlines": ("outline_combo", False), "dams": ("dam_layer_combo", False),
    "boundary": ("boundary_combo", False), "parcels": ("parcel_combo", False),
    "centerline": ("centerline_combo", False),
}
COLUMNS = ("Group", "Data set", "Layer / file found", "Why")
NOT_USED = "(not used - QGIS layer or download)"


class DataFolderTabMixin(object):
    """Needs the dialog's _start_background, _log, _update_status, _busy and the layer
    lists of the data tab."""

    # ------------------------------------------------------------- widgets --
    def _build_datafolder_group(self):
        grp = QGroupBox("Data folder - search this computer for the data sets of groups 3-6")
        lay = QVBoxLayout()
        intro = QLabel(
            "Pick a folder that holds your data sets (sub-folders to 4 levels; GeoPackage, "
            "Shapefile, GeoJSON, KML/KMZ, CSV and ZIP files). The plugin reads each layer, "
            "matches it to a data set below by its name and fields, and uses it <b>first</b>; "
            "then the layers in the QGIS layer tree; then the internet. A layer you pick by "
            "hand in a list of groups 3-6 is never replaced. Nothing is written into the "
            "folder. <b>HIFLD</b> can no longer be downloaded by the plugin: get the layers "
            "from <a href=\"https://www.hsdl.org/hifld/\">hsdl.org/hifld</a> (Homeland "
            "Security Digital Library), save them in a folder and search it here.")
        intro.setWordWrap(True)
        lay.addWidget(intro)
        row = QHBoxLayout()
        self.datafolder_edit = QLineEdit()
        self.datafolder_edit.setPlaceholderText("Folder with data sets (optional)")
        self.datafolder_edit.setToolTip(
            "Search this folder (sub-folders to 4 levels) for the data sets of groups 3-6. "
            "Order of use: this folder, then the QGIS layer tree, then the internet.")
        self.datafolder_browse_btn = QPushButton("Browse...")
        self.datafolder_browse_btn.clicked.connect(self._on_browse_data_folder)
        self.datafolder_search_btn = QPushButton("Search folder")
        self.datafolder_search_btn.setToolTip(
            "Reads the layers of the folder in the background and shows what was found for "
            "each data set. QGIS stays usable.")
        self.datafolder_search_btn.clicked.connect(self._on_search_data_folder)
        self.datafolder_hifld_btn = QPushButton("Get HIFLD (hsdl.org)")
        self.datafolder_hifld_btn.setToolTip(
            "Opens the Homeland Security Digital Library HIFLD page in your web browser. "
            "Download the layers you need (fire stations, EMS, EOC, law enforcement, "
            "wastewater, ... for North Dakota), save them in a folder and search it here.")
        self.datafolder_hifld_btn.clicked.connect(self._on_open_hifld_page)
        row.addWidget(self.datafolder_edit, 3)
        row.addWidget(self.datafolder_browse_btn)
        row.addWidget(self.datafolder_search_btn)
        row.addWidget(self.datafolder_hifld_btn)
        lay.addLayout(row)
        self.datafolder_use_check = QCheckBox(
            "Use the data sets found in this folder first (before QGIS layers and downloads)")
        self.datafolder_use_check.setChecked(True)
        self.datafolder_use_check.toggled.connect(lambda *_a: self._df_parcels_changed())
        lay.addWidget(self.datafolder_use_check)
        self.datafolder_table = QTableWidget()
        self.datafolder_table.setColumnCount(len(COLUMNS))
        self.datafolder_table.setHorizontalHeaderLabels(list(COLUMNS))
        lay.addWidget(self.datafolder_table)
        self.datafolder_status = QLabel("No folder searched yet.")
        self.datafolder_status.setWordWrap(True)
        lay.addWidget(self.datafolder_status)
        # the picks travel with the project settings (a hidden text box)
        self.datafolder_picks_edit = QLineEdit()
        self.datafolder_picks_edit.setVisible(False)
        self.datafolder_picks_edit.textChanged.connect(self._df_picks_text_changed)
        lay.addWidget(self.datafolder_picks_edit)
        grp.setLayout(lay)
        self._df_rows = []                  # one dict per table row
        self._df_updating = False
        self._df_match = None
        return grp

    # ------------------------------------------------------------- actions --
    def _on_browse_data_folder(self, *args):
        if self._busy:
            return
        start = self.datafolder_edit.text().strip() or self.config_dam_folder or \
            os.path.expanduser("~")
        folder = QFileDialog.getExistingDirectory(self, "Folder with data sets", start)
        if folder:
            self.datafolder_edit.setText(folder)
            self._on_search_data_folder()

    def _on_open_hifld_page(self, *args):
        from .constants import HIFLD_PORTAL_URL
        try:
            from qgis.PyQt.QtCore import QUrl
            from qgis.PyQt.QtGui import QDesktopServices
            QDesktopServices.openUrl(QUrl(HIFLD_PORTAL_URL))
        except Exception as e:
            log_message("HIFLD page not opened: {}".format(e))
        self._log(">>> HIFLD: download the layers from {} , save them in a folder and search "
                  "it here".format(HIFLD_PORTAL_URL))

    def _on_search_data_folder(self, *args):
        if self._busy:
            return
        folder = self.datafolder_edit.text().strip()
        if not folder or not os.path.isdir(folder):
            QMessageBox.warning(self, "Data folder",
                                "Pick an existing folder to search first.")
            return

        def work(progress, log):
            found = DF.scan_folder(folder, progress=progress)
            return found, DF.match_roles(found)

        def done(outcome):
            if outcome.cancelled:
                self._update_status("Data folder search cancelled", False)
                self._log(">>> Data folder search cancelled")
                return
            if outcome.error is not None:
                self._log(outcome.traceback)
                self._update_status("Data folder search FAILED: {}".format(outcome.error), False)
                QMessageBox.critical(self, "Data folder", str(outcome.error))
                return
            scan, match = outcome.result
            self._df_match = match
            self._df_show_match(match)
            self._df_use_pack(self.datafolder_edit.text().strip())
            for line in DF.describe(match):
                self._log(">>> " + line)
            n = len([r for r in DF.ROLES if match.best(r)])
            self._update_status("Data folder: {} layer(s) read, {} of {} data sets found".format(
                len(scan), n, len(DF.ROLES)), n > 0)
        self._start_background("Searching the data folder...", work, done, project_bound=False)

    # --------------------------------------------------------------- table --
    def _df_use_pack(self, folder):
        """Point the hazard run at the suite pack. Does not load the layers."""
        try:
            from .damsuite.suite_fill import find_pack
            from .download import set_offline_config
        except Exception:
            return
        pack = find_pack(folder)
        if not pack:
            return
        if os.path.isdir(pack) and not os.path.isfile(os.path.join(pack, "nd_offline_pack.sqlite")):
            self._log("This is a suite data folder (one GeoPackage per layer). "
                      "Use it as the Data folder. The Offline pack box is only for an old .sqlite or .zip pack.")
            try:
                from .damsuite.suite_config import set as save
                save("data_folder", pack)
            except Exception:
                pass
            return
        if hasattr(self, "pack_path_edit") and not self.pack_path_edit.text().strip():
            self.pack_path_edit.setText(pack)
        try:
            set_offline_config(pack, getattr(self, "offline_only_check", None) and
                               self.offline_only_check.isChecked())
        except Exception as e:
            self._log(">>> Offline pack not saved: {}".format(e))
            return
        self._log(">>> Offline pack set from the suite folder: {}".format(pack))
        if hasattr(self, "_show_pack_status"):
            self._show_pack_status()

    def _df_show_match(self, match):
        rows = []
        for role in DF.ROLES:
            if role in DF.MULTI_ROLES:
                for c in match.lifelines:
                    label = "Lifeline facilities" + (" - " + c.category if c.category else "")
                    rows.append({"role": role, "category": c.category, "label": label,
                                 "options": [(c.dataset.name, c.dataset.uri, c.reason)],
                                 "selected": c.dataset.uri, "multi": True,
                                 "why": c.reason})
                continue
            cands = match.candidates.get(role) or []
            chosen = match.chosen.get(role)
            if not cands:
                continue
            rows.append({"role": role, "category": None,
                         "label": DF.ROLE_LABELS.get(role, role),
                         "options": [(c.dataset.name + "  (score {})".format(c.score),
                                      c.dataset.uri, c.reason) for c in cands],
                         "selected": chosen.dataset.uri if chosen else "",
                         "multi": False,
                         "why": chosen.reason if chosen else ""})
        self._df_fill_table(rows)
        self._df_store_picks()
        missing = [DF.ROLE_LABELS[r] for r in match.missing if r not in DF.MULTI_ROLES]
        self.datafolder_status.setText(
            "{} data set(s) chosen. Not found here (QGIS layers or downloads are used): {}".format(
                len([r for r in rows if r["selected"]]),
                ", ".join(missing) if missing else "none"))

    def _df_fill_table(self, rows):
        self._df_updating = True
        try:
            t = self.datafolder_table
            t.setRowCount(0)
            t.setRowCount(len(rows))
            self._df_rows = rows
            for i, r in enumerate(rows):
                group = DF.ROLE_GROUP.get(r["role"], "")
                t.setItem(i, 0, QTableWidgetItem(str(group)))
                t.setItem(i, 1, QTableWidgetItem(r["label"]))
                combo = QComboBox()
                combo.addItem(NOT_USED if not r["multi"] else "(not used)", "")
                for text, uri, _why in r["options"]:
                    combo.addItem(text, uri)
                for k in range(combo.count()):
                    if combo.itemData(k) == r["selected"]:
                        combo.setCurrentIndex(k)
                        break
                combo.setToolTip(r["selected"] or "")
                combo.currentIndexChanged.connect(lambda _i, row=i: self._df_combo_changed(row))
                r["combo"] = combo
                t.setCellWidget(i, 2, combo)
                t.setItem(i, 3, QTableWidgetItem(r.get("why", "")))
            try:
                t.resizeColumnsToContents()
            except Exception:
                pass
        finally:
            self._df_updating = False

    def _df_combo_changed(self, row):
        if self._df_updating or row >= len(self._df_rows):
            return
        r = self._df_rows[row]
        uri = r["combo"].currentData() or ""
        r["selected"] = uri
        why = ""
        for _text, u, w in r["options"]:
            if u == uri:
                why = w
        r["why"] = why
        it = self.datafolder_table.item(row, 3)
        if it is not None:
            try:
                it.setText(why)
            except Exception:
                self.datafolder_table.setItem(row, 3, QTableWidgetItem(why))
        r["combo"].setToolTip(uri)
        self._df_store_picks()

    # --------------------------------------------------------------- picks --
    def _df_picks(self):
        """{role: uri, "lifelines": [[uri, category]]} of the table."""
        out = {}
        for r in self._df_rows:
            uri = r.get("selected") or ""
            if not uri:
                continue
            if r["role"] in DF.MULTI_ROLES:
                out.setdefault(r["role"], []).append([uri, r.get("category")])
            else:
                out[r["role"]] = uri
        return out

    def _df_store_picks(self):
        self._df_updating = True
        try:
            self.datafolder_picks_edit.setText(json.dumps(self._df_picks(), sort_keys=True))
        finally:
            self._df_updating = False
        self._df_parcels_changed()

    def _df_parcels_changed(self):
        """v2.9: the parcel hints' field pickers follow the folder's parcel layer."""
        refresh = getattr(self, "_refresh_parcel_fields290", None)
        if refresh is not None:
            try:
                refresh()
            except Exception as e:                               # noqa
                self._log(">>> Parcel fields not refreshed: {}".format(e))

    def _df_picks_text_changed(self, text):
        """The project's settings were restored: show the saved picks (no new search)."""
        if self._df_updating:
            return
        try:
            picks = json.loads(text) if text else {}
        except ValueError:
            picks = {}
        rows = []
        for role in DF.ROLES:
            v = picks.get(role) if isinstance(picks, dict) else None
            if not v:
                continue
            if role in DF.MULTI_ROLES:
                for item in v:
                    try:
                        uri, cat = item[0], item[1]
                    except (IndexError, TypeError):
                        continue
                    rows.append({"role": role, "category": cat,
                                 "label": "Lifeline facilities" + (" - " + cat if cat else ""),
                                 "options": [(DF._name_from_uri(uri), uri, "saved pick")],
                                 "selected": uri, "multi": True, "why": "saved pick"})
            else:
                rows.append({"role": role, "category": None,
                             "label": DF.ROLE_LABELS.get(role, role),
                             "options": [(DF._name_from_uri(v), v, "saved pick")],
                             "selected": v, "multi": False, "why": "saved pick"})
        self._df_fill_table(rows)
        self._df_parcels_changed()
        if rows:
            self.datafolder_status.setText(
                "{} saved pick(s) - search the folder again to refresh the list.".format(
                    len(rows)))

    # ----------------------------------------------------------- cfg (run) --
    def _local_data_for_run(self):
        """cfg["local_data"]: the table's picks, without the roles whose list holds a
        layer the engineer picked by hand. {} when the folder is not to be used."""
        if not getattr(self, "datafolder_use_check", None) or \
                not self.datafolder_use_check.isChecked():
            return {}
        picks = self._df_picks()
        if not picks:
            return {}
        from .classify_tab import _AUTO, _NONE, _DOWNLOAD
        for role, (attr, none_is_decision) in _ROLE_COMBOS.items():
            combo = getattr(self, attr, None)
            if role not in picks or combo is None:
                continue
            data = combo.currentData()
            explicit = data not in (None, _NONE, _AUTO, _DOWNLOAD) or \
                (data == _NONE and none_is_decision)
            if explicit:
                picks.pop(role, None)
        return picks
