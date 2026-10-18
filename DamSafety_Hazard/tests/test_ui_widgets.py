"""Hazard 2.18.33: ui_widgets.py is the shared widget file (identical in the Runner, Screening and
Hazard); ui_check_panel.py re-exports its CheckPanel, so the 2.18.32 imports keep working."""
import hashlib
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(HERE)
REPO = os.path.dirname(PLUGIN)
sys.path.insert(0, REPO)


def test_copies_identical():
    paths = [os.path.join(REPO, p, "ui_widgets.py")
             for p in ("DamSafety_Runner", "DamSafety_Screening", "DamSafety_Hazard")]
    paths = [p for p in paths if os.path.isfile(p)]          # an installed plugin has only its own
    assert paths and len({hashlib.sha256(open(p, "rb").read()).hexdigest() for p in paths}) == 1


def test_check_panel_reexported():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from DamSafety_Hazard import ui_check_panel, ui_widgets, checks
    assert ui_check_panel.CheckPanel is ui_widgets.CheckPanel
    assert ui_check_panel.Collapsible is ui_widgets.Collapsible
    assert (ui_widgets.ERROR, ui_widgets.WARNING, ui_widgets.OK) == (checks.ERROR, checks.WARNING, checks.OK)


if __name__ == "__main__":
    test_copies_identical()
    test_check_panel_reexported()
    print("ok")
