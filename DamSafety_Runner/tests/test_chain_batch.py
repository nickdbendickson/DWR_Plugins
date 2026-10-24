"""3.10.67: ndbreach:chainbatch - a list of dams through Screening, Runner, HEC-RAS 2D and
Hazard, dam by dam. Mocks only: no model runs, no HEC-RAS, no Wine."""
import csv
import json
import os
import shutil
import tempfile

from nd_breach_runner.core import chain as CH
from nd_breach_runner.core import workflow
from nd_breach_runner.tests.registry import test

K = lambda key: (CH.PLUGIN, CH.TOOL, key)        # noqa: E731


def _tmp():
    return tempfile.mkdtemp(prefix="chain_")


def _cv(**kw):
    vals = {K(k): v for k, v in kw.items()}
    cv, probs = CH.chain_values(vals)
    assert not probs, probs
    return vals, cv


@test
def chain_names_match_runner():
    from nd_breach_runner import algorithms as A
    from nd_breach_runner import algorithms_chain as AC
    for s in ("DWR-5289", "ND 00123", "a/b:c", "", "Ünï"):
        assert CH.safe_name(s) == A._safe_name(s), s
    for k in ("depth", "velocity", "arrival"):
        assert CH.FINAL_RASTERS[k] == workflow.FINAL_NAMES[k]
    assert AC.screening_safe_name(" x/y.") == "x_y"
    assert any(c().name() == "chainbatch" for c in A.ALGORITHMS)


@test
def chain_ids_and_overrides():
    assert CH.parse_ids("a, b;c\nd  a") == ["a", "b", "c", "d"]
    d = _tmp()
    try:
        p = os.path.join(d, "ids.csv")
        with open(p, "w") as f:
            f.write("name,NID\nx,ND001\ny,ND002\n")
        assert CH.read_ids_csv(p) == ["ND001", "ND002"]
        o = os.path.join(d, "ov.csv")
        with open(o, "w") as f:
            f.write("dam_id,HEC_MODE,ndbreach.batch.METHOD,bogus col\nND001,2,,x\nND002,,3,\n")
        ov, probs = CH.read_overrides(o)
        assert ov["ND001"][K("HEC_MODE")] in (2, "2"), ov
        assert ("ndbreach", "batch", "METHOD") not in ov["ND001"]          # blank = default
        assert ov["ND002"][("ndbreach", "batch", "METHOD")] in (3, "3")
        assert probs and "bogus" in " ".join(probs)
        vals, _cv0 = _cv(HEC_MODE=0)
        v, applied = CH.resolve_for_dam(vals, ov["ND001"])
        cv, _ = CH.chain_values(v)
        assert CH.hec_mode(cv) == "skip" and applied
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test
def chain_plan_by_hec_mode():
    lin = {"available": True, "platform": "linux"}
    no = {"available": False, "platform": "linux", "reason": "no Wine"}
    win = {"available": False, "platform": "win32", "windows_launcher": True}
    _v, cv = _cv(HEC_MODE=0)
    assert CH.effective_hec_mode(cv, lin)[0] == "run_linux"
    m, note = CH.effective_hec_mode(cv, no)
    assert m == "wait" and note
    assert CH.effective_hec_mode(cv, win)[0] == "run_windows"
    assert CH.plan_stages(cv, "run_linux") == ["screening", "runner", "hec_run",
                                                "hazard_import", "classify"]
    assert CH.plan_stages(cv, "wait") == list(CH.STAGES)
    _v, cv = _cv(HEC_MODE=2, SCREENING=False)
    m, _ = CH.effective_hec_mode(cv, lin)
    assert m == "skip" and CH.plan_stages(cv, m) == ["runner", "classify"]
    _v, cv = _cv(STOP_AFTER=CH.STOP_STAGES.index("runner"))
    assert CH.plan_stages(cv, "wait")[-1] == "runner"


@test
def chain_wait_for_plan():
    d = _tmp()
    try:
        t = [0.0]
        p = os.path.join(d, "Dam.p01.hdf")

        def sleep(s):
            t[0] += s
            if t[0] >= 60 and not os.path.exists(p):
                with open(p, "wb") as f:
                    f.write(b"x" * 10)
        got = CH.wait_for_plan([d], 5, since=0, poll_s=30, sleep=sleep, clock=lambda: t[0],
                               has_results=lambda q: True)
        assert got == p, got
        open(os.path.join(d, "Other.p02.hdf.tmp"), "w").close()
        assert CH.wait_for_plan([d], 0, since=1e12, has_results=lambda q: True) is None
        assert CH.wait_for_plan([d], 0, since=0, has_results=lambda q: False) is None
    finally:
        shutil.rmtree(d, ignore_errors=True)


class FakeOps(object):
    def __init__(self, fail=None):
        self.calls, self.fail = [], dict(fail or {})

    def run(self, stage, dam):
        self.calls.append((dam.dam_id, stage))
        if self.fail.get(dam.dam_id) == stage:
            raise CH.ChainError("boom at " + stage)
        if self.fail.get(dam.dam_id) == "wait" and stage == "hec_run":
            raise CH.Waiting("waiting for HEC-RAS: run it")
        f = os.path.join(dam.folder, stage + ".out")
        os.makedirs(dam.folder, exist_ok=True)
        open(f, "w").close()
        key = CH.REQUIRED_OUTPUT[stage]
        out = {key: f}
        if stage == "classify":
            out.update(nd_class="High", tm11_class="Significant", project=f)
        return out


def _dams(root, ids, mode="wait", **kw):
    vals, cv = _cv(OUT_FOLDER=root, **kw)
    return [CH.Dam(i, os.path.join(root, CH.safe_name(i)), vals, cv, mode=mode) for i in ids]


@test
def chain_batch_keeps_going_and_resumes():
    root = _tmp()
    try:
        ops = FakeOps(fail={"B": "runner", "C": "wait"})
        rows, sp, mp = CH.run_batch(_dams(root, ["A", "B", "C"]), ops, root,
                                    versions={"ndbreach": "x"})
        st = {r["Dam"]: r["Status"] for r in rows}
        assert st == {"A": "done", "B": "failed", "C": "waiting for HEC-RAS"}, st
        b = [r for r in rows if r["Dam"] == "B"][0]
        assert b["Failed_Stage"] == "runner" and "boom" in b["Message"]
        assert rows[0]["ND_Class"] == "High" and rows[0]["TM11_Class"] == "Significant"
        with open(sp, newline="") as f:
            assert len(list(csv.DictReader(f))) == 3
        man = json.load(open(mp))
        assert man["counts"]["done"] == 1 and len(man["dams"]) == 3
        used = json.load(open(os.path.join(root, "A", CH.CHAIN_DIR, CH.USED_NAME)))
        assert used["versions"] == {"ndbreach": "x"} and used["dam"] == "A"
        # second run: A kept entirely, C resumes at the HEC-RAS run
        ops2 = FakeOps()
        rows2, _, _ = CH.run_batch(_dams(root, ["A", "B", "C"]), ops2, root)
        assert not [c for c in ops2.calls if c[0] == "A"], ops2.calls
        assert [s for d, s in ops2.calls if d == "C"] == ["hec_run", "hazard_import",
                                                          "classify"], ops2.calls
        assert {r["Status"] for r in rows2} == {"done"}
        # a changed setting reruns from the stage it affects
        ops3 = FakeOps()
        CH.run_batch(_dams(root, ["A"], HAZARD_PROFILE="other.json"), ops3, root)
        assert ops3.calls and ops3.calls[0][1] != "screening", ops3.calls
        # keep going off: stops at the first failure
        ops4 = FakeOps(fail={"X": "screening"})
        rows4, _, _ = CH.run_batch(_dams(root, ["X", "Y"]), ops4, root, keep_going=False,
                                   skip_done=False)
        assert [r["Dam"] for r in rows4] == ["X"]
    finally:
        shutil.rmtree(root, ignore_errors=True)


@test
def chain_skip_hec_and_stop_after():
    root = _tmp()
    try:
        ops = FakeOps()
        rows, _, _ = CH.run_batch(_dams(root, ["S"], mode="skip"), ops, root)
        assert [s for _d, s in ops.calls] == ["screening", "runner", "classify"]
        assert rows[0]["Stage_hec_build"] == "not in plan" and rows[0]["Status"] == "done"
        ops = FakeOps()
        rows, _, _ = CH.run_batch(_dams(root, ["T"], STOP_AFTER=CH.STOP_STAGES.index(
            "runner")), ops, root)
        assert rows[0]["Status"].startswith("stopped after"), rows[0]
    finally:
        shutil.rmtree(root, ignore_errors=True)


@test
def chain_preflight():
    root = _tmp()
    try:
        dem = os.path.join(root, "dem.tif")
        open(dem, "w").close()
        vals, _ = _cv(OUT_FOLDER=root, HEC_MODE=1, SCREENING=False, DEM=dem)
        env = {"algs": {"ndbreach:batch", "ndbreach:hecrasexport", "ndhazard:hecrasplan"},
               "engine": {"available": False, "platform": "linux"}}
        out = CH.preflight(["A"], vals, env, missing_ids=["Z"])
        errs = [t for lv, _d, t in out if lv == CH.ERROR]
        warns = [t for lv, _d, t in out if lv == CH.WARNING]
        assert any("ndhazard:classify" in t for t in errs), out
        assert any("Z" in t for t in out for t in [t[2]]), out
        assert any("savedamstate" in t for t in warns + errs), out
        env["algs"] |= {"ndhazard:classify", "ndhazard:savedamstate"}
        out = CH.preflight(["A"], vals, env)
        assert not [t for t in out if t[0] == CH.ERROR], out
        assert not [t for t in CH.preflight([], vals, env) if "No dams" in t[2]] == []
    finally:
        shutil.rmtree(root, ignore_errors=True)


@test
def chain_review_overrides_and_keys():
    """Review fixes: 3-part keys case-normalised (#7); override cells kept raw and dams keyed by
    the canonical ID (#17/#18); wait_for_plan proves stability for an unverifiable result (#6/#14)."""
    # #7: a stage tool's 3-part key is normalised to (plugin.lower, tool.lower, KEY.upper)
    assert CH.key_of_column("ndbreach.batch.method") == ("ndbreach", "batch", "METHOD")
    assert CH.key_of_column("NDBREACH.BATCH.method") == ("ndbreach", "batch", "METHOD")
    assert CH.key_of_column("ndhazard.classify.foo") == ("ndhazard", "classify", "FOO")
    # #18 canonical IDs: a layer feature, an IDs list and the CSV all match through canon_id
    assert CH.canon_id("123.0") == "123" and CH.canon_id("00123") == "123"
    assert CH.canon_id("NULL") == "" and CH.canon_id(" ND001 ") == "ND001"
    d = _tmp()
    try:
        o = os.path.join(d, "ov.csv")
        with open(o, "w") as f:
            f.write("dam_id,ndbreach.batch.METHOD\n123.0,00123\n")
        ov, probs = CH.read_overrides(o)
        assert "123" in ov and "123.0" not in ov, ov                 # #18 keyed by canonical id
        assert ov["123"][("ndbreach", "batch", "METHOD")] == "00123", ov   # #17 raw string kept
        # #6/#14: an unverifiable (h5py None) result accepted only once its size is stable
        p = os.path.join(d, "M.p01.hdf")
        with open(p, "wb") as f:
            f.write(b"x" * 10)
        t = [0.0]
        got = CH.wait_for_plan([d], 0, since=0, poll_s=1, sleep=lambda s: t.__setitem__(0, t[0] + s),
                               clock=lambda: t[0], has_results=lambda q: None)
        assert got == p, got                                         # stable -> accepted
        # a still-growing HDF is never accepted (truncated-read guard)
        sz = [10]

        def grow(s):
            t[0] += s
            sz[0] += 10
            with open(p, "wb") as f:
                f.write(b"x" * sz[0])
        t[0] = 0.0
        got = CH.wait_for_plan([d], 0, since=0, poll_s=1, sleep=grow, clock=lambda: t[0],
                               has_results=lambda q: None)
        assert got is None, got
        # a verified result with minutes<=0 is still taken at once
        assert CH.wait_for_plan([d], 0, since=0, has_results=lambda q: True) == p
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test
def chain_review_batch_and_preflight():
    """Review fixes: a summary write failure does not abort the batch (#1); a folder collision
    is a preflight ERROR (#15) and a foreign saved state is dropped (#15 defense); a per-dam
    override that needs an uninstalled stage is caught in preflight (#12)."""
    # #1: write_summary raising (Summary.csv open in Excel) must not stop the remaining dams
    root = _tmp()
    try:
        orig = CH.write_summary
        CH.write_summary = lambda path, rows: (_ for _ in ()).throw(
            PermissionError("Summary.csv is open"))
        logs = []
        try:
            rows, _sp, _mp = CH.run_batch(_dams(root, ["A", "B"], mode="skip"), FakeOps(), root,
                                          log=logs.append)
        finally:
            CH.write_summary = orig
        assert {r["Dam"] for r in rows} == {"A", "B"} and all(r["Status"] == "done" for r in rows)
        assert any("could not write" in m for m in logs), logs
    finally:
        shutil.rmtree(root, ignore_errors=True)
    # #15 defense: a saved state stamped with another dam is not reused for this dam
    root = _tmp()
    try:
        folder = os.path.join(root, "X")
        os.makedirs(CH.chain_dir(folder))
        vals, cv = _cv(OUT_FOLDER=root, SCREENING=False)
        dam = CH.Dam("X", folder, vals, cv, mode="skip")
        fp = CH.fingerprint(CH.stage_inputs("runner", vals, cv))
        dep = os.path.join(folder, "depth.tif")
        open(dep, "w").close()
        CH.save_json(os.path.join(CH.chain_dir(folder), CH.STATE_NAME),
                     {"dam": "OTHER", "stages": {"runner": {"stage": "runner", "status": "done",
                      "fingerprint": fp, "outputs": {"depth": dep}}}})
        ops = FakeOps()
        CH.run_dam(dam, ops, skip_done=True)
        assert ("X", "runner") in ops.calls, ops.calls              # not kept from OTHER's state
    finally:
        shutil.rmtree(root, ignore_errors=True)
    # #15 collision + #12 per-dam override validation
    root = _tmp()
    try:
        dem = os.path.join(root, "dem.tif")
        open(dem, "w").close()
        vals, _ = _cv(OUT_FOLDER=root, HEC_MODE=2, SCREENING=False, DEM=dem)   # global: skip HEC
        env = {"algs": {"ndbreach:batch", "ndhazard:classify", "ndhazard:savedamstate"},
               "engine": {"available": False, "platform": "linux"}}
        out = CH.preflight(["Smith/Dam", "Smith:Dam"], vals, env)             # both -> "Smith_Dam"
        assert any("share the output folder" in t for lv, _d, t in out if lv == CH.ERROR), out
        ov = {"A": {(CH.PLUGIN, CH.TOOL, "HEC_MODE"): "1"}}                   # this dam -> wait
        out = CH.preflight(["A"], vals, env, overrides=ov)
        errs = [t for lv, dd, t in out if lv == CH.ERROR and dd == "A"]
        assert any("hecrasexport" in t for t in errs) and any("hecrasplan" in t for t in errs), out
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------- QGIS ops ---
class _FakeAlg(object):
    pass


def _ops(results, calls):
    from nd_breach_runner import algorithms_chain as AC

    def run(aid, params):
        calls.append((aid, dict(params)))
        r = results.get(aid)
        return r(params) if callable(r) else (r or {})
    ops = AC.QgisOps(None, None, "", algs=None, run=run, lookup=lambda a: _FakeAlg())
    ops.tool_params = lambda dam, stage, fixed: (CH.ALGS[stage], dict(
        (k, v) for k, v in fixed.items() if v is not None))
    return ops


@test
def chain_qgis_ops_runner_and_classify():
    root = _tmp()
    try:
        dam = _dams(root, ["ND 1"], mode="skip")[0]
        dam.id_field = "NID"
        os.makedirs(dam.folder)
        for k in ("depth", "velocity"):
            open(os.path.join(dam.folder, CH.FINAL_RASTERS[k]), "w").close()
        summ = os.path.join(root, "Batch_Breach_Runs.csv")
        with open(summ, "w", newline="") as f:
            w = csv.DictWriter(f, ["Dam", "Status", "Final_Model", "Message"])
            w.writeheader()
            w.writerow({"Dam": "ND 1", "Status": "done", "Final_Model": "ANUGA"})
        calls = []
        ops = _ops({"ndbreach:batch": {"SUMMARY": summ}}, calls)
        import nd_breach_runner.algorithms_chain as AC
        orig, AC.write_point = AC.write_point, lambda feat, p: p
        try:
            out = ops.run("runner", dam)
        finally:
            AC.write_point = orig
        assert out["depth"].endswith(CH.FINAL_RASTERS["depth"]) and out["final_model"] == "ANUGA"
        p = calls[0][1]
        assert p["DAMS_FOLDER"] == root and p["SKIP_DONE"] is False and p["CLASSIFY"] is False
        dam.outputs["runner"] = out
        sumtxt = os.path.join(dam.folder, "summary.txt")
        open(sumtxt, "w").close()
        calls[:] = []
        ops = _ops({"ndhazard:classify": {"SUMMARY": sumtxt, "ND_CLASS": "High",
                                          "TM11_CLASS": "Low"},
                    "ndhazard:savedamstate": {"PROJECT": "/x/Hazard_ND_1.qgz"}}, calls)
        out = ops.run("classify", dam)
        cp = calls[0][1]
        assert cp["DEPTH"].endswith("Depth (Max).tif") and "ARRIVAL" not in cp, cp
        assert "MODEL_BOUNDARY" not in cp
        sp = calls[1][1]
        assert calls[1][0] == "ndhazard:savedamstate" and sp["SCENARIO_NAME"] == "Breach (ANUGA)"
        assert out["project"] == "/x/Hazard_ND_1.qgz" and out["nd_class"] == "High"
        # runner: no breach -> a clear failure
        with open(summ, "w", newline="") as f:
            w = csv.DictWriter(f, ["Dam", "Status", "Message"])
            w.writeheader()
            w.writerow({"Dam": "ND 1", "Status": "no breach"})
        ops = _ops({"ndbreach:batch": {"SUMMARY": summ}}, [])
        AC.write_point = lambda feat, p: p
        try:
            ops.run("runner", dam)
            raise AssertionError("no error")
        except CH.ChainError as e:
            assert "no breach" in str(e)
        finally:
            AC.write_point = orig
    finally:
        shutil.rmtree(root, ignore_errors=True)


@test
def chain_qgis_ops_hec_wait_and_import():
    root = _tmp()
    try:
        dam = _dams(root, ["H"], mode="wait", HEC_WAIT_MIN=0)[0]
        pkg = os.path.join(dam.folder, "hecras_model")
        os.makedirs(os.path.join(pkg, "Terrain"))
        open(os.path.join(pkg, "Terrain", "Terrain.tif"), "w").close()
        dam.outputs["hec_build"] = {"package": pkg, "built_at": 0}
        ops = _ops({}, [])
        try:
            ops.run("hec_run", dam)
            raise AssertionError("no Waiting")
        except CH.Waiting as e:
            assert "waiting for HEC-RAS" in str(e)
        plan = os.path.join(pkg, "Dam.p01.hdf")
        with open(plan, "wb") as f:
            f.write(b"x")
        orig = CH.plan_has_results
        CH.plan_has_results = lambda p: True
        try:
            out = ops.run("hec_run", dam)
        finally:
            CH.plan_has_results = orig
        assert out["plan_hdf"] == plan
        dam.outputs["hec_run"] = out
        dep = os.path.join(root, "d.tif")
        open(dep, "w").close()
        calls = []
        ops = _ops({"ndhazard:hecrasplan": {"DEPTH": dep, "BOUNDARY": "/b.gpkg"}}, calls)
        imp = ops.run("hazard_import", dam)
        assert calls[0][1]["TERRAIN"].endswith("Terrain.tif") and imp["boundary"] == "/b.gpkg"
        # Linux run: the Runner's HEC-RAS record
        dam.mode = "run_linux"
        try:
            ops.run("hec_run", dam)
            raise AssertionError("no error")
        except CH.ChainError:
            pass
    finally:
        shutil.rmtree(root, ignore_errors=True)
