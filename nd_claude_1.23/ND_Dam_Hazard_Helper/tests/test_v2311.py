"""2.31.1 (Lumo audit short list): suite settings path/grid guard, autosave catch-all, and the
suite home's pure helpers (Dams folder resolution, diagnostics text, recent-problem list).
Run: python3 -m ND_Dam_Hazard_Helper.tests.run_tests (from the folder above the plugin)."""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_DIR = os.path.dirname(HERE)
if os.path.dirname(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, os.path.dirname(PLUGIN_DIR))
from ND_Dam_Hazard_Helper.tests.registry import test  # noqa: E402


class _Edit(object):
    def __init__(self, t=""):
        self.t = t

    def text(self):
        return self.t

    def setText(self, t):
        self.t = t


class _Grid(object):
    def __init__(self, ok=True, boom=None):
        from ND_Dam_Hazard_Helper.damsuite import settings as DS
        self.settings = DS.Settings()
        self.ok, self.boom, self.loaded, self.catalog, self.versions = ok, boom, [], [], {}
        if boom is not None:
            grid = self

            class _S(object):
                path = None

                def save(self, *a, **k):
                    raise grid.boom
            self.settings = _S()

    def load(self, p, quiet=False):
        self.loaded.append(p)
        return self.ok

    def fill(self):
        pass


def _host(path_text, loaded, grid):
    from ND_Dam_Hazard_Helper import suite_settings_tab as T

    class _Box(object):
        @staticmethod
        def warning(*a, **k):
            _Box.warned = a

    T.QMessageBox = _Box                      # never a modal box in a test

    class H(T.SuiteSettingsTabMixin):
        pass
    h = H()
    h.suite_settings_file_edit = _Edit(path_text)
    h.suite_settings_status = _Edit("")
    h.suite_grid = grid
    h._suite_loaded_path = loaded
    return h


@test
def test_settings_missing_file_is_refused_and_box_reverts():
    from ND_Dam_Hazard_Helper.damsuite import settings as DS
    before = DS.current_path()
    d = tempfile.mkdtemp()
    old = os.path.join(d, "old.csv")
    open(old, "w").close()
    h = _host(os.path.join(d, "missing.xlsx"), old, _Grid())
    assert h._suite_settings_file_edited() is False
    assert h.suite_settings_file_edit.text() == old
    assert "does not exist" in h.suite_settings_status.text()
    assert DS.current_path() == before          # never made current
    assert h.suite_grid.loaded == []


@test
def test_settings_load_failure_reverts():
    d = tempfile.mkdtemp()
    new = os.path.join(d, "new.csv")
    open(new, "w").close()
    h = _host(new, "", _Grid(ok=False))
    assert h._suite_settings_file_edited() is False
    assert h.suite_settings_file_edit.text() == ""
    assert h._suite_loaded_path == ""


@test
def test_autosave_refuses_when_box_and_grid_disagree():
    h = _host("/tmp/other.xlsx", "/tmp/loaded.xlsx", _Grid(boom=AssertionError("must not save")))
    assert h._autosave_suite_settings() is False
    assert h.suite_settings_status.text().startswith("Not saved")


@test
def test_autosave_catches_any_exception():
    d = tempfile.mkdtemp()
    p = os.path.join(d, "s.csv")
    h = _host(p, p, _Grid(boom=ValueError("bad cell")))
    assert h._autosave_suite_settings() is False
    assert "ValueError" in h.suite_settings_status.text()


@test
def test_home_resolve_dams_folder_and_diagnostics():
    from ND_Dam_Hazard_Helper import suite_home as H
    assert H.resolve_dams_folder(" /p ", "/c", "/f") == "/p"
    assert H.resolve_dams_folder("", "/c", "/f") == "/c"
    assert H.resolve_dams_folder(None, None, "/f") == "/f"
    assert H.resolve_dams_folder("", "", "") == ""
    del H.RECENT_ERRORS[:]
    for i in range(H.MAX_ERRORS + 5):
        H.note_error("e{}".format(i))
    H.note_error("   ")
    assert len(H.RECENT_ERRORS) == H.MAX_ERRORS and H.RECENT_ERRORS[-1] == "e{}".format(H.MAX_ERRORS + 4)
    t = H.diagnostics_text({"plugin": "ND Dam Hazard", "plugin_version": "2.31.1", "qgis": "3.34.6",
                            "os": "Windows-10", "settings_file": "C:/s.xlsx", "dams_folder": "D:/Dams",
                            "errors": ["boom"], "installed": {"ND Dam Breach Runner": "3.21.0"}})
    for frag in ("ND Dam Hazard 2.31.1", "QGIS: 3.34.6", "OS: Windows-10", "Settings file: C:/s.xlsx",
                 "Dams folder: D:/Dams", "Recent problems: 1", "  - boom", "ND Dam Breach Runner 3.21.0"):
        assert frag in t, frag
    assert "Python: ?" in H.diagnostics_text({})
    del H.RECENT_ERRORS[:]
