"""Runner 3.10.67: the 'Batch chain…' window - a novice front for ndbreach:chainbatch.

Pick the dams, the output folder and the DEM, choose what happens at the HEC-RAS stage
(Run / Wait for a manual run / Skip), Check, then Run. The choices are the chain rows of the
suite settings file (Fill from the settings file / Save to the settings file); every other
tool's values come from that file's rows too (the Settings window of any of the plugins)."""
import os

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (QButtonGroup, QCheckBox, QComboBox, QDialog, QDoubleSpinBox,
                                 QFormLayout, QGroupBox, QHBoxLayout, QLineEdit, QPushButton,
                                 QRadioButton, QScrollArea, QVBoxLayout, QWidget)

from .core import chain as CH
from .damsuite import settings as DS
from .ui_widgets import CheckPanel, InfoBanner, WrapLabel

TITLE = "DamSafety Runner \u2013 Batch chain"
INTRO = ("Runs each dam of the list through the suite before starting the next: the Screening "
         "steps for that dam, the Runner, the HEC-RAS 2D model, the Hazard import and the ND + "
         "TM11 classification. A dam that fails does not stop the list. Each dam's Hazard "
         "window is saved as <dam folder>/Hazard_<ID>.qgz: open it (Project \u203a Open), "
         "open the Hazard window and use Add scenario / Add from a folder\u2026 to add "
         "HEC-RAS or DSS-WISE runs, then Classify again.")


def _narrow(combo):
    """A combo box that does not widen the window to its longest item."""
    P = QComboBox.SizeAdjustPolicy if hasattr(QComboBox, "SizeAdjustPolicy") else QComboBox
    combo.setSizeAdjustPolicy(P.AdjustToMinimumContentsLengthWithIcon)
    combo.setMinimumContentsLength(24)
    return combo


def _gui():
    from qgis import gui
    return gui


class _Path(QWidget):
    """A path box with a Browse… button (a QgsFileWidget when QGIS is there)."""

    def __init__(self, folder=False, filt="", parent=None):
        super().__init__(parent)
        h = QHBoxLayout(self)
        h.setContentsMargins(0, 0, 0, 0)
        try:
            w = _gui().QgsFileWidget()
            w.setStorageMode(_gui().QgsFileWidget.GetDirectory if folder else
                             _gui().QgsFileWidget.GetFile)
            if filt:
                w.setFilter(filt)
            self.w, self._qgs = w, True
        except Exception:                                        # noqa
            self.w, self._qgs = QLineEdit(), False
        h.addWidget(self.w)

    def text(self):
        return (self.w.filePath() if self._qgs else self.w.text()).strip()

    def setText(self, t):
        (self.w.setFilePath if self._qgs else self.w.setText)(t or "")


class ChainDialog(QDialog):
    def __init__(self, parent=None, iface=None):
        super().__init__(parent)
        self.iface = iface
        self.setWindowTitle(TITLE)
        self.setObjectName("NDChainBatchDialog")
        outer = QVBoxLayout(self)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff if hasattr(
            Qt, "ScrollBarAlwaysOff") else Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        page = QWidget()
        v = QVBoxLayout(page)
        v.addWidget(InfoBanner(INTRO))
        self.strip = WrapLabel("Each dam: " + " \u2192 ".join(CH.STAGE_LABELS[s]
                                                             for s in CH.STAGES))
        v.addWidget(self.strip)
        v.addWidget(self._dams_box())
        v.addWidget(self._where_box())
        v.addWidget(self._hec_box())
        v.addWidget(self._options_box())
        v.addStretch(1)
        scroll.setWidget(page)
        outer.addWidget(scroll, 1)
        self.check_btn = QPushButton("Check")
        self.run_btn = QPushButton("Run the batch\u2026")
        self.run_btn.setToolTip("Opens the Processing window of 'Batch chain' with these "
                                "choices; press Run there")
        self.checks = CheckPanel(self.check_btn, self.run_btn, "running")
        outer.addWidget(self.checks)
        b = QHBoxLayout()
        self.fill_btn = QPushButton("Fill from the settings file")
        self.save_btn = QPushButton("Save to the settings file")
        self.close_btn = QPushButton("Close")
        for w in (self.fill_btn, self.save_btn):
            b.addWidget(w)
        b.addStretch(1)
        for w in (self.check_btn, self.run_btn, self.close_btn):
            b.addWidget(w)
        outer.addLayout(b)
        self.fill_btn.clicked.connect(self.fill_from_settings)
        self.save_btn.clicked.connect(self.save_to_settings)
        self.check_btn.clicked.connect(self.run_check)
        self.run_btn.clicked.connect(self.run_batch)
        self.close_btn.clicked.connect(self.reject)
        self.hec_group.buttonClicked.connect(lambda *_: self._sync())
        self.fill_from_settings()
        self.resize(900, 720)

    # -- the boxes ------------------------------------------------------------------------
    def _dams_box(self):
        box = QGroupBox("Dams")
        f = QFormLayout(box)
        try:
            from qgis.core import QgsMapLayerProxyModel
            self.layer = _gui().QgsMapLayerComboBox()
            self.layer.setFilters(getattr(QgsMapLayerProxyModel, "PointLayer", None) or
                                  QgsMapLayerProxyModel.Filter.PointLayer)
            self.layer.setAllowEmptyLayer(True)
            self.id_field = _gui().QgsFieldComboBox()
            self.id_field.setAllowEmptyFieldName(True)
            self.layer.layerChanged.connect(self.id_field.setLayer)
            self.id_field.setLayer(self.layer.currentLayer())
        except Exception:                                        # noqa - no QGIS map layers
            self.layer, self.id_field = QComboBox(), QComboBox()
        _narrow(self.layer)
        _narrow(self.id_field)
        self.selected_only = QCheckBox("Only the selected dams of the layer")
        self.selected_only.setChecked(True)
        self.ids = QLineEdit()
        self.ids.setPlaceholderText("e.g. ND00123, ND00456 (empty = every dam of the layer)")
        self.ids_csv = _Path(filt="CSV (*.csv)")
        f.addRow("Dams layer (points)", self.layer)
        f.addRow("", self.selected_only)
        f.addRow("Dam ID field", self.id_field)
        f.addRow("Dam IDs", self.ids)
        f.addRow("or an IDs CSV", self.ids_csv)
        return box

    def _where_box(self):
        box = QGroupBox("Where")
        f = QFormLayout(box)
        self.out = _Path(folder=True)
        self.dem = _Path(filt="Rasters (*.tif *.tiff *.vrt *.img);;All files (*)")
        self.screening = QCheckBox("Run the Screening steps for each dam first")
        self.screen_work = _Path(folder=True)
        self.profile = _Path(filt="Hazard profile (*.json);;All files (*)")
        f.addRow("Output folder (one folder per dam)", self.out)
        f.addRow("DEM (or statewide VRT)", self.dem)
        f.addRow("", self.screening)
        f.addRow("Screening work folder", self.screen_work)
        f.addRow("Hazard profile (optional)", self.profile)
        return box

    def _hec_box(self):
        box = QGroupBox("HEC-RAS 2D stage")
        v = QVBoxLayout(box)
        self.hec_group = QButtonGroup(self)
        self.hec_radios = []
        for i, label in enumerate(CH.HEC_MODE_LABELS):
            r = QRadioButton(label)
            self.hec_group.addButton(r, i)
            self.hec_radios.append(r)
            v.addWidget(r)
        f = QFormLayout()
        self.wait_min = QDoubleSpinBox()
        self.wait_min.setRange(0, 7 * 24 * 60)
        self.wait_min.setDecimals(0)
        self.wait_min.setSuffix(" min")
        self.wait_min.setToolTip("0 = do not wait: the dam stops at 'waiting for HEC-RAS' and "
                                 "the next run of the chain continues it")
        self.structures = QComboBox()
        self.structures.addItems(list(CH.STRUCTURE_CHOICES))
        _narrow(self.structures)
        f.addRow("Wait for the manual run up to", self.wait_min)
        f.addRow("Culverts and bridges", self.structures)
        v.addLayout(f)
        self.hec_note = WrapLabel("")
        v.addWidget(self.hec_note)
        return box

    def _options_box(self):
        box = QGroupBox("Run options")
        f = QFormLayout(box)
        self.models = QComboBox()
        self.models.addItems(list(CH.RUNNER_MODELS))
        self.stop_after = QComboBox()
        self.stop_after.addItems(list(CH.STOP_CHOICES))
        for c in (self.models, self.stop_after):
            _narrow(c)
        self.skip_done = QCheckBox("Keep the stages already done (resume)")
        self.keep_going = QCheckBox("Keep going when a dam fails")
        self.overrides = _Path(filt="CSV (*.csv)")
        self.settings_file = _Path(filt="Settings (*.xlsx *.csv)")
        f.addRow("Runner models", self.models)
        f.addRow("Stop after", self.stop_after)
        f.addRow("", self.skip_done)
        f.addRow("", self.keep_going)
        f.addRow("Per-dam overrides CSV", self.overrides)
        f.addRow("Suite settings file", self.settings_file)
        return box

    def _sync(self):
        mode = CH.HEC_MODES[max(0, self.hec_group.checkedId())]
        self.wait_min.setEnabled(mode == "wait")
        self.structures.setEnabled(mode != "skip")
        self.hec_note.setText({
            "run": "Runs HEC-RAS 2D where it can run here; otherwise the chain builds the "
                   "model and waits for a manual run (the Check says which).",
            "wait": "The chain builds each HEC-RAS 2D model (instructions in "
                    "_chain/CHAIN_HECRAS_TODO.txt) and waits for its <name>.p01.hdf.",
            "skip": "No HEC-RAS: the Runner's LISFLOOD-FP / ANUGA rasters go straight to "
                    "the Hazard classification."}[mode])
        self.checks.clear()

    # -- values <-> widgets ---------------------------------------------------------------
    def chain_values(self):
        """{KEY: value} of the window."""
        return {"OUT_FOLDER": self.out.text(), "DEM": self.dem.text(),
                "ID_FIELD": self._field_name(), "SCREENING": self.screening.isChecked(),
                "SCREEN_WORK": self.screen_work.text(),
                "SCREEN_INCLUDE": self._kept.get("SCREEN_INCLUDE", ""),
                "SCREEN_PREFLIGHT": bool(self._kept.get("SCREEN_PREFLIGHT", True)),
                "RUNNER_MODELS": self.models.currentIndex(),
                "HEC_MODE": max(0, self.hec_group.checkedId()),
                "HEC_WAIT_MIN": float(self.wait_min.value()),
                "HEC_STRUCTURES": self.structures.currentIndex(),
                "ANUGA_RUN": self._kept.get("ANUGA_RUN", ""),
                "HEC_PLAN_HDF": self._kept.get("HEC_PLAN_HDF", ""),
                "HEC_TERRAIN": self._kept.get("HEC_TERRAIN", ""),
                "HAZARD_PROFILE": self.profile.text(),
                "STOP_AFTER": self.stop_after.currentIndex(),
                "SKIP_DONE": self.skip_done.isChecked(),
                "KEEP_GOING": self.keep_going.isChecked()}

    def set_chain_values(self, cv):
        self._kept = dict(cv)
        self.out.setText(cv.get("OUT_FOLDER", ""))
        self.dem.setText(cv.get("DEM", ""))
        self.screening.setChecked(bool(cv.get("SCREENING")))
        self.screen_work.setText(cv.get("SCREEN_WORK", ""))
        self.profile.setText(cv.get("HAZARD_PROFILE", ""))
        self.models.setCurrentIndex(int(cv.get("RUNNER_MODELS") or 0))
        self.hec_radios[int(cv.get("HEC_MODE") or 0)].setChecked(True)
        self.wait_min.setValue(float(cv.get("HEC_WAIT_MIN") or 0))
        self.structures.setCurrentIndex(int(cv.get("HEC_STRUCTURES") or 0))
        self.stop_after.setCurrentIndex(int(cv.get("STOP_AFTER") or 0))
        self.skip_done.setChecked(bool(cv.get("SKIP_DONE")))
        self.keep_going.setChecked(bool(cv.get("KEEP_GOING")))
        self._sync()

    def _field_name(self):
        try:
            return self.id_field.currentField() or ""
        except AttributeError:
            return self.id_field.currentText() or ""

    def _settings_path(self):
        return self.settings_file.text() or DS.current_path() or ""

    def _values(self):
        """The settings file's values with this window's chain rows on top."""
        path = self._settings_path()
        values = {}
        if path and os.path.exists(path):
            try:
                values = dict(DS.Settings.load(path).values)
            except DS.SettingsError:
                values = {}
        for k, v in self.chain_values().items():
            values[(CH.PLUGIN, CH.TOOL, k)] = v
        return values

    def fill_from_settings(self):
        """The chain rows of the settings file (its defaults where it has none)."""
        path = self._settings_path()
        self.settings_file.setText(path)
        values = {}
        if path and os.path.exists(path):
            try:
                values = dict(DS.Settings.load(path).values)
            except DS.SettingsError:
                values = {}
        cv, _p = CH.chain_values(values)
        self.set_chain_values(cv)

    def save_to_settings(self):
        path = self._settings_path()
        if not path:
            self.checks.set_results([(CH.ERROR, "Choose the suite settings file first")])
            return False
        try:
            s = DS.Settings.load(path) if os.path.exists(path) else DS.Settings(path=path)
            for k, v in self.chain_values().items():
                s.set(CH.PLUGIN, CH.TOOL, k, v)
            s.save(path)
        except (DS.SettingsError, OSError) as e:
            self.checks.set_results([(CH.ERROR, "Not saved: {}".format(e))])
            return False
        self.checks.set_results([(CH.OK, "Saved to {}".format(path))])
        return True

    # -- the dams, the check, the run -----------------------------------------------------
    def _layer(self):
        try:
            return self.layer.currentLayer()
        except AttributeError:
            return None

    def dam_ids(self):
        """(dam IDs of the run, IDs asked for but not in the layer)."""
        wanted = CH.parse_ids(self.ids.text())
        if self.ids_csv.text():
            try:
                wanted += [i for i in CH.read_ids_csv(self.ids_csv.text()) if i not in wanted]
            except (OSError, ValueError):
                pass
        lyr, fld = self._layer(), self._field_name()
        if lyr is None or not fld:
            return wanted, []
        feats = lyr.selectedFeatures() if (self.selected_only.isChecked() and
                                           lyr.selectedFeatureCount()) else lyr.getFeatures()
        from .algorithms_chain import _id_text     # the same normaliser the run uses (NULL/'' dropped)
        have = []
        for f in feats:
            s = _id_text(f[fld])
            if s and s not in have:
                have.append(s)
        if wanted:
            return [i for i in wanted if i in have], [i for i in wanted if i not in have]
        return have, []

    def run_check(self):
        from .algorithms_chain import hec_engine, registry_algs
        ids, missing = self.dam_ids()
        ov_path = self.overrides.text()
        overrides, probs = CH.read_overrides(ov_path) if ov_path else ({}, [])
        res = CH.preflight(ids, self._values(), {"algs": registry_algs(),
                                                 "engine": hec_engine()},
                           overrides, probs, missing)
        if self._layer() is None:
            res.insert(0, (CH.ERROR, "", "Choose the dams layer"))
        rows = [(lv, "{}: {}".format(d, t) if d else t) for lv, d, t in res]
        self.checks.set_results(rows)
        return rows

    def params(self):
        p = {k: v for k, v in self.chain_values().items()}
        lyr = self._layer()
        if lyr is not None:
            if self.selected_only.isChecked() and lyr.selectedFeatureCount():
                from qgis.core import QgsProcessingFeatureSourceDefinition
                p["DAMS"] = QgsProcessingFeatureSourceDefinition(lyr.id(), True)
            else:
                p["DAMS"] = lyr.id()
        p.update({"DAM_ID_FIELD": self._field_name(), "DAM_IDS": self.ids.text(),
                  "IDS_CSV": self.ids_csv.text(), "OVERRIDES_CSV": self.overrides.text(),
                  "SETTINGS_FILE": self._settings_path(), "CHECK_ONLY": False})
        return p

    def run_batch(self):
        rows = self.run_check()
        if any(lv == CH.ERROR for lv, _t in rows):
            return {}
        from qgis import processing
        from .tool_dialog import exec_tool_dialog
        return exec_tool_dialog(processing, "ndbreach:chainbatch", self.params(), self)


def open_dialog(iface=None, parent=None):
    d = ChainDialog(parent, iface)
    d.show()
    return d
