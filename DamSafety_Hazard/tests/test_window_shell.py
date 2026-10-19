"""Hazard 2.18.34: the three-view window (Classify / Results / Review) with the gear menu,
built in a real Qt (offscreen) - no project, no network.

    QT_QPA_PLATFORM=offscreen python3 DamSafety_Hazard/tests/test_window_shell.py
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qgis.core import QgsApplication  # noqa: E402
_app = QgsApplication.instance() or QgsApplication([], True)
_app.setPrefixPath(os.environ.get("QGIS_PREFIX_PATH", "/home/box/miniforge3/envs/qgis334"), True)
_app.initQgis()

import numpy as np  # noqa: E402
from osgeo import gdal, osr  # noqa: E402
from qgis.PyQt.QtWidgets import QAbstractButton, QMessageBox, QTableWidget  # noqa: E402

from DamSafety_Hazard import checks as CK  # noqa: E402
from DamSafety_Hazard import dialog as DLG  # noqa: E402
from DamSafety_Hazard.constants import PLUGIN_VERSION, DISCLAIMER  # noqa: E402

CK.check_services = lambda *a, **k: []          # Check inputs without the network


def _pump(n=4):
    for _ in range(n):
        _app.processEvents()


def _raster(path, arr):
    ds = gdal.GetDriverByName("GTiff").Create(path, arr.shape[1], arr.shape[0], 1, gdal.GDT_Float32)
    ds.SetGeoTransform((600000, 10, 0, 5200000, 0, -10))
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(26914)
    ds.SetProjection(srs.ExportToWkt())
    b = ds.GetRasterBand(1)
    b.WriteArray(arr)
    b.SetNoDataValue(-9999)
    ds = None


def _dam(depth):
    """A dam folder with RAS Mapper style names, found by the auto-fill."""
    d = os.path.join(tempfile.mkdtemp(prefix="hz_shell_"), "DWR-7")
    os.makedirs(d)
    _raster(os.path.join(d, "Depth (Max).tif"), depth)
    _raster(os.path.join(d, "Velocity (Max).tif"), np.full((40, 40), 3.0, "float32"))
    _raster(os.path.join(d, "Arrival Time (Max).tif"), np.full((40, 40), 1.5, "float32"))
    return d


class _NoPopups(object):
    """QMessageBox.warning / information / critical recorded instead of shown."""

    def __enter__(self):
        self.calls, self._old = [], {}
        for name in ("warning", "information", "critical"):
            self._old[name] = getattr(QMessageBox, name)
            setattr(QMessageBox, name, staticmethod(
                lambda *a, _n=name, **k: self.calls.append((_n, a[1:3] if len(a) > 2 else a))))
        return self

    def __exit__(self, *exc):
        for name, fn in self._old.items():
            setattr(QMessageBox, name, fn)


def _make():
    from qgis.core import QgsProject
    from DamSafety_Hazard import settings as ST
    QgsProject.instance().removeEntry(ST.SCOPE, ST.KEY)     # no settings of an earlier test
    d = DLG.NDDamHazardDialog()
    d.show()
    _pump()
    return d


def _done(d):
    d._busy = False
    d.close()
    d.deleteLater()
    _pump()


def _tab_texts(d):
    return [d.tab_widget.tabText(i).replace("&&", "&") for i in range(d.tab_widget.count())]


def test_shell_no_project():
    d = _make()
    try:
        assert d.windowTitle() == "DamSafety Hazard {}".format(PLUGIN_VERSION)
        assert _tab_texts(d) == ["Classify", "Results", "Review"]
        assert d.tab_widget.currentIndex() == 0 and d.action_area.isVisible()
        assert [a.text() for a in d.gear_menu.actions() if a.text()] == [
            "Suite settings\u2026", "Documentation (README)\u2026", "Run self-test",
            "Edit data sources\u2026"]
        assert d.tab_widget.cornerWidget() is d.gear_btn
        assert any(lbl.text() == DISCLAIMER for lbl in d.findChildren(DLG.QLabel))
        assert not hasattr(d, "advanced_check") and not hasattr(d, "_advanced_groups")
        assert not hasattr(d, "run_all_btn") and not hasattr(d, "run_class_btn")
        assert d.classify_btn.isDefault() and d.classify_btn.isEnabled()
        secs = d.option_sections
        assert len(secs) == 7
        assert [t for t, s in secs.items() if s.is_expanded()] == ["Report and map outputs"]
        assert not d.dss_section.is_expanded() and not d.ras_section.is_expanded()
        assert d.suite_group.isVisible()
        # Review: two separate tables, as before (the decisions are tied to their row lists)
        assert [d.review_tabs.tabText(i).replace("&&", "&") for i in range(2)] == [
            "Structures", "Roads & facilities"]
        assert d.review_table is not d.facrev_table
        assert d.review_table in d.review_tabs.widget(0).findChildren(QTableWidget)
        assert d.facrev_table in d.review_tabs.widget(1).findChildren(QTableWidget)
        # the raster box says what unticked means, readable while the box is off
        assert not d.raster_group.isChecked() and d.raster_hint.isVisible()
        assert "Depth is always needed" in d.raster_hint.text()
        # one ellipsis character on every button of the window and its two windows
        for host in (d, d.readme_dialog, d.suite_settings_dialog):
            bad = [b.text() for b in host.findChildren(QAbstractButton) if "..." in b.text()]
            assert not bad, bad
    finally:
        _done(d)


def test_dam_folder_autofill_and_errors_block_classify():
    d = _make()
    try:
        folder = _dam(1500 + np.random.RandomState(1).rand(40, 40).astype("float32") * 20)
        d.dam_folder_edit.setText(folder)                     # -> auto-fill (boxes empty)
        assert d.depth_edit.text().endswith("Depth (Max).tif")
        assert d.vel_edit.text().endswith("Velocity (Max).tif")
        assert d.output_folder_edit.text() == os.path.join(folder, "outputs")
        with _NoPopups() as np_:
            d._on_check_inputs()
        assert not np_.calls, np_.calls
        panel = d.check_panel
        assert panel.blocked and panel.isVisible() and not d.classify_btn.isEnabled()
        assert d.check_btn.text().startswith("Check inputs \u26a0")
        # a finished run / step (_set_busy(False)) must not switch Classify back on
        d._set_busy(True)
        assert not d.classify_btn.isEnabled()
        d._set_busy(False)
        assert d._checks_block and not d.classify_btn.isEnabled()
        d._update_button_states()
        assert not d.classify_btn.isEnabled()
        # Classify itself (if reached) shows the errors on Classify, no pop-up, no run
        d._show_view("Review")
        started = []
        d._launch_classification = lambda *a, **k: started.append(1)
        with _NoPopups() as np_:
            d._on_run_all()
        assert not np_.calls and not started
        assert d.tab_widget.tabText(d.tab_widget.currentIndex()) == "Classify"
        assert panel.blocked and not d.classify_btn.isEnabled()
        # an input change clears the stale results: Classify is back
        d.depth_edit.setText(d.depth_edit.text() + "x")
        assert not panel.blocked and d.classify_btn.isEnabled()
    finally:
        _done(d)


def test_validation_errors_in_panel_not_popup():
    d = _make()
    try:
        started = []
        d._launch_classification = lambda *a, **k: started.append(1)
        with _NoPopups() as np_:
            d._on_run_all()                                   # no dam folder, no rasters
            d._on_run_classification()                        # the review re-run path
        assert not np_.calls and not started, np_.calls
        assert d.check_panel.blocked and not d.classify_btn.isEnabled()
        assert any("Dam folder" in lbl.text() for lbl in d.check_panel.findChildren(DLG.QLabel))
    finally:
        _done(d)


def test_busy_disables_gear_actions_and_readme_buttons():
    d = _make()
    try:
        # the README window's buttons exist before it is ever opened
        assert d.selftest_btn is not None and d.sources_btn is not None
        assert not d.readme_dialog.isVisible()
        d._set_busy(True)
        assert not d.selftest_action.isEnabled() and not d.sources_action.isEnabled()
        assert not d.selftest_btn.isEnabled() and not d.sources_btn.isEnabled()
        assert not d.classify_btn.isEnabled() and not d.check_btn.isEnabled()
        assert not d.submittal_btn.isEnabled() and not d.watch_btn.isEnabled()
        d._set_busy(False)
        assert d.selftest_action.isEnabled() and d.sources_action.isEnabled()
        assert d.selftest_btn.isEnabled() and d.classify_btn.isEnabled()
    finally:
        _done(d)


def test_settings_and_readme_windows_open():
    d = _make()
    try:
        s = d._open_suite_settings()
        _pump()
        assert s is d.suite_settings_dialog and s.isVisible() and not s.isModal()
        assert d.suite_grid is not None
        r = d._open_readme()
        _pump()
        assert r.isVisible() and not r.isModal()
        assert "Version {}".format(PLUGIN_VERSION) in d.readme_text.toPlainText()
        s.close()
        r.close()
        # a grid that cannot be built never stops the window (the v2.13 guard)
        old = DLG.NDDamHazardDialog._build_suite_settings_tab

        def boom(self):
            raise RuntimeError("no catalog")
        DLG.NDDamHazardDialog._build_suite_settings_tab = boom
        try:
            d2 = _make()
            assert d2.suite_settings_dialog is None
            with _NoPopups() as np_:
                assert d2._open_suite_settings() is None
            assert np_.calls and "no catalog" in str(np_.calls[0])
            _done(d2)
        finally:
            DLG.NDDamHazardDialog._build_suite_settings_tab = old
    finally:
        _done(d)


def test_after_classification_shows_results_and_project_change_clears():
    d = _make()
    try:
        d._populate_review = lambda *a: None
        d._populate_facility_review = lambda *a: None
        res = {"nd": {"cls": "High"}, "tm11": {"cls": "Significant", "lij": 3.4},
               "results": {}, "governing": "Breach", "report": "", "notes": [],
               "lines": ["ND HIGH", "TM 11 SIGNIFICANT"],
               "result_items": [{"section": "5.2.1", "item": "Habitable structures",
                                 "found": "3", "result": "HIGH", "geoms": []}]}
        d._after_classification(res)
        assert d.tab_widget.tabText(d.tab_widget.currentIndex()) == "Results"
        assert not d.action_area.isVisible()                 # the action bar is Classify's
        assert d.result_table.rowCount() == 1 and "HIGH" in d.result_label.text()
        assert not d.result_hint.isVisible()
        # back on Classify: the layer lists are refreshed, the action bar is back
        calls = []
        d._refresh_layer_combos = lambda *a: calls.append(1)
        d._show_view("Classify")
        _pump()
        assert calls and d.action_area.isVisible()
        # Project > Open another project: nothing of the old run stays
        d.review_table.setRowCount(3)
        d._review_rows = [{}, {}, {}]
        d.open_report_btn.setEnabled(True)
        d._on_project_changed()
        assert d.review_table.rowCount() == 0 and d._review_rows == []
        assert d.review_status.text() == "Run Hazard Classification first"
        assert not d.open_report_btn.isEnabled()
        assert d.result_table.rowCount() == 0
        assert d.result_label.text() == "No classification run yet"
        assert d.class_result_text.toPlainText() == ""
    finally:
        _done(d)


def test_old_state_with_advanced_check_loads():
    d = _make()
    try:
        assert "advanced_check" not in d._SETTING_WIDGETS
        assert "advanced_check" in d._RETIRED_SETTINGS
        d._apply_state({"advanced_check": True, "judgment_check": False, "pages_spin": 5})
        assert d._apply_failed == []
        assert not d.judgment_check.isChecked() and d.pages_spin.value() == 5
        st = d._current_state()
        assert "advanced_check" not in st and st["judgment_check"] is False
        # the option sections are not opened by the old value
        assert [t for t, s in d.option_sections.items() if s.is_expanded()] == [
            "Report and map outputs"]
    finally:
        _done(d)


if __name__ == "__main__":
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            n += 1
            print("ok", name)
    print("{} passed".format(n))
    # Exit without interpreter teardown: some QGIS builds (conda-forge on GitHub Actions)
    # crash while destroying the QgsApplication at exit, after every check has passed.
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)
