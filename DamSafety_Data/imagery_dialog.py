"""1.5.0: the "Download imagery for these dams…" window (imagery.py does the work): NAIP
around each dam of a dams layer (all, the selected ones, or a list of ids), one JPEG COG per
dam in <output folder>/imagery."""
import threading

from qgis.core import (
    QgsApplication, QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsMessageLog,
    QgsProject, QgsTask)
from qgis.PyQt.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDoubleSpinBox, QFileDialog, QGridLayout, QHBoxLayout, QLabel,
    QLineEdit, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QVBoxLayout)
from qgis.PyQt.QtCore import QTimer

from . import imagery as IM
from .qtcompat import MSG_INFO, MSG_WARNING, TASK_CAN_CANCEL, _first
from .ui_fit import fit_window

TAG = "DamSafety Data"
SETTING = "nd_data_updater/imagery_out"
RES_LABELS = (("0.6 m (native NAIP)", 0.6), ("1 m", 1.0), ("2 m", 2.0))


def _vector_filter():
    from qgis.core import Qgis, QgsMapLayerProxyModel  # noqa: F401 - one of them exists
    return _first(lambda: Qgis.LayerFilter.VectorLayer,
                  lambda: QgsMapLayerProxyModel.Filter.VectorLayer,
                  lambda: QgsMapLayerProxyModel.VectorLayer)


def layer_dams(layer, id_field=None, selected=False, only_ids=None):
    """[(dam_id, lon, lat)] of a QGIS vector layer (points, or the centre of a line/polygon)."""
    names = [f.name() for f in layer.fields()]
    fld = id_field or IM.pick_id_field(names)
    tr = QgsCoordinateTransform(layer.crs(), QgsCoordinateReferenceSystem("EPSG:4326"),
                                QgsProject.instance())
    want = {str(i).strip() for i in only_ids} if only_ids else None
    feats = layer.getSelectedFeatures() if selected else layer.getFeatures()
    out = []
    for f in feats:
        g = f.geometry()
        if g is None or g.isEmpty():
            continue
        did = f[fld] if fld else f.id()
        if did is None or str(did) in ("", "NULL"):
            did = f.id()
        if want is not None and str(did).strip() not in want:
            continue
        p = tr.transform(g.centroid().asPoint())
        out.append((str(did), p.x(), p.y()))
    return out


class ImageryTask(QgsTask):
    def __init__(self, kwargs, on_done):
        super().__init__("Download dam imagery", TASK_CAN_CANCEL)
        self.kwargs = kwargs
        self.on_done = on_done
        self.lines = []
        self.lock = threading.Lock()
        self.result = None
        self.error = ""

    def log(self, text):
        with self.lock:
            self.lines.append(str(text))
        QgsMessageLog.logMessage(str(text), TAG, MSG_INFO)

    def take(self):
        with self.lock:
            out, self.lines = self.lines, []
        return out

    def run(self):
        try:
            self.result = IM.download(log=self.log, progress=self.setProgress,
                                      cancel=self.isCanceled, **self.kwargs)
            return True
        except Exception as e:                                   # noqa: BLE001 - shown
            self.error = str(e) or e.__class__.__name__
            self.log("STOPPED: " + self.error)
            return False

    def finished(self, ok):
        self.on_done(self, ok)


class ImageryDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("DamSafety Data - Download imagery for these dams")
        self.task = None
        lay = QVBoxLayout(self)
        intro = QLabel(
            "Downloads NAIP aerial imagery (USGS The National Map, the newest NAIP year, 0.6 m "
            "in North Dakota) in a square around each dam and writes one GeoTIFF per dam "
            "(<dam id>_naip_<year>.tif, JPEG-compressed with overviews) into an imagery "
            "folder of the folder you pick, with a manifest. Never written into a pack. Run "
            "again to resume: finished dams are kept.")
        intro.setWordWrap(True)
        lay.addWidget(intro)
        g = QGridLayout()
        from qgis.gui import QgsFieldComboBox, QgsMapLayerComboBox
        self.layer = QgsMapLayerComboBox()
        self.layer.setFilters(_vector_filter())
        g.addWidget(QLabel("Dams layer:"), 0, 0)
        g.addWidget(self.layer, 0, 1, 1, 2)
        self.field = QgsFieldComboBox()
        self.field.setAllowEmptyFieldName(True)
        self.layer.layerChanged.connect(self._layer_changed)
        g.addWidget(QLabel("Dam id field:"), 1, 0)
        g.addWidget(self.field, 1, 1, 1, 2)
        self.selected = QCheckBox("Selected dams only")
        g.addWidget(self.selected, 2, 1, 1, 2)
        self.ids = QLineEdit()
        self.ids.setPlaceholderText("Optional: only these dam ids, comma separated")
        g.addWidget(QLabel("Dam ids:"), 3, 0)
        g.addWidget(self.ids, 3, 1, 1, 2)
        self.radius = QDoubleSpinBox()
        self.radius.setRange(100, 20000)
        self.radius.setDecimals(0)
        self.radius.setSingleStep(250)
        self.radius.setSuffix(" m")
        self.radius.setValue(IM.DEFAULT_RADIUS_M)
        g.addWidget(QLabel("Buffer (radius):"), 4, 0)
        g.addWidget(self.radius, 4, 1)
        self.res = QComboBox()
        for label, _v in RES_LABELS:
            self.res.addItem(label)
        g.addWidget(QLabel("Resolution:"), 5, 0)
        g.addWidget(self.res, 5, 1)
        self.out = QLineEdit(self._setting(SETTING, ""))
        self.out.setPlaceholderText("Folder to write the imagery folder into")
        g.addWidget(QLabel("Output folder:"), 6, 0)
        g.addWidget(self.out, 6, 1)
        b = QPushButton("Browse\u2026")
        b.clicked.connect(self._browse)
        g.addWidget(b, 6, 2)
        lay.addLayout(g)
        self.est = QLabel("")
        self.est.setWordWrap(True)
        lay.addWidget(self.est)
        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        lay.addWidget(self.bar)
        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        lay.addWidget(self.text, 1)
        row = QHBoxLayout()
        row.addStretch(1)
        self.estimate_btn = QPushButton("Estimate size")
        self.estimate_btn.clicked.connect(self.estimate)
        self.go = QPushButton("Download")
        self.go.clicked.connect(self.start)
        self.stop = QPushButton("Stop")
        self.stop.setEnabled(False)
        self.stop.clicked.connect(self.cancel)
        close = QPushButton("Close")
        close.clicked.connect(self.close)
        for w in (self.estimate_btn, self.go, self.stop, close):
            row.addWidget(w)
        lay.addLayout(row)
        self.timer = QTimer(self)
        self.timer.setInterval(500)
        self.timer.timeout.connect(self._poll)
        self._layer_changed(self.layer.currentLayer())
        fit_window(self, (640, 600))

    def _setting(self, key, default):
        try:
            from qgis.core import QgsSettings
            return QgsSettings().value(key, default) or default
        except Exception:                                        # noqa: BLE001
            return default

    def _browse(self):
        p = QFileDialog.getExistingDirectory(self, "Choose a folder", self.out.text())
        if p:
            self.out.setText(p)

    def _layer_changed(self, layer):
        self.field.setLayer(layer)
        if layer is not None:
            f = IM.pick_id_field([x.name() for x in layer.fields()])
            if f:
                self.field.setField(f)

    def options(self):
        """(download kwargs, plans) of the window (ImageryError when not usable)."""
        layer = self.layer.currentLayer()
        if layer is None:
            raise IM.ImageryError("Choose a dams layer.")
        ids = [s for s in self.ids.text().replace(";", ",").split(",") if s.strip()] or None
        if self.selected.isChecked() and layer.selectedFeatureCount() == 0:
            raise IM.ImageryError("No dams are selected on {}.".format(layer.name()))
        dams = layer_dams(layer, self.field.currentField() or None, self.selected.isChecked(),
                          ids)
        if not dams:
            raise IM.ImageryError("No dams to download (check the ids and the selection).")
        res = RES_LABELS[self.res.currentIndex()][1]
        radius = float(self.radius.value())
        IM.imagery_folder(self.out.text().strip())
        plans = [IM.dam_plan(d, x, y, radius, res) for d, x, y in dams]
        return {"dams": dams, "out_parent": self.out.text().strip(), "radius_m": radius,
                "res_m": res}, plans

    def _estimate_text(self, plans):
        dl, out = IM.estimate(plans)
        n = plans[0]["pixels"]
        return ("{} dam(s), {} x {} px each: about {} to download and {} on disk "
                "(about {} per dam).".format(len(plans), n, n, IM.human(dl), IM.human(out),
                                            IM.human(out / max(1, len(plans)))))

    def estimate(self):
        try:
            _kw, plans = self.options()
        except IM.ImageryError as e:
            QMessageBox.warning(self, "Download imagery", str(e))
            return
        self.est.setText(self._estimate_text(plans))

    def start(self):
        try:
            kw, plans = self.options()
        except IM.ImageryError as e:
            QMessageBox.warning(self, "Download imagery", str(e))
            return
        txt = self._estimate_text(plans)
        self.est.setText(txt)
        if QMessageBox.question(self, "Download imagery", txt + "\n\nDownload now?") != \
                _first(lambda: QMessageBox.StandardButton.Yes, lambda: QMessageBox.Yes):
            return
        try:
            from qgis.core import QgsSettings
            QgsSettings().setValue(SETTING, kw["out_parent"])
        except Exception:                                        # noqa: BLE001
            pass
        self.text.clear()
        self.task = ImageryTask(kw, self._done)
        self.task.progressChanged.connect(lambda p: self.bar.setValue(int(p)))
        self.go.setEnabled(False)
        self.stop.setEnabled(True)
        self.timer.start()
        QgsApplication.taskManager().addTask(self.task)

    def cancel(self):
        if self.task is not None:
            self.task.cancel()

    def _poll(self):
        if self.task is not None:
            for line in self.task.take():
                self.text.appendPlainText(line)

    def _done(self, task, ok):
        self._poll()
        self.timer.stop()
        self.go.setEnabled(True)
        self.stop.setEnabled(False)
        self.task = None
        if ok:
            r = task.result or {}
            msg = "{} new, {} kept. The imagery is in {}.".format(
                len(r.get("done", {})), len(r.get("skipped", [])), r.get("folder", ""))
            if r.get("failed"):
                msg += "\n\nNot downloaded (run again to retry): " + ", ".join(
                    sorted(r["failed"]))
            QMessageBox.information(self, "Download imagery", msg)
        else:
            QgsMessageLog.logMessage("Imagery download stopped: " + task.error, TAG,
                                     MSG_WARNING)
            QMessageBox.warning(self, "Download imagery", "The download stopped: {}\n\nRun it "
                                "again to continue.".format(task.error))

    def closeEvent(self, event):                                 # noqa: N802 - Qt API
        if self.task is not None:
            QMessageBox.information(self, "Download imagery", "The download keeps running in "
                                    "the background (QGIS task bar).")
        super().closeEvent(event)

