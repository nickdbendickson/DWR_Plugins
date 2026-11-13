"""3.20.0: a batch runs several dams at the same time, each in its own child process
(core/batch_workers.py; `WORKERS` on ndbreach:batch, `--workers` on `ndbreach_cli.py batch`).

* The children are the same tool for ONE dam; the parent merges their one-row summaries in
  the order of the dams.
* WORKERS = 1 (the default) is the old loop, line for line.
* When workers cannot be used (no qgis_process, a temporary layer, the plugin not enabled for
  qgis_process) the log says why and the dams run one after another.

The child processes are doubles here; the model test runs real ones on the built-in engine."""
import csv
import json
import os
import shutil
import subprocess
import sys
import tempfile

from nd_breach_runner.core import batch_workers as bw
from nd_breach_runner.tests.preflight_stub import without_dem_and_smoke_checks
from nd_breach_runner.tests.registry import model_test, test


class _Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


class _Proc:
    """A child that ends with `code` after `secs` of the fake clock (None = never)."""
    live = []

    def __init__(self, clock, secs, code=0, on_end=None):
        self.clock, self.t_end, self.code, self.on_end = clock, clock() + secs if secs is not None else None, code, on_end
        self.killed = False
        _Proc.live.append(self)

    def poll(self):
        if self.killed:
            return -9
        if self.t_end is not None and self.clock() >= self.t_end:
            if self.on_end:
                self.on_end()
                self.on_end = None
            return self.code
        return None

    def terminate(self):
        self.killed = True

    kill = terminate


def _jobs(d, names):
    return [{"key": i, "name": n, "cmd": ["x", n], "log": os.path.join(d, n + ".log")}
            for i, n in enumerate(names)]


# --------------------------------------------------------------------------- the plan
@test
def workers_plan_shares_the_cores_and_never_exceeds_the_dams():
    assert bw.clamp_workers("x") == 1 and bw.clamp_workers(0) == 1 and bw.clamp_workers(3) == 3
    assert bw.clamp_workers(999) == bw.MAX_WORKERS
    used, each, text = bw.plan(4, 10, cpus=8)
    assert (used, each) == (4, 2) and "4 dam(s) at a time, 2 thread(s) each" in text
    used, each, text = bw.plan(4, 2, cpus=8)              # two dams: two workers
    assert (used, each) == (2, 4) and "4 were asked for" in text
    used, each, text = bw.plan(8, 20, cpus=4)             # never more workers than CPUs
    assert (used, each) == (4, 1)
    used, each, text = bw.plan(4, 10, threads=6, cpus=8)  # the user's own thread count is kept
    assert (used, each) == (4, 6) and "more threads than CPUs" in text
    assert bw.plan(1, 5, cpus=8)[:2] == (1, 8)


# ----------------------------------------------------------------------- the children
@test
def workers_run_at_most_n_children_and_report_each_one():
    d = tempfile.mkdtemp()
    try:
        clock, seen, log = _Clock(), [], []
        peak = [0]

        def popen(cmd, **kw):
            assert kw["stdout"] is not None and kw["env"] == {"A": "1"}
            running = sum(1 for p in _Proc.live if p.poll() is None)
            peak[0] = max(peak[0], running + 1)
            seen.append(cmd[1])
            return _Proc(clock, 10.0, 0 if cmd[1] != "D3" else 4)
        _Proc.live = []
        res = bw.run_children(_jobs(d, ["D1", "D2", "D3", "D4", "D5"]), 2, log=log.append,
                              env={"A": "1"}, popen=popen, sleep=clock.sleep, clock=clock,
                              poll_s=1.0)
        assert seen == ["D1", "D2", "D3", "D4", "D5"] and peak[0] == 2, (seen, peak)
        assert [res[i][0] for i in range(5)] == [0, 0, 4, 0, 0]
        assert "ended with code 4" in res[2][1]
        assert any(m.startswith("D3 (") and "code 4" in m for m in log)
        assert any("D1: started (1 running)" == m for m in log)
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test
def workers_time_limit_cancel_and_a_child_that_cannot_start():
    d = tempfile.mkdtemp()
    try:
        clock = _Clock()
        _Proc.live = []
        procs = {}

        def popen(cmd, **kw):
            if cmd[1] == "BAD":
                raise OSError("no such program")
            procs[cmd[1]] = _Proc(clock, None if cmd[1] == "SLOW" else 5.0)
            return procs[cmd[1]]
        res = bw.run_children(_jobs(d, ["BAD", "SLOW", "OK"]), 3, timeout_s=60.0, popen=popen,
                              sleep=clock.sleep, clock=clock, poll_s=1.0)
        assert res[0][0] == bw.NOT_STARTED and "could not be started" in res[0][1]
        assert res[1][0] == bw.TIMED_OUT and "1 min" in res[1][1] and procs["SLOW"].killed
        assert res[2] == (0, "")
        # cancel: the running child is ended, the one waiting never starts and has no result
        clock, n = _Clock(), [0]
        procs.clear()

        def cancelled():
            n[0] += 1
            return n[0] > 3
        res = bw.run_children(_jobs(d, ["SLOW", "LATER"]), 1, cancelled=cancelled, popen=popen,
                              sleep=clock.sleep, clock=clock, poll_s=1.0)
        assert res == {0: (bw.CANCELLED, "cancelled")} and procs["SLOW"].killed
        assert "LATER" not in procs
        # stdin goes through a file next to the log
        got = {}

        def popen2(cmd, **kw):
            got["stdin"] = kw["stdin"].read()
            return _Proc(clock, 0.0)
        job = dict(_jobs(d, ["J"])[0], stdin=bw.stdin_json({"A": 1}))
        bw.run_children([job], 1, popen=popen2, sleep=clock.sleep, clock=clock)
        assert json.loads(got["stdin"]) == {"inputs": {"A": 1}}
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test
def workers_row_comes_from_the_child_or_says_why_not():
    d = tempfile.mkdtemp()
    try:
        fields = ("Dam", "Folder", "Status", "Peak_Breach_cfs", "Message")
        p = os.path.join(d, "a.csv")
        with open(p, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            w.writerow({"Dam": "A1", "Folder": "/child/A1", "Status": "done",
                        "Peak_Breach_cfs": "1234.0", "Message": ""})
        row = dict.fromkeys(fields, "")
        row.update({"Dam": "A1", "Folder": "/parent/A1"})
        bw.fill_row(row, fields, (0, ""), p, "a.log")
        assert row["Status"] == "done" and row["Peak_Breach_cfs"] == "1234.0"
        assert row["Folder"] == "/parent/A1"
        # a failed dam is the child's own row (the CLI child exits 3 for it)
        row2 = dict(row, Status="")
        bw.fill_row(row2, fields, (3, "ended with code 3"), p, "a.log")
        assert row2["Status"] == "done"
        # no row: failed, with the reason and the log
        row3 = dict.fromkeys(fields, "")
        row3["Dam"] = "A2"
        bw.fill_row(row3, fields, (1, "ended with code 1: Traceback | boom"),
                    os.path.join(d, "none.csv"), "/w/0002_A2.log")
        assert row3["Status"] == "failed" and "boom" in row3["Message"]
        assert "/w/0002_A2.log" in row3["Message"]
        row4 = dict.fromkeys(fields, "")
        row4["Dam"] = "A1"                                   # a time limit beats a stale row
        bw.fill_row(row4, fields, (bw.TIMED_OUT, "stopped after 5 min"), p, "a.log")
        assert row4["Status"] == "failed" and "5 min" in row4["Message"]
        row5 = dict.fromkeys(fields, "")
        bw.fill_row(row5, fields, (bw.CANCELLED, "cancelled"), os.path.join(d, "none.csv"), "l")
        assert row5["Status"] == "cancelled"
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ----------------------------------------------------------------------- qgis_process
@test
def workers_find_qgis_process_and_share_the_profile():
    d = tempfile.mkdtemp()
    try:
        exe = os.path.join(d, "bin", "qgis_process.exe" if os.name == "nt" else "qgis_process")
        os.makedirs(os.path.dirname(exe))
        with open(exe, "w") as f:
            f.write("#!/bin/sh\n")
        os.chmod(exe, 0o755)
        assert bw.find_qgis_process([d], env={}) == (exe, "")
        assert bw.find_qgis_process([], env={bw.ENV_QGIS_PROCESS: exe}) == (exe, "")
        got, why = bw.find_qgis_process([os.path.join(d, "nowhere")],
                                        env={bw.ENV_QGIS_PROCESS: os.path.join(d, "no")})
        if got is None:                                    # (a qgis_process on PATH is fine)
            assert "not a program" in why and bw.ENV_QGIS_PROCESS in why
        # the session's profile is the default one: the children get its configuration folder
        root = os.path.join(d, "QGIS3")
        for name in ("default", "damsafety"):
            os.makedirs(os.path.join(root, "profiles", name))
        work = os.path.join(d, "work")
        os.makedirs(work)
        env, note = bw.profile_env({"X": "1"}, os.path.join(root, "profiles", "default") + os.sep,
                                   work)
        assert env == {"X": "1", "QGIS_CUSTOM_CONFIG_PATH": root} and note == ""
        # another profile: a configuration folder whose default profile links to it
        links = []
        env, note = bw.profile_env({}, os.path.join(root, "profiles", "damsafety"), work,
                                   link=lambda link, target: links.append((link, target)))
        cfg = os.path.join(work, "qgis_config")
        assert env["QGIS_CUSTOM_CONFIG_PATH"] == cfg and "damsafety" in note
        assert links == [(os.path.join(cfg, "profiles", "default"),
                          os.path.join(root, "profiles", "damsafety"))]
        # qgis_process 3.34 opens 'default' whatever profiles.ini says, so a profile that
        # profiles.ini names as the default is linked all the same, and 'default' itself is
        # linked too when profiles.ini names another one (a later QGIS might follow it)
        with open(os.path.join(root, "profiles", "profiles.ini"), "w") as f:
            f.write("[core]\ndefaultProfile=damsafety\n")
        for name in ("damsafety", "default"):
            shutil.rmtree(cfg, ignore_errors=True)
            env, note = bw.profile_env({}, os.path.join(root, "profiles", name), work,
                                       link=lambda link, target: None)
            assert env["QGIS_CUSTOM_CONFIG_PATH"] == cfg, name
        # a link that cannot be made: no workers, with the reason
        os.remove(os.path.join(root, "profiles", "profiles.ini"))

        def no_link(link, target):
            raise OSError("not permitted")
        shutil.rmtree(cfg, ignore_errors=True)
        try:
            bw.profile_env({}, os.path.join(root, "profiles", "damsafety"), work, link=no_link)
            raise AssertionError("expected WorkersUnavailable")
        except bw.WorkersUnavailable as e:
            assert "damsafety" in str(e) and "not permitted" in str(e)
        # not a profile folder (a script, a test): the environment is inherited as it is
        assert bw.profile_env({"X": "1"}, "", work) == ({"X": "1"}, "")
        # the probe
        class R:
            returncode = 1
        try:
            bw.probe(exe, "ndbreach:batch", {}, run=lambda *a, **k: R())
            raise AssertionError("expected WorkersUnavailable")
        except bw.WorkersUnavailable as e:
            assert "ndbreach:batch" in str(e) and "plugins" in str(e)
        R.returncode = 0
        bw.probe(exe, "ndbreach:batch", {}, run=lambda *a, **k: R())
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ----------------------------------------------------------------- the QGIS batch tool
class _DamsLayer:
    """A dams layer as the tool sees it; `provider` decides whether a worker can open it."""

    def __init__(self, fq, feats, provider="ogr", path="/data/dams.gpkg|layername=dams"):
        self._src = fq.FakeSource(feats)
        self._provider, self._path = provider, path

    def __getattr__(self, name):
        return getattr(self._src, name)

    def providerType(self):
        return self._provider

    def source(self):
        return self._path


def _batch_setup(provider="ogr"):
    from nd_breach_runner.tests.test_v320 import _algs, _tmp
    algs, fq = _algs()
    d = _tmp()

    class _R:
        def crs(self):
            return fq.QgsCoordinateReferenceSystem("EPSG:26914")

        def source(self):
            return "/data/dem.vrt"

        def providerType(self):
            return "gdal"
    pt = lambda: fq.FakeGeometry("point", [[(0.0, 0.0)]])
    feats = [fq.FakeFeature(pt(), ID="A1", H=20.0, S=500.0),
             fq.FakeFeature(pt(), ID="A2", H=25.0, S=600.0),
             fq.FakeFeature(pt(), ID="A2", H=25.0, S=600.0),          # the same dam again
             fq.FakeFeature(pt(), ID="A3", H=30.0, S=700.0)]
    a = algs.BatchAlgorithm()
    a.initAlgorithm()
    params = {"DAMS": _DamsLayer(fq, feats, provider), "F_ID": "ID", "F_HEIGHT": "H",
              "F_STORAGE": "S", "DEM": _R(), "DAMS_FOLDER": d, "LISFLOOD_EXE": sys.executable,
              "CELL": 15.0, "WORKERS": 2}
    return algs, fq, a, params, d


def _fake_children(algs, fields, status=None):
    """Replace the worker machinery: every child 'runs' by writing its one-row summary."""
    calls = {}

    def run_children(jobs, workers, **kw):
        calls["workers"], calls["env"], calls["jobs"] = workers, kw.get("env"), jobs
        out = {}
        for job in jobs:
            p = json.loads(job["stdin"].decode("utf-8"))["inputs"]
            st = (status or {}).get(p["ONLY_IDS"], "done")
            if st == "crash":
                out[job["key"]] = (1, "ended with code 1: boom")
                continue
            with open(p["SUMMARY_FILE"], "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=fields)
                w.writeheader()
                w.writerow(dict(dict.fromkeys(fields, ""), Dam=p["ONLY_IDS"], Status=st,
                                Peak_Breach_cfs="1000.0", Folder="child"))
            out[job["key"]] = (0, "")
        return out
    saved = (algs.bworkers.find_qgis_process, algs.bworkers.probe, algs.bworkers.run_children)
    algs.bworkers.find_qgis_process = lambda *a, **k: ("/usr/bin/qgis_process", "")
    algs.bworkers.probe = lambda exe, alg_id, env, **k: calls.setdefault("probe", (exe, alg_id))
    algs.bworkers.run_children = run_children

    def restore():
        (algs.bworkers.find_qgis_process, algs.bworkers.probe,
         algs.bworkers.run_children) = saved
    return calls, restore


def _rows(path):
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


@test
@without_dem_and_smoke_checks
def batch_tool_hands_each_dam_to_a_worker_and_merges_the_rows():
    algs, fq, a, params, d = _batch_setup()
    calls, restore = _fake_children(algs, algs.BATCH_FIELDS, {"A3": "crash"})
    try:
        for name in ("WORKERS", "WORKER_TIMEOUT_MIN", "ONLY_IDS", "SUMMARY_FILE"):
            assert a.parameterDefinition(name) is not None, name
        assert a.parameterDefinition("WORKERS").kwargs["defaultValue"] == 1
        fb = fq.QgsProcessingFeedback()
        r = a.processAlgorithm(params, fq.QgsProcessingContext(), fb)
        rows = _rows(r["SUMMARY"])
        assert r["SUMMARY"] == os.path.join(d, "Batch_Breach_Runs.csv")
        assert [(x["Dam"], x["Status"]) for x in rows] == [
            ("A1", "done"), ("A2", "done"), ("A2", "skipped"), ("A3", "failed")], rows
        assert rows[0]["Peak_Breach_cfs"] == "1000.0" and rows[0]["Folder"] == os.path.join(d, "A1")
        assert "same dam ID" in rows[2]["Message"]
        assert "boom" in rows[3]["Message"] and "_batch_workers" in rows[3]["Message"]
        assert r["MODE"].startswith("in worker processes, 2 dam(s) at a time"), r["MODE"]
        assert calls["probe"] == ("/usr/bin/qgis_process", "ndbreach:batch")
        jobs = calls["jobs"]
        assert [j["name"] for j in jobs] == ["A1", "A2", "A3"] and calls["workers"] == 2
        assert jobs[0]["cmd"] == ["/usr/bin/qgis_process", "run", "ndbreach:batch", "-"]
        p = json.loads(jobs[1]["stdin"].decode("utf-8"))["inputs"]
        assert p["ONLY_IDS"] == "A2" and p["WORKERS"] == 1 and p["CLASSIFY"] is False
        assert p["DAMS"] == "/data/dams.gpkg|layername=dams" and p["DEM"] == "/data/dem.vrt"
        assert p["F_ID"] == "ID" and p["CELL"] == 15.0 and p["DAMS_FOLDER"] == d
        assert p["LISFLOOD_EXE"] == sys.executable and p["THREADS"] >= 1
        assert os.path.dirname(p["SUMMARY_FILE"]) == os.path.join(d, bw.WORK_FOLDER)
        assert any("failed" not in m and m.startswith("Dams were run: in worker") for m in fb.info)
    finally:
        restore()
        shutil.rmtree(d, ignore_errors=True)


def _here(rows):
    """The dams that were run in this session: the harness has no GDAL DEM, so each one fails
    in its own run (a worker's failure would name the worker log instead)."""
    return [r["Dam"] for r in rows if r["Status"] == "failed" and "worker" not in r["Message"]]


@test
@without_dem_and_smoke_checks
def batch_tool_runs_one_after_another_when_workers_cannot_be_used():
    algs, fq, a, params, d = _batch_setup(provider="memory")
    calls, restore = _fake_children(algs, algs.BATCH_FIELDS)
    try:
        fb = fq.QgsProcessingFeedback()
        r = a.processAlgorithm(params, fq.QgsProcessingContext(), fb)
        assert "jobs" not in calls and "probe" not in calls
        rows = _rows(r["SUMMARY"])
        assert _here(rows) == ["A1", "A2", "A3"], rows       # in this session, in order
        assert "temporary layer" in r["MODE"] and r["MODE"].startswith("one dam after another")
        assert any("Workers are not used" in m and "save it to a file" in m for m in fb.errors)
        # no qgis_process
        algs.bworkers.find_qgis_process = lambda *a, **k: (None, "qgis_process was not found")
        params["DAMS"] = _DamsLayer(fq, params["DAMS"].feats)
        r = a.processAlgorithm(params, fq.QgsProcessingContext(), fb)
        assert _here(_rows(r["SUMMARY"])) == ["A1", "A2", "A3"] and "was not found" in r["MODE"]
        assert "jobs" not in calls
        # WORKERS = 1: no worker machinery at all, and the duplicate row is run as before
        algs.bworkers.find_qgis_process = lambda *a, **k: ("/usr/bin/qgis_process", "")
        r = a.processAlgorithm(dict(params, WORKERS=1), fq.QgsProcessingContext(), fb)
        assert _here(_rows(r["SUMMARY"])) == ["A1", "A2", "A2", "A3"]
        assert r["MODE"] == "one dam after another in this session" and "jobs" not in calls
    finally:
        restore()
        shutil.rmtree(d, ignore_errors=True)


@test
@without_dem_and_smoke_checks
def batch_tool_as_a_worker_runs_only_its_dam_and_writes_its_own_summary():
    algs, fq, a, params, d = _batch_setup()
    calls, restore = _fake_children(algs, algs.BATCH_FIELDS)
    try:
        out = os.path.join(d, "one.csv")
        r = a.processAlgorithm(dict(params, ONLY_IDS="A3", SUMMARY_FILE=out, WORKERS=4),
                               fq.QgsProcessingContext(), fq.QgsProcessingFeedback())
        assert "jobs" not in calls                           # a worker never starts workers
        assert r["SUMMARY"] == out and _here(_rows(out)) == ["A3"] and len(_rows(out)) == 1
        assert not os.path.exists(os.path.join(d, "Batch_Breach_Runs.csv"))
    finally:
        restore()
        shutil.rmtree(d, ignore_errors=True)


# ------------------------------------------------------------------- the command line
def _cli_table(d):
    from nd_breach_runner.tests.test_cli import base_config, dump
    p, cfg = base_config(d)
    cfg["dam"] = {"point": None}
    cfg["batch"] = {"crs": None}
    cfg["scenario"] = {"type": "sunny"}
    dump(p, cfg)
    table = os.path.join(d, "dams.csv")
    with open(table, "w") as f:
        f.write("NIDID,Dam Name,Dam Height (ft),Max Storage (acre-feet),Surface Area (acres),"
                "Top of Dam (ft),x,y\n")
        f.write("A1,Alpha,25,20,4,1850,0,0\n")
        f.write("A2,Beta,30,40,8,1900,500,100\n")
        f.write("A3,Gamma,25,20,4,1850,0,0\n")
    return p, table


@test
def cli_batch_workers_start_one_child_per_dam_and_merge_the_rows():
    from nd_breach_runner.tests.test_cli import cli, run_cli, Tmp, _FakeResult
    c = cli()
    with Tmp() as d:
        p, table = _cli_table(d)
        calls = {}

        def run_children(jobs, workers, **kw):
            calls["jobs"], calls["workers"] = jobs, workers
            out = {}
            for job in jobs:
                cmd = job["cmd"]
                row = int(cmd[cmd.index("--only-row") + 1])
                path = cmd[cmd.index("--summary-file") + 1]
                if row == 3:
                    out[job["key"]] = (bw.TIMED_OUT, "stopped after 2 min (the worker time limit)")
                    continue
                with open(path, "w", newline="", encoding="utf-8") as f:
                    w = csv.DictWriter(f, fieldnames=c.BATCH_FIELDS)
                    w.writeheader()
                    w.writerow(dict(dict.fromkeys(c.BATCH_FIELDS, ""), Dam=job["name"],
                                    Status="done", Flooded_ac="12.5"))
                out[job["key"]] = (0, "")
            return out
        keep = c.bworkers.run_children
        c.bworkers.run_children = run_children
        try:
            code, out, err = run_cli("batch", table, p, "--lisflood", c.lfmod.BUILTIN_EXE, "--workers", "2",
                                     "--worker-timeout-min", "2")
        finally:
            c.bworkers.run_children = keep
        rows = _rows(os.path.join(d, "out", "Batch_Breach_Runs.csv"))
        assert [(r["Dam"], r["Status"]) for r in rows] == [
            ("A1", "done"), ("A2", "done"), ("A3", "failed")], rows
        assert rows[0]["Flooded_ac"] == "12.5" and rows[0]["Folder"].endswith(os.path.join("out", "A1"))
        assert "worker time limit" in rows[2]["Message"] and code == 3
        assert calls["workers"] == 2 and "Workers: 2 dam(s) at a time" in out
        cmd = calls["jobs"][1]["cmd"]
        assert cmd[0] == sys.executable and cmd[2] == "batch" and cmd[1].endswith("ndbreach_cli.py")
        assert cmd[cmd.index("--only-row") + 1] == "2"
        assert cmd[cmd.index("--lisflood") + 1] == c.lfmod.BUILTIN_EXE
        assert any(x.startswith("--threads=") and int(x.split("=")[1]) >= 1 for x in cmd), cmd
        assert cmd[cmd.index("--output") + 1] == os.path.join(d, "out")
        # the child's side: --only-row runs that row alone and writes the summary it was given
        ran = []

        def fake_run_dam(dam, *rest):
            ran.append(dam.dam_id)
            return _FakeResult(dam.dam_id)
        keep = c.workflow.run_dam
        c.workflow.run_dam = fake_run_dam
        try:
            one = os.path.join(d, "one.csv")
            code, out, err = run_cli("batch", table, p, "--lisflood", c.lfmod.BUILTIN_EXE, "--only-row", "2",
                                     "--summary-file", one, "--workers", "5")
            assert code == 0 and ran == ["A2"], (code, ran, err)
            assert [(r["Dam"], r["Status"]) for r in _rows(one)] == [("A2", "done")]
            # one dam left to run: no workers, it runs here
            del ran[:]
            code, out, err = run_cli("batch", table, p, "--lisflood", c.lfmod.BUILTIN_EXE, "--limit", "1",
                                     "--workers", "3")
            assert ran == ["A1"] and "Workers:" not in out
        finally:
            c.workflow.run_dam = keep


@model_test
def cli_batch_workers_run_real_children_on_the_builtin_engine():
    """Three dams, two real child processes at a time, the built-in engine: the same rows as
    one dam after another."""
    from nd_breach_runner.tests.test_cli import Tmp
    here = os.path.dirname(os.path.abspath(__file__))
    script = os.path.join(os.path.dirname(here), "scripts", "ndbreach_cli.py")
    from nd_breach_runner.core import lisflood as lf
    with Tmp() as d:
        p, table = _cli_table(d)
        env = dict(os.environ, ND_DAM_PRE_RUN_SMOKE="0")

        def run(out, *extra):
            r = subprocess.run([sys.executable, script, "batch", table, p, "--lisflood", lf.BUILTIN_EXE,
                                "--output", out] + list(extra), env=env, capture_output=True,
                               text=True, timeout=1800)
            assert r.returncode in (0, 3), (r.returncode, r.stdout[-2000:], r.stderr[-2000:])
            return _rows(os.path.join(out, "Batch_Breach_Runs.csv")), r.stdout
        par, text = run(os.path.join(d, "par"), "--workers", "2")
        assert "Workers: 2 dam(s) at a time" in text, text
        seq, _t = run(os.path.join(d, "seq"))
        key = lambda rows: [(r["Dam"], r["Status"], r["Peak_Breach_cfs"], r["Flooded_ac"],
                             r["Max_Depth_ft"]) for r in rows]
        assert key(par) == key(seq), (key(par), key(seq))
        assert [r["Status"] for r in par] == ["done"] * 3, par
        work = os.path.join(d, "par", bw.WORK_FOLDER)
        assert sorted(f for f in os.listdir(work) if f.endswith(".csv")) == [
            "0001_A1.csv", "0002_A2.csv", "0003_A3.csv"]
