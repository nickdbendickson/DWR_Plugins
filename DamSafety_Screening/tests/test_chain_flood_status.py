"""0.20.4 (install test 2026-11-18): the run-in-order chain (window and tool 15) fails when step 7
failed or skipped dams, instead of saying OK because step 7 still wrote its layer."""
import os, shutil, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
from nd_dam_screening.core import workflow as wf  # noqa: E402
import _gpkg  # noqa: E402

base = tempfile.mkdtemp(prefix="chainflood_")


def sim_layer(work, statuses):
    p = wf.path(work, "7", "OUT")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    rows = [(("Point", (float(i), 0.0)), {"dam_id": "DWR-{}".format(i), "sim_status": st})
            for i, st in enumerate(statuses)]
    _gpkg.write(p, {"sims": ("Point", [("dam_id", "TEXT"), ("sim_status", "TEXT")], rows)})


try:
    w = os.path.join(base, "w")
    os.makedirs(w)
    assert wf.sim_status_counts(w) is None and wf.flood_problem(w) == ""      # no layer: no opinion
    sim_layer(w, ["ok", "cached", "triage"])
    assert wf.flood_problem(w) == "", wf.flood_problem(w)
    sim_layer(w, ["ok", "FAILED", "triage"])
    msg = wf.flood_problem(w)
    assert "1 of 3" in msg and "DWR-1" in msg, msg
    sim_layer(w, ["triage", "noflood"])
    assert "flooded none" in wf.flood_problem(w)
    with open(wf.path(w, "7", "OUT"), "w") as fh:                           # not a GeoPackage
        fh.write("x")
    assert wf.sim_status_counts(w) is None and wf.flood_problem(w) == ""

    # the chain: every step "runs" by writing its output; step 7 writes a layer with a failed dam
    statuses = {"v": ["ok", "FAILED"]}

    def start(step, aid, params, on_done):
        if step == "7":
            sim_layer(w2, statuses["v"])
        else:
            for k, rel in wf.LAYOUT[step].items():
                if k != wf.DONE_MARK.get(step):
                    continue
                p = os.path.join(w2, rel)
                if os.path.splitext(p)[1]:
                    os.makedirs(os.path.dirname(p), exist_ok=True)
                    open(p, "w").write("x")
                else:
                    os.makedirs(p, exist_ok=True)
                    open(os.path.join(p, "f.txt"), "w").write("x")
            if step == "10":
                p = os.path.join(w2, wf.REPORT_INDEX)
                os.makedirs(os.path.dirname(p), exist_ok=True)
                open(p, "w").write("x")
        on_done(True, "")

    own = {"dwr": "dwr_layer", "waterbodies": "wb_layer", "dam_lines": "dl_layer", "structures": "bld_layer"}
    w2 = os.path.join(base, "w2")
    os.makedirs(w2)
    ch = wf.Chain(w2, lambda: own, (), start)
    ch.begin()
    assert not ch.ok and "8" in ch.ran and "1 of 2" in ch.result and "DWR-1" in ch.result, (ch.ran, ch.result)
    # nothing left to run, but the old step 7 layer still has a failure: not OK either
    ch = wf.Chain(w2, lambda: own, (), start)
    ch.begin()
    assert not ch.ok and ch.ran == [] and "failed or was skipped" in ch.result, ch.result

    # every dam left out: the chain stops right after step 7
    w2 = os.path.join(base, "w3")
    os.makedirs(w2)
    statuses["v"] = ["triage", "noflood"]
    ch = wf.Chain(w2, lambda: own, (), start)
    ch.begin()
    assert not ch.ok and ch.ran[-1] == "7" and "flooded none" in ch.result, (ch.ran, ch.result)
    print("OK")
finally:
    shutil.rmtree(base, ignore_errors=True)
