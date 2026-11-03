"""Style A (Crest build pack, 2026-11-02): the folder scan as a per-layer status list with what
each missing layer is for, the Start here… step strip, and failures that are shown, not
swallowed. Real Qt, offscreen, in the QGIS python:
    QT_QPA_PLATFORM=offscreen python -m pytest DamSafety_Setup/tests/test_style_a.py
"""
import os
import sys
import tempfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QGIS_CUSTOM_CONFIG_PATH", tempfile.mkdtemp(prefix="setup_style_a_cfg_"))

qgis_core = pytest.importorskip("qgis.core")
_app = qgis_core.QgsApplication.instance() or qgis_core.QgsApplication([], True)

from DamSafety_Setup import plugin as P  # noqa: E402


def test_scan_list_found_and_missing_with_notes():
    text = P.scan_marks_html({"dwr": "a.gpkg", "roads": "r.gpkg"})
    assert "✓ DWR dams</span> — found" in text
    assert "✓ Roads</span> — found" in text
    for label, note in (("Dam lines", "optional — locates crests precisely"),
                        ("NSI structures", "optional — improves receptor mapping"),
                        ("Rail", "optional — rail-crossing impacts")):
        assert "✕ {}</span> — {}".format(label, note) in text
    assert P.SCAN_ENOUGH in text and P.GREEN in text and P.RED in text


def test_scan_list_without_dwr_says_required_and_no_green_line():
    text = P.scan_marks_html({})
    assert "✕ DWR dams</span> — required — the dam list comes from here" in text
    assert P.SCAN_ENOUGH not in text
    assert text.count("✕") == len(P.SCAN_LAYERS)


def test_steps_match_the_runner_wizard():
    runner = pytest.importorskip("DamSafety_Runner.wizard")
    assert P.WIZARD_STEPS == runner.WIZARD_STEPS
    for n in range(len(P.WIZARD_STEPS)):
        for done in (True, False):
            assert P.steps_html(n, done) == runner.steps_html(n, done)
    assert P.WIZARD_STEPS[0] == "Data folder" and P.WIZARD_STEPS[-1] == "Run tool"


def test_folder_page_shows_the_step_strip():
    w = P.SetupWizard(runner=object())
    try:
        page = w.page(P.FOLDER_PAGE)
        text = page.steps.text()
        assert text == P.steps_html(0, False) and page.steps.wordWrap()
        assert "\u25b6 Data folder" in text
        assert all("\u25cb " + s in text for s in P.WIZARD_STEPS[1:])
    finally:
        w.deleteLater()


def test_dams_folder_failure_is_shown(monkeypatch):
    seen = []
    monkeypatch.setattr(P, "_msg", lambda text, level="Warning": seen.append(text))
    from DamSafety_Setup.damsuite import layout as LY

    def boom(path):
        raise OSError("read-only")
    monkeypatch.setattr(LY, "ensure_dams_folder", boom)
    assert P.ensure_dams_folder("/x/Dams") == []
    assert seen and "read-only" in seen[0] and "Suite setup / status" in seen[0]


def test_broken_runner_wizard_is_shown(monkeypatch):
    import importlib
    import qgis.utils
    seen = []
    monkeypatch.setattr(P, "_msg", lambda text, level="Warning": seen.append(text))
    monkeypatch.setattr(qgis.utils, "plugins", {"DamSafety_Runner": object()})
    real = importlib.import_module

    def fake(name, *a, **k):
        if name == "DamSafety_Runner.wizard":
            raise ImportError("broken install")
        return real(name, *a, **k)
    monkeypatch.setattr(importlib, "import_module", fake)
    assert P.runner_wizard_module() is None
    assert seen and "broken install" in seen[0] and "Reinstall" in seen[0]
