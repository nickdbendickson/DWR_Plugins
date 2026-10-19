"""DamSafety Screening GUI fixes (0.17.23 rework): Qt5/Qt6-safe enums + dead-code removal.

Two concrete GUI bugs the review found in dialog.py:
 (1) _run_selected_dams() set the list's selection mode with the UNSCOPED enum
     QListWidget.MultiSelection. That works on PyQt5 but raises AttributeError under
     PyQt6, where the scoped QListWidget.SelectionMode.MultiSelection is required -
     while the same method already guards its other enums with
     getattr(Qt, 'CheckState', Qt). Fixed to the same scoped-first / legacy-fallback
     getattr access; behaviour is identical on Qt5.
 (2) refresh() built the running-step note as
       (note if False else <text> + " | " + extra)
     where 'note if False' can never be true, so the branch was dead/confusing.
     Simplified to the plain expression.

This test:
  - source-scans the touched methods (_run_selected_dams, _build_results_tab,
    _build_review_tab) and asserts no unscoped Qt5-only selection / edit-trigger enum
    spelling remains, that the selection mode is reached through the compat getattr,
    and that the dead 'note if False else' branch is gone;
  - proves, with a tiny Qt6-shaped stand-in (no PyQt6 needed), that the OLD unscoped
    access raises AttributeError while the FIXED expression resolves;
  - with real PyQt5 installed, builds the dialog offscreen (QT_QPA_PLATFORM=offscreen,
    tests/_realqt.py) and confirms the fixed selection-mode expression resolves and
    applies on a real QListWidget, and that the results/review tabs build.

Run:  QT_QPA_PLATFORM=offscreen python3.12 tests/test_gui_qt6_enums.py
      (or plain python3 tests/test_gui_qt6_enums.py for the source-scan only)
"""
import ast
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(HERE)
DIALOG = os.path.join(PLUGIN, "dialog.py")

SRC = open(DIALOG, encoding="utf-8").read()
TREE = ast.parse(SRC)


def _method_source(name):
    """The exact source text of the top-level method `name` on the dialog class."""
    best = None
    for node in ast.walk(TREE):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            if best is None or (node.end_lineno - node.lineno) < (best.end_lineno - best.lineno):
                best = node
    assert best is not None, "method not found: " + name
    seg = ast.get_source_segment(SRC, best)
    assert seg, name
    return seg


# ---- 1. source-scan the touched methods -------------------------------------------------
TOUCHED = ("_run_selected_dams", "_build_results_tab", "_build_review_tab")

# Unscoped Qt5-only spellings <QtClass>.<Enumerator> that raise AttributeError under PyQt6.
BANNED = (
    "QListWidget.MultiSelection", "QListWidget.SingleSelection",
    "QListWidget.ExtendedSelection", "QListWidget.ContiguousSelection",
    "QListWidget.NoSelection",
    "QAbstractItemView.MultiSelection", "QAbstractItemView.SingleSelection",
    "QTableWidget.NoEditTriggers", "QTableWidget.SelectRows",
    "QTableWidget.SelectColumns", "QTableWidget.SelectItems",
    "QAbstractItemView.NoEditTriggers", "QAbstractItemView.SelectRows",
)

for meth in TOUCHED:
    body = _method_source(meth)
    for bad in BANNED:
        assert bad not in body, "{}: unscoped Qt5-only enum still present: {}".format(meth, bad)

# the selection mode is reached through the scoped-first / legacy-fallback getattr,
# the same compat pattern the method uses for Qt.CheckState / Qt.ItemFlag / Qt.ItemDataRole.
run = _method_source("_run_selected_dams")
assert "setSelectionMode(" in run, "selection mode call missing"
assert "getattr(QListWidget, 'SelectionMode', QListWidget).MultiSelection" in run, run

# the build-tab methods keep their scoped (Qt6-safe, Qt5-compatible) spellings.
res = _method_source("_build_results_tab")
assert "QTableWidget.EditTrigger.NoEditTriggers" in res
assert "QTableWidget.SelectionBehavior.SelectRows" in res
rev = _method_source("_build_review_tab")
assert "SelectionBehavior.SelectRows" in rev

# the dead 'note if False else ...' branch is gone (it lived in refresh()).
assert "note if False" not in SRC, "dead 'note if False else' expression still present"
assert 'refresh' in {n.name for n in ast.walk(TREE) if isinstance(n, ast.FunctionDef)}
assert '(self.simple_note[cur].text() + " | " + extra).strip(" |")' in SRC, \
    "simplified simple-note expression not found"

print("source-scan: ok (no unscoped Qt5-only enum in touched methods; dead branch removed)")


# ---- 2. Qt6-shaped stand-in: old access raises, fixed access resolves -------------------
class _SelMode:
    MultiSelection = 2


class _Qt6ListWidget:
    """Mimics PyQt6: only the scoped enum exists, no unscoped alias."""
    SelectionMode = _SelMode


try:
    _ = _Qt6ListWidget.MultiSelection  # this is exactly the OLD code's access
    raise AssertionError("expected AttributeError for unscoped MultiSelection under a Qt6-shaped class")
except AttributeError:
    pass
assert getattr(_Qt6ListWidget, "SelectionMode", _Qt6ListWidget).MultiSelection == 2

print("qt6-shape: ok (old unscoped access raises; fixed getattr resolves)")


# ---- 3. real PyQt5 offscreen: expression applies and the dialog builds -------------------
try:
    import PyQt5  # noqa: F401
    REAL = True
except ImportError:
    REAL = False

app = None
if REAL:
    sys.path.insert(0, HERE)
    import _realqt
    app = _realqt.load()
    REAL = app is not None

if REAL:
    from qgis.PyQt.QtWidgets import QListWidget

    lw = QListWidget()
    mode = getattr(QListWidget, "SelectionMode", QListWidget).MultiSelection
    lw.setSelectionMode(mode)                       # must not raise, and must take effect
    assert int(lw.selectionMode()) == int(mode)

    from nd_dam_screening.damsuite import settings as DS
    from nd_dam_screening import dialog as D

    DS.set_current_path("", remember=False)
    dlg = D.NDDamScreeningDialog()                  # runs _build_results_tab / _build_review_tab
    dlg.show()
    app.processEvents()
    assert dlg.results_table is not None
    assert dlg.review_table is not None
    dlg.hide()
    print("real-Qt: ok (QListWidget selection mode applies; dialog builds offscreen)")
else:
    print("real-Qt: skipped (PyQt5 not importable); source-scan + qt6-shape only")

print("test_gui_qt6_enums: ok ({})".format("real Qt offscreen" if REAL else "source-scan"))
