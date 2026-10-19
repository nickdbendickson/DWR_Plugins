"""The 3.10.15 window shell with the real Qt of QGIS (not the fakes of the suite), offscreen.

    QT_QPA_PLATFORM=offscreen <QGIS python> DamSafety_Runner/tests/realqt_window_shell.py [shots_dir]

Not named test_*: run_tests.py imports every test_* module into its fake-Qt process. Opens no
Processing dialog and runs no model. With a folder argument it also saves screenshots.
"""
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
RUNNER = os.path.dirname(HERE)
ROOT = tempfile.mkdtemp()
os.symlink(RUNNER, os.path.join(ROOT, "DamSafety_Runner"))
if os.path.isdir(os.path.join(os.path.dirname(RUNNER), "DamSafety_Screening")):   # the DEM download
    os.symlink(os.path.join(os.path.dirname(RUNNER), "DamSafety_Screening"), os.path.join(ROOT, "DamSafety_Screening"))
sys.path.insert(0, ROOT)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.pop("NDSUITE_SETTINGS", None)

from qgis.core import QgsApplication, QgsProject  # noqa: E402
_app = QgsApplication([], True)
_app.setPrefixPath(os.environ.get("QGIS_PREFIX_PATH", "/home/box/miniforge3/envs/qgis334"), True)
_app.initQgis()

from DamSafety_Runner import dialog as D  # noqa: E402
from DamSafety_Runner import popup_logic as pl  # noqa: E402
from DamSafety_Runner.core import workflow as wf  # noqa: E402
from DamSafety_Runner.ui_widgets import StageStrip  # noqa: E402
from DamSafety_Runner.damsuite import settings as DS  # noqa: E402

SHOTS = sys.argv[1] if len(sys.argv) > 1 else ""
Y, P, N = StageStrip.YES, StageStrip.PART, StageStrip.NO
DS.set_current_path("")


def pump():
    for _ in range(5):
        _app.processEvents()


def shot(w, name):
    if SHOTS:
        os.makedirs(SHOTS, exist_ok=True)
        pump()
        w.grab().save(os.path.join(SHOTS, name + ".png"))


QgsProject.instance().removeEntry(D.SCOPE, D.ENTRY)
dlg = D.NDBreachRunnerDialog()
if SHOTS:            # the offscreen screen is 800x600: show the size a normal screen gets
    dlg.setMaximumSize(4000, 4000)
    dlg.resize(980, 680)
dlg.show()
pump()

# 1. built with no Dams folder
assert [dlg.tabs.tabText(i) for i in range(dlg.tabs.count())] == ["Run", "Tools"]
assert dlg.status_label.text() == "Choose the Dams folder and the dam ID."
assert dlg.strip.states == [N, N, N, N], dlg.strip.states
assert [c.text() for c in dlg.strip.chips] == ["○ Dam", "○ Terrain", "○ Model run", "○ Products"]
assert not dlg.chips_row.isVisible() and not dlg.contents_section.isVisible()
assert not dlg.folder_btn.isEnabled() and not dlg.add_btn.isEnabled()
assert dlg.run_dam_btn.isDefault()
# punch list: Run is greyed until a dam is chosen; Close sits alone at the bottom right
assert not dlg.run_dam_btn.isEnabled() and "Dams folder and the dam ID first" in dlg.run_dam_btn.toolTip()
assert "border:none" in dlg.lis_chip.styleSheet() and type(dlg.lis_chip).__name__ == "EngineChip"
_cb = dlg.close_btn.mapTo(dlg, dlg.close_btn.rect().topLeft())
_lg = dlg.log_section.header.mapTo(dlg, dlg.log_section.header.rect().topLeft())
assert _cb.y() > _lg.y() and _cb.x() + dlg.close_btn.width() > dlg.width() - 40, (_cb, _lg)
assert dlg.lis_chip.text().split()[0] == "LISFLOOD-FP" and dlg.anuga_chip.text().split()[0] == "ANUGA"
assert not dlg.adv_section.is_expanded() and not dlg.adv_section.inner().isVisible()
assert not dlg.log_section.inner().isVisible()
assert not hasattr(dlg, "adv_box") and not hasattr(dlg, "models_label")
shot(dlg, "run_tab_empty")

# 2. advanced: the header carries the values and stays right on toggle and edits
t0 = dlg.adv_section.title()
assert t0 == "Advanced  ·  grid 10 m · 12 h simulated · up to 96 h", t0
for _ in range(3):
    dlg.adv_section.header.click()
assert dlg.adv_section.is_expanded() and dlg.adv_section.inner().isVisible()
assert dlg.adv_section.header.text() == "▾ " + t0, dlg.adv_section.header.text()
dlg.grid_m.setValue(5.0)
dlg.force_anuga.setChecked(True)
assert dlg.adv_section.title() == "Advanced  ·  grid 5 m · 12 h simulated · up to 96 h · Force ANUGA"
assert dlg.adv_section.header.text().startswith("▾ Advanced  ·  grid 5 m")
assert dlg.settings()["force_anuga"] is True
shot(dlg, "advanced_expanded")
dlg.force_anuga.setChecked(False)
dlg.grid_m.setValue(10.0)
# v3.10.22: the optional gmsh points for HEC-RAS: off by default, a plain checkbox in Advanced
g = dlg.hecras_gmsh
assert g.text() == "Refine HEC-RAS mesh with Gmsh points (optional)" and not g.isChecked()
assert g.isVisible() and "Needs gmsh" in g.toolTip()
assert "regular grid is used" in g.toolTip() and "off by default" in g.toolTip()
assert "hecras_gmsh" not in dlg.settings()
assert "HECRAS_GMSH" not in pl.defaults("rundam", "", "", dict(pl.DEFAULT_SETTINGS, **dlg.settings()))
g.setChecked(True)
assert dlg.settings()["hecras_gmsh"] is True
assert dlg.adv_section.title().endswith(" · HEC-RAS gmsh points"), dlg.adv_section.title()
assert pl.defaults("rundam", "", "", dict(pl.DEFAULT_SETTINGS, **dlg.settings()))["HECRAS_GMSH"] is True
shot(dlg, "advanced_hecras_gmsh_checked")
g.setChecked(False)
assert "gmsh" not in dlg.adv_section.title()
# the HEC-RAS dam line along the crest: on by default, unticked = the straight line
c = dlg.hecras_crest
assert c.text() == "HEC-RAS dam line follows the crest (recommended)" and c.isChecked()
assert c.isVisible() and "On by default" in c.toolTip() and "log says so" in c.toolTip()
assert "hecras_dam_axis" not in dlg.settings()
assert "HECRAS_CREST" not in pl.defaults("rundam", "", "", dict(pl.DEFAULT_SETTINGS, **dlg.settings()))
c.setChecked(False)
assert dlg.settings()["hecras_dam_axis"] == "straight"
assert dlg.adv_section.title().endswith(" · HEC-RAS straight dam line"), dlg.adv_section.title()
assert pl.defaults("rundam", "", "", dict(pl.DEFAULT_SETTINGS, **dlg.settings()))["HECRAS_CREST"] is False
shot(dlg, "advanced_hecras_crest_unticked")
c.setChecked(True)
assert "straight" not in dlg.adv_section.title()
dlg.adv_section.header.click()
assert dlg.adv_section.header.text().startswith("▸ Advanced") and "▾" not in dlg.adv_section.header.text()

# 3. a valid Dams folder and a dam with flood rasters
dams = tempfile.mkdtemp()
dlg.dams_edit.setText(dams)
dlg.dam_edit.setText("DWR-7")
dlg._changed()
assert "No folder for dam DWR-7" in dlg.status_label.text()
assert dlg.strip.states == [P, N, N, N], dlg.strip.states
f = os.path.join(dams, "DWR-7")
os.makedirs(f)
for k in ("depth", "velocity", "arrival"):
    open(os.path.join(f, wf.FINAL_NAMES[k]), "wb").close()
dlg.refresh()
assert "Dam folder " + f in dlg.status_label.text() and dlg.folder_btn.isEnabled()
assert dlg.run_dam_btn.isEnabled() and dlg.run_dam_btn.toolTip() == D.RUN_TIP
assert "dam.json not written yet" in dlg.strip.chips[0].toolTip()
assert dlg.strip.states == [P, N, Y, N], dlg.strip.states
assert dlg.card_title.text() == "<b>DWR-7</b>" and dlg.card_path.text() == f and dlg.chips_row.isVisible()
assert dlg.card_chips["run"].text() == "✓ Flood run" and dlg.card_chips["submittal"].text() == "○ Submittal"
assert dlg.contents_section.title() == "Folder contents (1)" and "✓ Flood rasters" in dlg.contents_edit.toPlainText()
assert dlg.tool_tag["rundam"].text() == "✓ done" and dlg.tool_tag["submittal"].text() == "not run yet"
assert "estimate" not in dlg.tool_tag
assert dlg.dam_combo.findData("DWR-7") > 0
open(os.path.join(f, "Flood Extent.geojson"), "w").close()
dlg.refresh()
assert dlg.strip.states == [P, N, Y, P], dlg.strip.states
os.makedirs(os.path.join(f, wf.MODEL_FOLDER, "lisflood"))
open(os.path.join(f, wf.MODEL_FOLDER, "lisflood", "dwr7.dem.asc"), "w").close()
open(os.path.join(f, wf.MODEL_FOLDER, "run_report.json"), "w").close()
os.utime(os.path.join(f, wf.MODEL_FOLDER, "run_report.json"), (1e9, 1e9))
open(os.path.join(f, "DWR-7 submittal.docx"), "w").close()
dlg.refresh()
assert dlg.strip.states == [P, Y, Y, P], dlg.strip.states
assert dlg.card_chips["submittal"].text() == "✓ Submittal" and dlg.tool_tag["submittal"].text() == "✓ done"
dlg.contents_section.set_expanded(True)
shot(dlg, "run_tab_with_dam")
# a DamSafety Hazard package older than the run report, and a review of another package
import json as _json  # noqa: E402
_m = os.path.join(f, "outputs", "Submittal", "DWR-7_2026-10-01", "MANIFEST.csv")
os.makedirs(os.path.dirname(_m))
with open(_m, "w") as _fh:
    _fh.write("File,SHA256\n")
os.utime(_m, (1e8, 1e8))
with open(os.path.join(f, wf.MODEL_FOLDER, "run_report.json"), "w") as _fh:
    _json.dump({"summary": {"final_model": "LISFLOOD-FP", "flooded_acres": 812.4},
                "cut_off": {"reason": "duration cap", "detail": "x"}}, _fh)
from DamSafety_Runner.damsuite import dam_record as _DR  # noqa: E402
_rec = _DR.load(f, create=True)
_rec.add_status("reviewed", "DamSafety Hazard", "2.18.32", {"decision": "accepted", "package_sha256": "0" * 64})
_rec.save()
dlg.contents_section.set_expanded(False)
dlg.refresh()
assert dlg.card_chips["package"].text() == "◐ Package", dlg.card_chips["package"].text()
assert dlg.warn_label.isVisible() and "rebuild it in DamSafety Hazard" in dlg.warn_label.text()
assert "has not been reviewed" in dlg.warn_label.text()
assert "RESULTS CUT OFF: duration cap" in dlg.contents_edit.toPlainText()
assert dlg.strip.states[0] == Y and "dam.json record exist" in dlg.strip.chips[0].toolTip()
# the warning sits above the chips and wraps (no elision), visible at 980x680 without scrolling
assert dlg.warn_label.wordWrap() and dlg.warn_label.mapTo(dlg, dlg.warn_label.rect().topLeft()).y() < dlg.chips_row.mapTo(dlg, dlg.chips_row.rect().topLeft()).y()
if SHOTS:
    pump()
    _wb = dlg.warn_label.mapTo(dlg, dlg.warn_label.rect().bottomLeft())
    assert _wb.y() < dlg.height(), (_wb, dlg.height())
    assert dlg.warn_label.height() >= dlg.warn_label.heightForWidth(dlg.warn_label.width()) - 2, (dlg.warn_label.height(), dlg.warn_label.heightForWidth(dlg.warn_label.width()), dlg.warn_label.width(), dlg.tabs.height())
shot(dlg, "run_tab_package_warning")
os.remove(os.path.join(f, "dam.json"))
shutil.rmtree(os.path.join(f, "outputs"))
dlg.refresh()
assert not dlg.warn_label.isVisible()
dlg.contents_section.set_expanded(True)
# a dam with only a DEM chosen, no run yet
dlg.dam_edit.setText("DWR-8")
dlg._pending_dem = os.path.join(dams, "dem.tif")
dlg._fill_dem()
dlg._changed()
assert dlg.strip.states == [P, P, N, N], dlg.strip.states
dlg.dam_edit.setText("DWR-7")
dlg.dem_combo.setCurrentIndex(0)
dlg._changed()

dlg.contents_section.set_expanded(False)
# offline-pack guard clears the card
dlg.dams_edit.setText(os.path.join(dams, "offline_pack"))
dlg._changed()
assert "offline pack" in dlg.status_label.text() and not dlg.chips_row.isVisible()
assert dlg.strip.states == [N, N, N, N] and not dlg.folder_btn.isEnabled()
assert all(t.text() == "" for t in dlg.tool_tag.values())
dlg.dams_edit.setText(dams)
dlg._changed()

# 4. the Tools tab: the rail switches the pages; every tool has exactly one button
dlg.tabs.setCurrentIndex(1)
assert dlg.rail.count() == len(pl.TABS) == dlg.pages.count()
for i in range(dlg.rail.count()):
    dlg.rail.setCurrentRow(i)
    assert dlg.pages.currentIndex() == i
assert set(dlg.tool_button) == {t[0] for t in pl.ALL_TOOLS}
assert all("..." not in b.text() for b in dlg.tool_button.values())
assert dlg.hazard_btn.text() == "Open DamSafety Hazard…" and dlg.screening_btn.text() == "Open DamSafety Screening…"
opened = []
dlg._open_other = lambda name, label: opened.append(name)
dlg.hazard_btn.click()
dlg.screening_btn.click()
assert opened == ["DamSafety_Hazard", "DamSafety_Screening"], opened
dlg.rail.setCurrentRow(0)
shot(dlg, "tools_tab")
dlg.rail.setCurrentRow(3)
shot(dlg, "tools_tab_postprocess")
dlg.tabs.setCurrentIndex(0)

# 5. the settings window: non-modal, behind the gear, the gear shows the changed count
assert not dlg.settings_dialog.isVisible() and not dlg.settings_dialog.isModal()
assert dlg.gear.text() == "⚙"
dlg.gear.click()
pump()
assert dlg.settings_dialog.isVisible()
cell = ("ndbreach", "rundam", "CELL")
sf = os.path.join(dams, "damsuite_settings.csv")
dlg.settings_file_edit.setText(sf)
dlg.grid.settings.path = sf
dlg.grid.save(sf, name="test")
assert dlg.grid.set_value(cell, "5") == ""
assert dlg.gear.text() == "⚙ 1", dlg.gear.text()
shot(dlg.settings_dialog, "settings_dialog")
dlg.settings_dialog.close()

# 5b. the window title and the size remembered on close (QSettings of the test profile)
assert dlg.windowTitle().startswith("DamSafety Runner"), dlg.windowTitle()
from DamSafety_Runner import ui_fit as _uf  # noqa: E402
dlg.resize(900, 600)
_uf.save_size(dlg, "DamSafety_Runner_test/window")
assert _uf._saved_size("DamSafety_Runner_test/window") == (dlg.width(), dlg.height())
assert _uf._screen_of(dlg) is not None

# 6. old saved state behaves the same: force_anuga false/true reloads as saved
for v in (False, True):
    s = dict(pl.DEFAULT_SETTINGS, force_anuga=v)
    dlg._apply(s)
    assert dlg.settings()["force_anuga"] is v
DS.set_current_path("")
print("realqt_window_shell: all checks passed")
