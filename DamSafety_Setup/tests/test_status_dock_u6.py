"""Audit U6 (2026-11-07): the Suite status dock's "Build LISFLOOD-FP…" runs its script through
QProcess with the output streamed into the dock's Log, remembers the executable in the suite
store key the Runner reads (`lisflood_exe` -> QgsSettings ndsuite/lisflood_exe, see M15), and the
button is off on Windows with a tooltip that says why. No QGIS here: the module is read as text
/ ast, the helper that needs no Qt is imported through a stub.

    python3 DamSafety_Setup/tests/test_status_dock_u6.py
"""
import ast
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DOCK = os.path.join(REPO, "DamSafety_Setup", "status_dock.py")
SRC = open(DOCK, encoding="utf-8").read()


def test_build_runs_through_qprocess_not_popen():
    assert "QProcess(" in SRC and "readyReadStandardOutput.connect" in SRC
    assert "subprocess.Popen" not in SRC, "the bare Popen gave no result to the user"
    assert "Started " not in SRC, "the 'Started…' message box is gone"


def test_success_writes_the_suite_key_the_runner_reads():
    # DamSafety_Runner/algorithms.py find_lisflood: _suite_setting("lisflood_exe") first
    runner = open(os.path.join(REPO, "DamSafety_Runner", "algorithms.py"), encoding="utf-8").read()
    assert '_suite_setting("lisflood_exe")' in runner
    assert re.search(r'set\("lisflood_exe",\s*exe\)', SRC), "Setup must write the suite key"
    assert "NDBREACH_LISFLOOD_EXE" not in SRC, "not the provider's own ProcessingConfig key"


def test_windows_disables_with_why():
    tree = ast.parse(SRC)
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    assert "WINDOWS_WHY" in names and "setEnabled(False)" in SRC
    i = SRC.find("if _is_windows():")
    assert 0 < i and "setToolTip(WINDOWS_WHY)" in SRC[i:i + 600]


def test_finished_text():
    # import the pure helper without Qt: stub the qgis modules
    import types
    saved = {k: sys.modules.get(k) for k in ("qgis", "qgis.PyQt", "qgis.PyQt.QtCore",
                                             "qgis.PyQt.QtWidgets", "DamSafety_Setup.ui_widgets")}
    try:
        core = types.ModuleType("qgis.PyQt.QtCore")
        core.QProcess = object
        widgets = types.ModuleType("qgis.PyQt.QtWidgets")
        for n in ("QDockWidget", "QWidget", "QVBoxLayout", "QLabel", "QPushButton", "QGridLayout",
                  "QFileDialog", "QPlainTextEdit"):
            setattr(widgets, n, type(n, (), {}))
        pyqt = types.ModuleType("qgis.PyQt")
        qgis = types.ModuleType("qgis")
        sys.modules.update({"qgis": qgis, "qgis.PyQt": pyqt, "qgis.PyQt.QtCore": core,
                            "qgis.PyQt.QtWidgets": widgets})
        uiw = types.ModuleType("DamSafety_Setup.ui_widgets")
        uiw.Collapsible = object
        sys.modules["DamSafety_Setup.ui_widgets"] = uiw
        if REPO not in sys.path:
            sys.path.insert(0, REPO)
        import importlib
        pkg = types.ModuleType("DamSafety_Setup")
        pkg.__path__ = [os.path.join(REPO, "DamSafety_Setup")]
        sys.modules.setdefault("DamSafety_Setup", pkg)
        sd = importlib.import_module("DamSafety_Setup.status_dock")
        assert sd._process_state_text(0, True) == "Finished (exit code 0)."
        assert sd._process_state_text(1, True) == "Finished (exit code 1)."
        assert "crashed" in sd._process_state_text(0, False)
    finally:
        sys.modules.pop("DamSafety_Setup.status_dock", None)
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("PASS", name)
            except Exception:
                import traceback
                traceback.print_exc()
                fails += 1
                print("FAIL", name)
    sys.exit(1 if fails else 0)
