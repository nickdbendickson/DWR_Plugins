"""Audit 2026-10-15: the suite-settings window's "Save as" must REPLACE the chosen file
(settings.save(replace=True)); without it the atomic save merged into that file. Checked in all
three ND copies' suite_settings.py (source check: the window needs a real Qt)."""
import os
import re

from nd_breach_runner.tests.registry import test as register

ND = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@register
def suite_settings_save_as_replaces_the_file():
    for plugin in ("nd_breach_runner", "nd_dam_screening", "ND_Dam_Hazard_Helper"):
        src = open(os.path.join(ND, plugin, "suite_settings.py"), encoding="utf-8").read()
        assert "def save(self, path=None, name=None, replace=False):" in src, plugin
        assert re.search(r"self\.settings\.save\([^)]*replace=replace\)", src, re.S), plugin
        assert "return self.save(path, replace=True)" in src, plugin
