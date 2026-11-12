# -*- coding: utf-8 -*-
"""Pierre's dialog.py review (11/03/2026), item 22: _set_busy and the settings save/restore read
their widgets with getattr(self, name, None), so a typo'd or renamed widget name silently
stops being disabled during a run (or saved with the project). The window now checks both
lists when it is built (_audit_widget_names, one warning in the QGIS log); this test makes
the same check a failure.

The audit's first outing found "run_all_btn" in this window's busy list - a leftover from
before the nine tabs became a Classify button + gear menu, disabling nothing - and it was
removed. This guards against the next such name.

    QT_QPA_PLATFORM=offscreen python3 DamSafety_Hazard/tests/test_dialog_widget_audit.py
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qgis.core import QgsApplication  # noqa: E402
_app = QgsApplication.instance() or QgsApplication([], True)
_app.setPrefixPath(os.environ.get("QGIS_PREFIX_PATH", "/home/box/miniforge3/envs/qgis334"), True)
_app.initQgis()


def test_every_busy_and_setting_widget_name_is_a_widget_of_the_built_window():
    from DamSafety_Hazard.dialog import NDDamHazardDialog
    dlg = NDDamHazardDialog()
    try:
        assert dlg._audit_widget_names() == [], dlg._audit_widget_names()
        # and the audit does catch a missing name
        dlg._BUSY_WIDGETS = dlg._BUSY_WIDGETS + ("no_such_btn",)
        assert dlg._audit_widget_names() == ["no_such_btn"]
    finally:
        dlg.close()


if __name__ == "__main__":
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            n += 1
            print("ok", name)
    print("{} passed".format(n))
    sys.stdout.flush()
    os._exit(0)
