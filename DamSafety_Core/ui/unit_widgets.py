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


class UnitParamsMixin(object):
    """Tags each number parameter as it is added (see tag_parameter)."""

    def addParameter(self, definition, createOutput=True):
        tag_parameter(definition)
        if createOutput is True:
            return super().addParameter(definition)
        return super().addParameter(definition, createOutput)
