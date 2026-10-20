"""0.17.23: the window's new shell. Builds the popup headless and checks the key widgets exist and
that the Data -> DEM -> Screen -> Report strip follows the step states (wf.statuses) and the report.

With PyQt5 installed the REAL widgets are used offscreen (QT_QPA_PLATFORM=offscreen, tests/_realqt.py);
without it the qgis mock's widget fakes are used. Either way:  python3 tests/test_ui_shell_01723.py
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
try:
    import PyQt5  # noqa: F401
    REAL = True
except ImportError:
    REAL = False

if REAL:
    sys.path.insert(0, HERE)
    import _realqt
    app = _realqt.load()
else:
    app = None
    sys.path.insert(0, os.path.join(HERE, "qgis_mock"))
    sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
    sys.path.insert(0, HERE)

from nd_dam_screening.core import workflow as wf  # noqa: E402
from nd_dam_screening.damsuite import settings as DS  # noqa: E402
from nd_dam_screening import dialog as D  # noqa: E402

DS.set_current_path("", remember=False)
dlg = D.NDDamScreeningDialog()
if REAL:
    dlg.show()
    app.processEvents()

# 1. the shell's widgets
title = dlg.windowTitle() if REAL else D.window_title()
assert title.startswith("DamSafety Screening"), title
assert [dlg.tabs.tabText(i) for i in range(dlg.tabs.count())] == ["Run", "Review", "Results"]
assert set(dlg.pipe_btn) == {"data", "dem", "screen", "report"}
for name in ("work_edit", "work_browse", "suite_edit", "suite_browse", "inventory_county", "work_hint",
             "data_hint", "disclaimer", "run_all_btn", "one_dam_btn", "stop_btn", "override_box",
             "inputs_page", "settings_btn", "settings_window", "settings_page", "log_edit", "log_line",
             "log_toggle", "close_btn", "status_line", "next_hint"):
    assert getattr(dlg, name, None) is not None, name
assert dlg.settings_page is dlg.grid                     # the existing grid, moved behind the gear
assert set(dlg.step_button) == set(wf.KEYS)              # every numbered step is still there
if REAL:                                                 # the mock's buttons keep no text
    assert dlg.work_browse.text() == dlg.suite_browse.text() == "Browse\u2026"
    assert dlg.one_dam_btn.text() == "One dam\u2026"

# 2. nothing set: hints show, strip all open
dlg.refresh()
assert dlg.work_hint.text() == "not set yet" and dlg.pipe_state == {
    "data": "waiting", "dem": "waiting", "screen": "waiting", "report": "waiting"}, dlg.pipe_state
assert not REAL or all(b.text().endswith("\u25cb") for b in dlg.pipe_btn.values())
assert not dlg.run_all_btn.isEnabled()

# 3. folders set and the step states move: the strip follows wf.statuses + the report file
work = tempfile.mkdtemp(prefix="ui01723_")
dlg.work_edit.setText(work)
dlg.suite_edit.setText(tempfile.mkdtemp(prefix="ui01723d_"))
real_statuses = wf.statuses
wf.statuses = lambda w, s: [(k, wf.TITLE[k], "done" if k in ("1", "2") else
                             ("optional" if k in wf.OPTIONAL else "waiting"), "") for k in wf.KEYS]
try:
    dlg.refresh()
finally:
    wf.statuses = real_statuses
assert dlg.pipe_state["data"] == "done" and dlg.pipe_state["dem"] == "done", dlg.pipe_state
assert dlg.pipe_state["screen"] == "waiting" and dlg.pipe_state["report"] == "waiting"
assert not REAL or (dlg.pipe_btn["data"].text() == "Data \u2713" and
                    dlg.pipe_btn["screen"].text() == "Screen \u25cb")
assert dlg.work_hint.text() == "" and dlg.run_all_btn.isEnabled()
rep = os.path.join(work, wf.REPORT_INDEX)
os.makedirs(os.path.dirname(rep), exist_ok=True)
open(rep, "w").close()
dlg.refresh()
assert dlg.pipe_state["report"] == "done"
assert not REAL or dlg.pipe_btn["report"].text() == "Report \u2713"

# 4. strip clicks jump to the tab; the gear opens the settings window; the log folds
dlg.pipe_btn["report"].click()
assert dlg.tabs.currentIndex() == 2
dlg.pipe_btn["dem"].click()
assert dlg.tabs.currentIndex() == 0
dlg.show_settings()
assert not REAL or dlg.settings_btn.text().startswith("\u2699 Settings")
dlg._log("hello\nlast line")
assert dlg.log_line.text() == "Log: last line"
if REAL:
    assert dlg.settings_window.isVisible()
    assert not dlg.log_edit.isVisible()
    dlg._toggle_log()
    assert dlg.log_edit.isVisible() and dlg.log_toggle.text() == "Hide log"
    dlg.settings_window.close()
print("test_ui_shell_01723: ok ({})".format("real Qt offscreen" if REAL else "qgis mock"))

# --- 0.17.23 layout fixes (round 2)
from nd_dam_screening import dialog as _dmod
src = open(_dmod.__file__, encoding="utf-8").read()
assert '"Leave these"' not in src
assert not any(('QPushButton("' in ln or 'QLabel("' in ln) and '...' in ln for ln in src.splitlines())
assert "Each button opens the step's Processing dialog, pre-filled." in src
assert _dmod.NDDamScreeningDialog._short_note("x" * 100).endswith("\u2026")
assert _dmod.NDDamScreeningDialog._short_note("needs step 1") == "needs step 1"
if REAL:
    import tempfile as _tf
    d2 = D.NDDamScreeningDialog()
    assert not d2.continue_btn.isVisible() and d2.continue_btn.isHidden()
    w2, s2 = _tf.mkdtemp(), _tf.mkdtemp()
    d2.work_edit.setText(w2); d2.suite_edit.setText(s2); d2._changed(); d2.refresh()
    assert d2.pipe_btn["data"].text() == "Data \u2713", d2.pipe_btn["data"].text()
    assert d2.continue_btn.isHidden()
    assert len({b.minimumWidth() for b in d2.step_button.values()}) == 1
print("ok layout round 2")
