"""Setup 1.0.12 (audit F-01): "Start here…" opens Setup's own 4-page wizard (with the NID
fill) also when DamSafety Runner is installed, then the Runner's rundam dialog with the
answers; errors are logged (DamSafety Setup log tab) and shown, never swallowed.

Real Qt, offscreen, in the QGIS python, with a throw-away QGIS settings folder:
    QT_QPA_PLATFORM=offscreen python -m pytest DamSafety_Setup/tests/test_start_here_f01.py
"""
import os
import sys
import tempfile
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["QGIS_CUSTOM_CONFIG_PATH"] = tempfile.mkdtemp(prefix="setup_f01_cfg_")
os.environ.pop("NDSUITE_SETTINGS", None)

qgis_core = pytest.importorskip("qgis.core")
from qgis.PyQt.QtCore import QSettings  # noqa: E402
from qgis.PyQt.QtWidgets import QMessageBox  # noqa: E402

for _fmt in (QSettings.NativeFormat, QSettings.IniFormat):
    QSettings.setPath(_fmt, QSettings.UserScope, os.environ["QGIS_CUSTOM_CONFIG_PATH"])
_app = qgis_core.QgsApplication.instance() or qgis_core.QgsApplication([], True)

import qgis.utils  # noqa: E402
from DamSafety_Setup import plugin as P  # noqa: E402


class Iface(object):
    def mainWindow(self):
        return None


@pytest.fixture
def env(monkeypatch):
    seen = {"wizards": 0, "runner_wizard": 0, "dialogs": [], "boxes": [], "logs": []}

    class FakeWizard(object):
        def __init__(self, parent=None):
            seen["wizards"] += 1

        def exec_(self):
            return seen.get("accept", 0)

        def params(self):
            if seen.get("params_error"):
                raise ValueError("bad answers")
            return {"DAM_ID": "Test Dam", "HEIGHT_SOURCE": "DWR",
                    "STORAGE_SOURCE": "NID ND00001 MAX_STORAGE"}

    runner_wizard = types.ModuleType("DamSafety_Runner.wizard")

    def _runner_wizard(_iface):
        seen["runner_wizard"] += 1
    runner_wizard.run_wizard = _runner_wizard
    monkeypatch.setitem(sys.modules, "DamSafety_Runner.wizard", runner_wizard)
    fake_proc = types.ModuleType("processing")

    def _exec(alg, params):
        if seen.get("dialog_error"):
            raise RuntimeError("no such algorithm")
        seen["dialogs"].append((alg, params))
    fake_proc.execAlgorithmDialog = _exec
    monkeypatch.setitem(sys.modules, "processing", fake_proc)
    monkeypatch.setattr(P, "SetupWizard", FakeWizard)
    monkeypatch.setattr(QMessageBox, "warning",
                        staticmethod(lambda _p, title, text: seen["boxes"].append(text)))
    monkeypatch.setattr(P, "_log", lambda text, level="Info": seen["logs"].append((level, text)))
    monkeypatch.setattr(qgis.utils, "plugins", {"DamSafety_Runner": object()})
    return seen


def test_setup_wizard_opens_even_with_the_runner_installed(env):
    P.SetupPlugin(Iface()).run()
    assert env["wizards"] == 1 and env["runner_wizard"] == 0
    assert env["dialogs"] == [] and env["boxes"] == []          # cancelled: nothing else


def test_accepted_wizard_hands_off_to_rundam(env):
    env["accept"] = 1
    P.SetupPlugin(Iface()).run()
    assert env["dialogs"] and env["dialogs"][0][0] == P.ALG
    assert env["dialogs"][0][1]["STORAGE_SOURCE"].startswith("NID ND00001")
    assert any("storage from NID ND00001" in t for _l, t in env["logs"])


def test_no_runner_says_so(env, monkeypatch):
    env["accept"] = 1
    monkeypatch.setattr(qgis.utils, "plugins", {})
    P.SetupPlugin(Iface()).run()
    assert env["dialogs"] == []
    assert env["boxes"] and "not installed" in env["boxes"][0]


def test_errors_are_logged_and_shown_not_swallowed(env):
    env["accept"] = 1
    env["dialog_error"] = True
    P.SetupPlugin(Iface()).run()
    assert env["boxes"] and "did not open" in env["boxes"][0]
    assert "no such algorithm" in env["boxes"][0]
    assert any(lvl == "Critical" and "RuntimeError" in t for lvl, t in env["logs"])
    env["boxes"].clear()
    env["dialog_error"] = False
    env["params_error"] = True
    P.SetupPlugin(Iface()).run()
    assert env["boxes"] and "bad answers" in env["boxes"][0]


def test_wizard_that_cannot_open_is_reported(env, monkeypatch):
    def broken(_parent=None):
        raise ImportError("missing module")
    monkeypatch.setattr(P, "SetupWizard", broken)
    P.SetupPlugin(Iface()).run()
    assert env["boxes"] and "Setup wizard did not open" in env["boxes"][0]
    assert any(lvl == "Critical" for lvl, _t in env["logs"])


def test_the_real_setup_wizard_has_the_four_pages():
    w = P.SetupWizard()
    try:
        assert len(w.pageIds()) == 4
        assert hasattr(w.page(P.DAM_PAGE), "nid_button")          # the NID fill is there
    finally:
        w.deleteLater()
