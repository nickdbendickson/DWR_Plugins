"""Guided wizard: five pages that fill in "Run breach model for one dam" for one dam.

    Dam -> Terrain -> Scenario -> Models and cell size (live run-time estimate) -> Review

then opens the tool's own dialog (processing.execAlgorithmDialog('ndbreach:rundam', ...)) with
everything filled in, so nothing the wizard does is hidden from the user.

Wiring (plugin.py):
    from . import wizard
    ... action.triggered.connect(lambda: wizard.run_wizard(self.iface))

DamSafety Setup's Start here… asks for the suite data folder and continues here with its
scan (run_wizard(found=...)): the DWR dams of that folder are then a "Dams from" choice. The
pages follow docs/gui-grammar.md: the DamCard summary, Required / Improves the estimate rows
led by their marks, the DEM coverage line, amber warnings, an Advanced fold whose header shows
the values, and a page's problems in red on the page (no message box).

Qt and QGIS are imported lazily through qgis.PyQt / qgis.core inside the functions, so this
module imports without them. Every QGIS call is wrapped: an exception becomes a message box
with the error, never a silent failure or a crashed QGIS. The decisions (what is valid, what
the parameters are, the estimate) are in wizard_logic.py, which is unit-tested; the widgets
are checked in real QGIS by tests/realqt_wizard.py and tools/gui_lint.py.
"""
import functools
import os
from .ui_fit import fit_window, wrap_wizard
import shutil
import sys
import tempfile
import traceback

from . import wizard_logic as wl
from .tool_dialog import exec_tool_dialog
from .core import breach as brmod
from .core import nd_data

TITLE = "DamSafety Runner"


# ------------------------------------------------------------------------------- messages
def _message(kind, text, detail="", parent=None):
    """A message box (kind: information / warning / critical); falls back to stderr."""
    try:
        from qgis.PyQt import QtWidgets
        box = getattr(QtWidgets.QMessageBox, kind)
        box(parent, TITLE, text + ("\n\n" + detail if detail else ""))
    except Exception:
        sys.stderr.write("{}: {}\n{}\n".format(TITLE, text, detail))


def _safe(default=None, what="the wizard"):
    """Decorator: an exception in a slot or page method is shown, not raised into Qt."""
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except Exception as e:
                parent = args[0] if args and hasattr(args[0], "window") else None
                _message("critical", "Something went wrong in {}: {}".format(what, e),
                         traceback.format_exc(), parent)
                return default
        return wrapper
    return deco


def _busy(on):
    try:
        from qgis.PyQt import QtCore, QtWidgets
        if on:
            shape = getattr(QtCore.Qt, "WaitCursor", None) or QtCore.Qt.CursorShape.WaitCursor
            from qgis.PyQt import QtGui
            QtWidgets.QApplication.setOverrideCursor(QtGui.QCursor(shape))
        else:
            QtWidgets.QApplication.restoreOverrideCursor()
    except Exception:
        pass


# ------------------------------------------------------------------------------- QGIS access
def _project_layers(kind):
    """[(layer id, name)] of the project's "vector" or "raster" layers."""
    from qgis.core import QgsProject, QgsRasterLayer, QgsVectorLayer
    want = QgsVectorLayer if kind == "vector" else QgsRasterLayer
    out = [(lyr.id(), lyr.name()) for lyr in QgsProject.instance().mapLayers().values()
           if isinstance(lyr, want)]
    return sorted(out, key=lambda t: t[1].lower())


def _layer(layer_id):
    from qgis.core import QgsProject
    return QgsProject.instance().mapLayer(layer_id) if layer_id else None


def _find_models():
    """{"lisflood": bool, "anuga": bool}: the provider settings, then auto-detection (the same
    order the tools use)."""
    exe = py = ""
    try:
        from .algorithms import SET_ANUGA, SET_LISFLOOD, _setting
        exe, py = _setting(SET_LISFLOOD), _setting(SET_ANUGA)
    except Exception:
        pass
    if not exe or not os.path.isfile(exe):
        exe = nd_data.detect_lisflood()
    if not py or not os.path.isfile(py):
        py = nd_data.detect_anuga_python()
    return {"lisflood": bool(exe and os.path.isfile(exe)),
            "anuga": bool(py and os.path.isfile(py))}


class EstimateSession(object):
    """The wizard's run-time estimate: the tool's own parameter handling
    (RunDamAlgorithm.prepare) and workflow.preview for the plan, lisflood.calibrate for this
    computer's speed, both cached in a wizard_logic.EstimateCache."""

    def __init__(self):
        self.cache = wl.EstimateCache(self._plan, self._rate)

    @staticmethod
    def _plan(params):
        from qgis.core import QgsProcessingContext, QgsProcessingFeedback, QgsProject
        from . import algorithms as algs
        from .core import workflow
        alg = algs.RunDamAlgorithm()
        alg.initAlgorithm()
        p = dict(params)
        p.setdefault("DAMS_FOLDER", tempfile.gettempdir())
        for d in alg.parameterDefinitions():
            if d.name() not in p and d.defaultValue() is not None:
                p[d.name()] = d.defaultValue()
        ctx = QgsProcessingContext()
        ctx.setProject(QgsProject.instance())
        dam, st, src, _writer, dam_xy, inflow, _folder = alg.prepare(p, ctx,
                                                                     QgsProcessingFeedback())
        plan = workflow.preview(dam, dam_xy, st, src, inflow, _folder)
        return plan, {"exe": st.lisflood_exe, "threads": st.threads}

    @staticmethod
    def _rate(context):
        from .core import lisflood
        tmp = tempfile.mkdtemp(prefix="ndbreach_wiz_")
        try:
            return lisflood.calibrate(context["exe"], tmp, context["threads"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------------------------------- the widgets
_CLASSES = {}


def _classes():
    """Build the Qt classes on first use (the base classes need qgis.PyQt)."""
    if _CLASSES:
        return _CLASSES
    from qgis.PyQt import QtWidgets as W

    def browse_file(parent, edit, caption, filt):
        start = edit.text() or os.path.expanduser("~")
        res = W.QFileDialog.getOpenFileName(parent, caption, start, filt)
        path = res[0] if isinstance(res, (tuple, list)) else res
        if path:
            edit.setText(path)

    def browse_folder(parent, edit):
        path = W.QFileDialog.getExistingDirectory(parent, "Dams folder",
                                                  edit.text() or os.path.expanduser("~"))
        if path:
            edit.setText(path)

    def path_row(page, filt=None, folder=False):
        """(container widget, line edit) with a Browse button."""
        box = W.QWidget()
        row = W.QHBoxLayout(box)
        row.setContentsMargins(0, 0, 0, 0)
        edit = W.QLineEdit()
        button = W.QPushButton("Browse\u2026")
        if folder:
            button.clicked.connect(lambda *_: browse_folder(page, edit))
        else:
            button.clicked.connect(lambda *_: browse_file(page, edit, "Choose a file", filt))
        row.addWidget(edit)
        row.addWidget(button)
        return box, edit

    def fill_layers(combo, kind, optional, keep=""):
        combo.clear()
        if optional:
            combo.addItem("(none)", "")
        for lid, name in _project_layers(kind):
            combo.addItem(name, lid)
        if keep:
            i = combo.findData(keep)
            if i >= 0:
                combo.setCurrentIndex(i)

    def number_or_none(edit, what, problems):
        t = edit.text().strip()
        if not t:
            return None
        x = wl.clean_number(t)
        if x is None:
            problems.append("'{}' is not a number for: {}.".format(t, what))
        return x

    from .tokens import TOKENS
    from .ui_widgets import Collapsible, DamCard

    # the row marks of docs/gui-grammar.md: the mark leads, coloured from tokens.py
    ROW_MARKS = {"ok": ("\u2713", TOKENS["success"]), "empty": ("\u25cb", TOKENS["off"]),
                 "flag": ("\u26a0", TOKENS["warn"]), "bad": ("\u2715", TOKENS["error"])}

    def mark_label():
        lab = W.QLabel("")
        lab.setMinimumWidth(16)
        return lab

    def set_mark(lab, state, tip=""):
        mark, colour = ROW_MARKS[state]
        lab.setText(mark)
        lab.setStyleSheet("color: {};".format(colour))
        lab.setToolTip(tip)

    def red_label():
        lab = W.QLabel("")
        lab.setWordWrap(True)
        lab.setStyleSheet("color: {};".format(TOKENS["error"]))
        lab.setVisible(False)
        return lab

    class Page(W.QWizardPage):
        problems = None                  # the page's red line (docs/gui-grammar.md: no message box)

        def answers(self):
            return self.wizard().answers

        def show_problems(self, errors):
            if self.problems is None:
                _message("warning", "Please fix:\n\n- " + "\n- ".join(errors), parent=self)
                return
            self.problems.setText("\n".join("\u2715 " + e for e in errors))
            self.problems.setVisible(bool(errors))

        def check(self, page):
            """Validate this page's answers; show the errors and return False if any."""
            wiz = self.wizard()
            kw = {"models": _find_models()} if page == "model" else {}
            errors, _warnings = wl.validate_page(page, wiz.answers, **kw)
            if errors:
                self.show_problems(errors)
                return False
            if self.problems is not None:
                self.show_problems([])
            return True

    # -- 1. the dam ---------------------------------------------------------------------
    SUITE_DWR = "suite:dwr"            # the layer combo's entry for the suite data folder's dams
    COUNTY_FIELDS = ("county", "county_name", "cnty_name")
    SOURCE_ROWS = (("HEIGHT_FT", "height", "Height"), ("STORAGE_ACFT", "storage", "Storage"))

    class DamPage(Page):
        """The DamCard summary on top; then the source (a project layer, or the DWR dams of the
        suite data folder Setup scanned), the county and the dam; then the values in two
        groups, Required and Improves the estimate, each row led by its mark.

        With the suite's DWR dams the list is read by suite_dams.dams(): the NID layer of the
        folder fills a blank height or storage, records that are not a dam to model are set
        aside (Show all records), and two buttons fill what is still blank: the NID web
        service (only on a click) and a screening estimate from the DEM. A value that is not
        the DWR record's is marked ⚠ and named in the card's amber lines."""

        def __init__(self):
            Page.__init__(self)
            self.setTitle("The dam")
            self.setSubTitle("Pick the dam; you can correct any value. Required values first; "
                             "the others improve the estimate.")
            self.layer_crs = ""
            self._suite_layer = None
            self.rows = []                   # suite_dams.dams() rows of the suite source
            self.fetch = None                # (url, done(text, error)); tests replace it
            self.run_task = None             # (work, done, progress); tests replace it
            self._nid_busy = self._est_busy = False
            outer = W.QVBoxLayout(self)
            self.card = DamCard()
            for w in (self.card.chips_row, self.card.status_label, self.card.record_label,
                      self.card.suggest_label):
                w.setVisible(False)          # the wizard's card: title, source, warnings only
            outer.addWidget(self.card)
            self.problems = red_label()      # under the card: seen without scrolling
            outer.addWidget(self.problems)
            form = W.QFormLayout()
            self.layer = W.QComboBox()
            self.county = W.QComboBox()
            self.search = W.QLineEdit()
            self.search.setPlaceholderText("Type part of the dam name, then press Enter")
            self.show_all = W.QCheckBox("Show all records")
            self.show_all.setToolTip("Also list the DWR records that are not a dam to model: "
                                     "status Removed, Breached, Not Built or Submerged, or "
                                     "remarks such as 'VOID' or 'No Dam Visible'. Each shows why.")
            self.feature = W.QComboBox()
            self.county_label, self.search_label = W.QLabel("County"), W.QLabel("Name")
            form.addRow("Dams from", self.layer)
            form.addRow(self.county_label, self.county)
            form.addRow(self.search_label, self.search)
            form.addRow("Dam", self.feature)
            form.addRow("", self.show_all)
            outer.addLayout(form)
            self.note = W.QLabel("")
            self.note.setWordWrap(True)
            self.note.setStyleSheet("color: {};".format(TOKENS["muted"]))
            outer.addWidget(self.note)
            grid = W.QGridLayout()
            self.field_head = W.QLabel("<b>Layer field</b>")
            grid.addWidget(self.field_head, 0, 2)
            grid.addWidget(W.QLabel("<b>Value</b>"), 0, 3)
            self.field_combo, self.value_edit, self.mark = {}, {}, {}
            row = 1
            for title, required in (("Required", True), ("Improves the estimate", False)):
                grid.addWidget(W.QLabel("<b>{}</b>".format(title)), row, 0, 1, 2)
                row += 1
                for key, param, label, _kind, req in wl.DAM_FIELDS:
                    if bool(req) != required:
                        continue
                    combo, edit, mark = W.QComboBox(), W.QLineEdit(), mark_label()
                    self.field_combo[key], self.value_edit[param] = combo, edit
                    self.mark[param] = mark
                    grid.addWidget(mark, row, 0)
                    grid.addWidget(W.QLabel(label), row, 1)
                    grid.addWidget(combo, row, 2)
                    grid.addWidget(edit, row, 3)
                    combo.currentIndexChanged.connect(self.values_from_feature)
                    edit.textChanged.connect(self.refresh)
                    row += 1
                if required:                     # the location is required too
                    self.point_edit = W.QLineEdit()
                    self.point_label = W.QLabel("Location (x, y)")
                    self.mark["point"] = mark_label()
                    grid.addWidget(self.mark["point"], row, 0)
                    grid.addWidget(self.point_label, row, 1)
                    grid.addWidget(self.point_edit, row, 3)
                    self.point_edit.textChanged.connect(self.refresh)
                    row += 1
                    # fill a blank height or storage: the NID web service, or the DEM
                    self.fill_box = W.QWidget()
                    fr = W.QGridLayout(self.fill_box)
                    fr.setContentsMargins(0, 0, 0, 0)
                    self.nid_button = W.QPushButton("Fill from NID (online)")
                    self.nid_button.setToolTip(
                        "Ask the National Inventory of Dams web service for the height and "
                        "storage this dam's record is missing. Only blanks are filled; DWR "
                        "values and typed values are kept. No other network call is made.")
                    self.est_button = W.QPushButton("Estimate from DEM")
                    self.est_button.setToolTip(
                        "Screening estimate from the DEM: height = crest along the dam line "
                        "minus the lowest ground just downstream; storage = DEM volume below "
                        "the crest in the basin upstream. Fills only blank values.")
                    self.nid_status, self.est_status = W.QLabel(""), W.QLabel("")
                    for lab in (self.nid_status, self.est_status):
                        lab.setWordWrap(True)
                        lab.setStyleSheet("color: {};".format(TOKENS["muted"]))
                    fr.addWidget(self.nid_button, 0, 0)
                    fr.addWidget(self.nid_status, 0, 1)
                    fr.addWidget(self.est_button, 1, 0)
                    fr.addWidget(self.est_status, 1, 1)
                    fr.setColumnStretch(1, 1)
                    grid.addWidget(self.fill_box, row, 1, 1, 3)
                    row += 1
            grid.setColumnStretch(3, 1)
            outer.addLayout(grid)
            outer.addStretch(1)
            self.layer.currentIndexChanged.connect(self.layer_changed)
            self.county.currentIndexChanged.connect(self.fill_features)
            self.search.returnPressed.connect(self.fill_features)
            self.show_all.toggled.connect(self.fill_features)
            self.feature.currentIndexChanged.connect(self.values_from_feature)
            self.nid_button.clicked.connect(self.fill_online)
            self.est_button.clicked.connect(self.estimate)

        @_safe(what="the dam page")
        def initializePage(self):
            wiz = self.wizard()
            keep = self.answers().get("layer_id", "")
            fill_layers(self.layer, "vector", True, keep)
            if (getattr(wiz, "found", None) or {}).get("dwr"):
                self.layer.insertItem(1, "DWR dams (suite data folder)", SUITE_DWR)
                if not keep or keep == SUITE_DWR:
                    self.layer.setCurrentIndex(1)
            self.layer_changed()

        def suite(self):
            return (self.layer.currentData() or "") == SUITE_DWR

        def source(self):
            lid = self.layer.currentData() or ""
            if lid == SUITE_DWR:
                return None                  # read by suite_dams.dams(), not as a layer
            return _layer(lid)

        def county_field(self, lyr):
            names = dict((f.name().lower(), f.name()) for f in lyr.fields()) if lyr else {}
            for n in COUNTY_FIELDS:
                if n in names:
                    return names[n]
            return ""

        @_safe(what="the dam layer")
        def layer_changed(self, *_):
            from . import suite_dams as SD
            suite = self.suite()
            lyr = self.source()
            names = [f.name() for f in lyr.fields()] if lyr is not None else []
            guess = wl.field_mapping(names)
            for key, combo in self.field_combo.items():
                combo.blockSignals(True)
                combo.clear()
                combo.addItem("(none)", "")
                for n in names:
                    combo.addItem(n, n)
                i = combo.findData(guess["mapping"].get(key, ""))
                combo.setCurrentIndex(max(i, 0))
                combo.blockSignals(False)
                combo.setVisible(not suite)          # the suite's records need no field mapping
            self.field_head.setVisible(not suite)
            for w in (self.search, self.search_label, self.show_all):
                w.setVisible(suite)
            cfield = self.county_field(lyr)
            self.county.blockSignals(True)
            self.county.clear()
            self.county.addItem("All counties", "")
            if suite:
                for v in SD.ND_COUNTIES:
                    self.county.addItem(v, v)
            elif cfield:
                values = set()
                for k, f in enumerate(lyr.getFeatures()):
                    if k >= 20000:
                        break
                    v = f[cfield]
                    if v not in (None, "") and not (hasattr(v, "isNull") and v.isNull()):
                        values.add(str(v).strip())
                for v in sorted(values, key=str.lower):
                    self.county.addItem(v, v)
            self.county.blockSignals(False)
            self.county.setVisible(suite or bool(cfield))
            self.county_label.setVisible(suite or bool(cfield))
            if suite:
                self.layer_crs = "EPSG:4326"
            elif lyr is None:
                self.layer_crs = self.project_crs()
                self.note.setText("No layer: type the values below. The location is in the "
                                  "project CRS ({}).".format(self.layer_crs))
            else:
                self.layer_crs = lyr.crs().authid()
                miss = guess["missing"]
                self.note.setText(
                    "Fields matched: {}.{}".format(
                        len(guess["mapping"]), " Not found: {}.".format(", ".join(miss))
                        if miss else ""))
            self.point_label.setText("Location (x, y) in {}".format(self.layer_crs or "?"))
            self.fill_features()

        @_safe(what="the dam list")
        def fill_features(self, *_):
            """The dams of the source (of the chosen county), 'Name (ID)' when both are known."""
            if self.suite():
                return self.fill_suite()
            lyr = self.source()
            mapping = dict((k, c.currentData()) for k, c in self.field_combo.items()
                           if c.currentData())
            id_field, name_field = mapping.get("F_ID"), mapping.get("F_NAME")
            cfield, county = self.county_field(lyr), self.county.currentData() or ""
            self.feature.blockSignals(True)
            self.feature.clear()
            if lyr is not None:
                selected = list(lyr.selectedFeatureIds())
                items = []
                for f in lyr.getFeatures():
                    if county and str(f[cfield] or "").strip() != county:
                        continue
                    fid_text = str(f[id_field]) if id_field else "feature {}".format(f.id())
                    name = str(f[name_field] or "").strip() if name_field else ""
                    items.append(("{} ({})".format(name, fid_text) if name else fid_text, f.id()))
                    if len(items) >= 5000:
                        break
                for label, fid in sorted(items, key=lambda t: t[0].lower()):
                    self.feature.addItem(label, fid)
                if selected:
                    i = self.feature.findData(selected[0])
                    if i >= 0:
                        self.feature.setCurrentIndex(i)
            self.feature.blockSignals(False)
            self.values_from_feature()

        def fill_suite(self):
            """suite_dams.dams() for the county / name; the NID layer fills the blanks."""
            from . import suite_dams as SD
            from .suite_dams import nid_fill
            found = self.wizard().found
            county, name = self.county.currentData() or "", self.search.text().strip()
            hidden = []
            self.rows = SD.dams(found.get("dwr"), name, county, lines_uri=found.get("dam_lines"),
                                hidden=None if self.show_all.isChecked() else hidden)
            nid_fill.fill_rows(self.rows, found.get("nid"))
            self.feature.blockSignals(True)
            self.feature.clear()
            for i, r in enumerate(self.rows):
                if r.get("error"):
                    continue
                text = "{} ({})".format(r["name"], r.get("id") or r.get("county") or "")
                if r.get("hidden"):
                    text += " – not a dam? {}".format(r["hidden"].split(":")[0])
                self.feature.addItem(text, i)
            self.feature.blockSignals(False)
            text = SD.match_count_text(self.rows, county, name)
            if hidden:
                text += "; {} record{} hidden (not a dam: void, removed, breached…)".format(
                    len(hidden), "" if len(hidden) == 1 else "s")
            self.note.setText(text)
            self.values_from_feature()

        def row(self):
            """The picked suite record (a dict), or {}."""
            if not self.suite():
                return {}
            i = self.feature.currentData()
            return self.rows[i] if isinstance(i, int) and 0 <= i < len(self.rows) else {}

        @staticmethod
        def project_crs():
            try:
                from qgis.core import QgsProject
                return QgsProject.instance().crs().authid()
            except Exception:
                return ""

        def put(self, param, value):
            self.value_edit[param].setText("" if value in (None, "") else
                                           (value if isinstance(value, str) else
                                            "{:g}".format(float(value))))

        @_safe(what="reading the dam's attributes")
        def values_from_feature(self, *_):
            for lab in (self.nid_status, self.est_status):
                lab.setText("")
            self._county_value = ""
            if self.suite():
                r = self.row()
                if r:
                    for param, key in (("DAM_ID", "id"), ("DAM_NAME", "name"),
                                       ("HEIGHT_FT", "height"), ("STORAGE_ACFT", "storage"),
                                       ("AREA_AC", "area")):
                        self.put(param, r.get(key))
                    for _key, param, _l, _k, _r in wl.DAM_FIELDS:
                        if param not in ("DAM_ID", "DAM_NAME", "HEIGHT_FT", "STORAGE_ACFT",
                                         "AREA_AC"):
                            self.put(param, None)
                    raw = (r.get("point") or "").split("[")[0].strip()
                    self.point_edit.setText(raw.replace(",", ", ") if raw else "")
                    self._county_value = r.get("county") or ""
                else:                            # no dam matches: nothing left from the last
                    for edit in list(self.value_edit.values()) + [self.point_edit]:
                        edit.setText("")
                self.refresh()
                return
            lyr = self.source()
            if lyr is None or self.feature.count() == 0:
                self.refresh()
                return
            fid = self.feature.currentData()
            feat = lyr.getFeature(fid)
            if not feat.isValid():
                self.refresh()
                return
            attrs = dict(zip([f.name() for f in lyr.fields()], feat.attributes()))
            mapping = dict((k, c.currentData()) for k, c in self.field_combo.items()
                           if c.currentData())
            values = wl.dam_values_from_attrs(attrs, mapping)
            for _key, param, _label, kind, _req in wl.DAM_FIELDS:
                v = values.get(param)
                self.value_edit[param].setText("" if v is None else
                                               (v if kind == "text" else "{:g}".format(v)))
            geom = feat.geometry()
            if geom is not None and not geom.isEmpty():
                p = geom.centroid().asPoint()
                self.point_edit.setText(wl.point_text(p.x(), p.y()).replace(",", ", "))
            cf = self.county_field(lyr)
            self._county_value = str(attrs.get(cf) or "").strip() if cf else ""
            self.refresh()

        def read(self):
            """(dam values, point, problems) from the page as it stands."""
            dam, problems = {}, []
            for _key, param, label, kind, _req in wl.DAM_FIELDS:
                t = self.value_edit[param].text().strip()
                if not t:
                    continue
                if kind == "text":
                    dam[param] = t
                else:
                    x = wl.clean_number(t)
                    if x is None:
                        problems.append("'{}' is not a number for: {}.".format(t, label))
                    else:
                        dam[param] = x
            pt = wl.parse_point(self.point_edit.text())
            if self.point_edit.text().strip() and pt is None:
                problems.append("The location must be two numbers, x and y.")
            return dam, pt, problems

        def sources(self, dam):
            """{param: where the value comes from} for the height and storage of a suite
            record: the record's source while the box still holds the record's value,
            'typed in' once it was changed."""
            from .suite_dams import nid_fill
            out, r = {}, self.row()
            for param, key, _label in SOURCE_ROWS:
                val = dam.get(param)
                if not r or val is None:
                    continue
                rec = nid_fill.number(r.get(key))
                out[param] = (r.get(key + "_source") or "DWR") if rec is not None and \
                    abs(rec - val) < 0.05 else "typed in"
            return out

        def refresh(self, *_):
            """The row marks, the DamCard and the fill buttons follow every edit."""
            from . import suite_dams as SD
            dam, pt, _problems = self.read()
            src = self.sources(dam)
            notes = []
            for _key, param, label, kind, req in wl.DAM_FIELDS:
                t = self.value_edit[param].text().strip()
                where = src.get(param, "")
                if not t:
                    set_mark(self.mark[param], "bad" if req else "empty",
                             "Required" if req else "Optional: improves the estimate")
                elif param not in dam:
                    set_mark(self.mark[param], "bad", "Not a number")
                elif where and where not in ("DWR", "typed in"):
                    set_mark(self.mark[param], "flag", SD.source_text(where))
                    short = dict((pp, lab) for pp, _k, lab in SOURCE_ROWS).get(param, label)
                    notes.append("{}: {}, not the DWR record.".format(short,
                                                                       SD.source_text(where)))
                else:
                    set_mark(self.mark[param], "ok", SD.source_text(where))
            if pt is not None:
                set_mark(self.mark["point"], "ok")
            else:
                set_mark(self.mark["point"], "bad", "Required: x, y of a point on the crest")
            parts = [dam.get("DAM_NAME") or dam.get("DAM_ID") or ""]
            if getattr(self, "_county_value", ""):
                parts.append(self._county_value)
            if dam.get("HEIGHT_FT") is not None:
                parts.append("{:g} ft".format(dam["HEIGHT_FT"]))
            if dam.get("STORAGE_ACFT") is not None:
                parts.append("{:,.0f} ac-ft".format(dam["STORAGE_ACFT"]))
            title = " · ".join(p for p in parts if p) if parts[0] else ""
            sub = []
            r = self.row()
            if dam.get("DAM_ID") and dam.get("DAM_NAME"):
                sub.append("DWR record {}".format(dam["DAM_ID"]) if r else
                           "ID {}".format(dam["DAM_ID"]))
            if r.get("nid"):
                sub.append("NID {}".format(r["nid"]))
            if not r and self.layer.currentText() and self.layer.currentData():
                sub.append(self.layer.currentText())
            self.card.set_context(title, " · ".join(sub))
            if r.get("hidden"):
                notes.insert(0, "Not a dam to model? {}".format(r["hidden"]))
            try:
                a = dict(self.answers())
                a["dam"], a["point"] = dam, ((pt[0], pt[1], self.layer_crs) if pt else None)
                _errors, warnings = wl.validate_page("dam", a)
            except Exception:                                    # noqa
                warnings = []
            self.card.set_warnings(notes + warnings)    # sources and notes: amber, not red
            blank = r and not r.get("error") and (dam.get("HEIGHT_FT") is None or
                                                   dam.get("STORAGE_ACFT") is None)
            from .suite_dams import nid_fill
            self.nid_button.setEnabled(bool(blank) and not self._nid_busy and
                                       bool(nid_fill.online_urls(self.work_row(dam))))
            self.est_button.setEnabled(bool(blank) and not self._est_busy and
                                       bool(r.get("point")))
            self.est_button.setText("Estimate from DEM" if not blank or self.dem_for_estimate()
                                    else "Choose DEM and estimate\u2026")
            # the fill buttons show while a height or storage is missing (or say what they did)
            said = self.nid_status.text() or self.est_status.text()
            self.fill_box.setVisible(self.suite() and bool(blank or said or self._nid_busy or
                                                           self._est_busy))
            if blank and not self.nid_button.isEnabled() and not self._nid_busy and \
                    not self.nid_status.text():
                self.nid_status.setText("No NID id or dam point to look up.")
            if blank and not self.est_button.isEnabled() and not self._est_busy and \
                    not self.est_status.text():
                self.est_status.setText("No dam point to estimate at.")

        def work_row(self, dam):
            """The picked record with the page's typed values (a copy)."""
            w = dict(self.row())
            w["height"] = dam.get("HEIGHT_FT")
            w["storage"] = dam.get("STORAGE_ACFT")
            return w

        def take(self, r, keys):
            """Write the filled keys of the record copy r back to the record and the page."""
            i = self.feature.currentData()
            if isinstance(i, int) and 0 <= i < len(self.rows):
                for key in keys:
                    self.rows[i][key] = r[key]
                    self.rows[i][key + "_source"] = r.get(key + "_source", "")
                if r.get("nid") and not self.rows[i].get("nid"):
                    self.rows[i]["nid"] = r["nid"]
            for param, key, _label in SOURCE_ROWS:
                if key in keys:
                    self.put(param, r[key])

        @_safe(what="the NID lookup")
        def fill_online(self, *_):
            from . import suite_dams as SD
            from .suite_dams import nid_fill
            dam, _pt, _p = self.read()
            r0 = self.row()
            work = self.work_row(dam)
            urls = nid_fill.online_urls(work)
            if not r0 or not urls or self._nid_busy:
                self.nid_status.setText("No NID id or dam point to look up.")
                return
            fetch = self.fetch or SD.nid_get
            self._nid_busy = True
            self.nid_button.setEnabled(False)
            self.nid_status.setText("Asking the NID…")
            answers, errors, picked = [], [], self.feature.currentData()

            def step(i):
                if i >= len(urls):
                    return finish()
                kind, url = urls[i]

                def done(text, error):
                    if error:
                        errors.append(error)
                    else:
                        answers.append((kind, text))
                        if nid_fill.fill_online(dict(work), [(kind, text)])[0]:
                            return finish()       # filled: no second request
                    step(i + 1)
                fetch(url, done)

            def finish():
                self._nid_busy = False
                if self.feature.currentData() != picked:
                    self.refresh()                # the engineer picked another dam
                    return
                filled, msg = nid_fill.fill_online(work, answers)
                if not answers and errors:
                    msg = "The NID did not answer ({}): type the values in.".format(errors[0])
                self.take(work, filled)
                self.nid_status.setText(msg)
                self.refresh()
            step(0)

        def dem_for_estimate(self):
            """This dam's DEM in the Dams folder, else a raster file loaded in the project
            that covers the dam; '' when none (the button then asks for one)."""
            from .suite_dams import nid_fill
            r = self.row()
            if not r:
                return ""
            hits = wl.dam_dem_candidates(self.answers().get("dams_folder"), r.get("id"))
            if hits:
                return hits[0]
            ll = nid_fill.parse_point(r.get("point"))
            if ll is None:
                return ""
            try:
                from qgis.core import (QgsCoordinateReferenceSystem, QgsCoordinateTransform,
                                       QgsPointXY, QgsProject, QgsRasterLayer)
                wgs = QgsCoordinateReferenceSystem("EPSG:4326")
                for lyr in QgsProject.instance().mapLayers().values():
                    src = lyr.source().split("|")[0] if isinstance(lyr, QgsRasterLayer) else ""
                    if not src or not os.path.isfile(src):
                        continue
                    p = QgsCoordinateTransform(wgs, lyr.crs(), QgsProject.instance()) \
                        .transform(QgsPointXY(ll[0], ll[1]))
                    if lyr.extent().contains(p):
                        return src
            except Exception:                                    # noqa
                pass
            return ""

        @_safe(what="the DEM estimate")
        def estimate(self, *_):
            from . import suite_dams as SD
            from .suite_dams import nid_fill
            r0 = self.row()
            if not r0 or self._est_busy:
                return
            dem = self.dem_for_estimate()
            if not dem:
                res = W.QFileDialog.getOpenFileName(self, "DEM for the estimate", "",
                                                    "Raster (*.tif *.tiff *.vrt *.img)")
                dem = res[0] if isinstance(res, (tuple, list)) else res
                if not dem:
                    return
            z = self.answers().get("z_units") or ""
            z = "" if z == "auto" else z
            lines_uri = self.wizard().found.get("dam_lines")
            sid, point = r0.get("structure_id"), nid_fill.parse_point(r0.get("point"))
            dam, _pt, _p = self.read()
            work_row, picked = self.work_row(dam), self.feature.currentData()
            self._est_busy = True
            self.est_button.setEnabled(False)
            name = os.path.basename(dem)
            self.est_status.setText("Estimating from {}… 0 %".format(name))

            def work(progress):
                from .suite_dams import dem_estimate as DE
                line = DE.dam_line(lines_uri, sid)
                return DE.estimate(dem, point, line, z,
                                   progress=lambda pct, text="": progress(pct))

            def show_progress(pct):
                if self._est_busy:
                    self.est_status.setText("Estimating from {}… {:.0f} %".format(name, pct))

            def done(res, error):
                from .suite_dams import dem_estimate as DE
                self._est_busy = False
                if self.feature.currentData() != picked:
                    self.refresh()
                    return
                if error:
                    self.est_status.setText("No estimate: {}".format(error))
                    self.refresh()
                    return
                filled = []
                for key, val, minimum in (("height", res.get("height_ft"), 0.5),
                                          ("storage", res.get("storage_acft"), 0.1)):
                    if val is None or val < minimum or work_row.get(key) is not None:
                        continue                   # typed / record values are kept
                    work_row[key] = round(float(val), 1)
                    work_row[key + "_source"] = DE.source(key, res)
                    filled.append(key)
                self.take(work_row, filled)
                msg = ("Estimated {} from the DEM (screening only)".format(" and ".join(filled))
                       if filled else "The DEM gave no usable value")
                self.est_status.setText("{}. {}".format(msg, " ".join(res.get("notes", [])[1:])))
                self.est_status.setToolTip(" ".join(res.get("notes", [])))
                self.refresh()
            (self.run_task or SD.run_task)(work, done, show_progress)

        @_safe(default=False, what="the dam page")
        def validatePage(self):
            a = self.answers()
            dam, pt, problems = self.read()
            if problems:
                self.show_problems(problems)
                return False
            a["layer_id"] = self.layer.currentData() or ""
            a["feature_id"] = self.feature.currentData()
            a["mapping"] = dict((k, c.currentData()) for k, c in self.field_combo.items()
                                if c.currentData())
            a["dam"] = dam
            a["point"] = (pt[0], pt[1], self.layer_crs) if pt else None
            a["sources"] = self.sources(dam)   # shown on the Review page
            return self.check("dam")

    # -- 2. the terrain -----------------------------------------------------------------
    class TerrainPage(Page):
        def __init__(self):
            Page.__init__(self)
            self.setTitle("Terrain")
            self.setSubTitle("The LiDAR DEM (a GeoTIFF or a VRT mosaic loaded in QGIS), its "
                             "elevation units and the roughness.")
            outer = W.QVBoxLayout(self)
            form = W.QFormLayout()
            self.dem, self.z = W.QComboBox(), W.QComboBox()
            for zid in wl.Z_IDS:
                self.z.addItem(wl.Z_LABELS[zid], zid)
            self.nlcd, self.manning = W.QComboBox(), W.QComboBox()
            self.n = W.QDoubleSpinBox()
            self.n.setDecimals(3)
            self.n.setRange(0.005, 1.0)
            self.n.setSingleStep(0.005)
            self.n.setValue(0.06)
            box, self.folder = path_row(self, folder=True)
            form.addRow("DEM", self.dem)
            # the live coverage line: does the chosen DEM cover the dam point?
            self.coverage = W.QLabel("")
            self.coverage.setWordWrap(True)
            form.addRow("", self.coverage)
            self.dem_fact = W.QLabel("USGS 3DEP: EPSG:5070, metres, GeoTIFF. ND DWR LiDAR: feet, often EPSG:3721, .img. Saved copy is EPSG:5070, metres.")
            self.dem_fact.setWordWrap(True)
            self.dem_fact.setStyleSheet("color: {};".format(TOKENS["muted"]))
            form.addRow("", self.dem_fact)
            self.dem_res = W.QComboBox()
            self.dem_res.addItem("10 m", 0)
            self.dem_res.addItem("1 m", 1)
            self.dem_download = W.QPushButton("Download DEM for this dam\u2026")
            self.dem_download.setToolTip("USGS 3DEP, half the max extent (10 mi at the 20 mi default) on each side of the dam point, EPSG:5070 metres GeoTIFF.")
            self.dem_download.clicked.connect(self.download_dem)
            form.addRow("Download", self.dem_res)
            form.addRow("", self.dem_download)
            form.addRow("DEM elevation units", self.z)
            form.addRow("Dams folder", box)
            outer.addLayout(form)
            # roughness: optional, folded; the header shows the values so nothing hides
            rough = W.QWidget()
            f2 = W.QFormLayout(rough)
            f2.addRow("NLCD land cover (optional)", self.nlcd)
            f2.addRow("or a Manning's n raster (optional)", self.manning)
            f2.addRow("Manning's n where there is no raster", self.n)
            self.advanced = Collapsible("Advanced", rough)
            outer.addWidget(self.advanced)
            self.problems = red_label()
            outer.addWidget(self.problems)
            outer.addStretch(1)
            for sig in (self.nlcd.currentIndexChanged, self.manning.currentIndexChanged,
                        self.n.valueChanged):
                sig.connect(self.sync_advanced)
            self.dem.currentIndexChanged.connect(self.show_coverage)

        def sync_advanced(self, *_):
            parts = ["Manning's n {:.3f}".format(self.n.value())]
            if self.manning.currentData():
                parts.append("n raster " + self.manning.currentText())
            parts.append("NLCD " + self.nlcd.currentText() if self.nlcd.currentData()
                         else "no land cover")
            self.advanced.set_title("Advanced  \u00b7  " + " \u00b7 ".join(parts))

        def dem_layer(self):
            ref = self.dem.currentData() or ""
            lyr = _layer(ref)
            if lyr is None and ref and os.path.isfile(ref):
                from qgis.core import QgsRasterLayer
                lyr = QgsRasterLayer(ref, os.path.basename(ref))
            return lyr if lyr is not None and lyr.isValid() else None

        @_safe(what="the DEM coverage check")
        def show_coverage(self, *_):
            """✓ the DEM covers the dam point / ✕ it does not / ○ nothing to check yet."""
            pt = self.answers().get("point")
            lyr = self.dem_layer()
            if lyr is None or not pt:
                state, text = "empty", ("Choose a DEM: its coverage of the dam is checked here."
                                        if lyr is None else "No dam location to check against.")
            else:
                from qgis.core import (QgsCoordinateReferenceSystem, QgsCoordinateTransform,
                                       QgsPointXY, QgsProject)
                p = QgsPointXY(pt[0], pt[1])
                src = QgsCoordinateReferenceSystem(pt[2]) if len(pt) > 2 and pt[2] else lyr.crs()
                if src.isValid() and src != lyr.crs():
                    p = QgsCoordinateTransform(src, lyr.crs(), QgsProject.instance()).transform(p)
                if lyr.extent().contains(p):
                    state, text = "ok", "The DEM covers the dam point."
                else:
                    state, text = "bad", ("The DEM does not cover the dam point: download one for "
                                          "this dam or choose another.")
            mark, colour = ROW_MARKS[state]
            self.coverage.setText("{} {}".format(mark, text))
            self.coverage.setStyleSheet("color: {};".format(colour if state != "empty"
                                                            else TOKENS["muted"]))

        @_safe(what="the DEM download")
        def download_dem(self, *_):
            import processing
            from .damsuite.suite_config import dem_buffer_mi
            a = self.answers()
            pt = a.get("point")
            point = "{},{}".format(pt[0], pt[1]) if pt else ""
            folder = self.folder.text().strip() or os.path.expanduser("~")
            out = os.path.join(folder, "terrain")
            try:
                from qgis.core import QgsApplication
                alg = "nddam:downloaddem"
                if QgsApplication.processingRegistry().algorithmById(alg) is None:
                    alg = "ndbreach:downloaddem"
            except Exception:
                alg = "nddam:downloaddem"
            exec_tool_dialog(processing, alg, {
                "SOURCE": 0, "RES": self.dem_res.currentData(), "SCOPE": 0,
                "DAM_POINT": point, "OUT": out,
                "BUFFER_MI": dem_buffer_mi(a.get("max_extent_mi")),     # half the max extent
                "DAM_ID": str((a.get("dam") or {}).get("DAM_ID") or ""),
            }, parent=self)
            dam = a.get("dam") or {}
            # This dam's download only: <DAM_ID>_*m_5070.tif (or dam_<x>_<y>_* of its point)
            hits = wl.dam_dem_candidates(folder, dam.get("DAM_ID"),
                                         point_stem=wl.point_dem_stem(pt))
            if hits:
                self.dem.addItem(os.path.basename(hits[0]), hits[0])
                self.dem.setCurrentIndex(self.dem.count() - 1)

        @_safe(what="the terrain page")
        def initializePage(self):
            a = self.answers()
            fill_layers(self.dem, "raster", False, a.get("dem_id", ""))
            folder = a.get("dams_folder") or self.folder.text().strip()
            # Only this dam's DEMs: never another dam's folder, never a walk from '/'.
            hits = wl.dam_dem_candidates(folder, (a.get("dam") or {}).get("DAM_ID"))
            if hits:
                self.dem.insertItem(0, os.path.basename(hits[0]) + " (from dam folder)", hits[0])
                self.dem.setCurrentIndex(0)
            fill_layers(self.nlcd, "raster", True, a.get("nlcd_id", ""))
            fill_layers(self.manning, "raster", True, a.get("manning_id", ""))
            self.z.setCurrentIndex(max(self.z.findData(a.get("z_units", "auto")), 0))
            self.n.setValue(a.get("manning_n", 0.06))
            dams = a.get("dams_folder") or ""
            if wl._in_suite_pack(dams):        # the pack is read-only: never default to it
                dams = ""
            self.folder.setText(dams or os.path.join(os.path.expanduser("~"), "ndbreach_dams"))
            self.sync_advanced()
            self.show_coverage()

        @_safe(default=False, what="the terrain page")
        def validatePage(self):
            a = self.answers()
            a["dem_id"] = self.dem.currentData() or ""
            a["dem_name"] = self.dem.currentText()
            a["z_units"] = self.z.currentData() or "auto"
            a["nlcd_id"] = self.nlcd.currentData() or ""
            a["nlcd_name"] = self.nlcd.currentText()
            a["manning_id"] = self.manning.currentData() or ""
            a["manning_name"] = self.manning.currentText()
            a["manning_n"] = self.n.value()
            a["dams_folder"] = self.folder.text().strip()
            wl.apply_dam_record(a)       # v3.2.0: empty dam values from the dam's dam.json
            return self.check("terrain")

    # -- 3. the scenario ----------------------------------------------------------------
    class ScenarioPage(Page):
        def __init__(self):
            Page.__init__(self)
            self.setTitle("Scenario")
            self.setSubTitle("Sunny-day failure, or a flood (PMF) with and without the breach.")
            outer = W.QVBoxLayout(self)
            form = W.QFormLayout()
            self.method, self.scenario = W.QComboBox(), W.QComboBox()
            for m in wl.METHOD_IDS:
                self.method.addItem(brmod.METHOD_LABELS[m], m)
            for s in wl.SCENARIO_IDS:
                self.scenario.addItem(wl.SCENARIO_LABELS[s], s)
            form.addRow("Breach", self.method)
            form.addRow("Scenario", self.scenario)
            outer.addLayout(form)
            self.flood = W.QGroupBox("Flood scenario inputs")
            f2 = W.QFormLayout(self.flood)
            box1, self.inflow = path_row(self, "CSV files (*.csv);;All files (*)")
            box2, self.spillway = path_row(self, "CSV files (*.csv);;All files (*)")
            self.pool, self.trigger_value = W.QLineEdit(), W.QLineEdit()
            self.trigger = W.QComboBox()
            for t in wl.TRIGGER_IDS:
                self.trigger.addItem(wl.TRIGGER_LABELS[t], t)
            f2.addRow("PMF inflow CSV (hr, cfs)", box1)
            f2.addRow("Spillway rating CSV (ft, cfs; optional)", box2)
            f2.addRow("Normal pool elevation (ft)", self.pool)
            f2.addRow("The dam fails", self.trigger)
            f2.addRow("Failure time (hr) or elevation (ft)", self.trigger_value)
            outer.addWidget(self.flood)
            self.problems = red_label()
            outer.addWidget(self.problems)
            self.scenario.currentIndexChanged.connect(self.toggle)

        @_safe(what="the scenario page")
        def toggle(self, *_):
            self.flood.setEnabled(self.scenario.currentData() == "flood")

        @_safe(what="the scenario page")
        def initializePage(self):
            a = self.answers()
            self.method.setCurrentIndex(max(self.method.findData(a.get("method")), 0))
            self.scenario.setCurrentIndex(max(self.scenario.findData(a.get("scenario")), 0))
            self.inflow.setText(a.get("inflow_csv", ""))
            self.spillway.setText(a.get("spillway_csv", ""))
            for edit, key in ((self.pool, "normal_pool_ft"), (self.trigger_value, "trigger_value")):
                v = a.get(key)
                edit.setText("" if v is None else "{:g}".format(v))
            self.trigger.setCurrentIndex(max(self.trigger.findData(a.get("trigger")), 0))
            self.toggle()

        @_safe(default=False, what="the scenario page")
        def validatePage(self):
            a, problems = self.answers(), []
            a["method"] = self.method.currentData()
            a["scenario"] = self.scenario.currentData()
            a["inflow_csv"] = self.inflow.text().strip()
            a["spillway_csv"] = self.spillway.text().strip()
            a["normal_pool_ft"] = number_or_none(self.pool, "normal pool elevation", problems)
            a["trigger"] = self.trigger.currentData()
            a["trigger_value"] = number_or_none(self.trigger_value, "failure time or elevation",
                                                problems)
            if problems:
                self.show_problems(problems)
                return False
            return self.check("scenario")

    # -- 4. models and cell size ----------------------------------------------------------
    class ModelPage(Page):
        def __init__(self):
            Page.__init__(self)
            self.setTitle("Models and cell size")
            self.setSubTitle("A finer cell is more accurate and much slower: LISFLOOD-FP's "
                             "run time grows about with the cube of 1/cell size.")
            outer = W.QVBoxLayout(self)
            form = W.QFormLayout()
            self.tier = W.QComboBox()
            for t in wl.TIER_IDS:
                self.tier.addItem(wl.TIER_LABELS[t], t)
            self.cell = W.QDoubleSpinBox()
            self.cell.setDecimals(1)
            self.cell.setRange(1.0, 200.0)
            self.cell.setSingleStep(1.0)
            self.cell.setValue(10.0)
            self.cell.setSuffix(" m")
            form.addRow("Models", self.tier)
            form.addRow("Cell size (m; 10 m = 32.8 ft)", self.cell)
            outer.addLayout(form)
            self.button = W.QPushButton("Estimate the run time")
            self.result = W.QLabel("")
            self.result.setWordWrap(True)
            outer.addWidget(self.button)
            outer.addWidget(self.result)
            self.problems = red_label()
            outer.addWidget(self.problems)
            self.button.clicked.connect(self.estimate)
            self.cell.valueChanged.connect(self.rescale)
            self.tier.currentIndexChanged.connect(self.rescale)

        def collect(self):
            a = self.answers()
            a["tier"] = self.tier.currentData()
            a["cell_m"] = float(self.cell.value())
            return a

        @_safe(what="the models page")
        def initializePage(self):
            a = self.answers()
            self.tier.setCurrentIndex(max(self.tier.findData(a.get("tier")), 0))
            self.cell.setValue(a.get("cell_m", 10.0))
            self.result.setText("Press the button to time a small test flood on this computer "
                                "and estimate this dam's run.")
            self.rescale()

        @_safe(what="the run-time estimate")
        def rescale(self, *_):
            a = self.collect()
            lines = self.wizard().session.cache.lines(a)
            if lines is not None:
                self.result.setText("\n".join(lines))

        @_safe(what="the run-time estimate")
        def estimate(self, *_):
            a = self.collect()
            wiz = self.wizard()
            self.result.setText("Setting up the dam and timing a test flood\u2026")
            _busy(True)
            try:
                lines = wiz.session.cache.compute(a)
            except wl.EstimateError as e:
                lines = ["Could not estimate: {}".format(e)]
            finally:
                _busy(False)
            self.result.setText("\n".join(lines))

        @_safe(default=False, what="the models page")
        def validatePage(self):
            self.collect()
            return self.check("model")

    # -- 5. review ------------------------------------------------------------------------
    class ReviewPage(Page):
        def __init__(self):
            Page.__init__(self)
            self.setTitle("Review")
            self.setSubTitle("Check the values, then open the tool.")
            lay = W.QVBoxLayout(self)
            self.text = W.QPlainTextEdit()
            self.text.setReadOnly(True)
            lay.addWidget(self.text)
            self.problems = red_label()
            lay.addWidget(self.problems)

        @_safe(what="the review page")
        def initializePage(self):
            wiz = self.wizard()
            a = wiz.answers
            errors, warnings = wl.validate_all(a, models=_find_models())
            est = wiz.session.cache.lines(a)
            text = wl.review_text(a, est, warnings)
            src = dict((k, v) for k, v in (a.get("sources") or {}).items() if v != "DWR")
            if src:                    # a height / storage that is not the DWR record's
                from . import suite_dams as SD
                text += "\n\nWhere the values come from:\n" + "\n".join(
                    "  - {}: {}".format("Height" if k == "HEIGHT_FT" else "Storage",
                                        SD.source_text(v)) for k, v in sorted(src.items()))
            if errors:
                text = "PROBLEMS TO FIX (go Back):\n  - " + "\n  - ".join(errors) + "\n\n" + text
            self.text.setPlainText(text)
            try:
                finish = getattr(W.QWizard, "FinishButton", None) or \
                    W.QWizard.WizardButton.FinishButton
                wiz.setButtonText(finish, "Open the tool\u2026")
            except Exception:
                pass

        @_safe(default=False, what="the review page")
        def validatePage(self):
            errors, _w = wl.validate_all(self.wizard().answers, models=_find_models())
            if errors:
                self.show_problems(errors)
                return False
            return True

    class Wizard(W.QWizard):
        def __init__(self, parent=None):
            W.QWizard.__init__(self, parent)
            self.setWindowTitle("{} \u2014 Model one dam".format(TITLE))
            self.answers = wl.default_answers()
            self.found = {}              # the suite data folder's layers, when Setup scanned it
            self.session = EstimateSession()
            for page in (DamPage(), TerrainPage(), ScenarioPage(), ModelPage(), ReviewPage()):
                self.addPage(page)
            try:
                wrap_wizard(self)
                fit_window(self, (800, 780))     # the dam page's two groups
            except Exception:
                pass

    _CLASSES.update({"Wizard": Wizard, "DamPage": DamPage, "TerrainPage": TerrainPage,
                     "ScenarioPage": ScenarioPage, "ModelPage": ModelPage,
                     "ReviewPage": ReviewPage})
    return _CLASSES


# ------------------------------------------------------------------------------- entry point
def open_tool(answers):
    """Open 'Run breach model for one dam' pre-filled from the wizard's answers."""
    import processing
    params = wl.build_rundam_params(answers)
    return processing.execAlgorithmDialog(wl.ALGORITHM_ID, params)


def run_wizard(iface=None, found=None, data_folder=""):
    """Show the wizard; on Finish open the tool's dialog. Returns the parameters given to the
    tool, or None when the wizard was cancelled or could not run.

    found / data_folder: DamSafety Setup's scan of the suite data folder (Start here\u2026 asks
    for the folder, then continues here): its DWR dams become a "Dams from" choice."""
    try:
        parent = iface.mainWindow() if iface is not None else None
        wiz = _classes()["Wizard"](parent)
        if found:
            wiz.found = dict(found)
        if data_folder:
            wiz.answers["data_folder"] = data_folder
        run = getattr(wiz, "exec_", None) or getattr(wiz, "exec")
        if not run():
            return None
        params = wl.build_rundam_params(wiz.answers)
    except Exception as e:
        _message("critical", "The wizard could not start: {}".format(e), traceback.format_exc())
        return None
    try:
        open_tool(wiz.answers)
    except Exception as e:
        _message("critical", "The tool's dialog could not be opened: {}\n\nThe values were:\n{}"
                 .format(e, "\n".join("{} = {}".format(k, v) for k, v in sorted(params.items()))),
                 traceback.format_exc())
    return params
