"""1.5.0: the "Build offline pack…" window and its background task (pack_build.py does the
work). Writes a NEW ND_Dam_Suite_Pack (format 2) and nd_offline_pack (format 1) into the folder
the user picks; an existing pack the plugins read is never written into."""
import os
import threading

from qgis.core import QgsApplication, QgsMessageLog, QgsTask
from qgis.PyQt.QtCore import Qt, QTimer
from qgis.PyQt.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QFileDialog, QGridLayout, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton,
    QVBoxLayout)

from . import pack_build as PB
from .qtcompat import MSG_INFO, MSG_WARNING, TASK_CAN_CANCEL, _first
from .ui_fit import fit_window

CHECKED = _first(lambda: Qt.CheckState.Checked, lambda: Qt.Checked)
UNCHECKED = _first(lambda: Qt.CheckState.Unchecked, lambda: Qt.Unchecked)
CHECKABLE = _first(lambda: Qt.ItemFlag.ItemIsUserCheckable, lambda: Qt.ItemIsUserCheckable)
ENABLED = _first(lambda: Qt.ItemFlag.ItemIsEnabled, lambda: Qt.ItemIsEnabled)
USER_ROLE = Qt.ItemDataRole.UserRole
TAG = "DamSafety Data"
SETTING = "nd_data_updater/pack_build_out"
AREAS = (("All of North Dakota (the full pack; can take hours)", None),
         ("A small test box around Bismarck (minutes)", (-100.85, 46.75, -100.70, 46.85)),
         ("My own box (west, south, east, north in degrees)\u2026", "custom"))


def active_packs():
    """The pack(s) the plugins read now (refused as an output)."""
    out = []
    try:
        from .damsuite import suitedata
        p = suitedata.active_pack()
        if p:
            out.append(p)
    except Exception:                                            # noqa: BLE001 - optional
        pass
    return out


class PackTask(QgsTask):
    def __init__(self, kwargs, on_done):
        super().__init__("Build offline pack", TASK_CAN_CANCEL)
        self.kwargs = kwargs
        self.on_done = on_done
        self.lines = []
        self.lock = threading.Lock()
        self.result = None
        self.error = ""
        self.check = []

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
            self.result = PB.build(progress=lambda p, m="": self.setProgress(p), log=self.log,
                                   cancel=self.isCanceled, **self.kwargs)
            ok, self.check = PB.verify(self.kwargs["out_parent"])
            for line in self.check:
                self.log(line)
            return True
        except Exception as e:                                   # noqa: BLE001 - shown
            self.error = str(e) or e.__class__.__name__
            self.log("STOPPED: " + self.error)
            return False

    def finished(self, ok):
        self.on_done(self, ok)


class PackDialog(QDialog):
    def __init__(self, parent=None, settings=None):
        super().__init__(parent)
        self.setWindowTitle("DamSafety Data - Build offline pack")
        self.settings = settings
        self.task = None
        lay = QVBoxLayout(self)
        intro = QLabel(
            "Downloads every layer of the North Dakota dam suite's offline pack and writes it "
            "twice from the same download:\n"
            "  \u2022 ND_Dam_Suite_Pack - one GeoPackage per layer (what Setup and the other "
            "plugins read)\n"
            "  \u2022 nd_offline_pack - the same vector layers in one .sqlite file (older "
            "plugin versions)\n"
            "Pick an empty folder (or the folder of an earlier build here, to finish or update "
            "it). An existing pack is never written into. Stopped builds continue where they "
            "left off.")
        intro.setWordWrap(True)
        lay.addWidget(intro)
        g = QGridLayout()
        self.out = QLineEdit(self._setting(SETTING, ""))
        self.out.setPlaceholderText("Folder to write the pack into")
        g.addWidget(QLabel("Output folder:"), 0, 0)
        g.addWidget(self.out, 0, 1)
        g.addWidget(self._browse(self.out, folder=True), 0, 2)
        self.area = QComboBox()
        for label, _box in AREAS:
            self.area.addItem(label)
        self.box = QLineEdit("-100.85, 46.75, -100.70, 46.85")
        self.box.setEnabled(False)
        self.area.currentIndexChanged.connect(
            lambda i: self.box.setEnabled(AREAS[i][1] == "custom"))
        g.addWidget(QLabel("Area:"), 1, 0)
        g.addWidget(self.area, 1, 1)
        g.addWidget(self.box, 2, 1)
        self.dwr = QLineEdit()
        self.dwr.setPlaceholderText("Optional: a folder of DWR GeoPackages (else downloaded "
                                    "from the DWR MapService)")
        g.addWidget(QLabel("DWR files:"), 3, 0)
        g.addWidget(self.dwr, 3, 1)
        g.addWidget(self._browse(self.dwr, folder=True), 3, 2)
        self.hifld = QLineEdit()
        self.hifld.setPlaceholderText("Optional: a folder of HIFLD files (else downloaded)")
        g.addWidget(QLabel("HIFLD files:"), 4, 0)
        g.addWidget(self.hifld, 4, 1)
        g.addWidget(self._browse(self.hifld, folder=True), 4, 2)
        self.nlcd = QLineEdit()
        self.nlcd.setPlaceholderText("Optional: an NLCD 2021 GeoTIFF (else downloaded)")
        g.addWidget(QLabel("Land cover file:"), 5, 0)
        g.addWidget(self.nlcd, 5, 1)
        g.addWidget(self._browse(self.nlcd, folder=False), 5, 2)
        lay.addLayout(g)
        self.all = QCheckBox("All pack layers (recommended)")
        self.all.setChecked(True)
        lay.addWidget(self.all)
        self.list = QListWidget()
        for t in PB.registry():
            it = QListWidgetItem("{}  -  {}".format(t["label"], t["name"]))
            it.setData(USER_ROLE, t["name"])
            it.setFlags(CHECKABLE | ENABLED)
            it.setCheckState(CHECKED)
            if t["kind"] == "none":
                it.setToolTip(t["info"].get("why", ""))
            self.list.addItem(it)
        self.list.setEnabled(False)
        self.all.toggled.connect(lambda on: self.list.setEnabled(not on))
        lay.addWidget(self.list, 1)
        self.refresh = QCheckBox("Download everything again (else finished layers are kept)")
        lay.addWidget(self.refresh)
        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        lay.addWidget(self.bar)
        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        lay.addWidget(self.text, 1)
        row = QHBoxLayout()
        row.addStretch(1)
        self.go = QPushButton("Build pack")
        self.go.clicked.connect(self.start)
        self.stop = QPushButton("Stop")
        self.stop.setEnabled(False)
        self.stop.clicked.connect(self.cancel)
        close = QPushButton("Close")
        close.clicked.connect(self.close)
        for b in (self.go, self.stop, close):
            row.addWidget(b)
        lay.addLayout(row)
        self.timer = QTimer(self)
        self.timer.setInterval(500)
        self.timer.timeout.connect(self._poll)
        fit_window(self, (760, 720))

    def _setting(self, key, default):
        try:
            from qgis.core import QgsSettings
            return QgsSettings().value(key, default) or default
        except Exception:                                        # noqa: BLE001
            return default

    def _browse(self, edit, folder):
        b = QPushButton("Browse\u2026")

        def pick():
            if folder:
                p = QFileDialog.getExistingDirectory(self, "Choose a folder", edit.text())
            else:
                p = QFileDialog.getOpenFileName(self, "Choose a file", edit.text(),
                                                "GeoTIFF (*.tif *.tiff);;All files (*)")[0]
            if p:
                edit.setText(p)
        b.clicked.connect(pick)
        return b

    def options(self):
        """The pack_build.build arguments of the window (PackBuildError when not usable)."""
        out = self.out.text().strip()
        box = AREAS[self.area.currentIndex()][1]
        if box == "custom":
            try:
                box = tuple(float(v) for v in self.box.text().replace(";", ",").split(","))
                assert len(box) == 4 and box[0] < box[2] and box[1] < box[3]
            except (ValueError, AssertionError):
                raise PB.PackBuildError("The box needs four numbers: west, south, east, north "
                                        "(for example -100.85, 46.75, -100.70, 46.85).")
        only = None
        if not self.all.isChecked():
            only = [self.list.item(i).data(USER_ROLE) for i in range(self.list.count())
                    if self.list.item(i).checkState() == CHECKED]
            if not only:
                raise PB.PackBuildError("Tick at least one layer, or All pack layers.")
        kw = {"out_parent": out, "only": only, "bbox": box, "refresh": self.refresh.isChecked(),
              "dwr_folder": self.dwr.text().strip() or None,
              "hifld_folder": self.hifld.text().strip() or None,
              "nlcd_file": self.nlcd.text().strip() or None,
              "active": active_packs()}
        if box:
            kw["area"] = "test box"
        PB.check_target(out, kw["active"])
        return kw

    def start(self):
        try:
            kw = self.options()
        except PB.PackBuildError as e:
            QMessageBox.warning(self, "Build offline pack", str(e))
            return
        try:
            from qgis.core import QgsSettings
            QgsSettings().setValue(SETTING, kw["out_parent"])
        except Exception:                                        # noqa: BLE001
            pass
        self.text.clear()
        self.task = PackTask(kw, self._done)
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
            fail = r.get("failed") or {}
            msg = "The pack is in {}.".format(os.path.dirname(r.get("pack2", "")))
            if fail:
                msg += "\n\nNot downloaded (run again to retry): " + ", ".join(sorted(fail))
            QMessageBox.information(self, "Build offline pack", msg)
        else:
            QgsMessageLog.logMessage("Offline pack build stopped: " + task.error, TAG,
                                     MSG_WARNING)
            QMessageBox.warning(self, "Build offline pack",
                                "The build stopped: {}\n\nRun it again to continue.".format(
                                    task.error))

    def closeEvent(self, event):                                 # noqa: N802 - Qt API
        if self.task is not None:
            QMessageBox.information(self, "Build offline pack", "The build keeps running in "
                                    "the background (QGIS task bar).")
        super().closeEvent(event)
