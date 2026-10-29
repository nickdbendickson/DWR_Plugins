# -*- coding: utf-8 -*-
"""'Get basin data from StreamStats…': the PMP window that fills the basin inputs of the Inflow
hydrograph tool from USGS StreamStats for the dam's outlet point.

The user clicks the outlet on the map (or takes the dam point from a point layer), and the tool
  - delineates the basin and loads it as a layer (for the PMP tool's 'Basin polygon'),
  - fills the drainage area, a SUGGESTED non-contributing area (isolated lakes / potholes) and
    Tc / lag (Kirpich, or NRCS lag with the user's CN),
  - shows a read-only sanity check: the ND peak flows 50%..0.2% AEP (SIR 2015-5096, each with an
    approximate 90% range from its standard error) and the Crippen & Bue (1977) envelope.
The curve number is NOT taken from StreamStats. The web requests run in a QgsTask (progress,
Cancel); all computation lives in core/streamstats.py (no QGIS)."""
import os
import re

from qgis.PyQt.QtCore import QObject, pyqtSignal
from qgis.PyQt.QtWidgets import (QAbstractItemView, QComboBox, QDialog, QDoubleSpinBox,
                                 QFileDialog, QFormLayout, QGridLayout, QGroupBox, QHBoxLayout,
                                 QHeaderView, QLabel, QLineEdit, QPlainTextEdit, QProgressBar,
                                 QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)
from qgis.core import (QgsApplication, QgsCoordinateReferenceSystem, QgsCoordinateTransform,
                       QgsMessageLog, QgsProject, QgsSettings, QgsTask, QgsVectorLayer)

from .core import streamstats as ss
from .qgis_transport import make_fetcher
from .tokens import TOKENS as T
from .ui_fit import FitScroll, fit_window
from .ui_widgets import ERROR, OK, WARNING, CheckPanel, Collapsible, InfoBanner, WrapLabel

TITLE = "Get basin data from StreamStats\u2026"
S_FOLDER = "nd_pmp/streamstats_folder"
S_METHOD = "nd_pmp/streamstats_tc_method"
ND_BOX = (45.9, 49.05, -104.1, -96.5)      # lat min/max, lon min/max (with a small margin)
DAM_ID_FIELDS = ("DWR_ID", "dwr_id", "DAM_ID", "dam_id", "NIDID", "nidid", "NID_ID", "nid_id")
METHODS = [("kirpich", "Kirpich (flow path length and slope)"),
           ("nrcs", "NRCS lag (needs your CN)")]


def plugin_version():
    import configparser
    cp = configparser.ConfigParser(interpolation=None)
    cp.read(os.path.join(os.path.dirname(__file__), "metadata.txt"), encoding="utf-8")
    return cp.get("general", "version", fallback="?")


def safe_name(text):
    return re.sub(r"[^A-Za-z0-9_-]+", "_", (text or "").strip()).strip("_") or "basin"


def _is_point_layer(layer):
    try:
        gt = layer.geometryType()
    except Exception:                                                 # noqa
        return False
    return int(getattr(gt, "value", gt)) == 0                         # Point in Qt5 and Qt6 enums


def _fmt(v, nd=0):
    return "\u2014" if v is None else ("{:,.%df}" % nd).format(v)


def default_client(cache_dir, task):
    """The plugin's client: QGIS networking (QGIS proxy / auth / SSL settings), urllib only when
    QGIS is not running."""
    fetcher, transport = make_fetcher()
    if task is not None:
        task.fetcher = fetcher               # so Cancel also aborts the request in flight
    return ss.StreamStatsClient(cache_dir, fetcher=fetcher, transport=transport,
                                plugin_version=plugin_version(),
                                should_cancel=task.isCanceled if task is not None else None,
                                progress=task.report if task is not None else None)


class StreamStatsTask(QgsTask):
    """Runs one StreamStats query off the GUI thread. `job(task)` returns the result."""

    def __init__(self, job, on_done):
        super().__init__("StreamStats basin data", QgsTask.Flag.CanCancel
                         if hasattr(QgsTask, "Flag") else QgsTask.CanCancel)
        self.job, self.on_done = job, on_done
        self.fetcher = None
        self.result, self.error, self.message = None, None, ""

    def cancel(self):
        stop = getattr(self.fetcher, "cancel", None)
        if stop is not None:
            stop()                            # abort the QGIS request in flight
        super().cancel()

    def report(self, message, pct):           # called by the client (worker thread)
        self.message = message
        self.setProgress(float(pct))

    def run(self):
        try:
            self.result = self.job(self)
            return True
        except Exception as e:                                        # noqa
            self.error = e
            return False

    def finished(self, ok):                   # main thread
        self.on_done(self)


class _PointPicker(QObject):
    picked = pyqtSignal(float, float)          # lat, lon (WGS84)

    def __init__(self, canvas):
        super().__init__()
        from qgis.gui import QgsMapToolEmitPoint
        self.canvas = canvas
        self.prev = canvas.mapTool()
        self.tool = QgsMapToolEmitPoint(canvas)
        self.tool.canvasClicked.connect(self._clicked)

    def start(self):
        self.prev = self.canvas.mapTool()
        self.canvas.setMapTool(self.tool)

    def _clicked(self, pt, _button):
        src = self.canvas.mapSettings().destinationCrs()
        xf = QgsCoordinateTransform(src, QgsCoordinateReferenceSystem("EPSG:4326"),
                                    QgsProject.instance())
        p = xf.transform(pt)
        if self.prev is not None and self.prev is not self.tool:
            self.canvas.setMapTool(self.prev)
        else:
            self.canvas.unsetMapTool(self.tool)
        self.picked.emit(p.y(), p.x())


class StreamStatsDialog(QDialog):
    """`client_factory(cache_dir, task)` and `run_sync` are for the tests (no network, no
    task manager)."""

    def __init__(self, parent=None, iface=None, client_factory=None, run_sync=False):
        super().__init__(parent)
        self.iface = iface
        self.client_factory = client_factory or default_client
        self.run_sync = run_sync
        self.task = None
        self.result = None
        self.inputs = None
        self.saved = None
        self.layer_id = None
        self.picker = None
        self.setWindowTitle("DamSafety PMP \u2014 Get basin data from StreamStats")
        self._build()
        fit_window(self, prefer=(820, 900))

    # ------------------------------------------------------------------ layout
    def _build(self):
        outer = QVBoxLayout(self)
        outer.addWidget(InfoBanner(
            "USGS StreamStats outlines the basin above a point on its stream network and runs the "
            "North Dakota peak-flow equations (USGS SIR 2015-5096). The values below are starting "
            "points for the PMF inputs: check them. Needs the internet; requests are sent one at "
            "a time, 2 s apart. North Dakota only. StreamStats has no PMF and no curve number."))
        page = QWidget()
        body = QVBoxLayout(page)
        body.setContentsMargins(0, 0, 0, 0)

        # 1 outlet point
        g1 = QGroupBox("1. Outlet point: on the channel just below the dam")
        f1 = QGridLayout(g1)
        self.pick_btn = QPushButton("Pick on map\u2026")
        self.pick_btn.clicked.connect(self.pick_on_map)
        if self.iface is None:
            self.pick_btn.setEnabled(False)
            self.pick_btn.setToolTip("No QGIS map here: type the point or use a layer")
        else:
            self.pick_btn.setToolTip("Click the creek just below the dam on the map")
        self.layer_combo = QComboBox()
        self.layer_combo.setToolTip("A point layer with the dam (the selected feature, or the "
                                    "first one)")
        self.layer_btn = QPushButton("Use the dam point")
        self.layer_btn.clicked.connect(self.use_layer_point)
        self.lat = QDoubleSpinBox()
        self.lon = QDoubleSpinBox()
        for sp, lo, hi in ((self.lat, 40.0, 50.0), (self.lon, -110.0, -90.0)):
            sp.setDecimals(7)
            sp.setRange(lo, hi)
            sp.setSingleStep(0.0005)
            sp.setToolTip("Decimal degrees, WGS84 (EPSG:4326)")
        self.lat.setValue(47.0)
        self.lon.setValue(-100.0)
        self.lat.valueChanged.connect(self._inputs_changed)
        self.lon.valueChanged.connect(self._inputs_changed)
        self.name = QLineEdit("basin")
        self.name.setToolTip("Used in the file and layer names, e.g. the DWR dam ID")
        self.folder = QLineEdit(QgsSettings().value(S_FOLDER, "") or "")
        self.folder.setPlaceholderText("Folder for the basin outline, values and provenance")
        browse = QPushButton("Browse\u2026")
        browse.clicked.connect(self._browse)
        self.layer_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.layer_combo.setMinimumContentsLength(10)
        f1.addWidget(self.pick_btn, 0, 0)
        f1.addWidget(self._note("click the creek just below the dam"), 0, 1, 1, 3)
        f1.addWidget(QLabel("or a dam point layer"), 1, 0)
        f1.addWidget(self.layer_combo, 1, 1, 1, 2)
        f1.addWidget(self.layer_btn, 1, 3)
        f1.addWidget(QLabel("Latitude (\u00b0)"), 2, 0)
        f1.addWidget(self.lat, 2, 1)
        f1.addWidget(QLabel("Longitude (\u00b0)"), 2, 2)
        f1.addWidget(self.lon, 2, 3)
        f1.addWidget(QLabel("Name"), 4, 0)
        f1.addWidget(self.name, 4, 1, 1, 3)
        f1.addWidget(QLabel("Save in folder"), 5, 0)
        f1.addWidget(self.folder, 5, 1, 1, 2)
        f1.addWidget(browse, 5, 3)
        hint = WrapLabel("Tip: a dam's mapped point is often on the embankment or in the "
                         "reservoir, off the stream line. If StreamStats says the point is not "
                         "on the stream network, zoom in and click the creek just downstream.")
        hint.setStyleSheet("color:%s;" % T["muted"])
        f1.addWidget(hint, 3, 0, 1, 4)
        body.addWidget(g1)

        # 2 basin inputs
        g2 = QGroupBox("2. Basin inputs for the Inflow hydrograph tool (you can edit them)")
        f2 = QFormLayout(g2)
        self.area = self._spin(0, 100000, 3)
        self.noncontrib = self._spin(0, 100000, 3)
        self.area_note = self._note("StreamStats DRNAREA: the whole delineated basin.")
        self.noncontrib_note = self._note(
            "Suggested starting value only: StreamStats ISOLAKESUM, the area that drains to "
            "isolated lakes and potholes. You decide the non-contributing area.")
        self.method = QComboBox()
        for _k, text in METHODS:
            self.method.addItem(text)
        saved = QgsSettings().value(S_METHOD, "kirpich")
        self.method.setCurrentIndex(1 if saved == "nrcs" else 0)
        self.method.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.method.setMinimumContentsLength(12)
        self.method.currentIndexChanged.connect(self._update_timing)
        self.cn = self._spin(0, 99, 1)
        self.cn.setSpecialValueText("not set")
        self.cn.setToolTip("Your curve number (e.g. from SSURGO); used only for the NRCS lag. "
                           "It is not sent to the Inflow hydrograph tool.")
        self.cn.valueChanged.connect(self._update_timing)
        self.tc = self._spin(0, 1000, 2)
        self.lag = self._spin(0, 1000, 2)
        self.timing_note = self._note("")
        cn_note = self._note("Curve number: not from StreamStats. Keep your own (SSURGO / land "
                             "cover) value in the Inflow hydrograph tool.")
        self.layer_note = self._note("Basin layer: not loaded yet")
        f2.addRow("Drainage area (mi\u00b2)", self.area)
        f2.addRow("", self.area_note)
        f2.addRow("Non-contributing (mi\u00b2), suggested", self.noncontrib)
        f2.addRow("", self.noncontrib_note)
        f2.addRow("Tc / lag method", self.method)
        f2.addRow("CN for the NRCS lag (yours)", self.cn)
        f2.addRow("Time of concentration Tc (h)", self.tc)
        f2.addRow("SCS lag (h)", self.lag)
        f2.addRow("", self.timing_note)
        f2.addRow("", cn_note)
        f2.addRow("", self.layer_note)
        body.addWidget(g2)

        # 3 sanity check
        g3 = QGroupBox("3. Sanity check (read-only): ND peak flows and the maximum-flood envelope")
        v3 = QVBoxLayout(g3)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["AEP", "Peak cfs", "~90% range cfs", "Std error %"])
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.setMinimumHeight(120)
        v3.addWidget(self.table)
        self.sanity_note = self._note(
            "Regression peaks are for rural, unregulated basins with inputs inside the ranges of "
            "SIR 2015-5096 (standard errors 51-83%); the ~90% range uses the average standard "
            "error only. The Crippen & Bue envelope is the largest observed US floods for the "
            "area, not a PMF. After the PMF run, the Inflow hydrograph tool compares its peak with "
            "these (a warning, never a block).")
        v3.addWidget(self.sanity_note)
        body.addWidget(g3)
        body.addStretch(1)
        scroll = FitScroll()
        scroll.setWidgetResizable(True)
        scroll.setWidget(page)
        outer.addWidget(scroll, 1)

        # checks, progress
        self.check_btn = QPushButton("Check inputs")
        self.check_btn.clicked.connect(self.check_inputs)
        self.run_btn = QPushButton("Get basin data")
        self.run_btn.setStyleSheet("font-weight:bold;")
        self.run_btn.setToolTip("Ask StreamStats for the basin above the point")
        self.run_btn.clicked.connect(self.start)
        self.checks = CheckPanel(self.check_btn, self.run_btn, action="asking StreamStats")
        outer.addWidget(self.checks)
        prow = QHBoxLayout()
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.status = QLabel("Ready")
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.setToolTip("Only while StreamStats is being asked")
        self.cancel_btn.clicked.connect(self.cancel)
        prow.addWidget(self.progress, 1)
        prow.addWidget(self.status, 2)
        prow.addWidget(self.cancel_btn)
        outer.addLayout(prow)

        # action bar
        bar = QHBoxLayout()
        self.open_pmp_btn = QPushButton("Open PMP tool\u2026")
        self.open_hydro_btn = QPushButton("Open Inflow hydrograph\u2026")
        for b in (self.open_pmp_btn, self.open_hydro_btn):
            b.setEnabled(False)
            b.setToolTip("Get the basin data first")
        self.open_pmp_btn.setWhatsThis("Opens the PMP tool with the StreamStats basin as its "
                                       "basin polygon")
        self.open_hydro_btn.setWhatsThis("Opens the Inflow hydrograph tool with the area, the "
                                         "non-contributing area and Tc / lag filled in (not the "
                                         "curve number)")
        self.open_pmp_btn.clicked.connect(self.open_pmp)
        self.open_hydro_btn.clicked.connect(self.open_hydro)
        bar.addWidget(self.open_pmp_btn)
        bar.addWidget(self.open_hydro_btn)
        bar.addStretch(1)
        bar.addWidget(self.check_btn)
        bar.addWidget(self.run_btn)
        outer.addLayout(bar)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumHeight(140)
        foot = QHBoxLayout()
        foot.addWidget(Collapsible("Log", self.log, expanded=False), 1)
        close = QPushButton("Close")
        close.clicked.connect(self.close)
        outer.addLayout(foot)
        crow = QHBoxLayout()
        crow.addStretch(1)
        crow.addWidget(close)
        outer.addLayout(crow)
        self.refresh_layers()

    def _spin(self, lo, hi, dec):
        s = QDoubleSpinBox()
        s.setRange(lo, hi)
        s.setDecimals(dec)
        return s

    def _note(self, text):
        lbl = WrapLabel(text)
        lbl.setStyleSheet("color:%s;" % T["muted"])
        return lbl

    def _log(self, text):
        self.log.appendPlainText(text)
        try:
            QgsMessageLog.logMessage(text, "DamSafety PMP")
        except Exception:                                             # noqa
            pass

    # ------------------------------------------------------------------ point
    def refresh_layers(self):
        self.layer_combo.clear()
        for lyr in QgsProject.instance().mapLayers().values():
            if isinstance(lyr, QgsVectorLayer) and _is_point_layer(lyr):
                self.layer_combo.addItem(lyr.name(), lyr.id())
        has = self.layer_combo.count() > 0
        self.layer_btn.setEnabled(has)
        self.layer_btn.setToolTip("Takes the selected feature, or the first one" if has else
                                  "No point layer in the project: add the DWR dams layer")

    def showEvent(self, event):
        super().showEvent(event)
        self.refresh_layers()

    def _browse(self):
        d = QFileDialog.getExistingDirectory(self, "Save the StreamStats results in",
                                             self.folder.text())
        if d:
            self.folder.setText(d)

    def pick_on_map(self):
        if self.iface is None:
            return
        if self.picker is None:
            self.picker = _PointPicker(self.iface.mapCanvas())
            self.picker.picked.connect(self.set_point)
        self.picker.start()
        self.status.setText("Click the creek just below the dam on the map")
        self.showMinimized()

    def set_point(self, lat, lon, name=None):
        self.lat.setValue(lat)
        self.lon.setValue(lon)
        if name:
            self.name.setText(safe_name(name))
        self.status.setText("Point %.6f, %.6f" % (lat, lon))
        if self.isMinimized():
            self.showNormal()
        self.raise_()

    def use_layer_point(self):
        lid = self.layer_combo.currentData()
        lyr = QgsProject.instance().mapLayer(lid) if lid else None
        if lyr is None:
            return
        feats = list(lyr.selectedFeatures()) or [next(lyr.getFeatures(), None)]
        f = feats[0]
        if f is None or not f.hasGeometry():
            self.checks.set_results([(ERROR, "Layer %s has no point to use" % lyr.name())])
            return
        xf = QgsCoordinateTransform(lyr.crs(), QgsCoordinateReferenceSystem("EPSG:4326"),
                                    QgsProject.instance())
        p = xf.transform(f.geometry().centroid().asPoint())
        names = [n for n in DAM_ID_FIELDS if n in lyr.fields().names()]
        self.set_point(p.y(), p.x(), str(f[names[0]]) if names and f[names[0]] else None)
        if not lyr.selectedFeatureCount() and lyr.featureCount() > 1:
            self._log("No feature selected in %s: used its first point. Select the dam to use "
                      "another." % lyr.name())

    def _inputs_changed(self, *_):
        checks = getattr(self, "checks", None)
        if checks is not None and checks.blocked:
            checks.clear()

    # ------------------------------------------------------------------ checks / run
    def input_problems(self):
        out = []
        lat, lon = self.lat.value(), self.lon.value()
        if not (ND_BOX[0] <= lat <= ND_BOX[1] and ND_BOX[2] <= lon <= ND_BOX[3]):
            out.append((ERROR, "The point (%.4f, %.4f) is outside North Dakota; this tool asks "
                               "the ND StreamStats region only." % (lat, lon)))
        folder = self.folder.text().strip()
        if not folder:
            out.append((ERROR, "Choose a folder to save the results in (Browse\u2026)."))
        elif os.path.exists(folder) and not os.path.isdir(folder):
            out.append((ERROR, "%s is a file, not a folder." % folder))
        if not out:
            out.append((OK, "Point in North Dakota, folder set"))
        return out

    def check_inputs(self):
        res = self.input_problems()
        self.checks.set_results(res)
        return not any(lvl == ERROR for lvl, _t in res)

    def start(self):
        if not self.check_inputs():
            return
        folder = self.folder.text().strip()
        os.makedirs(folder, exist_ok=True)
        QgsSettings().setValue(S_FOLDER, folder)
        name = safe_name(self.name.text())
        lat, lon = self.lat.value(), self.lon.value()
        cache = os.path.join(folder, "streamstats_cache")
        factory = self.client_factory
        self._log("StreamStats: %s at %.6f, %.6f (cache %s)" % (name, lat, lon, cache))

        def job(task):
            client = factory(cache, task)
            return client.run("ND", lat, lon, label=name + "_")

        self.run_btn.setEnabled(False)
        self.cancel_btn.setEnabled(True)
        self.progress.setValue(0)
        self.status.setText("Asking StreamStats\u2026")
        task = StreamStatsTask(job, self._done)
        task.progressChanged.connect(self._progress)
        self.task = task
        if self.run_sync:
            task.finished(task.run())
        else:
            QgsApplication.taskManager().addTask(task)

    def _progress(self, pct):
        self.progress.setValue(int(pct))
        if self.task is not None and self.task.message:
            self.status.setText(self.task.message)

    def cancel(self):
        if self.task is not None:
            self.task.cancel()
            self.status.setText("Cancelling\u2026")

    def _done(self, task):
        self.task = None
        self.cancel_btn.setEnabled(False)
        self.run_btn.setEnabled(True)
        if task.error is not None or task.result is None:
            err = task.error
            if isinstance(err, ss.Cancelled) or (err is None and task.isCanceled()):
                self.status.setText("Cancelled. Nothing was changed.")
                self._log("Cancelled")
                return
            msg = str(err) if err is not None else "StreamStats did not answer"
            if isinstance(err, ss.NotOnStreamError):
                self.status.setText("Not on the stream network")
                self._log(msg + (" (" + err.detail + ")" if err.detail else ""))
            elif isinstance(err, ss.NetworkError):
                self.status.setText("Could not reach StreamStats")
                self._log(msg + " [" + err.url + "]")
            else:
                msg = "StreamStats failed: " + msg
                self.status.setText("Failed")
                self._log(msg)
            self.checks.set_results([(ERROR, msg)])
            return
        self.apply_result(task.result)

    # ------------------------------------------------------------------ results
    def apply_result(self, result):
        self.result = result
        cn = self.cn.value() or None
        self.inputs = ss.pmf_inputs(result, cn)
        inp = self.inputs
        self.area.setValue(inp["area_mi2"] or 0.0)
        self.noncontrib.setValue(inp["noncontrib_mi2_suggested"] or 0.0)
        self.noncontrib_note.setText(
            "Suggested starting value only: %s = %s mi\u00b2. You decide the non-contributing area."
            % (inp["noncontrib_source"], _fmt(inp["noncontrib_mi2_suggested"], 2)))
        self.area_note.setText("StreamStats DRNAREA: the whole delineated basin (outline %s mi\u00b2)."
                               % _fmt(result.get("geodesic_area_sqmi_local"), 2))
        self._update_timing()
        self._fill_table(result)
        self.saved = self._save()
        self._load_layer()
        problems = []
        if self.result.get("warning"):
            problems.append((WARNING, "StreamStats: " + self.result["warning"]))
        flags = ss.sanity_summary(result, inp)["out_of_range"]
        if flags:
            problems.append((WARNING, "Outside the equations' ranges (less reliable): "
                             + "; ".join(flags)))
        problems.append((OK, "Basin delineated"))
        self.checks.set_results(problems)
        for b in (self.open_pmp_btn, self.open_hydro_btn):
            b.setEnabled(True)
            b.setToolTip(b.whatsThis())
        self.progress.setValue(100)
        self.status.setText("Done: %s mi\u00b2" % _fmt(inp["area_mi2"], 2))
        self._log("Saved %s" % ", ".join(str(p) for p in (self.saved or {}).values()))

    def timing_method(self):
        return METHODS[self.method.currentIndex()][0]

    def _update_timing(self, *_):
        QgsSettings().setValue(S_METHOD, self.timing_method())
        if self.result is None:
            return
        self.inputs = ss.pmf_inputs(self.result, self.cn.value() or None)
        tm = ss.timing(self.inputs, self.timing_method())
        self.tc.setValue(tm["tc_hr"] or 0.0)
        self.lag.setValue(tm["lag_hr"] or 0.0)
        self.timing_note.setText("Method used: " + tm["note"])
        self.timing = tm

    def _fill_table(self, result):
        peaks = ss.peak_table(result)
        env = ss.envelope_cfs(result)
        self.table.setRowCount(len(peaks) + 1)
        for i, p in enumerate(peaks):
            rng = "%s \u2013 %s" % (_fmt(p["pi90_lo_cfs"]), _fmt(p["pi90_hi_cfs"])) \
                if p["pi90_lo_cfs"] else "\u2014"
            for c, t in enumerate([p["aep"], _fmt(p["q_cfs"]), rng, _fmt(p["asep_pct"], 1)]):
                self.table.setItem(i, c, QTableWidgetItem(t))
        r = len(peaks)
        for c, t in enumerate(["Crippen & Bue max", _fmt(env), "envelope, not a PMF", ""]):
            self.table.setItem(r, c, QTableWidgetItem(t))
        self.table.resizeRowsToContents()
        h = self.table.horizontalHeader().height() + 2 * self.table.frameWidth() + sum(
            self.table.rowHeight(i) for i in range(self.table.rowCount()))
        self.table.setMinimumHeight(h + 2)
        regions = ", ".join("%s (%s%%)" % (x.get("code"), _fmt(x.get("percentWeight"), 0))
                            for x in result.get("regression_regions", []))
        self._log("Regression regions: %s" % regions)

    def _save(self):
        folder = self.folder.text().strip()
        try:
            return ss.save_run(self.result, folder, safe_name(self.name.text()),
                               cn=self.cn.value() or None, timing_method=self.timing_method(),
                               plugin_version=plugin_version())
        except Exception as e:                                        # noqa
            self._log("Could not save the results: %s" % e)
            return None

    def _load_layer(self):
        if not self.saved:
            return
        path = str(self.saved["watershed"])
        name = "StreamStats basin %s" % safe_name(self.name.text())
        lyr = QgsVectorLayer(path, name, "ogr")
        if not lyr.isValid():
            self.layer_note.setText("Basin layer: could not load %s" % path)
            return
        old = QgsProject.instance().mapLayer(self.layer_id) if self.layer_id else None
        if old is not None:
            QgsProject.instance().removeMapLayer(old.id())
        QgsProject.instance().addMapLayer(lyr)
        self.layer_id = lyr.id()
        self.layer_note.setText("Basin layer: '%s' loaded (%s)" % (name, os.path.basename(path)))

    # ------------------------------------------------------------------ hand-over
    def basin_json(self):
        return str(self.saved["basin"]) if self.saved else ""

    def hydro_params(self):
        """The Inflow hydrograph tool's fields this window fills (CN is left alone)."""
        if self.saved:   # re-save so the file has the edited timing method / CN
            self.saved = self._save() or self.saved
        return {"AREA_MI2": self.area.value(), "NONCONTRIB_MI2": self.noncontrib.value(),
                "TRANSFORM": 0, "TC_HR": self.tc.value(), "LAG_HR": self.lag.value(),
                "STREAMSTATS_JSON": self.basin_json()}

    def pmp_params(self):
        lyr = QgsProject.instance().mapLayer(self.layer_id) if self.layer_id else None
        return {"BASIN": lyr.id() if lyr is not None else str(self.saved["watershed"])}

    def open_hydro(self):
        import processing
        processing.execAlgorithmDialog("ndpmp:inflow_hydrograph", self.hydro_params())

    def open_pmp(self):
        import processing
        processing.execAlgorithmDialog("ndpmp:pmp", self.pmp_params())

    def closeEvent(self, event):
        if self.task is not None:
            self.task.cancel()
        super().closeEvent(event)
