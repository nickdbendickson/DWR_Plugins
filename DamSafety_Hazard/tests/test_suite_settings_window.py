"""Core 1.6.0: one suite settings window (DamSafety_Core/ui/suite_settings_window.py), a tab per
plugin, in a real Qt (offscreen). Three stand-in plugin sections, each a real SettingsGrid on its
own catalog with its own settings-file box, as Hazard, the Runner and Screening give.

    QT_QPA_PLATFORM=offscreen python3 DamSafety_Hazard/tests/test_suite_settings_window.py
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["HOME"] = tempfile.mkdtemp(prefix="sw_home_")      # never the engineer's QSettings

from qgis.core import QgsApplication  # noqa: E402
_app = QgsApplication.instance() or QgsApplication([], True)
_app.initQgis()

from qgis.PyQt.QtWidgets import QLineEdit, QWidget, QVBoxLayout  # noqa: E402

from DamSafety_Core.damsuite import settings as DS  # noqa: E402
from DamSafety_Core.ui import suite_settings as SS  # noqa: E402
from DamSafety_Core.ui import suite_settings_window as SW  # noqa: E402

DS.set_current_path("", remember=False)


def _pump(n=4):
    for _ in range(n):
        _app.processEvents()


class Plugin(object):
    """A plugin window's settings page, as the three build theirs: a page with its own file row,
    a grid on the plugin's catalog, a handler that loads the file named in its box."""
    built = []

    def __init__(self, key, tool):
        self.key = key
        self.owner = QWidget()
        self.page = QWidget()
        lay = QVBoxLayout(self.page)
        self.file_edit = QLineEdit()
        lay.addWidget(self.file_edit)
        catalog = [DS.Setting(key, tool, "N", "A number", "number", 1.0, group="Main")]
        self.grid = SS.SettingsGrid(key, catalog, DS.Settings(), parent=self.page,
                                    on_change=lambda _s: self.autosave())
        lay.addWidget(self.grid)
        self.loaded = []

    def apply_file(self):
        p = self.file_edit.text().strip()
        self.loaded.append(p)
        if p and os.path.isfile(p):
            return self.grid.load(p, quiet=True)
        return True

    def autosave(self):
        if self.grid.settings.path:
            self.grid.save()

    def section(self):
        Plugin.built.append(self.key)
        return {"page": self.page, "owner": self.owner, "grid": self.grid,
                "file_edit": self.file_edit, "apply_file": self.apply_file, "hide": [self.file_edit]}


def test_one_window_tabs_built_when_shown_and_one_file():
    haz, run, scr = Plugin("ndhazard", "classify"), Plugin("ndbreach", "rundam"), Plugin("nddam", "triage")
    SW.register_section("nddam", scr.section)              # registration order does not matter
    SW.register_section("ndhazard", haz.section)
    SW.register_section("ndbreach", run.section)
    w = SW.open_settings("ndbreach")
    _pump()
    assert w.windowTitle().startswith("DamSafety "), w.windowTitle()
    assert [w.tabs.tabText(i) for i in range(w.tabs.count())] == ["Hazard", "Breach Runner", "Screening"]
    # only the tab shown is built: opening the window does not build every plugin's window
    assert Plugin.built == ["ndbreach"], Plugin.built
    assert run.page.parent() is w.holders["ndbreach"] and not run.file_edit.isVisible()
    assert SW.open_settings("nddam") is w                  # one window per QGIS
    _pump()
    assert Plugin.built == ["ndbreach", "nddam"]
    w.show_section("ndhazard")
    _pump()
    # the one file row: every tab's plugin reads the file named there
    d = tempfile.mkdtemp(prefix="sw_")
    path = os.path.join(d, "damsuite_settings.csv")
    assert w.new_file(path) == path and os.path.isfile(path)
    for p in (haz, run, scr):
        assert p.file_edit.text() == path and p.loaded[-1] == path, (p.key, p.loaded)
    assert DS.current_path() == path and w.file_edit.text() == path
    # an edit in one tab lands in the one file; the other plugins' rows stay
    assert run.grid.set_value(("ndbreach", "rundam", "N"), "5") == ""
    assert haz.grid.set_value(("ndhazard", "classify", "N"), "7") == ""
    disk = DS.Settings.load(path)
    assert disk.values.get(("ndbreach", "rundam", "N")) in (5, 5.0, "5"), disk.values
    assert disk.values.get(("ndhazard", "classify", "N")) in (7, 7.0, "7"), disk.values
    # Load in one tab's own grid moves every tab to that file
    other = os.path.join(d, "other.csv")
    DS.Settings().save(other, replace=True)
    assert scr.grid.load(other, quiet=True)
    assert haz.file_edit.text() == other and run.file_edit.text() == other, (haz.file_edit.text(),)
    assert w.file_edit.text() == other
    # a plugin that unloads takes its tab and page with it
    SW.unregister_section("ndbreach")
    _pump()
    assert [w.tabs.tabText(i) for i in range(w.tabs.count())] == ["Hazard", "Screening"]
    # a tab whose plugin window is gone is rebuilt from the plugin when next shown
    from qgis.PyQt import sip
    sip.delete(haz.owner)                                  # Qt destroyed Hazard's window
    _pump()
    haz2 = Plugin("ndhazard", "classify")
    SW.register_section("ndhazard", haz2.section)
    w.sync_tabs()
    w.show_section("ndhazard")
    _pump()
    assert haz2.page.parent() is w.holders["ndhazard"]
    # a section that cannot be built says why in its tab and never stops the window
    SW.register_section("ndextra", lambda: (_ for _ in ()).throw(RuntimeError("no catalog")),
                        title="Extra", order=9)
    w.show_section("ndextra")
    _pump()
    assert "no catalog" in w.errors.get("ndextra", "")
    w.close()


if __name__ == "__main__":
    test_one_window_tabs_built_when_shown_and_one_file()
    print("1 passed")
    os._exit(0)
