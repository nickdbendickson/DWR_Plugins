"""3.10.72 core/anuga_slowcheck.py: the read-only freeze / time-step collapse checker, on
tiny synthetic anuga.log files, checkpoints, timestep_diag.csv and a fake /proc (no model
runs). Milliseconds."""
import calendar
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
from nd_breach_runner.core import anuga_slowcheck as SC  # noqa: E402
from nd_breach_runner.tests.registry import test  # noqa: E402

NOW = 1792900000.0
PID = 4242
HZ = 100.0


def _utc(t):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


class Run:
    """A fake run folder (_breach_model/anuga) and a fake /proc."""

    def __init__(self):
        self.root = tempfile.mkdtemp(prefix="slowchk_")
        self.work = os.path.join(self.root, "_breach_model", "anuga")
        self.proc = os.path.join(self.root, "proc")
        os.makedirs(self.work)
        os.makedirs(self.proc)

    def log(self, lines, mtime=NOW - 10, job_mtime=None):
        p = os.path.join(self.work, "anuga.log")
        with open(p, "w") as f:
            f.write("ANUGA OpenMP threads: 4 (asked for 4)\nPROGRESS 0 mesh 600 triangles\n")
            f.write("\n".join(lines) + "\n")
        if job_mtime is not None:
            j = os.path.join(self.work, "anuga_job.npz")
            open(j, "wb").close()
            os.utime(j, (job_mtime, job_mtime))
        os.utime(p, (mtime, mtime))
        return p

    def worker(self, cpu_s, start_uptime, uptime, threads=7, comm="python3.12", cmd=None,
               state="R"):
        d = os.path.join(self.proc, str(PID))
        os.makedirs(d, exist_ok=True)
        f = [state, "1", "1", "1", "0", "-1", "0", "0", "0", "0", "0",
             str(int(cpu_s * HZ * 0.9)), str(int(round(cpu_s * HZ)) - int(cpu_s * HZ * 0.9)),
             "0", "0", "25", "5", str(threads), "0", str(int(start_uptime * HZ)), "0", "0"]
        with open(os.path.join(d, "stat"), "w") as fh:
            fh.write("{} ({}) {}\n".format(PID, comm, " ".join(f)))
        with open(os.path.join(d, "cmdline"), "wb") as fh:
            fh.write(cmd or b"python3.12\0-u\0/x/core/anuga_worker.py\0job\0res\0")
        if not os.path.lexists(os.path.join(d, "cwd")):
            os.symlink(self.work, os.path.join(d, "cwd"))
        self.uptime(uptime)

    def uptime(self, u):
        with open(os.path.join(self.proc, "uptime"), "w") as fh:
            fh.write("{:.2f} 1000.00\n".format(u))

    def checkpoint(self, t, mtime):
        d = os.path.join(self.work, "anuga_checkpoints", "f00")
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, "anuga_breach_{:.1f}.pickle".format(t))
        open(p, "wb").close()
        os.utime(p, (mtime, mtime))

    def check(self, **kw):
        kw.setdefault("sample_s", 0)
        return SC.check(self.root, proc=self.proc, now=kw.pop("now", NOW), hz=HZ, **kw)

    def close(self):
        shutil.rmtree(self.root)


def _new_lines(n, dsteps=1000, wall=36.0, t0=0.0, start=NOW - 4000, override=None):
    out, total, w = [], 0, start
    for i in range(1, n + 1):
        ds, ws = (override or {}).get(i, (dsteps, wall))
        total += ds
        w += ws
        out.append("PROGRESS {:.2f} t={:.0f}s utc={} steps={} dsteps={} wall_s={:.1f} "
                   "rate={:.3g}".format(i, t0 + 300 * i, _utc(w), total, ds, ws, 300 / ws))
        if i % 6 == 0:
            out.append("PROGRESS {:.2f} checkpoint t={:.0f}s".format(i, t0 + 300 * i))
    return out


def _legacy_lines(n, t0=0.0):
    return ["PROGRESS {:.2f} t={:.0f}s".format(i * 0.1, t0 + 300 * i) for i in range(1, n + 1)]


@test
def slowcheck_parses_both_progress_formats():
    ys, res, done = SC.parse_log(_new_lines(3) + [
        "PROGRESS 4.00 resumed from the checkpoint at t=600s", "PROGRESS 0 mesh 5 triangles",
        "  warnings.warn(msg)"] + _legacy_lines(2) + ["PROGRESS 100 done"])
    assert [y["t"] for y in ys] == [300.0, 600.0, 900.0, 300.0, 600.0] and res == 600.0 and done
    assert ys[1]["dsteps"] == 1000 and ys[1]["steps"] == 2000 and ys[1]["wall_s"] == 36.0
    assert ys[1]["utc"].endswith("Z") and "dsteps" not in ys[3]


@test
def slowcheck_read_proc_comm_with_spaces_and_parens():
    r = Run()
    try:
        r.worker(cpu_s=1234.5, start_uptime=100.0, uptime=500.0, comm="py (x) 3")
        d = SC.read_proc(PID, r.proc, HZ)
        assert abs(d["cpu_s"] - 1234.5) < 0.02 and d["start_uptime_s"] == 100.0, d
        assert d["threads"] == 7 and "anuga_worker" in d["cmdline"]
        assert SC.find_worker(r.work, r.proc, HZ)["pid"] == PID
        assert SC.read_proc(999, r.proc, HZ) is None
        r.worker(cpu_s=1, start_uptime=1, uptime=5, state="Z")
        assert SC.read_proc(PID, r.proc, HZ) is None            # a zombie is not running
    finally:
        r.close()


@test
def slowcheck_no_log_finished_and_not_running():
    r = Run()
    try:
        assert r.check()["verdict"] == "insufficient_data"
        r.log(_new_lines(5) + ["PROGRESS 100 done"])
        assert r.check()["verdict"] == "finished"
        r.log(_new_lines(5))
        x = r.check()
        assert x["verdict"] == "not_running" and "no live anuga_worker" in x["reasons"][0]
        assert r.check(pid=PID)["verdict"] == "not_running"
        r.worker(cpu_s=100, start_uptime=1000, uptime=1100)
        r.log(_new_lines(1))
        assert r.check()["verdict"] == "insufficient_data"
    finally:
        r.close()


@test
def slowcheck_new_format_normal():
    r = Run()
    try:
        r.log(_new_lines(20), mtime=NOW - 20, job_mtime=NOW - 800)
        r.worker(cpu_s=2800, start_uptime=1000, uptime=1790)
        x = r.check()
        assert x["verdict"] == "normal" and x["confidence"] == "high", x
        assert x["dsteps_trend"] == 1.0 and x["expected_yield_wall_s"] == 36.0
        assert x["freeze_total_s"] == 10.0 and x["cores_now"] == round(2800 / 790, 2)
        ln = SC.line(x)
        assert ln.startswith("VERDICT=normal confidence=high ") and "dsteps_trend=1 " in ln
    finally:
        r.close()


@test
def slowcheck_new_format_freeze_yields_and_collapse():
    r = Run()
    try:
        r.worker(cpu_s=2800, start_uptime=1000, uptime=1790)
        # yield 19 took 40x the wall time with the usual 1000 steps: frozen
        r.log(_new_lines(20, override={19: (1010, 1500.0)}), job_mtime=NOW - 9000)
        x = r.check()
        assert x["verdict"] == "host_freeze" and x["confidence"] == "high", x
        assert x["freeze_yields_recent"] == 1
        # the last 3 yields took 3x the internal steps: a real time-step collapse
        r.log(_new_lines(20, override={i: (3000, 108.0) for i in (18, 19, 20)}),
              job_mtime=NOW - 9000)
        x = r.check()
        assert x["verdict"] == "timestep_collapse" and x["dsteps_trend"] == 3.0, x
    finally:
        r.close()


@test
def slowcheck_legacy_single_call_bounds_freeze_vs_collapse():
    r = Run()
    try:
        # legacy lines, 300 s yields; 30,000 model s in 3,000 s awake -> 30 s per yield
        r.log(_legacy_lines(100), mtime=NOW - 900, job_mtime=NOW - 20000)
        r.checkpoint(27000.0, NOW - 7000)
        r.checkpoint(29400.0, NOW - 1000)
        r.worker(cpu_s=12000, start_uptime=1000, uptime=4000)
        x = r.check()
        assert x["log_format"] == "legacy" and x["expected_yield_wall_s"] == 30.0, x
        assert x["freeze_total_s"] == 17000.0 and x["stalled"]
        assert x["verdict"] == "host_freeze" and x["confidence"] == "low", x
        assert x["model_s_per_wall_s"] == 0.4 and x["cpu_s_per_model_s"] == 0.4
        # the box was frozen only 100 s since the start: a 900 s stall is mostly real
        r.log(_legacy_lines(100), mtime=NOW - 900, job_mtime=NOW - 3100)
        x = r.check()
        assert x["verdict"] == "timestep_collapse" and x["confidence"] == "low", x
        # not stalled -> normal, medium (legacy, one call)
        r.log(_legacy_lines(100), mtime=NOW - 20, job_mtime=NOW - 3100)
        x = r.check()
        assert (x["verdict"], x["confidence"]) == ("normal", "medium"), x
    finally:
        r.close()


@test
def slowcheck_state_snapshots_freeze_idle_and_cost():
    r = Run()
    try:
        r.log(_legacy_lines(100), mtime=NOW - 900, job_mtime=NOW - 3100)
        r.worker(cpu_s=12000, start_uptime=1000, uptime=4000)
        snap = r.check()["snapshot"]
        # 1,200 wall s later but only 200 s of uptime: frozen 1,000 s in between
        r.worker(cpu_s=12800, start_uptime=1000, uptime=4200)
        x = r.check(state=snap, now=NOW + 1200)
        assert x["verdict"] == "host_freeze" and x["confidence"] == "high", x
        assert x["state_freeze_s"] == 1000.0 and x["window_cpu_s"] == 800.0
        # awake, no new line, no CPU used since the last check: stalled_unknown
        r.worker(cpu_s=12000, start_uptime=1000, uptime=4600)
        x = r.check(state=snap, now=NOW + 600)
        assert x["verdict"] == "stalled_unknown" and x["cores_now"] == 0.0, x
        # awake and computing, one more yield line only: too little to judge the cost
        r.log(_legacy_lines(101), mtime=NOW + 590, job_mtime=NOW - 3100)
        r.worker(cpu_s=13200, start_uptime=1000, uptime=4600)
        x = r.check(state=snap, now=NOW + 600)
        assert "cost_trend" not in x and x["verdict"] == "normal", x
        # the model moved 900 s for 3,600 CPU s (4 per model s, 10x the run's 0.4)
        r.log(_legacy_lines(103), mtime=NOW + 590, job_mtime=NOW - 3100)
        r.worker(cpu_s=15600, start_uptime=1000, uptime=4600)
        x = r.check(state=snap, now=NOW + 600)
        assert x["verdict"] == "timestep_collapse" and x["cost_trend"] > 2, x
        # another pid: the snapshot is ignored
        x = r.check(state=dict(snap, pid=1), now=NOW + 600)
        assert "state_freeze_s" not in x
    finally:
        r.close()


@test
def slowcheck_limiting_triangle_and_cli():
    r = Run()
    try:
        r.log(_new_lines(20), mtime=NOW - 20, job_mtime=NOW - 800)
        r.worker(cpu_s=2800, start_uptime=1000, uptime=1790)
        with open(os.path.join(r.work, "timestep_diag.csv"), "w") as f:
            f.write("utc,t_s,steps,tri,x,y,lon,lat,area_m2,depth_m,speed_mps,dt_tri_s,"
                    "near_op,near_op_dist_m\n"
                    "2026-10-24T09:00:00Z,6000,1000,1234,-103768.1,2665955.2,-98.9,46.9,"
                    "19.5,0.41,1.9,0.29,NDDOT 09-117-18.1,12.3\n")
        with open(os.path.join(r.work, "timestep_diag_summary.txt"), "w") as f:
            f.write("timestep diagnostic: 1 samples\ntop...\ntri 1234: 1 samples (100%)\n")
        x = r.check()
        assert x["limiting"]["tri"] == "1234" and x["limiting"]["summary_top"].startswith("tri 1234")
        ln = SC.line(x)
        assert "lim_tri=1234" in ln and "lim_near_op=NDDOT_09-117-18.1" in ln, ln
        # the CLI (real clock, fake /proc): exit code, --json, --state written
        st = os.path.join(r.root, "state.json")
        for argv in (["--json"], []):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = SC.main([r.root, "--proc", r.proc, "--sample-s", "0", "--state", st]
                             + argv)
            assert rc in SC.EXIT.values()
            out = buf.getvalue()
            if argv:
                d = json.loads(out)
                assert rc == SC.EXIT[d["verdict"]] and d["pid"] == PID
            else:
                assert out.startswith("VERDICT=") and out.count("\n") == 1
        with open(st) as f:
            assert json.load(f)["pid"] == PID
        # window in minutes
        ys = SC.parse_log(_new_lines(20))[0]
        assert 1 <= len(SC._window(ys, "3m")) <= 6
    finally:
        r.close()


@test
def slowcheck_is_stdlib_only():
    src = open(SC.__file__).read()
    for mod in ("numpy", "qgis", "anuga", "from ."):
        assert "import " + mod not in src and mod + " import" not in src, mod
    assert calendar.timegm(time.gmtime(0)) == 0
