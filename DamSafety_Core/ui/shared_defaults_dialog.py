# -*- coding: utf-8 -*-
"""The Shared defaults window (Core 1.2.0): view and change the suite-wide starting values.

Opened from Plugins > Dam safety > Shared defaults… (DamSafety Core's menu) and from the
"Shared defaults…" button under every plugin's Settings grid. One row per key of
damsuite.shared_defaults.SCHEMA: a drop-down or number, the suite's value beside it, and whether
the row is the suite's value or yours. Save writes only your changes to your file
(~/DamSafety/defaults.json unless the suite setting defaults_file names another); "Use the suite
values" empties it. Tools opened after a save start with the new values; a run dialog can still
change any of them for one run.

Logic is damsuite.shared_defaults (pure Python, tested); this file holds only the widgets.
"""
import os

from qgis.PyQt.QtCore import QUrl
from qgis.PyQt.QtGui import QDesktopServices
from qgis.PyQt.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QLabel, QComboBox, QDoubleSpinBox,
    QPushButton, QWidget,
)

from ..damsuite import shared_defaults as SD
from .tokens import TOKENS


class SharedDefaultsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Shared defaults")
        self.editors = {}           # key -> widget
        self.tags = {}              # key -> QLabel ("suite" / "yours")
        self._build()
        self.fill()

    def _build(self):
        lay = QVBoxLayout()
        intro = QLabel("The starting values every DamSafety tool (and Crest) uses. A run dialog can "
                       "still change any of them for one run, and each run writes what it used into "
                       "the dam's dam.json. Tools opened after you save start with these values.")
        intro.setWordWrap(True)
        lay.addWidget(intro)
        form = QFormLayout()
        for key, spec in SD.SCHEMA.items():
            if spec["kind"] == "choice":
                w = QComboBox()
                for cid, label in spec["choices"].items():
                    w.addItem(label, cid)
                w.currentIndexChanged.connect(lambda _i, k=key: self._changed(k))
            else:
                w = QDoubleSpinBox()
                w.setDecimals(spec.get("decimals", 3))
                w.setRange(spec["minimum"], spec["maximum"])
                w.setSingleStep(0.005)
                w.valueChanged.connect(lambda _v, k=key: self._changed(k))
            w.setToolTip("{} Read by: {}.".format(spec["help"], spec["tools"]))
            tag = QLabel("")
            tag.setStyleSheet("color: {};".format(TOKENS["muted"]))
            row = QHBoxLayout()
            row.addWidget(w, 1)
            row.addWidget(tag)
            holder = QWidget()
            holder.setLayout(row)
            form.addRow(spec["label"], holder)
            self.editors[key], self.tags[key] = w, tag
        lay.addLayout(form)
        self.file_label = QLabel("")
        self.file_label.setWordWrap(True)
        self.file_label.setStyleSheet("color: {};".format(TOKENS["muted"]))
        lay.addWidget(self.file_label)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        lay.addWidget(self.status)
        buttons = QHBoxLayout()
        self.save_btn = QPushButton("Save")
        self.save_btn.clicked.connect(lambda _c=False: self.save())
        self.reset_btn = QPushButton("Use the suite values")
        self.reset_btn.setToolTip("Forget your changes: every row follows DamSafety Core's file.")
        self.reset_btn.clicked.connect(lambda _c=False: self.reset())
        self.open_btn = QPushButton("Open folder")
        self.open_btn.setToolTip("Show the folder of your defaults file (to copy it to another "
                                 "computer, or open it in Crest).")
        self.open_btn.clicked.connect(lambda _c=False: self.open_folder())
        close = QPushButton("Close")
        close.clicked.connect(lambda _c=False: self.reject())
        for b in (self.save_btn, self.reset_btn, self.open_btn):
            buttons.addWidget(b)
        buttons.addStretch(1)
        buttons.addWidget(close)
        lay.addLayout(buttons)
        self.setLayout(lay)
        self.resize(640, self.sizeHint().height())

    # -- values
    def fill(self):
        self.d = SD.load()
        self._filling = True
        try:
            for key, w in self.editors.items():
                v = self.d.get(key)
                if v is None:
                    continue
                if isinstance(w, QComboBox):
                    i = w.findData(v)
                    if i >= 0:
                        w.setCurrentIndex(i)
                else:
                    w.setValue(float(v))
        finally:
            self._filling = False
        for key in self.editors:
            self._changed(key)
        your = self.d.user_path
        self.file_label.setText("Suite file: {}\nYour file: {}{}".format(
            self.d.suite_path, your, "" if os.path.isfile(your) else " (not created yet)"))
        self._say("; ".join(self.d.problems), error=bool(self.d.problems))

    def values(self):
        out = {}
        for key, w in self.editors.items():
            out[key] = w.currentData() if isinstance(w, QComboBox) else float(w.value())
        return out

    def _changed(self, key):
        if getattr(self, "_filling", False) or not hasattr(self, "d"):
            return
        v = self.values()[key]
        suite = self.d.suite.get(key)
        if suite is None:
            text = "no suite value"
        elif SD._same(v, suite):
            text = "suite value"
        else:
            text = "yours (suite: {})".format(SD.label_of(key, suite))
        self.tags[key].setText(text)

    def _say(self, text, error=False):
        self.status.setText(text or "")
        self.status.setStyleSheet("color: {};".format(TOKENS["error"] if error else TOKENS["muted"]))

    def save(self):
        try:
            p = SD.save_user(self.values(), self.d.user_path)
        except SD.DefaultsError as e:
            self._say(str(e), error=True)
            return False
        self.fill()
        self._say("Saved to {}. Tools opened from now on start with these values.".format(p))
        return True

    def reset(self):
        try:
            p = SD.save_user({}, self.d.user_path)
        except SD.DefaultsError as e:
            self._say(str(e), error=True)
            return False
        self.fill()
        self._say("Every row now follows the suite file ({} holds no values).".format(p))
        return True

    def open_folder(self):
        folder = os.path.dirname(self.d.user_path)
        try:
            os.makedirs(folder, exist_ok=True)
        except OSError:
            pass
        QDesktopServices.openUrl(QUrl.fromLocalFile(folder))


def show(parent=None):
    """Open the window (modal); returns True when the user saved."""
    dlg = SharedDefaultsDialog(parent)
    try:
        return bool(dlg.exec())
    except AttributeError:                                       # Qt5 spelling
        return bool(dlg.exec_())
