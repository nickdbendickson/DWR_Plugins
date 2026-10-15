"""QA maps (core/qa_maps.py): qa_inflow_map.png and qa_depth_map.png after every run, behind
QA_MAPS (off / inflow / inflow+depth, default inflow+depth); never fail the run; recorded in
run_manifest.json."""
import json
import os
import shutil
import tempfile

import numpy as np

from nd_breach_runner.core import qa_maps as QA
from nd_breach_runner.core.grid import Grid
from nd_breach_runner.tests.registry import test as register, model_test

PNG = b"\x89PNG\r\n\x1a\n"


def _valley(n=60, cell=10.0):
    """A dam across a valley at x = 0: reservoir floor west, toe and channel east."""
    x = -300 + (np.arange(n) + 0.5) * cell
    y = 300 - (np.arange(n) + 0.5) * cell
    X, Y = np.meshgrid(x, y)
    z = 100.0 + 0.002 * -X + 0.01 * np.abs(Y)
    z = np.where(np.abs(X) < 15, 110.0, z)                    # embankment, crest 110
    z = np.where(X > 15, z - 4.0, z)                           # downstream channel lower
    return Grid(z, -300.0, 300.0, cell)


def _have_mpl():
    try:
        import matplotlib  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


@register
def qa_maps_mode_names_and_default():
    assert QA.DEFAULT_QA_MAPS == "inflow+depth" == QA.normalize(None)
    assert QA.normalize(0) == "off" and QA.normalize(1) == "inflow" and QA.normalize(2) == "inflow+depth"
    assert QA.normalize("Inflow map only") == "inflow" and QA.normalize("OFF") == "off"
    assert QA.normalize(False) == "off" and QA.normalize(True) == "inflow+depth"
    for bad in (7, "sideways"):
        try:
            QA.normalize(bad)
        except ValueError:
            pass
        else:
            raise AssertionError(bad)


@register
def qa_maps_write_both_pngs_on_a_synthetic_grid():
    if not _have_mpl():
        print("   (skipped: no matplotlib; the Qt fallback is used in QGIS)")
        return
    g = _valley()
    d = tempfile.mkdtemp(prefix="qa_")
    try:
        depth = np.where(g.x_centers()[None, :] > 15, 1.2, 0.0) * np.ones((g.ny, 1))
        trace = [(25.0, 0.0), (100.0, 5.0), (250.0, 10.0)]
        info, problems = QA.write_qa_maps("inflow+depth", d, "T1", g, (0.0, 0.0), (25.0, 0.0),
                                          110.0, radius_m=90.0, trace=trace,
                                          crest_line=[(0.0, -200.0), (0.0, 200.0)],
                                          model_grid=g, depth_m=depth)
        assert not problems, problems
        for k, name in (("inflow_map", QA.INFLOW_PNG), ("depth_map", QA.DEPTH_PNG)):
            assert info[k] == os.path.join(d, name) and open(info[k], "rb").read(8) == PNG
        assert info["mode"] == "inflow+depth" and info["seconds"] >= 0
        # the title: dam id and the inflow elevation against the top of dam
        z = g.masked()[g.rc(25.0, 0.0)]
        t = QA.inflow_title("T1", z, 110.0)
        assert t.startswith("T1: inflow at") and "below the top of dam" in t
        # inflow only
        os.remove(info["depth_map"])
        info, problems = QA.write_qa_maps("inflow", d, "T1", g, (0.0, 0.0), (25.0, 0.0), 110.0,
                                          model_grid=g, depth_m=depth)
        assert "depth_map" not in info and not os.path.exists(os.path.join(d, QA.DEPTH_PNG))
    finally:
        shutil.rmtree(d)


@register
def qa_maps_off_writes_nothing_and_errors_never_raise():
    g = _valley()
    d = tempfile.mkdtemp(prefix="qa_")
    try:
        info, problems = QA.write_qa_maps("off", d, "T1", g, (0, 0), (25, 0), 110.0)
        assert info == {"mode": "off"} and not problems and os.listdir(d) == []
        # a depth array that does not fit and a broken grid: problems, no exception
        info, problems = QA.write_qa_maps("inflow+depth", d, "T1", object(), (0, 0), (25, 0),
                                          110.0, model_grid=g, depth_m=np.zeros((3, 3)))
        assert problems and all("not written" in p for p in problems), problems
        info, problems = QA.write_qa_maps("sideways", d, "T1", g, (0, 0), (25, 0), 110.0)
        assert problems and "QA_MAPS" in problems[0]
    finally:
        shutil.rmtree(d)


@register
def qa_maps_hook_in_run_dam_never_fails_and_records_the_paths():
    from nd_breach_runner.core import workflow
    g = _valley()
    d = tempfile.mkdtemp(prefix="qa_")
    try:
        class Dam:
            dam_id = "T1"
            crest_line_xy = None
        st = workflow.RunSettings()
        assert st.qa_maps == "inflow+depth"
        report, warnings, logs = {}, [], []
        fin = {"depth": np.ones(g.data.shape)}
        workflow._qa_maps(report, st, Dam(), (0.0, 0.0), (25.0, 0.0), 110.0,
                          {"breach_xy": (0.0, 0.0), "local": g, "radius_m": 90.0},
                          [(25.0, 0.0), (200.0, 0.0)], g, fin, d, warnings, logs.append)
        if _have_mpl():
            assert os.path.isfile(report["qa_maps"]["inflow_map"]), (report, warnings)
            assert os.path.isfile(report["qa_maps"]["depth_map"])
        extra = workflow._manifest_extra(dict(report, settings={}), {"final_model": "lisflood"}, [])
        assert extra["qa_maps"] == report["qa_maps"]
        # broken inputs: a warning, never an exception
        report, warnings = {}, []
        workflow._qa_maps(report, st, Dam(), (0.0, 0.0), None, None, {}, None, None, None, d,
                          warnings, logs.append)
        assert warnings
        st.qa_maps = "off"
        report, warnings = {}, []
        workflow._qa_maps(report, st, Dam(), (0.0, 0.0), (25.0, 0.0), 110.0, {}, None, g, fin,
                          os.path.join(d, "off"), warnings, logs.append)
        assert report["qa_maps"] == {"mode": "off"} and not os.path.exists(os.path.join(d, "off"))
    finally:
        shutil.rmtree(d)


@register
def qa_maps_setting_reaches_the_tools_and_cli():
    from nd_breach_runner.scripts import ndbreach_cli as cli
    assert cli.MODEL_KEYS["qa_maps"][0] == "qa_maps"
    assert cli.TEMPLATE["model"]["qa_maps"] == "inflow+depth"


@model_test
def qa_maps_are_written_by_a_real_synthetic_run():
    """A LISFLOOD-FP run of the synthetic valley leaves both PNGs in the dam folder and lists
    them in run_manifest.json."""
    if not os.environ.get("NDBREACH_LISFLOOD") or not _have_mpl():
        print("   (skipped: NDBREACH_LISFLOOD or matplotlib missing)")
        return
    from nd_breach_runner.core import workflow, breach as br
    from nd_breach_runner.tests.synthetic import SyntheticTerrain, npy_writer
    d = tempfile.mkdtemp(prefix="qa_run_")
    try:
        dam = br.DamData(dam_id="T1", dam_height_ft=25.0, storage_top_acft=500.0,
                         top_of_dam_ft=1850.0, surface_area_top_ac=60.0)
        st = workflow.RunSettings(cell_size_m=20.0, initial_length_mi=1.0, max_iterations=1,
                                  lisflood_exe=os.environ["NDBREACH_LISFLOOD"], threads=2)
        folder = os.path.join(d, "T1")
        r = workflow.run_dam(dam, (0.0, 0.0), st, SyntheticTerrain(), npy_writer, folder)
        for name in (QA.INFLOW_PNG, QA.DEPTH_PNG):
            assert open(os.path.join(folder, name), "rb").read(8) == PNG, name
        man = json.load(open(r.manifest))
        qa = man.get("extra", man).get("qa_maps") or {}
        assert qa.get("inflow_map", "").endswith(QA.INFLOW_PNG), man.keys()
        print("   qa maps in {:.2f} s".format(qa.get("seconds", -1)))
    finally:
        shutil.rmtree(d)
