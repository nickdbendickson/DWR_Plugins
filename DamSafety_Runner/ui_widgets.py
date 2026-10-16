"""Small shared shell widgets of the Runner window (the same look as the DamSafety Screening
redesign): a muted information banner, a disclosure section and a stage strip."""
from qgis.PyQt.QtWidgets import QFrame, QHBoxLayout, QLabel, QToolButton, QVBoxLayout, QWidget


class InfoBanner(QFrame):
    """The disclaimer, muted so it reads as context, not instructions."""

    def __init__(self, text, parent=None):
        super().__init__(parent)
        self.setObjectName("info_banner")
        self.setStyleSheet("QFrame#info_banner {background:#f2f2f7; border:1px solid #d8d8e0;"
                           "border-radius:4px;} QLabel {color:#55555f;}")
        h = QHBoxLayout(self)
        h.setContentsMargins(10, 6, 10, 6)
        h.addWidget(QLabel("ℹ"))
        self.label = QLabel(text)
        self.label.setWordWrap(True)
        h.addWidget(self.label, 1)


class Collapsible(QWidget):
    """A disclosure section. The title is kept apart from the arrow, so the header stays right
    however often it is toggled or retitled."""

    def __init__(self, title, inner, parent=None):
        super().__init__(parent)
        self._title = title
        self.button = QToolButton()
        self.button.setCheckable(True)
        self.button.setStyleSheet("QToolButton {border:none; font-weight:bold; color:#444;}")
        self.inner = inner
        self.inner.setVisible(False)
        self.button.toggled.connect(self._toggle)
        v = QVBoxLayout(self)
        v.setContentsMargins(2, 0, 2, 0)
        v.setSpacing(2)
        v.addWidget(self.button)
        v.addWidget(self.inner)
        self._paint()

    def _paint(self):
        self.button.setText(("▾  " if self.isExpanded() else "▸  ") + self._title)

    def _toggle(self, on):
        self.inner.setVisible(bool(on))
        self._paint()

    def isExpanded(self):
        return bool(self.button.isChecked())

    def setExpanded(self, on):
        self.button.setChecked(bool(on))
        self._toggle(on)

    def title(self):
        return self._title

    def set_title(self, title):
        self._title = title
        self._paint()


class StageStrip(QWidget):
    """Stage chips with an arrow between them: ✓ yes (done), ◐ part, ○ no (not yet)."""

    YES, PART, NO = "yes", "part", "no"
    _MARK = {YES: "✓", PART: "◐", NO: "○"}
    _COLOR = {YES: "#137333", PART: "#b26b00", NO: "#888888"}

    def __init__(self, stages, parent=None):
        super().__init__(parent)
        h = QHBoxLayout(self)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(6)
        self.names = list(stages)
        self.chips = []
        for i, name in enumerate(self.names):
            if i:
                arrow = QLabel("→")
                arrow.setStyleSheet("color:#999;")
                h.addWidget(arrow)
            lab = QLabel(name)
            self.chips.append(lab)
            h.addWidget(lab)
        h.addStretch(1)
        self.states = []
        self.set_states([self.NO] * len(self.names))

    def set_states(self, states, tips=None):
        self.states = [st if st in self._MARK else self.NO for st in states]
        for i, (lab, st) in enumerate(zip(self.chips, self.states)):
            lab.setText("{} {}".format(self._MARK[st], self.names[i]))
            lab.setStyleSheet("QLabel {color:%s; padding:2px 8px; border:1px solid %s; "
                              "border-radius:9px;}" % (self._COLOR[st], self._COLOR[st]))
            if tips and i < len(tips):
                lab.setToolTip(tips[i])
