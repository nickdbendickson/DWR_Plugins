"""Keep a plugin window on the screen and scroll a long tab."""
from qgis.PyQt.QtCore import Qt, QSize
from qgis.PyQt.QtWidgets import QApplication, QScrollArea, QSizePolicy, QVBoxLayout, QWidget, QWizardPage


def _policy(name):
    enum = getattr(QSizePolicy, "Policy", QSizePolicy)
    return getattr(enum, name)


class FitScroll(QScrollArea):
    """Scrolls a long page. The size hint stays small so the page does not cover the log."""

    def sizeHint(self):
        return QSize(640, 240)

    def minimumSizeHint(self):
        return QSize(240, 120)


def _scroll(page):
    scroll = FitScroll()
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(getattr(getattr(QScrollArea, "Shape", QScrollArea), "NoFrame"))
    bar = getattr(Qt, "ScrollBarPolicy", Qt)
    scroll.setHorizontalScrollBarPolicy(bar.ScrollBarAsNeeded)
    scroll.setVerticalScrollBarPolicy(bar.ScrollBarAsNeeded)
    scroll.setSizePolicy(_policy("Expanding"), _policy("Expanding"))
    scroll.setMinimumHeight(120)
    scroll.setWidget(page)
    return scroll


def wrap_tabs(tabs):
    """Put each tab page in a scroll area. A short page does not show a bar."""
    if tabs is None:
        return
    current = tabs.currentIndex()
    i = 0
    while i < tabs.count():
        page = tabs.widget(i)
        if isinstance(page, QScrollArea):
            i += 1
            continue
        title = tabs.tabText(i)
        tabs.removeTab(i)
        tabs.insertTab(i, _scroll(page), title)
        i += 1
    tabs.setSizePolicy(_policy("Expanding"), _policy("Expanding"))
    if 0 <= current < tabs.count():
        tabs.setCurrentIndex(current)


def scroll_page(page):
    """Scroll a wizard page without changing its fields."""
    if page is None or isinstance(page, QScrollArea):
        return
    lay = page.layout()
    if lay is None:
        return
    holder = QWidget()
    holder.setLayout(lay)
    outer = QVBoxLayout()
    outer.setContentsMargins(0, 0, 0, 0)
    outer.addWidget(_scroll(holder))
    page.setLayout(outer)


def wrap_wizard(wizard):
    if wizard is None:
        return
    for page_id in wizard.pageIds():
        scroll_page(wizard.page(page_id))


def _screen_of(dialog):
    """The screen the window is on (or its parent's), else the primary screen."""
    for w in (dialog, getattr(dialog, "parentWidget", lambda: None)()):
        try:
            handle = w.windowHandle() if w is not None else None
            if handle is None and w is not None and hasattr(w, "window"):
                handle = w.window().windowHandle()
            if handle is not None and handle.screen() is not None:
                return handle.screen()
        except Exception:                                        # noqa
            continue
    try:
        if hasattr(dialog, "screen") and dialog.screen() is not None:
            return dialog.screen()
    except Exception:                                            # noqa
        pass
    return QApplication.primaryScreen()


def _saved_size(key):
    if not key:
        return None
    try:
        from qgis.PyQt.QtCore import QSettings
        v = QSettings().value(key + "/size")
        if v is None:
            return None
        if hasattr(v, "width"):
            return int(v.width()), int(v.height())
        w, h = v
        return int(w), int(h)
    except Exception:                                            # noqa
        return None


def save_size(dialog, key):
    """Remember the window size under QSettings `key` (call it on close)."""
    if not key:
        return
    try:
        from qgis.PyQt.QtCore import QSettings
        QSettings().setValue(key + "/size", QSize(int(dialog.width()), int(dialog.height())))
    except Exception:                                            # noqa
        pass


def fit_window(dialog, prefer=(980, 680), key=None):
    """Size the window to its screen, and never larger than the screen. With `key`, the size
    save_size() stored under that QSettings key is preferred (still clamped to the screen)."""
    prefer = _saved_size(key) or prefer
    screen = _screen_of(dialog)
    if screen is None:
        dialog.resize(prefer[0], prefer[1])
        return
    avail = screen.availableGeometry()
    max_w = max(520, avail.width() - 48)
    max_h = max(400, avail.height() - 80)
    dialog.setMaximumSize(max_w, max_h)
    dialog.setMinimumSize(min(640, max_w), min(420, max_h))
    dialog.resize(min(prefer[0], max_w), min(prefer[1], max_h))
