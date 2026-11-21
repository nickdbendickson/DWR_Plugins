"""0.28.0 (Crest install test 2026-11-21): a step stopped part-way is run again, not counted as done.

A one-dam chain under a 600 s limit was killed in step 7 after the tool had created an empty 7_simulations.gpkg. The
next chain in that work folder planned "8, 10" (step 7's output was there, so it counted as done) and step 8 failed:
"Could not load source layer for SIM: 7_simulations.gpkg not found". The chain now marks a step it starts and clears
the mark only when the step ends well; a marked step is not done, and its half-written output file is removed before
it runs again. Pure Python (the chain is driven with a synchronous start, as in test_chain_flood_status.py)."""
import os, shutil, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
from nd_dam_screening.core import workflow as wf  # noqa: E402
import _gpkg  # noqa: E402

base = tempfile.mkdtemp(prefix="chainstop_")
OWN = {"dwr": "dwr_layer", "waterbodies": "wb_layer", "dam_lines": "dl_layer", "structures": "bld_layer"}


def write_output(work, step):
    if step == "7":
        p = wf.path(work, "7", "OUT")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        _gpkg.write(p, {"sims": ("Point", [("dam_id", "TEXT"), ("sim_status", "TEXT")],
                                 [(("Point", (0.0, 0.0)), {"dam_id": "DWR-2801", "sim_status": "ok"})])})
        return
    for k, rel in wf.LAYOUT[step].items():
        if k != wf.DONE_MARK.get(step):
            continue
        p = os.path.join(work, rel)
        if os.path.splitext(p)[1]:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            open(p, "w").write("x")
        else:
            os.makedirs(p, exist_ok=True)
            open(os.path.join(p, "f.txt"), "w").write("x")
    if step == "10":
        p = os.path.join(work, wf.REPORT_INDEX)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        open(p, "w").write("x")


try:
    w = os.path.join(base, "w")
    os.makedirs(w)
    started = []

    # run 1: step 7 creates its (empty) output file and is then killed: on_done is never called
    def killed_in_7(step, aid, params, on_done):
        started.append(step)
        if step == "7":
            open(wf.path(w, "7", "OUT"), "wb").write(b"\0" * 65536)      # the sink QGIS created, nothing in it
            return                                                         # qgis_process killed here
        write_output(w, step)
        on_done(True, "")

    wf.Chain(w, lambda: OWN, (), killed_in_7).begin()
    assert started[-1] == "7", started
    assert os.path.isfile(wf.path(w, "7", "OUT")), "the half-written file is there, as after the real kill"
    assert wf.was_interrupted(w, "7")
    assert not wf.is_done(w, "7"), "an interrupted step is not done although its output file exists"
    assert wf.is_done(w, "5") and not wf.was_interrupted(w, "5"), "the steps that ended well stay done"
    plan = wf.chain_plan(w, OWN, ())
    assert "7" in plan and plan.index("7") < plan.index("8"), plan
    assert "1" not in plan and "5" not in plan, plan

    # run 2: step 7 runs again; the half-written file is gone before the tool starts
    seen = {}
    lines = []

    def finishes(step, aid, params, on_done):
        if step == "7":
            seen["file_before_7"] = os.path.exists(wf.path(w, "7", "OUT"))
        write_output(w, step)
        on_done(True, "")

    ch = wf.Chain(w, lambda: OWN, (), finishes, log=lines.append)
    ch.begin()
    assert ch.ran[0] == "7", ch.ran
    assert seen["file_before_7"] is False, "the half-written 7_simulations.gpkg was removed first"
    assert any("stopped part-way" in ln and "7_simulations.gpkg" in ln for ln in lines), lines
    assert "8" in ch.ran, ch.ran
    assert not wf.was_interrupted(w, "7") and wf.is_done(w, "7")
    assert ch.ok, ch.result

    # a step that FAILS keeps its mark too: what it wrote is not counted as done next time
    w2 = os.path.join(base, "w2")
    os.makedirs(w2)

    def fails_in_3(step, aid, params, on_done):
        write_output(w2, step)
        on_done(step != "3", "boom" if step == "3" else "")

    ch = wf.Chain(w2, lambda: OWN, (), fails_in_3)
    ch.begin()
    assert not ch.ok and wf.was_interrupted(w2, "3") and not wf.is_done(w2, "3"), ch.result
    assert wf.chain_plan(w2, OWN, ())[0] == "3"
    # a folder output is kept (terrain caches, per-dam runs): only an output FILE is removed
    assert wf.remove_interrupted_output(w2, "2") == ""
    print("OK")
finally:
    shutil.rmtree(base, ignore_errors=True)
