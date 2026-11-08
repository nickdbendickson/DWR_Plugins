# -*- coding: utf-8 -*-
"""The Review tab of the DamSafety Screening popup: the dams that need a look and the reviewer's decisions.

0.18.19 (usability plan, change 5/8-style clean-up): split out of dialog.py mechanically - the
methods are moved, not changed - so the window reads tab by tab, as DamSafety Hazard's does.
Dialog contract: the host is NDDamScreeningDialog (dialog.py); these methods use its widgets,
settings(), _log() and refresh()."""
import os

from qgis.PyQt.QtWidgets import (
    QVBoxLayout, QHBoxLayout, QWidget, QLabel, QPushButton, QComboBox, QTableWidget,
    QTableWidgetItem, QInputDialog, QMessageBox,
)

from .core import workflow as wf
from .core import review as rv
from .dialog_consts import TITLE, REVIEW_COLUMNS


class ReviewTabMixin(object):
    """The review queue table and its decisions."""

    # -- Review
    def _build_review_tab(self):
        w = QWidget()
        lay = QVBoxLayout()
        info = QLabel("The dams that need a look: uncertain breach direction (no dam line), uncertain or "
                      "unusual pond type, borderline class, the report's review queue. Zoom to one, decide, "
                      "and the decision is written into the layers (rev_decision / rev_by / rev_date) and "
                      "to review_decisions.csv. An excluded dam is skipped by steps 5-8 from then on. Your "
                      "name: Settings > Run options > Reviewer.")
        info.setWordWrap(True)
        lay.addWidget(info)
        self.review_table = QTableWidget()
        self.review_table.setColumnCount(len(REVIEW_COLUMNS))
        try:
            self.review_table.setHorizontalHeaderLabels(list(REVIEW_COLUMNS))
            self.review_table.setSelectionBehavior(self.review_table.SelectionBehavior.SelectRows)
        except Exception:                                        # noqa
            pass
        lay.addWidget(self.review_table, 1)
        row = QHBoxLayout()
        self.review_refresh_btn = QPushButton("Refresh")
        self.review_refresh_btn.clicked.connect(lambda _c=False: self.refresh_review())
        self.zoom_btn = QPushButton("Zoom to")
        self.zoom_btn.clicked.connect(lambda _c=False: self._zoom_to_selected())
        self.accept_btn = QPushButton("Accept")
        self.accept_btn.setToolTip("Screened and looked at; keep the result as it is.")
        self.accept_btn.clicked.connect(lambda _c=False: self._decide("accept"))
        self.exclude_btn = QPushButton("Exclude")
        self.exclude_btn.setToolTip("Not a dam / not to be screened: steps 5-8 skip it from now on.")
        self.exclude_btn.clicked.connect(lambda _c=False: self._decide("exclude"))
        self.type_combo = QComboBox()
        for t in rv.POND_TYPES:
            self.type_combo.addItem(t, t)
        self.set_type_btn = QPushButton("Set pond type")
        self.set_type_btn.clicked.connect(lambda _c=False: self._decide("set_type", self.type_combo.currentData()))
        self.move_btn = QPushButton("Move breach point…")
        self.move_btn.setToolTip("Give the new point as 'x, y' in EPSG:5070 metres (the map canvas centre is "
                                 "offered when a map is open); dir_conf becomes 'reviewed'.")
        self.move_btn.clicked.connect(lambda _c=False: self._move_breach())
        self.note_btn = QPushButton("Add note…")
        self.note_btn.clicked.connect(lambda _c=False: self._add_note())
        for b in (self.review_refresh_btn, self.zoom_btn, self.accept_btn, self.exclude_btn):
            row.addWidget(b)
        row.addWidget(self.type_combo)
        row.addWidget(self.set_type_btn)
        row.addWidget(self.move_btn)
        row.addWidget(self.note_btn)
        row.addStretch(1)
        lay.addLayout(row)
        self.review_label = QLabel("")
        self.review_label.setWordWrap(True)
        lay.addWidget(self.review_label)
        w.setLayout(lay)
        return w


    # ------------------------------------------------------------------ review --
    def refresh_review(self):
        work = self.work_edit.text().strip()
        self.review_rows = rv.queue(work, self._read_rows) if work and os.path.isdir(work) else []
        t = self.review_table
        t.setRowCount(len(self.review_rows))
        for i, r in enumerate(self.review_rows):
            for j, key in enumerate(("dam_id", "name", "why", "source", "decision")):
                t.setItem(i, j, QTableWidgetItem(str(r.get(key) or "")))
        try:
            t.resizeColumnsToContents()
        except Exception:                                        # noqa
            pass
        n_dec = sum(1 for r in self.review_rows if r["decision"])
        excl = rv.excluded_ids(work) if work and os.path.isdir(work) else []
        self.review_label.setText("{} dam(s) to look at, {} decided; {} excluded in total.".format(
            len(self.review_rows), n_dec, len(excl)))
        has = bool(self.review_rows)
        for b in (self.zoom_btn, self.accept_btn, self.exclude_btn, self.set_type_btn, self.move_btn, self.note_btn):
            b.setEnabled(has)

    def _selected_review(self):
        i = self.review_table.currentRow()
        if i is None or i < 0 or i >= len(self.review_rows):
            try:
                item = self.review_table.item(i, 0)
            except Exception:                                    # noqa
                item = None
            if item is None:
                return None
        try:
            item = self.review_table.item(i, 0)
            did = item.text() if item is not None else None
        except Exception:                                        # noqa
            did = None
        for r in self.review_rows:
            if did and r["dam_id"] == did:
                return r
        return self.review_rows[i] if 0 <= i < len(self.review_rows) else None

    def _decide(self, action, value="", note=""):
        r = self._selected_review()
        if r is None:
            QMessageBox.information(self, TITLE, "Select a dam in the list first.")
            return None
        work = self.work_edit.text().strip()
        reviewer = self.reviewer_edit.text().strip()
        if not reviewer:
            reviewer, ok = QInputDialog.getText(self, TITLE, "Your name or initials (kept in Settings > Run options):")
            if not ok or not reviewer.strip():
                return None
            self.reviewer_edit.setText(reviewer.strip())
            self._save_state()
        try:
            row, done = rv.decide(work, r["dam_id"], action, value, reviewer, note)
        except (ValueError, OSError) as e:
            QMessageBox.warning(self, TITLE, str(e))
            return None
        changed = [(os.path.basename(p), n) for p, n in done]
        self._log("Review: {} {} {} -> {}".format(r["dam_id"], action, value or "", "; ".join(
            "{} ({})".format(p, n) for p, n in changed) or "no layer written"))
        if any(isinstance(n, str) for _p, n in done):
            QMessageBox.information(self, TITLE, "Recorded in review_decisions.csv, but not every layer could be "
                                                 "written:\n" + "\n".join("{}: {}".format(p, n) for p, n in changed
                                                                         if isinstance(n, str)))
        self.refresh_review()
        return row

    def _move_breach(self):
        r = self._selected_review()
        if r is None:
            QMessageBox.information(self, TITLE, "Select a dam in the list first.")
            return None
        start = ""
        try:
            c = self.iface.mapCanvas().center() if self.iface is not None else None
            if c is not None:
                start = "{:.1f}, {:.1f}".format(c.x(), c.y())
        except Exception:                                        # noqa
            start = ""
        text, ok = QInputDialog.getText(self, TITLE, "New breach point for {} as 'x, y' in EPSG:5070 metres:".format(
            r["dam_id"]), text=start) if start else QInputDialog.getText(
            self, TITLE, "New breach point for {} as 'x, y' in EPSG:5070 metres:".format(r["dam_id"]))
        if not ok or not str(text).strip():
            return None
        return self._decide("move_breach", str(text).strip())

    def _add_note(self):
        r = self._selected_review()
        if r is None:
            QMessageBox.information(self, TITLE, "Select a dam in the list first.")
            return None
        text, ok = QInputDialog.getText(self, TITLE, "Note for {}:".format(r["dam_id"]))
        if not ok or not str(text).strip():
            return None
        return self._decide("note", "", str(text).strip())

    def _zoom_to_selected(self):
        r = self._selected_review()
        if r is None or self.iface is None:
            return False
        work = self.work_edit.text().strip()
        ref = wf.out(work, "18", "OUTP") or wf.out(work, "4", "OUTP") or wf.out(work, "16", "OUTP")
        lyr = self._read_layer(ref) if ref else None
        if lyr is None:
            return False
        try:
            from qgis.core import QgsCoordinateTransform, QgsProject, QgsRectangle
            names = lyr.fields().names()
            for f in lyr.getFeatures():
                if "dam_id" in names and str(f["dam_id"]) == r["dam_id"]:
                    g = f.geometry()
                    tr = QgsCoordinateTransform(lyr.crs(), self.iface.mapCanvas().mapSettings().destinationCrs(),
                                                QgsProject.instance())
                    g.transform(tr)
                    p = g.centroid().asPoint()
                    half = 400.0
                    self.iface.mapCanvas().setExtent(QgsRectangle(p.x() - half, p.y() - half, p.x() + half, p.y() + half))
                    self.iface.mapCanvas().refresh()
                    return True
        except Exception as e:                                   # noqa
            self._log("zoom: {}".format(e))
        return False

