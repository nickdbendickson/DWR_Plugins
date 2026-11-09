# -*- coding: utf-8 -*-
"""The Results tab of the DamSafety Screening popup: the counts, the result layers, the report, the hand-offs to the Runner and Hazard.

0.18.19 (usability plan, change 5/8-style clean-up): split out of dialog.py mechanically - the
methods are moved, not changed - so the window reads tab by tab, as DamSafety Hazard's does.
Dialog contract: the host is NDDamScreeningDialog (dialog.py); these methods use its widgets,
settings(), _log() and refresh()."""
import os

from qgis.PyQt.QtCore import QUrl
from qgis.PyQt.QtGui import QDesktopServices
from qgis.PyQt.QtWidgets import (
    QVBoxLayout, QHBoxLayout, QWidget, QLabel, QPushButton, QComboBox, QTableWidget,
    QTableWidgetItem, QMessageBox,
)

from .core import workflow as wf
from .dialog_consts import TITLE


class ResultsTabMixin(object):
    """The results table, Add results, the report and the hand-offs."""

    # -- Results
    def _build_results_tab(self):
        w = QWidget()
        lay = QVBoxLayout()
        self.counts_label = QLabel("")
        self.counts_label.setWordWrap(True)
        self.record_label = QLabel("")
        self.record_label.setWordWrap(True)
        lay.addWidget(self.counts_label)
        lay.addWidget(self.record_label)
        self.results_table = QTableWidget(0, 7)
        self.results_table.setHorizontalHeaderLabels(
            ["Dam", "Name", "Status", "Peak cfs", "Flooded ac", "Max depth ft", "Edge"])
        self.results_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.results_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.results_table.setMaximumHeight(220)
        lay.addWidget(self.results_table)
        row = QHBoxLayout()
        self.report_btn = QPushButton("Open the report (index.html)")
        self.report_btn.clicked.connect(self._open_report)
        self.add_btn = QPushButton("Add the results to the project")
        self.add_btn.setToolTip("One layer group 'DamSafety Screening - <work folder>' with the dams by class, "
                                "the flood extents, the breach points and arrows, and the review queue; "
                                "every layer styled.")
        self.add_btn.clicked.connect(lambda _c=False: self._add_results())
        self.hazard_btn = QPushButton("Open DamSafety Hazard…")
        self.hazard_btn.setToolTip("The classification authority: take the dams that matter there "
                                   "(its 'Dam suite' group reads the same dam.json records).")
        self.hazard_btn.clicked.connect(self._open_hazard)
        row.addWidget(self.report_btn)
        row.addWidget(self.add_btn)
        row.addWidget(self.hazard_btn)
        lay.addLayout(row)
        self.one_dam_label = QLabel("")
        self.one_dam_label.setWordWrap(True)
        lay.addWidget(self.one_dam_label)
        row = QHBoxLayout()
        self.one_dam_combo = QComboBox()
        self.one_dam_report_btn = QPushButton("Open this one-dam report")
        self.one_dam_report_btn.clicked.connect(lambda _c=False: self._open_one_dam_report())
        row.addWidget(self.one_dam_combo, 1)
        row.addWidget(self.one_dam_report_btn)
        lay.addLayout(row)
        lay.addStretch(1)
        w.setLayout(lay)
        return w


    # ----------------------------------------------------------------- results --
    def _open_work_folder(self):
        work = self.work_edit.text().strip()
        if work and os.path.isdir(work):
            QDesktopServices.openUrl(QUrl.fromLocalFile(work))

    def _open_report(self):
        p = os.path.join(self.work_edit.text().strip(), wf.REPORT_INDEX)
        if os.path.isfile(p):
            QDesktopServices.openUrl(QUrl.fromLocalFile(p))

    def _add_results(self, work=None, title=None):
        """Every result layer of the work folder, styled, in one layer group (0.13); the plain
        add when the project has no layer tree here."""
        work = work or self.work_edit.text().strip()
        added = 0
        try:
            from qgis.core import QgsProject, QgsVectorLayer
        except Exception as e:                                   # noqa
            self._log("!!! layers cannot be added here: {}".format(e))
            return 0
        try:
            from . import suite_styles
        except Exception:                                        # noqa
            suite_styles = None
        group = None
        try:
            root = QgsProject.instance().layerTreeRoot()
            name = title or "DamSafety Screening - {}".format(os.path.basename(os.path.normpath(work)) or work)
            group = root.findGroup(name) or root.insertGroup(0, name)
        except Exception:                                        # noqa
            group = None
        for title_l, p in wf.result_layers(work):
            try:
                layer = QgsVectorLayer(p, title_l, "ogr")
                if layer.isValid():
                    if suite_styles is not None:
                        suite_styles.apply(layer, suite_styles.style_for_path(p), log=self._log)
                    if group is not None:
                        QgsProject.instance().addMapLayer(layer, False)
                        group.addLayer(layer)
                    else:
                        QgsProject.instance().addMapLayer(layer)
                    added += 1
                else:
                    self._log("!!! {} could not be read as a layer".format(p))
            except Exception as e:                               # noqa
                self._log("!!! {}: {}".format(p, e))
        self._fill_layers()     # before the summary: _fill_layers can log a work-folder-layout
        self._log("Added {} result layer(s) to the project{}".format(added, " in one group" if group is not None else ""))
        return added


    def _continue(self):
        """Open the plugin dam.json says this dam needs next."""
        plugin, _label, why = wf.continue_target(
            self.work_edit.text().strip(), self.settings(), self._selected_dam())
        if plugin == "runner":
            self._handoff_runner()
        elif plugin == "hazard":
            self._handoff_hazard()
        else:
            try:
                QMessageBox.information(self, TITLE, why or "Continue is not available for this dam.")
            except Exception:
                self._log(why or "Continue is not available.")

    def _selected_dam(self):
        combo = getattr(self, "one_dam_combo", None)
        if combo is not None and combo.currentText():
            return combo.currentText().split()[0]
        return ""

    def _handoff_runner(self):
        work = self.work_edit.text().strip()
        dams = wf.dams_folder(work, self.settings()) if work else ""
        dam = self._selected_dam()
        try:
            from qgis.utils import plugins
            plug = plugins.get("DamSafety_Runner") or plugins.get("nd_breach_runner")
            if plug is None:
                raise KeyError("Runner is not installed")
            plug.run_plugin()
            dlg = getattr(plug, "dialog", None)
            if dlg is not None and dams:
                dlg.dams_edit.setText(dams)
                if dam and hasattr(dlg, "dam_edit"):
                    dlg.dam_edit.setText(dam)
            self._log("Runner opened on {} {}".format(dams, dam))
        except Exception as e:
            self._log("Runner window not opened ({}). Opening step 7b.".format(e))
            self._open_step("7b")

    def _handoff_hazard(self):
        work = self.work_edit.text().strip()
        dam = self._selected_dam()
        folder = os.path.join(wf.dams_folder(work, self.settings()), dam) if work and dam else ""
        try:
            from qgis.utils import plugins
            plug = plugins.get("DamSafety_Hazard") or plugins.get("ND_Dam_Hazard_Helper")
            if plug is None:
                raise KeyError("Hazard is not installed")
            plug.run_plugin()
            dlg = getattr(plug, "dialog", None)
            if dlg is not None and folder and hasattr(dlg, "dam_folder_edit"):
                dlg.dam_folder_edit.setText(folder)
            self._log("Hazard opened on {}".format(folder or work))
        except Exception as e:
            QMessageBox.information(self, TITLE, "Hazard could not be opened ({}).".format(e))

    def _fill_results_table(self, work):
        table = getattr(self, "results_table", None)
        if table is None:
            return
        path = os.path.join(work, "7_results.gpkg")
        if not os.path.isfile(path):
            table.setRowCount(0)
            return
        try:
            import sqlite3
            con = sqlite3.connect(path)
            rows = con.execute(
                'select dam_id, name, sim_status, td_qp_m3s, area_ha, max_depth_m, edge_flag from "7_results"'
            ).fetchall()
            con.close()
        except Exception:
            table.setRowCount(0)
            return
        table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            peak = (r[3] or 0) * 35.3147
            ac = (r[4] or 0) * 2.47105
            depth = (r[5] or 0) * 3.28084
            vals = [r[0] or "", r[1] or "", r[2] or "", "{:,.0f}".format(peak),
                    "{:,.0f}".format(ac), "{:.1f}".format(depth), "yes" if r[6] else "no"]
            for c, val in enumerate(vals):
                table.setItem(i, c, QTableWidgetItem(str(val)))

    def _open_hazard(self):
        try:
            from qgis.utils import plugins
            plug = plugins.get("DamSafety_Hazard") or plugins.get("ND_Dam_Hazard_Helper")
            if plug is None:
                raise KeyError("Hazard is not installed or not enabled")
            plug.run_plugin()
        except Exception as e:                                   # noqa
            QMessageBox.information(self, TITLE, "DamSafety Hazard could not be opened ({}).".format(e))

