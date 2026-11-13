# -*- coding: utf-8 -*-
"""Number inputs that show and accept the suite's display unit system (Core 1.1.0).

The logic is damsuite.display_units (pure Python): which unit a value is stored in, what it is
shown in, the factors and the relabelled text. This file holds the Qt side:

  UnitSpinBox            a QDoubleSpinBox for the plugins' own windows: storedValue() and
                         setStoredValue() talk in the stored unit (ft, acres ...), the box shows
                         the display unit. A default the engineer did not touch comes back
                         exactly as it went in (no ft -> m -> ft rounding).
  UnitNumberWrapper      the same for a Processing tool's dialog, given to a number parameter
                         through its widget_wrapper metadata. Only the standard tool dialog uses
                         it; the batch and model designer dialogs get QGIS's own widget in the
                         stored unit (a saved model or batch file always means feet).
  tag_parameter(p)       give a number parameter whose label declares a convertible unit
                         ('(ft', '(acres;' ...) the wrapper, when the display system is SI.
  UnitParamsMixin        an algorithm base: every parameter added through addParameter() is
                         tagged. Put it first in the bases: class _Base(UnitParamsMixin,
                         QgsProcessingAlgorithm).

Under the US system nothing is tagged and every dialog is exactly QGIS's own. The tool always
receives the value in the stored unit, so its keys (HEAD_FT ...), its log, "Copy as Python
command", qgis_process and the settings workbook are unchanged.
"""
import functools
import os

from ..damsuite import display_units as DU

try:
    from qgis.PyQt.QtWidgets import QDoubleSpinBox, QLabel
except Exception:                                                 # noqa - tests without Qt
    QDoubleSpinBox = object
    QLabel = None

_BIG = 1.0e12
STORED_UNIT_KEY = "stored_unit"


class _Exact(object):
    """Remembers the stored value last set, so an untouched box returns it exactly."""

    def __init__(self, unit, system_value):
        self.unit = unit
        self.system = system_value
        self.places = DU.decimals(unit, system_value, default=3)
        self.stored = None
        self.shown = None

    def show(self, stored):
        self.stored = None if stored is None else float(stored)
        self.shown = None if stored is None else round(DU.to_display(stored, self.unit,
                                                                     self.system), self.places)
        return self.shown

    def back(self, shown):
        if shown is None:
            return None
        if self.shown is not None and round(float(shown), self.places) == self.shown:
            return self.stored
        return DU.from_display(shown, self.unit, self.system)


def _limit(v, unit, system_value, fallback):
    if v is None:
        return fallback
    try:
        f = float(v)
    except (TypeError, ValueError):
        return fallback
    if abs(f) >= 1.0e300:
        return fallback
    return DU.to_display(f, unit, system_value)


class UnitSpinBox(QDoubleSpinBox):
    """A spin box in the display unit for a value stored in `stored_unit`."""

    def __init__(self, stored_unit, parent=None, minimum=None, maximum=None, system_value=None):
        super().__init__(parent)
        sysv = DU.system() if system_value is None else DU.normalize(system_value)
        self._x = _Exact(stored_unit, sysv)
        self.setDecimals(self._x.places)
        self.setRange(_limit(minimum, stored_unit, sysv, -_BIG),
                      _limit(maximum, stored_unit, sysv, _BIG))
        self.setSuffix(" " + DU.display_unit(stored_unit, sysv))
        self.setToolTip(self._tip())

    def _tip(self):
        if DU.factor(self._x.unit, self._x.system) == 1.0:
            return ""
        return ("Shown in {} (DamSafety Setup > Suite setup / status). The tool receives {}."
                .format(DU.display_unit(self._x.unit, self._x.system), self._x.unit))

    def storedUnit(self):
        return self._x.unit

    def setStoredValue(self, stored):
        shown = self._x.show(stored)
        self.setValue(0.0 if shown is None else shown)

    def storedValue(self):
        return self._x.back(self.value())


def _processing_wrapper_base():
    try:
        from processing.gui.wrappers import WidgetWrapper
        return WidgetWrapper
    except Exception:                                             # noqa - no Processing GUI
        return None


def _standard_dialog(dialog):
    try:
        from processing.gui.wrappers import dialogTypes, DIALOG_STANDARD
        return dialogTypes.get(dialog.__class__.__name__, DIALOG_STANDARD) == DIALOG_STANDARD
    except Exception:                                             # noqa
        return False


def _without_wrapper(param):
    """A copy of `param` without this module's widget_wrapper metadata."""
    p = param.clone()
    md = dict(p.metadata() or {})
    md.pop("widget_wrapper", None)
    p.setMetadata(md)
    return p


_WRAPPER = None


def wrapper_class():
    """UnitNumberWrapper, built on first use (Processing's WidgetWrapper is a GUI import)."""
    global _WRAPPER
    if _WRAPPER is not None:
        return _WRAPPER
    base = _processing_wrapper_base()
    if base is None:
        return None

    class UnitNumberWrapper(base):
        def __new__(cls, param, dialog, row=0, col=0, **kwargs):
            if _standard_dialog(dialog):
                return super().__new__(cls)
            # batch / model designer: QGIS's own widget, in the stored unit
            from processing.gui.wrappers import WidgetWrapperFactory
            plain = _without_wrapper(param)
            w = WidgetWrapperFactory.create_wrapper(plain, dialog, row, col)
            try:
                w._unit_param_keepalive = plain
            except Exception:                                     # noqa
                pass
            return w

        def __init__(self, param, dialog, row=0, col=0, **kwargs):
            self._stored_unit = kwargs.pop(STORED_UNIT_KEY, None) \
                or DU.unit_in_label(param.description())
            self._sys = DU.system()
            super().__init__(param, dialog, row, col, **kwargs)

        def createWidget(self, **kwargs):
            p = self.parameterDefinition()
            box = UnitSpinBox(self._stored_unit, minimum=p.minimum(), maximum=p.maximum(),
                              system_value=self._sys)
            box.valueChanged.connect(lambda _v: self.widgetValueHasChanged.emit(self))
            return box

        def createLabel(self):
            p = self.parameterDefinition()
            desc = DU.relabel(p.description(), self._sys)
            from qgis.core import QgsProcessingParameterDefinition
            if p.flags() & QgsProcessingParameterDefinition.FlagOptional:
                desc += self.tr(" [optional]")
            label = QLabel(desc)
            label.setWordWrap(True)
            label.setToolTip("{}\n\nShown in {}; the tool receives {}. As written in the tool: {}"
                             .format(p.name(), DU.display_unit(self._stored_unit, self._sys),
                                     self._stored_unit, p.description()))
            return label

        def setValue(self, value):
            if value is None or value == "":
                return
            try:
                self.widget.setStoredValue(float(value))
            except (TypeError, ValueError):
                pass

        def value(self):
            v = self.widget.storedValue()
            p = self.parameterDefinition()
            try:
                from qgis.core import QgsProcessingParameterNumber
                if p.dataType() == QgsProcessingParameterNumber.Integer and v is not None:
                    return int(round(v))
            except Exception:                                     # noqa
                pass
            return v

    _WRAPPER = UnitNumberWrapper
    return _WRAPPER


def tag_parameter(param, system_value=None):
    """Give `param` the unit wrapper when it is a number in a convertible unit and the display
    system is SI. Returns True when tagged. Never raises: a parameter it cannot tag keeps
    QGIS's own widget."""
    try:
        sysv = DU.system() if system_value is None else DU.normalize(system_value)
        if sysv != DU.SI:
            return False
        if param.type() != "number":
            return False
        md = dict(param.metadata() or {})
        if md.get("widget_wrapper"):
            return False                     # the tool chose its own widget
        unit = DU.unit_in_label(param.description())
        if unit is None or DU.factor(unit, sysv) == 1.0:
            return False
        cls = wrapper_class()
        if cls is None:
            return False
        md["widget_wrapper"] = {"class": cls, STORED_UNIT_KEY: unit}
        param.setMetadata(md)
        return True
    except Exception:                                             # noqa
        return False


def si_copies_of(results, feedback=None):
    """Core 1.4.0: under SI, the SI copies of the result tables a tool returned
    (display_units.write_si_csvs_of: SI_units/<name>.csv with an SI column beside each US
    one). Says so in the log. Never raises; returns the copies written."""
    try:
        made = DU.write_si_csvs_of(results)
    except Exception:                                             # noqa: BLE001
        return []
    if made and feedback is not None:
        try:
            folders = sorted({os.path.dirname(p) for p in made})
            feedback.pushInfo(
                "SI units: {} table(s) copied with SI columns beside the US ones, in {}. The "
                "original CSV files keep US units.".format(len(made), "; ".join(folders)))
        except Exception:                                         # noqa: BLE001
            pass
    return made


def _with_si_copies(fn):
    """processAlgorithm followed by si_copies_of(its results). functools.wraps keeps the
    tool's own name, doc and source (inspect.getsource still shows the tool's code)."""
    @functools.wraps(fn)
    def processAlgorithm(self, parameters, context, feedback):
        results = fn(self, parameters, context, feedback)
        si_copies_of(results, feedback)
        return results
    processAlgorithm._si_copies = True
    return processAlgorithm


class UnitParamsMixin(object):
    """Tags each number parameter as it is added (see tag_parameter), and (Core 1.4.0) writes
    the SI copies of the CSV tables a tool returns when the display units are SI."""

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        fn = cls.__dict__.get("processAlgorithm")
        if callable(fn) and not getattr(fn, "_si_copies", False):
            cls.processAlgorithm = _with_si_copies(fn)

    def addParameter(self, definition, createOutput=True):
        tag_parameter(definition)
        if createOutput is True:
            return super().addParameter(definition)
        return super().addParameter(definition, createOutput)


# -- the plugins' own windows -------------------------------------------------------------------
# A window builds its spin boxes and line edits in the stored unit as it always has, then calls
# adopt_spinbox / adopt_lineedit once. Under SI the widget then SHOWS the display unit while the
# window's own code keeps talking in the stored unit: value() / setValue() / setRange() /
# setMinimum() / setMaximum() / setSingleStep() / setSuffix() (spin box) and text() / setText()
# (line edit) are replaced on that one instance with converting versions, so no read or write
# site in the window changes. Under US nothing is replaced. Qt's own signals (valueChanged,
# textChanged) still carry the shown value: connect them to a slot that reads value() / text().

def _is_int_spin(spin):
    return not hasattr(spin, "decimals")


def adopt_spinbox(spin, stored_unit, label=None, system_value=None):
    """Make an existing QSpinBox / QDoubleSpinBox show `stored_unit` in the display system.
    `label` (a QLabel, optional) is relabelled. Returns True when converted."""
    sysv = DU.system() if system_value is None else DU.normalize(system_value)
    if sysv != DU.SI or DU.factor(stored_unit, sysv) == 1.0:
        return False
    if getattr(spin, "_unit_adopted", False):
        return True
    cls = type(spin)
    f = DU.factor(stored_unit, sysv)
    is_int = _is_int_spin(spin)
    x = _Exact(stored_unit, sysv)
    if is_int:
        x.places = 0
    # the state the window gave it, in the stored unit
    lo, hi = cls.minimum(spin), cls.maximum(spin)
    step = cls.singleStep(spin)
    cur = cls.value(spin)
    suffix = cls.suffix(spin)

    def _show(v):
        return int(round(v * f)) if is_int else v * f

    def set_range(a, b):
        cls.setRange(spin, _show(a), _show(b))

    def set_minimum(a):
        cls.setMinimum(spin, _show(a))

    def set_maximum(b):
        cls.setMaximum(spin, _show(b))

    def set_step(s):
        v = s * f
        cls.setSingleStep(spin, max(1, int(round(v))) if is_int else v)

    def set_value(v):
        shown = x.show(v)
        cls.setValue(spin, int(shown) if is_int else shown)

    def value():
        v = x.back(cls.value(spin))
        return int(round(v)) if is_int and v is not None else v

    def minimum():
        return cls.minimum(spin) / f

    def maximum():
        return cls.maximum(spin) / f

    def set_suffix(s):
        cls.setSuffix(spin, DU.resuffix(s, stored_unit, sysv))

    if not is_int:
        cls.setDecimals(spin, max(cls.decimals(spin), x.places))
    set_range(lo, hi)
    set_step(step)
    set_suffix(suffix)
    set_value(cur)
    spin.setRange, spin.setMinimum, spin.setMaximum = set_range, set_minimum, set_maximum
    spin.setSingleStep, spin.setValue, spin.value = set_step, set_value, value
    spin.minimum, spin.maximum, spin.setSuffix = minimum, maximum, set_suffix
    spin._unit_adopted = True
    tip = cls.toolTip(spin)
    note = ("Shown in {}; the tool works in {}.".format(DU.display_unit(stored_unit, sysv),
                                                         stored_unit))
    cls.setToolTip(spin, (tip + "\n\n" + note) if tip else note)
    if label is not None:
        relabel_widget(label, sysv)
    return True


def adopt_lineedit(edit, stored_unit, label=None, system_value=None, places=None):
    """Make a QLineEdit that holds one number (or nothing) in `stored_unit` show the display
    system: text() returns the stored-unit number as text, setText() takes one. Text that is not
    a number passes through unchanged (the window's own check reports it)."""
    sysv = DU.system() if system_value is None else DU.normalize(system_value)
    if sysv != DU.SI or DU.factor(stored_unit, sysv) == 1.0:
        return False
    if getattr(edit, "_unit_adopted", False):
        return True
    cls = type(edit)
    x = _Exact(stored_unit, sysv)
    if places is not None:
        x.places = places

    def _num(t):
        try:
            return float(str(t).strip().replace(",", ""))
        except (TypeError, ValueError):
            return None

    def set_text(t):
        v = _num(t)
        if v is None:
            cls.setText(edit, "" if t is None else str(t))
            return
        shown = x.show(v)
        cls.setText(edit, ("{:.%df}" % x.places).format(shown).rstrip("0").rstrip("."))

    def text():
        raw = cls.text(edit)
        v = _num(raw)
        if v is None:
            return raw
        back = x.back(v)
        return "{!r}".format(back) if back != int(back) else "{:g}".format(back)

    current = cls.text(edit)
    edit.setText, edit.text = set_text, text
    set_text(current)
    tip = cls.toolTip(edit)
    note = "Type the value in {}; the tool works in {}.".format(
        DU.display_unit(stored_unit, sysv), stored_unit)
    cls.setToolTip(edit, (tip + "\n\n" + note) if tip else note)
    edit._unit_adopted = True
    if label is not None:
        relabel_widget(label, sysv)
    return True


def relabel_widget(label, system_value=None):
    """A QLabel / QCheckBox / QGroupBox whose text names a stored unit in its first parenthesis
    gets the display unit (damsuite.display_units.relabel)."""
    try:
        txt = label.text() if hasattr(label, "text") else label.title()
        new = DU.relabel(txt, system_value)
        if new != txt:
            (label.setText if hasattr(label, "setText") else label.setTitle)(new)
    except Exception:                                             # noqa - never break a window
        pass


def label(text, system_value=None):
    """The text of a row label for an adopted field, in the display system."""
    return DU.relabel(text, system_value)


def adopt_form_field(form, field, stored_unit, system_value=None):
    """adopt_spinbox / adopt_lineedit for a QFormLayout row, relabelling the row's label."""
    lab = None
    try:
        lab = form.labelForField(field)
    except Exception:                                             # noqa
        lab = None
    if hasattr(field, "decimals") or hasattr(field, "singleStep"):
        return adopt_spinbox(field, stored_unit, label=lab, system_value=system_value)
    return adopt_lineedit(field, stored_unit, label=lab, system_value=system_value)


# -- whole windows ------------------------------------------------------------------------------
# watch_window(window) - one line in a window's __init__ - converts every spin box in it whose
# suffix is a stored unit (' ft', ' mi', ' ac', ' ft²', ' mph', ' cfs' ...) and every spin box or
# line edit the window marked with the Qt property storedUnit (e.g.
# spin.setProperty("storedUnit", "ft") for a box whose unit is only in its row label), with its
# QFormLayout row label. A QLabel marked setProperty("unitLabel", True) is relabelled. The scan
# runs when the window (or a dialog parented to it) is shown, so widgets built after __init__
# are caught too. Under US it does nothing at all (not even an event filter).

STORED_UNIT_PROP = "storedUnit"
UNIT_LABEL_PROP = "unitLabel"


def _form_label(widget):
    try:
        from qgis.PyQt.QtWidgets import QFormLayout
    except Exception:                                             # noqa
        return None
    p = widget.parentWidget()
    while p is not None:
        lays = []
        if isinstance(p.layout(), QFormLayout):
            lays.append(p.layout())
        lays += p.findChildren(QFormLayout)
        for lay in lays:
            try:
                lab = lay.labelForField(widget)
            except Exception:                                     # noqa
                lab = None
            if lab is not None:
                return lab
        if p.isWindow():
            return None
        p = p.parentWidget()
    return None


def _prop_unit(w):
    try:
        v = w.property(STORED_UNIT_PROP)
    except Exception:                                             # noqa
        return None
    return str(v) if v else None


def _suffix_unit(spin):
    s = (spin.suffix() or "").strip()
    if not s:
        return None
    for alias in sorted(DU.ALIASES, key=len, reverse=True):
        if s.lower().startswith(alias) and (len(s) == len(alias) or not s[len(alias)].isalnum()):
            return DU.ALIASES[alias]
    return None


def scan_window(root, system_value=None):
    """Convert what watch_window converts, now. Returns the number of widgets converted."""
    sysv = DU.system() if system_value is None else DU.normalize(system_value)
    if sysv != DU.SI:
        return 0
    from qgis.PyQt.QtWidgets import QSpinBox, QDoubleSpinBox, QLineEdit, QLabel
    n = 0
    for spin in root.findChildren(QSpinBox) + root.findChildren(QDoubleSpinBox):
        if getattr(spin, "_unit_adopted", False):
            continue
        unit = _prop_unit(spin) or _suffix_unit(spin)
        if unit and DU.canonical(unit) and adopt_spinbox(spin, unit, label=_form_label(spin),
                                                         system_value=sysv):
            n += 1
    for le in root.findChildren(QLineEdit):
        unit = _prop_unit(le)
        if unit and not getattr(le, "_unit_adopted", False) and DU.canonical(unit) \
                and adopt_lineedit(le, unit, label=_form_label(le), system_value=sysv):
            n += 1
    for lab in root.findChildren(QLabel):
        try:
            if lab.property(UNIT_LABEL_PROP) and not getattr(lab, "_unit_relabelled", False):
                relabel_widget(lab, sysv)
                lab._unit_relabelled = True
        except Exception:                                         # noqa
            pass
    return n


_WATCHER = None


def _watcher():
    global _WATCHER
    if _WATCHER is not None:
        return _WATCHER
    from qgis.PyQt.QtCore import QObject, QEvent

    show = getattr(QEvent, "Show", None) or QEvent.Type.Show
    child_added = getattr(QEvent, "ChildAdded", None) or QEvent.Type.ChildAdded

    class _Watcher(QObject):
        def eventFilter(self, obj, ev):
            try:
                t = ev.type()
                if t == show:
                    scan_window(obj)
                elif t == child_added:
                    ch = ev.child()
                    if ch is not None and ch.isWidgetType() and ch.isWindow():
                        ch.installEventFilter(self)
            except Exception:                                     # noqa - never break a window
                pass
            return False

    _WATCHER = _Watcher()
    return _WATCHER


def watch_window(window):
    """Convert `window`'s unit widgets to the display system whenever it (or a window parented
    to it) is shown. Does nothing under US. Never raises."""
    try:
        if DU.system() != DU.SI:
            return False
        w = _watcher()
        window.installEventFilter(w)
        from qgis.PyQt.QtWidgets import QWidget
        for ch in window.findChildren(QWidget):
            if ch.isWindow():
                ch.installEventFilter(w)
        # a wizard's next page is not "shown" to the wizard: scan on each page change
        sig = getattr(window, "currentIdChanged", None)
        if sig is not None:
            sig.connect(lambda _i=0, win=window: scan_window(win))
        return True
    except Exception:                                             # noqa
        return False
