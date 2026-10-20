# -*- coding: utf-8 -*-
"""Hazard 2.18.44 (#108): widgets that wrap instead of widening the Classify view.

A QCheckBox never wraps its text, so a long one sets the minimum width of the whole scrolled
page (one 1 250 px lifeline checkbox made the Classify tab 1 530 px wide and pushed the
Browse… buttons out of sight). WrapCheckBox keeps every QCheckBox behaviour (signals, text(),
isChecked(), isinstance checks, saved settings) and only wraps its text onto more lines when
the window is narrower than the text.

    WrapCheckBox     a check box whose text wraps (height follows width)
    narrow_combos    lets long-item combo boxes shrink (and grow to fill the row)
    page_fits_width  the minimum width a scrolled page needs vs a viewport width

Hazard only: the other plugins carry their own (byte-identical) ui_fit / ui_widgets."""
from qgis.PyQt.QtCore import QRect, QSize, Qt
from qgis.PyQt.QtGui import QPalette
from qgis.PyQt.QtWidgets import (QCheckBox, QComboBox, QSizePolicy, QStyle, QStyleOptionButton,
                                 QStyleOptionFocusRect, QStylePainter)


def _enum(cls, scope, name):
    """cls.<name> on Qt5, cls.<scope>.<name> on Qt6."""
    holder = getattr(cls, scope, cls)
    val = getattr(holder, name, None)
    return val if val is not None else getattr(cls, name)


_WRAP_FLAGS = None


def _flags():
    global _WRAP_FLAGS
    if _WRAP_FLAGS is None:
        al = getattr(Qt, "AlignmentFlag", Qt)
        tf = getattr(Qt, "TextFlag", Qt)
        _WRAP_FLAGS = 0
        for flag in (al.AlignLeft, al.AlignTop, tf.TextWordWrap, tf.TextShowMnemonic):
            _WRAP_FLAGS |= flag.value if hasattr(flag, "value") else int(flag)   # Qt6 / Qt5
    return _WRAP_FLAGS


class WrapCheckBox(QCheckBox):
    """A QCheckBox whose text wraps when the layout gives it less than its one-line width.
    At its one-line width or wider it is painted by QCheckBox itself (looks the same)."""

    MIN_TEXT_PX = 160          # never narrower than this much text (or its longest word)

    def __init__(self, text="", parent=None):
        super().__init__(text, parent)
        # QCheckBox is (Minimum, Fixed): never narrower than its one-line text and never
        # taller than one line. Wrapping needs (Preferred, Minimum) + height-for-width.
        pp = getattr(QSizePolicy, "Policy", QSizePolicy)
        pol = QSizePolicy(pp.Preferred, pp.Minimum)
        pol.setHeightForWidth(True)
        self.setSizePolicy(pol)

    # -- geometry ----------------------------------------------------------------------
    def _one_line(self):
        return super().sizeHint()

    def _text_px(self):
        return self.fontMetrics().horizontalAdvance(self.text().replace("&&", "&"))

    def _extra(self):
        """Indicator + spacing + style margins: the one-line width minus the text."""
        return max(0, self._one_line().width() - self._text_px())

    def minimumSizeHint(self):
        one = self._one_line()
        fm = self.fontMetrics()
        words = [w for w in self.text().split() if w]
        longest = max([fm.horizontalAdvance(w) for w in words] or [0])
        w = min(one.width(), self._extra() + max(longest, self.MIN_TEXT_PX))
        return QSize(w, one.height())     # the real height: heightForWidth()

    def sizeHint(self):
        return self._one_line()

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        one = self._one_line()
        if width >= one.width() or not self.text():
            return one.height()
        tw = max(1, width - self._extra())
        fm = self.fontMetrics()
        h = fm.boundingRect(QRect(0, 0, tw, 100000), _flags(), self.text()).height()
        return max(one.height(), h + max(0, one.height() - fm.height()))

    def setText(self, text):
        super().setText(text)
        self.updateGeometry()

    # -- painting / clicking -----------------------------------------------------------
    def _wrapped(self):
        return self.width() < self._one_line().width()

    def paintEvent(self, event):
        if not self._wrapped():
            super().paintEvent(event)
            return
        p = QStylePainter(self)
        opt = QStyleOptionButton()
        self.initStyleOption(opt)
        one = self._one_line()
        text = opt.text
        opt.text = ""
        opt.rect = QRect(0, 0, self.width(), one.height())   # indicator beside the first line
        p.drawControl(_enum(QStyle, "ControlElement", "CE_CheckBox"), opt)
        contents = _enum(QStyle, "SubElement", "SE_CheckBoxContents")
        left = self.style().subElementRect(contents, opt, self).left()
        top = max(0, (one.height() - self.fontMetrics().height()) // 2)
        rect = QRect(left, top, max(1, self.width() - left), self.height() - top)
        role = _enum(QPalette, "ColorRole", "WindowText")
        p.drawItemText(rect, _flags(), self.palette(), self.isEnabled(), text, role)
        if self.hasFocus():                    # keyboard focus shown around the wrapped text
            fopt = QStyleOptionFocusRect()
            fopt.initFrom(self)
            fopt.rect = self.fontMetrics().boundingRect(rect, _flags(), text).adjusted(-1, 0, 1, 1)
            p.drawPrimitive(_enum(QStyle, "PrimitiveElement", "PE_FrameFocusRect"), fopt)

    def _wrapped_hit_rect(self):
        """The indicator and the painted text block of a wrapped box (paintEvent's geometry)."""
        opt = QStyleOptionButton()
        self.initStyleOption(opt)
        one = self._one_line()
        opt.text = ""
        opt.rect = QRect(0, 0, self.width(), one.height())
        indicator = self.style().subElementRect(
            _enum(QStyle, "SubElement", "SE_CheckBoxIndicator"), opt, self)
        left = self.style().subElementRect(
            _enum(QStyle, "SubElement", "SE_CheckBoxContents"), opt, self).left()
        top = max(0, (one.height() - self.fontMetrics().height()) // 2)
        area = QRect(left, top, max(1, self.width() - left), self.height() - top)
        text = self.fontMetrics().boundingRect(area, _flags(), self.text())
        return indicator.united(text).adjusted(0, 0, 2, 2)

    def hitButton(self, pos):
        """Unwrapped: QCheckBox's own click area (the box and its text, not the empty space
        beside them). Wrapped: the box and the whole wrapped text block."""
        if not self._wrapped():
            return super().hitButton(pos)
        return self._wrapped_hit_rect().contains(pos)


def narrow_combos(root, max_px=300, chars=28, always=()):
    """Combo boxes under `root` whose longest item makes them wider than max_px, and the
    combos in `always` (layer lists, whose items are the project's layer names): sized to
    `chars` characters - the full item shows in the open list and the tooltip - and set to
    grow, so in a wide window they still fill their row."""
    adjust = _enum(QComboBox, "SizeAdjustPolicy", "AdjustToMinimumContentsLengthWithIcon")
    grow = getattr(QSizePolicy, "Policy", QSizePolicy).Expanding
    picked = [c for c in always if c is not None]
    for combo in root.findChildren(QComboBox):
        if combo not in picked and combo.minimumWidth() <= 0 and \
                combo.minimumSizeHint().width() > max_px:
            picked.append(combo)
    for combo in picked:
        combo.setSizeAdjustPolicy(adjust)
        combo.setMinimumContentsLength(chars)
        combo.setSizePolicy(grow, combo.sizePolicy().verticalPolicy())
        # Qt 5 still reports the longest item as the minimum size hint of a combo with a
        # minimum contents length; an explicit minimum width is what the layout uses
        combo.setMinimumWidth(combo.sizeHint().width())
        if not combo.toolTip():                       # the whole item, when it is cut
            combo.currentTextChanged.connect(lambda t, _c=combo: _c.setToolTip(t))
            combo.setToolTip(combo.currentText())
    return picked


def page_fits_width(page, viewport_width):
    """(fits, needed_px): does the scrolled page fit a viewport this wide without sideways
    scrolling?"""
    need = page.minimumSizeHint().expandedTo(page.minimumSize()).width()
    return need <= viewport_width, need
