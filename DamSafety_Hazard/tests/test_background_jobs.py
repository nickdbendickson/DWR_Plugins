# -*- coding: utf-8 -*-
"""Hazard 2.23.15 (audit, 2026-11-07): the HEC-RAS plan import, the DSS-WISE import, Check
inputs (with the service probes), the creep watch and the self-test run in a QgsTask through
dialog._start_background(), as the classification does, instead of on the GUI thread behind
a modal bar that pumped events. Their project / widget work (Setup boxes, layers, message
boxes, the check panel) happens in the task's done callback on the main thread.

Real PyQGIS is not importable in this sandbox: the modules are loaded against the stand-in
qgis / osgeo modules of test_classify_busy_guard.py and the real mixin methods are driven
against a stub "self" that records what they called. runner.start_task is replaced by a
version that runs the work at once and records that it was used.

    python3 DamSafety_Hazard/tests/test_background_jobs.py
"""
import importlib.util
import os
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def _fakes():
    spec = importlib.util.spec_from_file_location(
        "_busy_guard_fakes", os.path.join(HERE, "test_classify_busy_guard.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod._install_fakes()


def _modules():
    """(dialog, rasimport_tab, dsswise_tab, classify_tab, runner) or None to skip."""
    try:
        _fakes()
        from DamSafety_Hazard import dialog as DLG, rasimport_tab as RT, dsswise_tab as DT
        from DamSafety_Hazard import classify_tab as CT, runner as RN
    except Exception:                                            # noqa
        traceback.print_exc()
        return None
    return DLG, RT, DT, CT, RN


class _Edit(object):
    def __init__(self, text=""):
        self._t = text

    def text(self):
        return self._t

    def setText(self, t):
        self._t = t


class _Spin(object):
    def __init__(self, v):
        self._v = v

    def value(self):
        return self._v


class _Check(object):
    def __init__(self, on):
        self._on = on

    def isChecked(self):
        return self._on


class _Combo(object):
    def __init__(self):
        self.index = None

    def findData(self, d):
        return 0 if d == "2d" else 1

    def setCurrentIndex(self, i):
        self.index = i


class _Text(object):
    def __init__(self):
        self.text = ""

    def setPlainText(self, t):
        self.text = t


class _Panel(object):
    def __init__(self):
        self.results = None

    def set_results(self, r):
        self.results = r


def _host_class(DLG, RT, DT):
    """A stub window: the dialog attributes the moved jobs touch, plus the real import
    mixins (their _ras_out / _dsw_name / _dsw_out helpers). _start_background is the REAL
    dialog method; start_task is replaced per test (_SyncRunner)."""

    class _Host(_HostBase, RT.RasImportTabMixin, DT.DssWiseTabMixin):   # stubs win
        pass
    return lambda: _Host(DLG)


class _HostBase(object):

    def __init__(self, DLG):
        self._DLG = DLG
        self._busy = False
        self._chain = None
        self._progress = None
        self._task = None
        self._finishing = False
        self._project_gen = 0
        self._last_tick = 0.0
        self._last_pct = 0.0
        self.config_dam_folder = "/dams/DWR-1"
        self.config_output_folder = "/dams/DWR-1/outputs"
        self.calls, self.logs, self.status, self.busy_trace = [], [], [], []
        self.depth_edit, self.vel_edit, self.arr_edit, self.dv_edit = (_Edit() for _ in range(4))
        self.model_combo = _Combo()
        self.check_panel = _Panel()
        # HEC-RAS tab
        self.ras_plan_edit = _Edit(__file__)          # an existing file
        self.ras_dem_edit = _Edit("")
        self.ras_cell_spin, self.ras_breach_spin, self.ras_arrival_spin = _Spin(0.0), _Spin(1.5), _Spin(0.1)
        self.ras_use_setup_check, self.ras_load_check = _Check(True), _Check(False)
        self.ras_result_text = _Text()
        # DSS-WISE tab
        self._dsw_items = ["item"]
        self.dsw_use_setup_check, self.dsw_load_check = _Check(True), _Check(False)
        self.dsw_add_scenario_check = _Check(False)
        self.dsw_result_text = _Text()
        self.dsw_path_edit, self.dsw_name_edit = _Edit("/pkg.zip"), _Edit("Scenario A")
        self.watch_workers_spin = _Spin(1)

    # -- the real dialog machinery
    def _start_background(self, *a, **k):
        return self._DLG.NDDamHazardDialog._start_background(self, *a, **k)

    def _task_progress(self, pct, msg):
        self.calls.append(("progress", float(pct)))

    def _check_job(self):
        return self._DLG.NDDamHazardDialog._check_job(self)

    def _check_work(self, *a, **k):
        return self._DLG.NDDamHazardDialog._check_work(*a, **k)

    def _progress_tick(self, pct, msg):
        self.calls.append(("tick", float(pct)))

    def _make_progress(self, title, modal=True):
        self.calls.append(("bar", title, modal))
        return _Bar()

    def _finish_progress(self, force=False):
        self._progress = None

    def _set_busy(self, busy):
        self._busy = busy
        self.busy_trace.append(busy)

    def _cancel_task(self, *a):
        pass

    def _log(self, m):
        self.logs.append(str(m))

    def _update_status(self, m, ok=True):
        self.status.append((m, ok))

    def _clear_status(self):
        self.status.append(("", True))

    def _update_button_states(self, *a):
        self.calls.append(("buttons",))

    def _open_path(self, p):
        self.calls.append(("open", p))

    # -- the input check's main-thread reads
    def _raster_ownership_problems(self):
        return []

    def _check_paths(self):
        return {"depth": "/d.tif", "vel": None, "arr": None, "dv": None}

    def _check_cfg(self):
        return {"one_d": False}

    def _dsw_table_items(self):
        return self._dsw_items, []

    def _scn_add_row(self, row):
        self.calls.append(("scenario", row))


class _Bar(object):
    def __init__(self):
        self.canceled = _Signal()

    def setLabelText(self, t):
        pass

    def wasCanceled(self):
        return False

    def close(self):
        pass

    def deleteLater(self):
        pass


class _Signal(object):
    def connect(self, fn):
        pass


class _SyncRunner(object):
    """runner.start_task replaced: runs the work now on this thread, records the call and
    whether on_done ran AFTER the work returned (the main-thread part follows the task)."""

    def __init__(self, RN):
        self.RN = RN
        self.started = []
        self.old = RN.start_task

    def __enter__(self):
        rn = self

        def start_task(description, work, on_progress, on_log, on_done):
            out = rn.RN.Outcome()
            order = []
            try:
                out.result = work(lambda p, m: on_progress(float(p), str(m)), on_log)
                order.append("work")
            except rn.RN.CancelledByUser:
                out.cancelled = True
            except Exception as e:
                out.error = e
                out.traceback = traceback.format_exc()
            order.append("done")
            on_done(out)
            rn.started.append((description, order))
            return None
        self.RN.start_task = start_task
        return self

    def __exit__(self, *exc):
        self.RN.start_task = self.old


def _patch(mod, name, value):
    old = getattr(mod, name)
    setattr(mod, name, value)
    return lambda: setattr(mod, name, old)


# ----------------------------------------------------------------------------- tests --

def test_hecras_import_runs_in_task_and_finishes_on_main_thread():
    mods = _modules()
    if mods is None:
        print("    SKIP: dialog not importable")
        return
    DLG, RT, DT, CT, RN = mods
    host = _host_class(DLG, RT, DT)()
    seen = {}

    def import_plan(plan, out, dem_path=None, cell_ft=0.0, breach_hr=0.0, arrival_ft=0.1,
                    progress=None, runner_dirs=None, use_runner=True):
        seen.update(plan=plan, out=out, dem=dem_path, cell=cell_ft, breach=breach_hr,
                    arr=arrival_ft, dirs=runner_dirs)
        progress(50.0, "Gridding")
        # the widgets must not have been touched while the "worker" runs
        assert host.depth_edit.text() == "" and host.ras_result_text.text == ""
        return {"paths": {"depth": out + "/Depth_ft.tif", "vel": out + "/Velocity_ftps.tif"},
                "lines": ["A: 10 cells"], "notes": [], "pixel_ft": 5.0,
                "depth_basis": "terrain"}
    undo = [_patch(RT.RI, "import_plan", import_plan),
            _patch(RT.RI, "slug", lambda p: "plan"),
            _patch(RN, "release_folder", lambda folder: 0)]
    try:
        with _SyncRunner(RN) as sr:
            RT.RasImportTabMixin._ras_import(host)
        assert len(sr.started) == 1 and sr.started[0][0].startswith("Importing HEC-RAS"), sr.started
        assert sr.started[0][1] == ["work", "done"]
        assert seen["plan"] == __file__ and seen["breach"] == 1.5 and seen["arr"] == 0.1
        assert seen["dirs"] == ["/dams/DWR-1"]
        # the task's progress reached the dialog's main-thread slot
        assert ("progress", 50.0) in host.calls, host.calls
        # the main-thread part ran in done: Setup boxes filled, model set to 2D, text shown
        assert host.depth_edit.text().endswith("Depth_ft.tif")
        assert host.vel_edit.text().endswith("Velocity_ftps.tif") and host.dv_edit.text() == ""
        assert host.model_combo.index == 0
        assert "Imported to" in host.ras_result_text.text
        assert any("imported" in m for m in host.logs), host.logs
        # busy on while it ran, off after; the bar was the non-modal background one
        assert host.busy_trace == [True, False], host.busy_trace
        assert ("bar", "Importing HEC-RAS plan results…", False) in host.calls
        assert not host._busy
    finally:
        for u in undo:
            u()


def test_hecras_import_error_reported_in_done_not_raised():
    mods = _modules()
    if mods is None:
        print("    SKIP: dialog not importable")
        return
    DLG, RT, DT, CT, RN = mods
    host = _host_class(DLG, RT, DT)()
    boxes = []

    def boom(*a, **k):
        raise RT.RI.RasImportError("no 2D results")
    undo = [_patch(RT.RI, "import_plan", boom), _patch(RN, "release_folder", lambda f: 0),
            _patch(RT.QMessageBox, "critical", staticmethod(lambda *a, **k: boxes.append(a[2])))]
    try:
        with _SyncRunner(RN) as sr:
            RT.RasImportTabMixin._ras_import(host)
        assert sr.started and sr.started[0][1] == ["done"]
        assert boxes == ["no 2D results"], boxes
        assert host.status and "FAILED" in host.status[-1][0]
        assert host.depth_edit.text() == "" and not host._busy
    finally:
        for u in undo:
            u()


def test_hecras_import_cancel_leaves_setup_alone():
    mods = _modules()
    if mods is None:
        print("    SKIP: dialog not importable")
        return
    DLG, RT, DT, CT, RN = mods
    host = _host_class(DLG, RT, DT)()

    def cancelled(*a, **k):
        raise RN.CancelledByUser()
    undo = [_patch(RT.RI, "import_plan", cancelled), _patch(RN, "release_folder", lambda f: 0)]
    try:
        with _SyncRunner(RN) as sr:
            RT.RasImportTabMixin._ras_import(host)
        assert sr.started
        assert host.status == [("HEC-RAS import cancelled", False)], host.status
        assert host.depth_edit.text() == "" and host.busy_trace == [True, False]
    finally:
        for u in undo:
            u()


def test_hecras_import_busy_guard():
    mods = _modules()
    if mods is None:
        print("    SKIP: dialog not importable")
        return
    DLG, RT, DT, CT, RN = mods
    host = _host_class(DLG, RT, DT)()
    host._busy = True
    with _SyncRunner(RN) as sr:
        RT.RasImportTabMixin._ras_import(host)
    assert sr.started == [] and host.calls == []


def test_dsswise_import_runs_in_task_and_finishes_on_main_thread():
    mods = _modules()
    if mods is None:
        print("    SKIP: dialog not importable")
        return
    DLG, RT, DT, CT, RN = mods
    host = _host_class(DLG, RT, DT)()
    DW = DT.DW
    seen = {}

    def convert(items, out, name="DSS-WISE Lite", progress=None):
        seen.update(items=items, out=out, name=name)
        progress(30.0, "Converting depth")
        assert host.depth_edit.text() == ""        # nothing of the window touched yet
        return {"paths": {DW.R_DEPTH: out + "/Depth_ft.tif", DW.R_DV: out + "/DxV_ft2s.tif"},
                "lines": ["depth: ok"], "notes": ["No velocity raster"], "warnings": [],
                "depth_only": True, "boundary": None, "report": None, "hazard_zones": [],
                "hcom": []}
    undo = [_patch(DW, "convert", convert), _patch(RN, "release_folder", lambda f: 0)]
    try:
        with _SyncRunner(RN) as sr:
            DT.DssWiseTabMixin._dsw_import(host)
        assert len(sr.started) == 1 and sr.started[0][0].startswith("Importing DSS-WISE")
        assert sr.started[0][1] == ["work", "done"]
        assert seen["items"] == ["item"] and seen["name"] == "Scenario A"
        assert seen["out"].endswith("DSSWISE_Scenario_A"), seen["out"]
        assert ("progress", 30.0) in host.calls
        assert host.depth_edit.text().endswith("Depth_ft.tif")
        assert host.dv_edit.text().endswith("DxV_ft2s.tif") and host.vel_edit.text() == ""
        assert host.model_combo.index == 1                  # depth only -> 1D
        assert "Imported to" in host.dsw_result_text.text
        assert host.busy_trace == [True, False] and not host._busy
        assert ("bar", "Importing DSS-WISE Lite results…", False) in host.calls
    finally:
        for u in undo:
            u()


def test_dsswise_import_cancel_and_error():
    mods = _modules()
    if mods is None:
        print("    SKIP: dialog not importable")
        return
    DLG, RT, DT, CT, RN = mods
    DW = DT.DW
    boxes = []

    def cancelled(*a, **k):
        raise RN.CancelledByUser()

    def boom(*a, **k):
        raise RuntimeError("cannot open depth")
    undo = [_patch(RN, "release_folder", lambda f: 0),
            _patch(DT.QMessageBox, "critical", staticmethod(lambda *a, **k: boxes.append(a[2])))]
    try:
        host = _host_class(DLG, RT, DT)()
        u = _patch(DW, "convert", cancelled)
        try:
            with _SyncRunner(RN):
                DT.DssWiseTabMixin._dsw_import(host)
        finally:
            u()
        assert host.status == [("DSS-WISE import cancelled", False)] and not host._busy
        host = _host_class(DLG, RT, DT)()
        u = _patch(DW, "convert", boom)
        try:
            with _SyncRunner(RN):
                DT.DssWiseTabMixin._dsw_import(host)
        finally:
            u()
        assert boxes == ["cannot open depth"] and "FAILED" in host.status[-1][0]
        assert host.depth_edit.text() == "" and not host._busy
    finally:
        for u in undo:
            u()


def test_check_inputs_runs_in_task_and_fills_panel_in_done():
    mods = _modules()
    if mods is None:
        print("    SKIP: dialog not importable")
        return
    DLG, RT, DT, CT, RN = mods
    from DamSafety_Hazard import checks as CK
    from DamSafety_Hazard import osm_processing as OP
    host = _host_class(DLG, RT, DT)()
    seen = {}

    def check_all(paths, output_folder="", dam_folder="", cfg=None, services=False,
                  progress=None, outline=None):
        seen.update(paths=paths, out=output_folder, dam=dam_folder, cfg=cfg,
                    services=services, outline=outline)
        progress(50.0, "Checking NSI…")
        return [("OK", "depth fine"), ("WARNING", "NSI: not reachable")]
    undo = [_patch(CK, "check_all", check_all),
            _patch(OP, "get_osm_candidates", lambda out, dam: None)]
    try:
        with _SyncRunner(RN) as sr:
            got = DLG.NDDamHazardDialog._on_check_inputs(host)
        assert len(sr.started) == 1 and sr.started[0][0].startswith("Checking inputs")
        assert sr.started[0][1] == ["work", "done"]
        assert seen["services"] is True and seen["paths"]["depth"] == "/d.tif"
        assert seen["outline"] == ""                 # resolved on the main thread, passed in
        assert seen["out"] == "/dams/DWR-1/outputs" and seen["cfg"] == {"one_d": False}
        assert ("progress", 50.0) in host.calls
        assert host.check_panel.results == [("OK", "depth fine"), ("WARNING", "NSI: not reachable")]
        assert got == host.check_panel.results         # the synchronous path returns them
        assert any("CHECK WARNING" in m for m in host.logs)
        assert host.busy_trace == [True, False] and not host._busy
    finally:
        for u in undo:
            u()


def test_check_inputs_outline_found_on_main_thread():
    mods = _modules()
    if mods is None:
        print("    SKIP: dialog not importable")
        return
    DLG, RT, DT, CT, RN = mods
    from DamSafety_Hazard import osm_processing as OP

    class _Lyr(object):
        def source(self):
            return "/dams/DWR-1/outputs/DWR-1_FEMA_Structures.gpkg|layername=x"

        def name(self):
            return "FEMA Structures"
    host = _host_class(DLG, RT, DT)()
    u = _patch(OP, "get_osm_candidates", lambda out, dam: _Lyr())
    try:
        job = DLG.NDDamHazardDialog._check_job(host)
    finally:
        u()
    assert job["outline"] == "DWR-1_FEMA_Structures.gpkg", job
    assert job["own"] == [] and job["paths"]["depth"] == "/d.tif"


def test_check_inputs_cancel_and_error():
    mods = _modules()
    if mods is None:
        print("    SKIP: dialog not importable")
        return
    DLG, RT, DT, CT, RN = mods
    from DamSafety_Hazard import checks as CK
    from DamSafety_Hazard import osm_processing as OP
    boxes = []

    def cancelled(*a, **k):
        raise RN.CancelledByUser()

    def boom(*a, **k):
        raise RuntimeError("disk gone")
    undo = [_patch(OP, "get_osm_candidates", lambda out, dam: None),
            _patch(DLG.QMessageBox, "critical", staticmethod(lambda *a, **k: boxes.append(a[2])))]
    try:
        host = _host_class(DLG, RT, DT)()
        u = _patch(CK, "check_all", cancelled)
        try:
            with _SyncRunner(RN):
                assert DLG.NDDamHazardDialog._on_check_inputs(host) is None
        finally:
            u()
        assert any("cancelled" in m for m in host.logs) and host.check_panel.results is None
        host = _host_class(DLG, RT, DT)()
        u = _patch(CK, "check_all", boom)
        try:
            with _SyncRunner(RN):
                DLG.NDDamHazardDialog._on_check_inputs(host)
        finally:
            u()
        assert boxes and "disk gone" in boxes[0] and "FAILED" in host.status[-1][0]
        assert not host._busy
    finally:
        for u in undo:
            u()


def test_classify_inline_checks_stay_synchronous():
    """_on_run_all's file-only check (services=False) still runs inline - no task."""
    mods = _modules()
    if mods is None:
        print("    SKIP: dialog not importable")
        return
    DLG, RT, DT, CT, RN = mods
    from DamSafety_Hazard import checks as CK
    from DamSafety_Hazard import osm_processing as OP
    host = _host_class(DLG, RT, DT)()
    undo = [_patch(CK, "check_all", lambda *a, **k: [("OK", "fine")]),
            _patch(OP, "get_osm_candidates", lambda out, dam: None)]
    try:
        with _SyncRunner(RN) as sr:
            res = DLG.NDDamHazardDialog._run_checks(host, services=False)
        assert res == [("OK", "fine")] and sr.started == []
    finally:
        for u in undo:
            u()


def test_check_all_takes_resolved_outline():
    mods = _modules()
    if mods is None:
        print("    SKIP: dialog not importable")
        return
    from DamSafety_Hazard import checks as CK
    from DamSafety_Hazard import osm_processing as OP
    scanned = []
    undo = [_patch(CK, "check_rasters", lambda *a, **k: []),
            _patch(OP, "get_osm_candidates", lambda out, dam: scanned.append(1))]
    try:
        out = CK.check_all({"depth": "/d.tif", "vel": "/v.tif", "arr": "/a.tif"}, "/out", "/dam",
                           {"one_d": False}, services=False, outline="X_FEMA_Structures.gpkg")
        assert ("OK", "building outlines: X_FEMA_Structures.gpkg") in out, out
        assert scanned == []                             # the project was not scanned
        out = CK.check_all({"depth": "/d.tif", "vel": "/v.tif", "arr": "/a.tif"}, "/out", "/dam",
                           {"one_d": False, "download_usa_structures": True}, outline="")
        assert any("will be downloaded" in t for _l, t in out), out
        # None (scripts, Processing): found here as before
        CK.check_all({"depth": "/d.tif", "vel": "/v.tif", "arr": "/a.tif"}, "/out", "/dam", {})
        assert scanned == [1]
    finally:
        for u in undo:
            u()


def test_creep_watch_runs_in_task_with_project_points_resolved_first():
    mods = _modules()
    if mods is None:
        print("    SKIP: dialog not importable")
        return
    DLG, RT, DT, CT, RN = mods
    from DamSafety_Hazard import watch as WT
    from DamSafety_Hazard import processing as PR
    host = _host_class(DLG, RT, DT)()
    seen = {}
    boxes = []

    def run_watch(parent, cfg_over=None, progress=None, log=None, classify=None, workers=1,
                  timeout_s=3600, info=None, qgis_process=None, popen=None):
        seen.update(parent=parent, over=cfg_over, workers=workers)
        progress(10.0, "dam 1")
        info["text"] = "sequential in this QGIS session"
        return ([{"Alert": "-", "Dam": "A"}], {"html": parent + "/Hazard_Creep_Watch.html"})
    undo = [_patch(WT, "run_watch", run_watch),
            _patch(PR, "discover_project_layers", lambda: {"NSI": [], "NDGISHUB": []}),
            _patch(RN, "detach_layers", lambda cfg: dict(cfg)),
            _patch(CT.QFileDialog, "getExistingDirectory", staticmethod(lambda *a, **k: "/dams")),
            _patch(CT.QMessageBox, "information", staticmethod(lambda *a, **k: boxes.append(a[2])))]
    try:
        with _SyncRunner(RN) as sr:
            CT.ClassificationTabMixin._run_watch(host)
        assert len(sr.started) == 1 and sr.started[0][0].startswith("Hazard creep watch")
        assert sr.started[0][1] == ["work", "done"]
        assert seen["parent"] == "/dams" and seen["workers"] == 1
        assert seen["over"] == {"point_sources": []}, seen["over"]
        assert ("progress", 10.0) in host.calls
        assert boxes and "Hazard_Creep_Watch.html" in boxes[0]
        assert ("open", "/dams/Hazard_Creep_Watch.html") in host.calls
        assert any("Creep watch ran: sequential" in m for m in host.logs), host.logs
        assert host.busy_trace == [True, False] and not host._busy
        # a second click while busy: nothing starts (the re-entrancy guard)
        host._busy = True
        with _SyncRunner(RN) as sr2:
            CT.ClassificationTabMixin._run_watch(host)
        assert sr2.started == []
    finally:
        for u in undo:
            u()


def test_watch_empty_point_sources_allow_parallel_mode():
    mods = _modules()
    if mods is None:
        print("    SKIP: dialog not importable")
        return
    from DamSafety_Hazard import watch as WT
    assert "point_sources" not in WT._unsupported_settings({"point_sources": []})
    assert "point_sources" in WT._unsupported_settings({"point_sources": [("NSI", object())]})


def test_selftest_runs_in_task_and_finishes_on_main_thread():
    mods = _modules()
    if mods is None:
        print("    SKIP: dialog not importable")
        return
    DLG, RT, DT, CT, RN = mods
    from DamSafety_Hazard import selftest as ST
    host = _host_class(DLG, RT, DT)()
    boxes = []
    order = []

    def run_self_test(progress_cb=None, deferred=False):
        assert deferred is True
        progress_cb(50.0, "Self-test: sampling")
        order.append("work")
        return True, "REP", [lambda: order.append("post")]

    def finish_self_test(res):
        ok, rep, post = res
        for fn in post:
            fn()
        order.append("finish")
        return ok, ["Self-test PASSED: 3 checks", "PASS  a"]
    undo = [_patch(ST, "run_self_test", run_self_test),
            _patch(ST, "finish_self_test", finish_self_test),
            _patch(DLG.QMessageBox, "information", staticmethod(lambda *a, **k: boxes.append(a[2])))]
    try:
        with _SyncRunner(RN) as sr:
            DLG.NDDamHazardDialog._on_run_selftest(host)
        assert len(sr.started) == 1 and sr.started[0][0].startswith("Running self-test")
        assert order == ["work", "post", "finish"], order
        assert ("progress", 50.0) in host.calls
        assert boxes and "Self-test PASSED" in boxes[0]
        assert host.status[-1] == ("Self-test PASSED", True)
        assert host.busy_trace == [True, False] and not host._busy
    finally:
        for u in undo:
            u()


def test_selftest_crash_in_task_is_reported():
    mods = _modules()
    if mods is None:
        print("    SKIP: dialog not importable")
        return
    DLG, RT, DT, CT, RN = mods
    from DamSafety_Hazard import selftest as ST
    host = _host_class(DLG, RT, DT)()
    boxes = []

    def boom(progress_cb=None, deferred=False):
        raise RuntimeError("gdal missing")
    undo = [_patch(ST, "run_self_test", boom),
            _patch(DLG.QMessageBox, "warning", staticmethod(lambda *a, **k: boxes.append(a[2])))]
    try:
        with _SyncRunner(RN):
            DLG.NDDamHazardDialog._on_run_selftest(host)
        assert boxes and "Self-test crashed" in boxes[0] and "gdal missing" in boxes[0]
        assert host.status[-1] == ("Self-test FAILED", False) and not host._busy
    finally:
        for u in undo:
            u()


def test_finish_self_test_runs_post_and_reports():
    mods = _modules()
    if mods is None:
        print("    SKIP: dialog not importable")
        return
    from DamSafety_Hazard import selftest as ST
    rep = ST._Report()
    rep.check("worker check", True)
    ran = []
    ok, lines = ST.finish_self_test((rep.ok, rep, [lambda: ran.append(1),
                                                   lambda: rep.check("layout", True)]))
    assert ok and ran == [1] and lines[0] == "Self-test PASSED: 2 checks", lines
    rep = ST._Report()

    def bad():
        raise RuntimeError("layout failed")
    ok, lines = ST.finish_self_test((rep.ok, rep, [bad]))
    assert not ok and lines[0].startswith("Self-test FAILED") and \
        any("layout failed" in ln for ln in lines), lines


def test_no_modal_bar_or_processevents_left_in_the_moved_jobs():
    """The moved jobs no longer make their own modal bar; _progress_tick keeps its pump for
    the main-thread steps that still use it (_finish_deferred, the inline checks)."""
    here = os.path.dirname(HERE)
    for name in ("rasimport_tab.py", "dsswise_tab.py"):
        src = open(os.path.join(here, name), encoding="utf-8").read()
        assert "_make_progress(" not in src and "processEvents" not in src, name
    src = open(os.path.join(here, "classify_tab.py"), encoding="utf-8").read()
    body = src[src.index("def _run_watch"):src.index("def _make_submittal")]
    assert "_make_progress(" not in body and "_start_background(" in body
    src = open(os.path.join(here, "dialog.py"), encoding="utf-8").read()
    for fn in ("def _on_check_inputs", "def _on_run_selftest"):
        body = src[src.index(fn):]
        body = body[:body.index("\n    def ", 10)]
        assert "_make_progress(" not in body and "_start_background(" in body, fn
    assert "_BUSY_WIDGETS" in src
    busy = src[src.index("_BUSY_WIDGETS = ("):src.index("def _set_busy")]
    for w in ("ras_import_btn", "dsw_import_btn", "check_btn", "watch_btn", "selftest_btn",
              "selftest_action", "ras_use_plan_btn", "watch_workers_spin", "dsw_table"):
        assert '"{}"'.format(w) in busy, w


if __name__ == "__main__":
    n = fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            n += 1
            try:
                fn()
                print("ok    ", name)
            except Exception:
                fails += 1
                print("FAIL  ", name)
                traceback.print_exc()
    print("\n{} tests, {} failed".format(n, fails))
    sys.exit(1 if fails else 0)
