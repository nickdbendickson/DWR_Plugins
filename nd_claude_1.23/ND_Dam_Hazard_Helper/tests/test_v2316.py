"""2.31.6 (Lumo helpers/dialog/review_tab): busy locks for the field-check import, cache clear
and web-map buttons; '&&' in the tab captions.
Run: python3 -m ND_Dam_Hazard_Helper.tests.run_tests (from the folder above the plugin)."""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_DIR = os.path.dirname(HERE)
if os.path.dirname(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, os.path.dirname(PLUGIN_DIR))
from ND_Dam_Hazard_Helper.tests.registry import test  # noqa: E402


def _src(name):
    with open(os.path.join(PLUGIN_DIR, name), encoding="utf-8") as fh:
        return fh.read()


def _busy_names():
    src = _src("dialog.py")
    body = src[src.index("def _set_busy"):]
    body = body[:body.index("w.setEnabled(not busy)")]
    return set(re.findall(r'"(\w+)"', body))


@test
def v2316_run_locks_field_import_cache_clear_and_webmap():
    names = _busy_names()
    for n in ("field_import_btn", "cache_clear_btn", "review_webmap_btn", "review_save_btn"):
        assert n in names, n
    rt = _src("review_tab.py")
    body = rt[rt.index("def _field_sheet_import"):][:600]
    assert 'getattr(self, "_busy", False)' in body


@test
def v2316_tab_captions_escape_the_ampersand():
    src = _src("dialog.py")
    assert '"Setup && Run"' in src and '"Data && Output"' in src
    assert 'FT.TAB_TITLE.replace("&", "&&")' in src
    assert not re.search(r'addTab\([^\n]*"[^"&]*[^&]&[^&][^"]*"\)', src)
    # the tab-changed checks still compare the unescaped text
    assert '.replace("&&", "&") in ("Hazard Classification", "Data & Output")' in src
    assert '.replace("&&", "&") == "Setup & Run"' in src
