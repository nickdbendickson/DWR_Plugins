# -*- coding: utf-8 -*-
"""One settings window for the whole suite (window-simplification plan change 1, Nick 2026-11-23).

Until Core 1.6.0 each of Hazard, the breach Runner and Screening opened its own settings window
on the same settings file: three windows, three file boxes. Now there is one, "DamSafety Suite -
Settings", one per QGIS: a file row at the top and one tab per plugin. A tab holds that plugin's
settings page (its grid of tool values, built by the plugin as before; Screening's Run options
too), built the first time the tab is shown, so opening the window does not open every plugin.

A plugin registers its tab when it loads (`register_section`) with a factory that returns its
section: {"page": QWidget, "owner": the object the page belongs to (its window), "grid":
SettingsGrid, "file_edit": the plugin's own settings-file box (or None), "apply_file": callable
that reloads the plugin after file_edit changed, "hide": [widgets of the page's own file row],
"on_show": optional callable}. The window's one file row sets every built tab's box and calls its
apply_file, so the three plugins always edit one file. Opened from Plugins > Dam safety > Suite
settings..., and from each plugin's Settings / gear (focused on that plugin's tab).
"""
from qgis.PyQt.QtWidgets import (QDialog, QFileDialog, QHBoxLayout, QLabel, QLineEdit, QPushButton,
                                 QTabWidget, QVBoxLayout, QWidget)

TITLE = "DamSafety Suite \u2014 Settings"   # gui-grammar: DamSafety <Tool> \u2014 <Window>
ACTION = ("Suite settings…", "NDSuiteSettings")          # (label, object name) of the menu entry
FILE_FILTER = "Settings workbook (*.xlsx);;Settings table (*.csv);;All files (*)"
NEW_FILTER = "Settings workbook (*.xlsx);;Settings table (*.csv)"
# plugin key -> (tab title, order)
SECTIONS = {"ndhazard": ("Hazard", 1), "ndbreach": ("Breach Runner", 2), "nddam": ("Screening", 3)}


def _registry():
    from .suite_home import _shared
    reg = _shared()
    if not hasattr(reg, "settings_sections"):
        reg.settings_sections = {}
        reg.settings_window = None
    return reg


def _alive(obj):
    from .suite_home import _alive as alive
    return alive(obj)


def register_section(key, factory, title=None, order=None):
    """The tab of plugin `key`; `factory()` returns its section dict (see the module doc). A key
    registered already keeps its factory unless `factory` replaces a dead one."""
    reg = _registry()
    t, o = SECTIONS.get(key, (key, 9))
    reg.settings_sections[key] = {"factory": factory, "title": title or t,
                                  "order": o if order is None else order}
    win = reg.settings_window
    if _alive(win):
        win.sync_tabs()


def unregister_section(key):
    """Plugin `key` unloads: its tab and page go (the page calls into the plugin's window)."""
    reg = _registry()
    reg.settings_sections.pop(key, None)
    win = reg.settings_window
    if _alive(win):
        win.drop(key)
        win.sync_tabs()


def window(parent=None, create=True):
    reg = _registry()
    win = reg.settings_window if _alive(reg.settings_window) else None
    if win is None and create:
        win = SuiteSettingsWindow(parent)
        reg.settings_window = win
    return win


def open_settings(key=None, factory=None, parent=None):
    """Show the one settings window, on plugin `key`'s tab when given. `factory` registers that
    tab first when the plugin has not (a window opened without its plugin, as in the tests)."""
    if key and factory is not None and key not in _registry().settings_sections:
        register_section(key, factory)
    win = window(parent)
    win.sync_tabs()
    if key:
        win.show_section(key)
    win.refresh_file()
    win.show()
    try:
        win.raise_()
        win.activateWindow()
    except Exception:                                            # noqa
        pass
    return win


def add_settings_action(plugin, iface):
    """The one "Suite settings..." entry of Plugins > Dam safety, added once per QGIS (as the
    home's). Appended to plugin.suite_actions so unload removes it."""
    from .suite_home import add_suite_action
    label, name = ACTION
    for a in getattr(plugin, "suite_actions", None) or []:
        try:
            if a.objectName() == name:
                return None
        except Exception:                                        # noqa
            pass
    action = add_suite_action(iface, label, name, lambda *_a: open_settings())
    if action is not None:
        plugin.suite_actions.append(action)
    return action


class SuiteSettingsWindow(QDialog):
    """The window: file row, one tab per registered plugin (built when first shown), Close."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(TITLE)
        self.setModal(False)
        self.built = {}                   # key -> section dict
        self.holders = {}                 # key -> the tab's QWidget (holds the page once built)
        self.errors = {}                  # key -> why the tab could not be built
        lay = QVBoxLayout(self)
        info = QLabel("One settings file for the whole suite: the values every tool of Hazard, the "
                      "breach Runner and Screening opens with. Each tab is one plugin's tools; every "
                      "value can still be changed in a tool's own dialog. Edits are saved to the file "
                      "at once.")
        info.setWordWrap(True)
        lay.addWidget(info)
        row = QHBoxLayout()
        row.addWidget(QLabel("Settings file:"))
        self.file_edit = QLineEdit()
        self.file_edit.setToolTip("The suite settings workbook (damsuite_settings.xlsx, or .csv) the three "
                                  "plugins share. Empty = the tools' own starting values.")
        self.file_edit.editingFinished.connect(lambda: self.set_path(self.file_edit.text().strip()))
        pick = QPushButton("File…")
        pick.clicked.connect(lambda _c=False: self.pick_file())
        new = QPushButton("New…")
        new.setToolTip("Write a new settings file with the current values.")
        new.clicked.connect(lambda _c=False: self.new_file())
        row.addWidget(self.file_edit, 1)
        row.addWidget(pick)
        row.addWidget(new)
        lay.addLayout(row)
        self.tabs = QTabWidget()
        # a tab is built when it is shown (inserting tabs moves the current one: not then)
        self.tabs.currentChanged.connect(lambda _i: self._build_current() if self.isVisible() else None)
        lay.addWidget(self.tabs, 1)
        bottom = QHBoxLayout()
        bottom.addStretch(1)
        done = QPushButton("Close")
        done.clicked.connect(self.close)
        bottom.addWidget(done)
        lay.addLayout(bottom)
        try:
            from .ui_fit import fit_window
            fit_window(self, prefer=(1140, 720))   # Screening's Run options rows are wide
        except Exception:                                        # noqa
            self.resize(1000, 680)
        self.sync_tabs()

    # ------------------------------------------------------------------ tabs --
    def keys(self):
        return [self.tabs.widget(i).property("section_key") for i in range(self.tabs.count())]

    def sync_tabs(self):
        """One tab per registered plugin, in order; a tab whose page belongs to a closed window
        is rebuilt when next shown."""
        secs = _registry().settings_sections
        for key in list(self.holders):
            if key not in secs:
                self.drop(key)
        for key, sec in list(self.built.items()):
            if not (_alive(sec.get("owner")) and _alive(sec.get("page"))):
                self.drop(key, keep_tab=True)
        order = sorted(secs, key=lambda k: (secs[k]["order"], k))
        for key in order:
            if key not in self.holders:
                holder = QWidget()
                holder.setProperty("section_key", key)
                QVBoxLayout(holder).setContentsMargins(0, 0, 0, 0)
                self.holders[key] = holder
                self.tabs.insertTab(order.index(key), holder, secs[key]["title"])
        if self.isVisible():
            self._build_current()

    def drop(self, key, keep_tab=False):
        sec = self.built.pop(key, None)
        self.errors.pop(key, None)
        holder = self.holders.get(key)
        if sec is not None and _alive(sec.get("page")):
            page = sec["page"]
            page.hide()
            page.setParent(None)
            if not keep_tab:
                page.deleteLater()
        if holder is not None and _alive(holder):
            lay = holder.layout()
            while lay.count():
                item = lay.takeAt(0)
                w = item.widget()
                if w is not None and (sec is None or w is not sec.get("page")):
                    w.deleteLater()
            if not keep_tab:
                self.tabs.removeTab(self.tabs.indexOf(holder))
                holder.deleteLater()
                self.holders.pop(key, None)

    def show_section(self, key):
        holder = self.holders.get(key)
        if holder is not None:
            self.tabs.setCurrentWidget(holder)
        self.build(key)

    def _build_current(self):
        w = self.tabs.currentWidget()
        if w is not None:
            self.build(w.property("section_key"))

    def build(self, key):
        """Put plugin `key`'s page in its tab (once); a page that cannot be built says why."""
        if key in self.built or key not in self.holders:
            return self.built.get(key)
        sec_def = _registry().settings_sections.get(key)
        holder = self.holders[key]
        lay = holder.layout()
        while lay.count():
            item = lay.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        try:
            sec = sec_def["factory"]()
            page = sec["page"]
        except Exception as e:                                   # noqa - one plugin never stops the window
            self.errors[key] = str(e) or repr(e)
            msg = QLabel("The {} settings could not be shown: {}".format(
                sec_def["title"] if sec_def else key, self.errors[key]))
            msg.setWordWrap(True)
            lay.addWidget(msg)
            lay.addStretch(1)
            return None
        for w in sec.get("hide") or ():
            try:
                w.hide()
            except Exception:                                    # noqa
                pass
        lay.addWidget(page, 1)
        page.show()
        self.built[key] = sec
        p, edit = self.file_edit.text().strip(), sec.get("file_edit")
        if p and _alive(edit) and edit.text().strip() != p and sec.get("apply_file"):
            edit.setText(p)                    # a tab built after the file changed reads it too
            try:
                sec["apply_file"]()
            except Exception:                                    # noqa
                pass
        grid = sec.get("grid")
        if grid is not None and not getattr(grid, "_suite_window_hooked", False):
            # a Load / Save as in one tab's grid moves every tab to that file
            own = grid.on_file

            def on_file(path, _own=own):
                if _own:
                    _own(path)
                win = window(create=False)
                if win is not None and path:
                    win.set_path(path)
            grid.on_file = on_file
            grid._suite_window_hooked = True
        if sec.get("on_show"):
            try:
                sec["on_show"]()
            except Exception:                                    # noqa
                pass
        return sec

    # ------------------------------------------------------------------ file --
    def current_path(self):
        try:
            from ..damsuite import settings as DS
            return DS.current_path() or ""
        except Exception:                                        # noqa
            return ""

    def refresh_file(self):
        """The file row shows the file the plugins use now (one may have changed it)."""
        p = self.current_path()
        for sec in self.built.values():
            edit = sec.get("file_edit")
            if _alive(edit) and edit.text().strip():
                p = edit.text().strip()
                break
        if p != self.file_edit.text().strip():
            self.file_edit.setText(p)

    def set_path(self, path):
        """Every built tab's plugin reads `path` (each plugin's own load and its checks)."""
        from ..damsuite import settings as DS
        ok = True
        for key, sec in sorted(self.built.items()):
            edit, apply = sec.get("file_edit"), sec.get("apply_file")
            if not (_alive(edit) and apply):
                continue
            if edit.text().strip() == path:
                continue
            edit.setText(path)
            try:
                if apply() is False and path:
                    ok = False
            except Exception:                                    # noqa
                ok = False
        if ok:
            DS.set_current_path(path)
        self.refresh_file()
        return ok

    def pick_file(self):
        start = self.file_edit.text().strip()
        p, _f = QFileDialog.getOpenFileName(self, "Settings file", start, FILE_FILTER)
        if p:
            self.file_edit.setText(p)
            self.set_path(p)
        return p

    def new_file(self, path=None):
        import os
        from ..damsuite import settings as DS
        if path is None:
            start = self.file_edit.text().strip() or DS.DEFAULT_NAME
            path, _f = QFileDialog.getSaveFileName(self, "New settings file", start, NEW_FILTER)
        if not path:
            return ""
        if not os.path.splitext(path)[1]:
            path += ".xlsx"
        name = os.path.splitext(os.path.basename(path))[0].replace("_", " ")
        grids = [sec["grid"] for _k, sec in sorted(self.built.items()) if _alive(sec.get("grid"))]
        if grids:
            for i, grid in enumerate(grids):       # the first writes the file, the others merge in
                grid.settings.path = path
                grid.save(path, name=name, replace=(i == 0))
        else:
            DS.Settings().save(path, name=name, replace=True)
        self.set_path(path)
        return path

    def showEvent(self, event):
        super().showEvent(event)
        self.sync_tabs()
        self.refresh_file()
