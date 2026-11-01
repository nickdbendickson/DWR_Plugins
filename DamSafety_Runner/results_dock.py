"""QGIS dock that reads a finished dam folder. It does not run the model.

Opened from the Runner window button "Breach results", for the dam
folder that window has open. plugin.py creates the dock on the first
click and removes it in unload. It is not a plugin menu entry.

Works in DamSafety_Runner and nd_breach_runner. The dock looks for
``_breach_model/run_report.json``, ``receptors_flooded.csv`` and
``breach_hydrograph.csv``. "Open dashboard" writes dashboard.html with
``core/dashboard_html.py`` if that module is installed, then opens it.

The numbers are the run's own summary. This dock does not classify hazard.

3.13.6 (Docs & QA dock test, DWR-2801 r4): a Hazard line from DamSafety Hazard's own
Hazard_Classification_Summary.txt and dam.json (ND / TM 11 class, provisional, Hazard version,
status, next action, the class of record hook), a stale-summary warning, the structure count
labelled as the Runner's receptors with Hazard's flooded count beside it, layer-load buttons
and the run warnings as an expandable list. The reading is core/results_info.py.
"""
import csv
import json
import os

from .core import results_info as RI

from qgis.PyQt.QtCore import Qt, QUrl
from qgis.PyQt.QtGui import QDesktopServices
from qgis.PyQt.QtWidgets import (
    QDockWidget,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTableWidget,
    QListWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

# 3.13.4 (built as 3.13.3): dock areas as the scoped Qt enum (QGIS 4 / PyQt6 has no Qt.RightDockWidgetArea;
# fulltest 2026-10-30). plugin.py uses RIGHT_DOCK for addDockWidget.
_DWA = getattr(Qt, "DockWidgetArea", Qt)
LEFT_DOCK = _DWA.LeftDockWidgetArea
RIGHT_DOCK = _DWA.RightDockWidgetArea
_TIF = getattr(Qt, "TextInteractionFlag", Qt)
_TEXT_SELECTABLE = _TIF.TextSelectableByMouse


def _load_json(path):
    if not path or not os.path.isfile(path):
        return {}
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return data if isinstance(data, dict) else {}


def _find_up(start, name, levels=4):
    folder = start
    for _ in range(levels):
        path = os.path.join(folder, name)
        if os.path.isfile(path):
            return path
        parent = os.path.dirname(folder)
        if parent == folder:
            break
        folder = parent
    return None


def _summary(report):
    block = report.get("summary")
    if isinstance(block, dict) and block:
        return block
    return report if isinstance(report, dict) else {}


def _hydro_peak(path):
    peak = None
    rows = 0
    if not path:
        return peak, rows
    with open(path, encoding="utf-8") as f:
        for line in f:
            parts = [p.strip() for p in line.replace(";", ",").split(",")]
            if len(parts) <= 1:
                continue
            try:
                q = float(parts[1])
            except ValueError:
                continue
            rows += 1
            peak = q if peak is None else max(peak, q)
    return peak, rows


def _cell(value, digits=1):
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    try:
        return "{:.{d}f}".format(float(text), d=digits)
    except ValueError:
        return text


def _receptors(path):
    if not path:
        return []
    with open(path, encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            return []
        fields = {n.lower().strip(): n for n in reader.fieldnames if n}

        def col(*names):
            for n in names:
                if n in fields:
                    return fields[n]
            return None

        name_k = col("id", "name", "structure", "feature", "label")
        kind_k = col("kind", "type", "layer")
        depth_k = col("depth_ft", "depth", "max_depth_ft")
        vel_k = col("velocity_fps", "velocity", "vel_fps")
        arr_k = col("arrival_hr", "arrival", "arrival_h")
        rows = []
        for row in reader:
            rows.append([
                (row.get(name_k) or "").strip() if name_k else "",
                (row.get(kind_k) or "").strip() if kind_k else "",
                _cell(row.get(depth_k) if depth_k else None),
                _cell(row.get(vel_k) if vel_k else None),
                _cell(row.get(arr_k) if arr_k else None),
            ])
    return rows


class ResultsDock(QDockWidget):
    def __init__(self, iface, parent=None):
        super().__init__("Breach results", parent or iface.mainWindow())
        self.iface = iface
        self.setObjectName("NdBreachResultsDock")
        self._folder = None
        self._model_dir = None
        self.setAllowedAreas(LEFT_DOCK | RIGHT_DOCK)

        root = QWidget()
        lay = QVBoxLayout(root)
        self.summary = QLabel("Open a dam folder. The dock only reads files already written.")
        self.summary.setWordWrap(True)
        lay.addWidget(self.summary)

        buttons = QHBoxLayout()
        open_btn = QPushButton("Dam folder…")
        open_btn.clicked.connect(self.choose_folder)
        dash_btn = QPushButton("Open dashboard")
        dash_btn.clicked.connect(self.open_dashboard)
        buttons.addWidget(open_btn)
        buttons.addWidget(dash_btn)
        lay.addLayout(buttons)

        self.hazard = QLabel("")
        self.hazard.setWordWrap(True)
        self.hazard.setTextInteractionFlags(_TEXT_SELECTABLE)
        lay.addWidget(self.hazard)
        self.stale = QLabel("")
        self.stale.setWordWrap(True)
        self.stale.setStyleSheet("color: #b45309;")
        self.stale.setVisible(False)
        lay.addWidget(self.stale)

        layers = QHBoxLayout()
        self.layer_buttons = {}
        for key in RI.LAYER_KEYS:
            b = QPushButton(RI.LAYER_LABELS[key])
            b.setToolTip("Add this layer to the map")
            b.setEnabled(False)
            b.clicked.connect(lambda _=False, k=key: self.load_layer(k))
            layers.addWidget(b)
            self.layer_buttons[key] = b
        lay.addLayout(layers)

        self.warn_toggle = QToolButton()
        self.warn_toggle.setCheckable(True)
        self.warn_toggle.setVisible(False)
        self.warn_toggle.toggled.connect(self._show_warnings)
        lay.addWidget(self.warn_toggle)
        self.warn_list = QListWidget()
        self.warn_list.setWordWrap(True)
        self.warn_list.setVisible(False)
        lay.addWidget(self.warn_list)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(
            ["Feature", "Kind", "Depth ft", "Vel ft/s", "Arrival h"])
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        lay.addWidget(self.table, 1)
        self.setWidget(root)

    def choose_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Dam folder or _breach_model")
        if folder:
            self.set_folder(folder)

    def set_folder(self, folder):
        self._folder = folder
        report_path = None
        if os.path.basename(folder) == "run_report.json":
            report_path = folder
        else:
            report_path = _find_up(folder, os.path.join("_breach_model", "run_report.json"))
            if report_path is None:
                report_path = _find_up(folder, "run_report.json")
        model_dir = os.path.dirname(report_path) if report_path else folder
        report = _load_json(report_path) if report_path else {}
        summary = _summary(report)
        dam = report.get("dam") if isinstance(report.get("dam"), dict) else {}
        hydro_path = _find_up(model_dir, "breach_hydrograph.csv")
        peak, nrows = _hydro_peak(hydro_path)
        if summary.get("peak_breach_cfs") is not None:
            try:
                peak = float(summary["peak_breach_cfs"])
            except (TypeError, ValueError):
                pass
        rec_path = _find_up(model_dir, "receptors_flooded.csv")
        rows = _receptors(rec_path)
        dam_id = summary.get("dam_id") or dam.get("dam_id") or os.path.basename(
            os.path.dirname(model_dir))
        bits = [str(dam_id)]
        if dam.get("name"):
            bits.append(str(dam["name"]))
        if summary.get("scenario"):
            bits.append(str(summary["scenario"]))
        method = summary.get("breach_method")
        if method:
            bits.append(str(method))
        solver = summary.get("final_model") or report.get("flood_engine_short")
        if solver:
            bits.append(str(solver))
        if peak is not None:
            bits.append("peak {:.0f} cfs".format(peak))
        if summary.get("flooded_acres") is not None:
            bits.append("{:.0f} ac".format(float(summary["flooded_acres"])))
        if summary.get("max_depth_ft") is not None:
            bits.append("max depth {:.1f} ft".format(float(summary["max_depth_ft"])))
        dam_dir = os.path.dirname(model_dir) if os.path.basename(model_dir) == "_breach_model" \
            else model_dir
        hz = RI.hazard_summary(dam_dir)
        if summary.get("receptors_flooded"):
            bits.append(_relabel_receptors(summary["receptors_flooded"], report, hz))
        warns = RI.warning_texts(report)
        if not report_path:
            bits.append("no run_report.json in this folder")
        if hydro_path and nrows:
            bits.append("{} hydrograph rows".format(nrows))
        self.summary.setText(" · ".join(bits))
        self._show_hazard(dam_dir, model_dir, hz)
        self._fill_warnings(warns)
        self._report = report
        self._layers = RI.layer_paths(dam_dir, model_dir, report, hz)
        for key, b in self.layer_buttons.items():
            b.setEnabled(key in self._layers)
            b.setToolTip(self._layers.get(key, "Not in this dam folder"))
        self.table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            for c, value in enumerate(row):
                self.table.setItem(r, c, QTableWidgetItem(value))
        self.table.resizeColumnsToContents()
        self._model_dir = model_dir

    def _show_hazard(self, dam_dir, model_dir, hz):
        run_t = RI.run_output_mtime(dam_dir, model_dir)
        stale = RI.staleness(hz, _installed_hazard(), run_t)
        newer = bool(hz and run_t is not None and hz.get("mtime", run_t) < run_t - 1.0)
        rec = RI.record_status(dam_dir, {"rasters_newer_than_class": newer})
        lines = [RI.class_line(hz, rec)]
        if rec.get("status"):
            lines.append("Status: {}{}".format(
                rec["status"], " (by {} {})".format(rec.get("classified_by"),
                                                    rec.get("classified_version"))
                if rec.get("classified_by") else ""))
        if rec.get("next_action"):
            lines.append("Next: {}".format(rec["next_action"][1]))
        if not rec.get("has_record"):
            lines.append("No dam.json in this folder.")
        self.hazard.setText("\n".join(lines))
        self.stale.setText("\n".join("Stale: " + w for w in stale))
        self.stale.setVisible(bool(stale))

    def _fill_warnings(self, warns):
        self.warn_list.clear()
        self.warn_list.addItems(warns)
        self._nwarn = len(warns)
        self.warn_toggle.setVisible(bool(warns))
        self.warn_toggle.setChecked(False)
        self._show_warnings(False)

    def _show_warnings(self, on):
        n = getattr(self, "_nwarn", 0)
        self.warn_toggle.setText("{} {} thing(s) to check".format("▾" if on else "▸", n))
        self.warn_list.setVisible(bool(on) and n > 0)

    def load_layer(self, key):
        path = (getattr(self, "_layers", None) or {}).get(key)
        if not path:
            return None
        from qgis.core import QgsProject, QgsRasterLayer, QgsVectorLayer
        dam = os.path.basename(os.path.dirname(self._model_dir or "")) or ""
        name = "{} {}".format(dam, RI.LAYER_LABELS[key]).strip()
        if key in RI.FINAL_NAMES:
            lyr = QgsRasterLayer(path, name)
        elif key == "receptors":
            crs = (getattr(self, "_report", None) or {}).get("work_crs")
            lyr = QgsVectorLayer(RI.receptor_csv_uri(path, crs), name, "delimitedtext")
        else:
            lyr = QgsVectorLayer(path, name, "ogr")
        if not lyr.isValid():
            self.summary.setText("Could not open {}".format(path))
            return None
        QgsProject.instance().addMapLayer(lyr)
        return lyr

    def open_dashboard(self):
        folder = self._model_dir or self._folder
        if not folder:
            self.choose_folder()
            folder = self._model_dir
        if not folder:
            return
        path = os.path.join(folder, "dashboard.html")
        write_dashboard = _load_writer()
        if write_dashboard is not None:
            path = write_dashboard(folder)
        if os.path.isfile(path):
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))
        else:
            self.summary.setText(
                "No dashboard.html yet. Add core/dashboard_html.py so this button can write it."
            )


def _relabel_receptors(text, report, hz):
    """'37 structures, 18.5 km of road' -> '37 Runner receptor structure(s), 18.5 km of road
    (Hazard: 45 flooded habitable)': the Runner counts its receptor points, Hazard its own."""
    parts = [p.strip() for p in str(text).split(",")]
    m = parts[0].split()
    if len(m) == 2 and m[1] == "structures" and m[0].isdigit():
        parts[0] = RI.structure_count_text(int(m[0]), {})
    out = ", ".join(parts)
    n_haz = (hz or {}).get("flooded_structures")
    if n_haz is not None:
        out += " (Hazard: {} flooded habitable)".format(n_haz)
    return out


def _installed_hazard():
    try:
        from .damsuite import versions
        v = versions.installed("ndhazard")
    except Exception:
        v = None
    if v:
        return v
    # not loaded in this QGIS: the Hazard plugin folder beside this one (same plugins folder)
    meta = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "DamSafety_Hazard", "metadata.txt")
    try:
        with open(meta, encoding="utf-8") as f:
            for line in f:
                if line.startswith("version="):
                    return line.split("=", 1)[1].strip() or None
    except OSError:
        pass
    return None


def _load_writer():
    try:
        from .core.dashboard_html import write_dashboard
        return write_dashboard
    except Exception:
        pass
    try:
        from .dashboard_html import write_dashboard
        return write_dashboard
    except Exception:
        return None
