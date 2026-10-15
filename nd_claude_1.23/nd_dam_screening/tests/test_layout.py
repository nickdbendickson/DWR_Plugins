"""damsuite/layout.py: the standard work-folder layout (idempotent, never overwrites, refusals)
and that pre-created empty folders do not mark any Screening step as done."""
import json, os, shutil, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
from nd_dam_screening.damsuite import layout as LY  # noqa: E402
from nd_dam_screening.core import workflow as wf  # noqa: E402

base = tempfile.mkdtemp(prefix="layout_")
try:
    # one table: the window's LAYOUT is damsuite's STEP_OUTPUTS (a copy, not the same object)
    assert wf.LAYOUT == LY.STEP_OUTPUTS and wf.LAYOUT is not LY.STEP_OUTPUTS
    assert LY.step_folders() == ["2_terrain", "3_stage_storage", "5_hydrographs", "6_triage_envelopes",
                                 "7_simulations", "9_sensitivity", "10_reports", "11_benchmark"]
    assert "16_terrain" in LY.step_folders(optional=True)

    work = os.path.join(base, "County")
    made = LY.ensure_layout(work)
    for rel in LY.step_folders() + ["_downloads", "_logs", "Dams", "README.txt", "_layout.json"]:
        assert os.path.exists(os.path.join(work, rel)), rel
    assert os.listdir(os.path.join(work, "Dams")) == []          # nothing inside Dams
    assert json.load(open(os.path.join(work, "_layout.json")))["dams_folder"] == "Dams"
    assert len(made) == 1 + 8 + 2 + 1 + 2                         # root + steps + support + Dams + 2 files

    # no step is done in a freshly laid-out folder (empty folders are "not run")
    for step in wf.KEYS:
        assert not wf.is_done(work, step), step

    # idempotent and never overwrites
    with open(os.path.join(work, "README.txt"), "w") as f:
        f.write("mine")
    with open(os.path.join(work, "2_terrain", "tile.tif"), "w") as f:
        f.write("x")
    os.rmdir(os.path.join(work, "_logs"))
    with open(os.path.join(work, "_logs"), "w") as f:              # a FILE named like a folder
        f.write("keep")
    assert LY.ensure_layout(work) == []
    assert open(os.path.join(work, "README.txt")).read() == "mine"
    assert open(os.path.join(work, "_logs")).read() == "keep"
    assert os.listdir(os.path.join(work, "2_terrain")) == ["tile.tif"]
    assert wf.is_done(work, "2")                                  # a real result still counts

    # a custom Dams folder outside the work folder: only that folder is created
    other = os.path.join(base, "elsewhere", "dams")
    LY.ensure_layout(os.path.join(base, "W2"), other)
    assert os.path.isdir(other) and os.listdir(other) == []
    assert not os.path.exists(os.path.join(base, "W2", "Dams"))
    assert LY.ensure_dams_folder(os.path.join(base, "D3")) == [os.path.join(base, "D3")]
    assert LY.ensure_dams_folder(os.path.join(base, "D3")) == []

    # refusals
    def refused(fn, *a):
        try:
            fn(*a)
        except LY.LayoutRefused:
            return True
        return False
    pack = os.path.join(base, "ND_Dam_Suite_Pack", "x")
    assert refused(LY.ensure_layout, pack) and not os.path.exists(os.path.join(base, "ND_Dam_Suite_Pack"))
    assert refused(LY.ensure_dams_folder, pack)
    assert refused(LY.ensure_layout, os.path.join(base, "W4"), os.path.join(base, "ND_Dam_Suite_Pack"))
    assert not os.path.exists(os.path.join(base, "W4"))
    rep = os.path.join(base, "repro")
    os.makedirs(rep)
    open(os.path.join(rep, LY.REPRO_MARKER), "w").close()
    assert refused(LY.ensure_layout, rep) and refused(LY.ensure_layout, os.path.join(rep, "sub"))
    assert os.listdir(rep) == [LY.REPRO_MARKER]
    dam = os.path.join(base, "Dams", "DWR-1")
    os.makedirs(dam)
    open(os.path.join(dam, "dam.json"), "w").close()
    assert refused(LY.ensure_layout, dam) and os.listdir(dam) == ["dam.json"]
    assert refused(LY.ensure_layout, "") and refused(LY.ensure_layout, os.path.expanduser("~"))
    print("ok  test_layout")
finally:
    shutil.rmtree(base, ignore_errors=True)
