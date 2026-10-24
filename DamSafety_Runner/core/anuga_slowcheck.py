"""3.10.73: read-only checker that tells an ANUGA run's host freeze from a real time-step
collapse (for Grok Bot's watch routine; standard library only, so it runs with any python3
without the ANUGA environment or QGIS):

    python3 <plugin>/core/anuga_slowcheck.py RUN_FOLDER_OR_anuga.log [--pid N]
        [--window N | Nm] [--state FILE] [--sample-s S] [--json]

It reads anuga.log (the worker's PROGRESS lines; 3.10.70 appends utc= steps= dsteps= wall_s=
rate=), the checkpoints' names and times, anuga_partial.npz's time, timestep_diag.csv /
timestep_diag_summary.txt (when the time-step diagnostic is on), /proc/uptime and
/proc/<pid>/stat (CPU seconds, start time). It never writes anything except the optional
--state file and never signals a process.

Why it can tell a freeze: when the host freezes the VM, the wall clock goes on but the box's
CLOCK_BOOTTIME (/proc/uptime) and the worker's CPU time do not. So
  * wall seconds since the worker started (anuga.log's start, anuga_job.npz's mtime) minus
    uptime seconds since it started (/proc/<pid>/stat starttime) = seconds the box was frozen
    since then (freeze_total_s) - from a single call;
  * between two calls (--state): wall gap minus uptime gap = seconds frozen in between
    (state_freeze_s), and CPU seconds per model second (freeze-proof) now vs the run's;
  * 3.10.70 logs: a yield step with a long wall_s but a normal dsteps was frozen; a recent
    dsteps much larger than the window's median is a real time-step collapse.

One line on stdout, VERDICT=<verdict> confidence=high|medium|low key=value ... (--json: a
dict). Exit codes:
  0 normal             the run advances as expected
  1 finished           "PROGRESS 100 done" or anuga_result.npz newer than the log's start
  2                    (argparse) bad arguments
  3 insufficient_data  no anuga.log, or too few yield lines to judge
  4 host_freeze        the box was frozen (the model itself is not slower)
  5 timestep_collapse  ANUGA really takes more / smaller internal steps
  6 stalled_unknown    no new line for long and the worker is not computing (or no evidence)
  7 not_running        no live worker for this run and it did not finish
"""
import argparse
import calendar
import csv
import json
import os
import re
import statistics
import sys
import time

EXIT = {"normal": 0, "finished": 1, "insufficient_data": 3, "host_freeze": 4,
        "timestep_collapse": 5, "stalled_unknown": 6, "not_running": 7}
COLLAPSE_FACTOR = 2.0      # recent dsteps (or CPU s per model s) >= 2x the baseline median
FREEZE_WALL_FACTOR = 3.0   # a yield step's wall_s >= 3x the median with dsteps <= 1.5x
FREEZE_STEPS_FACTOR = 1.5
STALL_FACTOR = 3.0         # no line for 3x the expected yield-step wall time (and >= 120 s)
STALL_MIN_S = 120.0
FREEZE_MIN_S = 60.0        # a wall-vs-uptime gap below this is clock noise
BUSY_CORES = 0.5           # the worker computes when it uses >= 0.5 core
RECENT = 3                 # yield steps that are "recent"
DEFAULT_WINDOW = 48        # yield steps looked at

_PROG = re.compile(r"^PROGRESS\s+(?P<pct>[-\d.eE+]+)\s+t=(?P<t>[-\d.eE+]+)s(?P<rest>.*)$")
_KV = re.compile(r"(\w+)=(\S+)")
_RESUMED = re.compile(r"^PROGRESS\s+\S+\s+resumed from the checkpoint at t=([\d.]+)s")
_CKPT = re.compile(r"anuga_breach_([\d.]+)\.pickle$")


# ----------------------------------------------------------------- inputs
def find_log(path):
    """anuga.log of a run: the path itself, or <path>[/_breach_model]/anuga/anuga.log, or the
    newest anuga.log up to three folders down; None when there is none."""
    if os.path.isfile(path):
        return path
    for sub in ("anuga.log", os.path.join("anuga", "anuga.log"),
                os.path.join("_breach_model", "anuga", "anuga.log")):
        p = os.path.join(path, sub)
        if os.path.isfile(p):
            return p
    found = []
    base = path.rstrip(os.sep).count(os.sep)
    for root, dirs, files in os.walk(path):
        if root.count(os.sep) - base >= 3:
            dirs[:] = []
        if "anuga.log" in files:
            p = os.path.join(root, "anuga.log")
            found.append((os.path.getmtime(p), p))
    return max(found)[1] if found else None


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def parse_log(lines):
    """The yield-step PROGRESS lines as dicts (t, pct and the 3.10.70 fields when present),
    the resume time, whether the run said it was done."""
    ys, resumed, done = [], None, False
    for ln in lines:
        ln = ln.rstrip("\n")
        if ln.startswith("PROGRESS 100 done"):
            done = True
            continue
        m = _RESUMED.match(ln)
        if m:
            resumed = float(m.group(1))
            continue
        m = _PROG.match(ln)
        if not m:
            continue            # "checkpoint t=...", mesh, warnings
        y = {"pct": _num(m.group("pct")), "t": float(m.group("t"))}
        for k, v in _KV.findall(m.group("rest")):
            y[k] = v if k == "utc" else _num(v)
        ys.append(y)
    return ys, resumed, done


def _utc_epoch(s):
    try:
        return float(calendar.timegm(time.strptime(s, "%Y-%m-%dT%H:%M:%SZ")))
    except (TypeError, ValueError):
        return None


def checkpoints(workdir):
    """[(model t, mtime)] of the checkpoint pickles, oldest first."""
    out = []
    root = os.path.join(workdir, "anuga_checkpoints")
    for dp, _, files in os.walk(root):
        for f in files:
            m = _CKPT.search(f)
            if m:
                out.append((float(m.group(1)), os.path.getmtime(os.path.join(dp, f))))
    return sorted(out)


def limiting_triangle(workdir):
    """The last timestep_diag.csv row's limiting triangle and the summary's top line."""
    out = {}
    p = os.path.join(workdir, "timestep_diag.csv")
    if os.path.isfile(p):
        try:
            with open(p, newline="", encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
            if rows:
                r = rows[-1]
                for k in ("t_s", "tri", "x", "y", "lon", "lat", "area_m2", "depth_m",
                          "speed_mps", "dt_min_s", "dt_tri_s", "steps", "near_op",
                          "near_op_dist_m"):
                    if r.get(k) not in (None, ""):
                        out[k] = r[k]
        except (OSError, csv.Error):
            pass
    p = os.path.join(workdir, "timestep_diag_summary.txt")
    if os.path.isfile(p):
        try:
            with open(p, encoding="utf-8") as f:
                top = [ln.strip() for ln in f if ln.startswith("tri ")]
            if top:
                out["summary_top"] = top[0]
        except OSError:
            pass
    return out


# ----------------------------------------------------------------- /proc
def read_uptime(proc="/proc"):
    try:
        with open(os.path.join(proc, "uptime")) as f:
            return float(f.read().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def clk_tck():
    try:
        return float(os.sysconf("SC_CLK_TCK"))
    except (AttributeError, ValueError, OSError):
        return 100.0


def read_proc(pid, proc="/proc", hz=None):
    """{cpu_s, start_uptime_s, threads, state, cmdline, cwd} of a live pid, else None."""
    hz = hz or clk_tck()
    base = os.path.join(proc, str(pid))
    try:
        with open(os.path.join(base, "stat")) as f:
            s = f.read()
    except OSError:
        return None
    f = s[s.rfind(")") + 2:].split()
    if len(f) < 20 or f[0] in ("Z", "X"):
        return None
    d = {"pid": int(pid), "state": f[0], "cpu_s": (int(f[11]) + int(f[12])) / hz,
         "start_uptime_s": int(f[19]) / hz, "threads": int(f[17])}
    try:
        with open(os.path.join(base, "cmdline"), "rb") as fh:
            d["cmdline"] = fh.read().replace(b"\0", b" ").decode("utf-8", "replace").strip()
    except OSError:
        d["cmdline"] = ""
    try:
        d["cwd"] = os.path.realpath(os.path.join(base, "cwd"))
    except OSError:
        d["cwd"] = None
    return d


def find_worker(workdir, proc="/proc", hz=None):
    """The live anuga_worker.py whose working folder is the run's anuga folder."""
    want = os.path.realpath(workdir)
    try:
        names = [n for n in os.listdir(proc) if n.isdigit()]
    except OSError:
        return None
    for n in sorted(names, key=int):
        d = read_proc(n, proc, hz)
        if d and "anuga_worker" in d["cmdline"] and d.get("cwd") == want:
            return d
    return None


def cpu_sample(pid, seconds, proc="/proc", hz=None):
    """Cores the worker used over a short sample (uptime-timed), or None."""
    a, u0 = read_proc(pid, proc, hz), read_uptime(proc)
    if not a or u0 is None or seconds <= 0:
        return None
    time.sleep(seconds)
    b, u1 = read_proc(pid, proc, hz), read_uptime(proc)
    if not b or u1 is None or u1 <= u0:
        return None
    return (b["cpu_s"] - a["cpu_s"]) / (u1 - u0)


# ----------------------------------------------------------------- analysis
def _med(v):
    v = [x for x in v if x is not None]
    return statistics.median(v) if v else None


def _window(ys, window):
    if isinstance(window, str) and window.endswith("m"):
        mins = float(window[:-1])
        last = _utc_epoch(ys[-1].get("utc")) if ys else None
        if last is None:
            return ys[-DEFAULT_WINDOW:]
        return [y for y in ys if (_utc_epoch(y.get("utc")) or 0) >= last - 60 * mins] or ys[-1:]
    return ys[-int(window or DEFAULT_WINDOW):]


def check(path, pid=None, window=DEFAULT_WINDOW, state=None, sample_s=1.0, proc="/proc",
          now=None, hz=None):
    """The verdict dict (see the module docstring). state: the previous call's snapshot or
    None; the caller saves r["snapshot"] for the next call."""
    now = time.time() if now is None else now
    r = {"verdict": None, "confidence": "high", "reasons": []}
    log = find_log(path)
    if not log:
        r.update(verdict="insufficient_data", reasons=["no anuga.log under " + path])
        return r
    workdir = os.path.dirname(os.path.abspath(log))
    r["log"] = log
    with open(log, encoding="utf-8", errors="replace") as f:
        ys, resumed, done = parse_log(f)
    st = os.stat(log)
    r["last_line_age_s"] = round(now - st.st_mtime, 1)
    r["yields"] = len(ys)
    if ys:
        r["last_t_s"], r["last_pct"] = ys[-1]["t"], ys[-1]["pct"]
    new_fmt = bool(ys) and "dsteps" in ys[-1]
    r["log_format"] = "3.10.70" if new_fmt else "legacy"
    ck = checkpoints(workdir)
    if ck:
        r["checkpoints"] = [[t, round(m, 1)] for t, m in ck]
        if len(ck) >= 2 and ck[-1][1] > ck[-2][1]:
            r["ckpt_model_s_per_wall_s"] = round((ck[-1][0] - ck[-2][0]) /
                                                 (ck[-1][1] - ck[-2][1]), 4)
    part = os.path.join(workdir, "anuga_partial.npz")
    if os.path.isfile(part):
        r["partial_age_s"] = round(now - os.path.getmtime(part), 1)
    tri = limiting_triangle(workdir)
    if tri:
        r["limiting"] = tri

    res = os.path.join(workdir, "anuga_result.npz")
    started = _start_wall(workdir, log)
    if done or (os.path.isfile(res) and started and os.path.getmtime(res) >= started):
        r.update(verdict="finished")
        return r

    # the worker process
    w = read_proc(pid, proc, hz) if pid else find_worker(workdir, proc, hz)
    uptime = read_uptime(proc)
    r["uptime_s"] = uptime
    if w is None:
        r.update(verdict="not_running", reasons=[
            "pid {} is not running".format(pid) if pid else "no live anuga_worker.py in "
            + workdir])
        if len(ys) < 2:
            r["confidence"] = "medium"
        return r
    r["pid"], r["cpu_s"] = w["pid"], round(w["cpu_s"], 1)
    r["proc_threads"] = w["threads"]
    snap = {"wall": now, "uptime": uptime, "pid": w["pid"], "cpu_s": w["cpu_s"],
            "t": ys[-1]["t"] if ys else None, "log": log}
    r["snapshot"] = snap
    if uptime is not None:
        age_up = uptime - w["start_uptime_s"]
        r["proc_uptime_age_s"] = round(age_up, 1)
        r["cores_avg"] = round(w["cpu_s"] / age_up, 2) if age_up > 0 else None
        if started:
            r["proc_wall_age_s"] = round(now - started, 1)
            # the log / job are written just before the worker starts: >= 0 but for noise
            r["freeze_total_s"] = round(max(0.0, (now - started) - age_up), 1)
    t0 = resumed if resumed is not None else (ys[0]["t"] - _yieldstep(ys) if ys else None)
    if ys and t0 is not None and ys[-1]["t"] > t0:
        r["cpu_s_per_model_s"] = round(w["cpu_s"] / (ys[-1]["t"] - t0), 4)
        if r.get("proc_uptime_age_s"):
            r["awake_model_s_per_s"] = round((ys[-1]["t"] - t0) / r["proc_uptime_age_s"], 4)
    cores_now = cpu_sample(w["pid"], sample_s, proc, hz) if sample_s else None

    # between calls (--state)
    state_freeze = state_cost = None
    if state and state.get("pid") == w["pid"] and state.get("log") == log \
            and uptime is not None and state.get("uptime") is not None:
        dwall, dup = now - state["wall"], uptime - state["uptime"]
        if dwall > 0 and dup >= 0:
            state_freeze = max(0.0, dwall - dup)
            r["state_wall_s"], r["state_uptime_s"] = round(dwall, 1), round(dup, 1)
            r["state_freeze_s"] = round(state_freeze, 1)
            dcpu = w["cpu_s"] - state["cpu_s"]
            r["window_cpu_s"] = round(dcpu, 1)
            if cores_now is None and dup > 0:
                cores_now = dcpu / dup
            # over >= 3 yield steps of model time, so one line's timing doesn't decide
            if ys and state.get("t") is not None and \
                    ys[-1]["t"] - state["t"] >= RECENT * (_yieldstep(ys) or 1.0) - 1e-6:
                state_cost = dcpu / (ys[-1]["t"] - state["t"])
                r["state_cpu_s_per_model_s"] = round(state_cost, 4)
    if cores_now is None:
        cores_now = r.get("cores_avg")
    r["cores_now"] = None if cores_now is None else round(cores_now, 2)

    if len(ys) < 2:
        r.update(verdict="insufficient_data", reasons=["fewer than 2 yield-step lines"])
        return r

    # the log's own evidence (3.10.70 lines)
    win = _window(ys, window)
    expected = None
    freeze_yields = 0
    collapse = False
    if new_fmt:
        rec, base = win[-RECENT:], win[:-RECENT] or win
        bd = _med([y.get("dsteps") for y in base])
        bw = _med([y.get("wall_s") for y in base
                   if y.get("dsteps") is not None and (bd is None or y["dsteps"] <= FREEZE_STEPS_FACTOR * bd)])
        rd = _med([y.get("dsteps") for y in rec])
        r.update(dsteps_baseline=bd, dsteps_recent=rd, wall_s_baseline=bw,
                 window_yields=len(win))
        if bd and rd is not None:
            r["dsteps_trend"] = round(rd / bd, 3)
            collapse = len(win) > RECENT and rd >= COLLAPSE_FACTOR * bd
        rates = [y.get("rate") for y in rec]
        r["model_s_per_wall_s"] = _med(rates)
        if bw and bd:
            freeze_yields = sum(1 for y in rec if (y.get("wall_s") or 0) >= FREEZE_WALL_FACTOR * bw
                                and (y.get("dsteps") or 0) <= FREEZE_STEPS_FACTOR * bd)
            r["freeze_yields_recent"] = freeze_yields
            expected = bw
        r["window_wall_s"] = round(sum(y.get("wall_s") or 0 for y in win), 1)
    else:
        if r.get("ckpt_model_s_per_wall_s"):
            r["model_s_per_wall_s"] = r["ckpt_model_s_per_wall_s"]
        if r.get("awake_model_s_per_s"):
            expected = _yieldstep(ys) / r["awake_model_s_per_s"]
    if expected:
        r["expected_yield_wall_s"] = round(expected, 1)
    stall_limit = max(STALL_FACTOR * (expected or 0), STALL_MIN_S)
    stalled = r["last_line_age_s"] > stall_limit
    r["stalled"] = stalled
    busy = cores_now is not None and cores_now >= BUSY_CORES
    cost_ratio = None
    if state_cost and r.get("cpu_s_per_model_s"):
        cost_ratio = state_cost / r["cpu_s_per_model_s"]
        r["cost_trend"] = round(cost_ratio, 3)

    # verdict
    if collapse:
        r.update(verdict="timestep_collapse", reasons=[
            "recent dsteps {} >= {}x the window median {}".format(rd, COLLAPSE_FACTOR, bd)])
    elif cost_ratio is not None and cost_ratio >= COLLAPSE_FACTOR:
        r.update(verdict="timestep_collapse", confidence="medium" if new_fmt else "high",
                 reasons=["CPU s per model s since the last check {:.3g} = {:.2f}x the run's"
                          .format(state_cost, cost_ratio)])
    elif state_freeze is not None and state_freeze >= max(FREEZE_MIN_S, 0.1 * r["state_wall_s"]):
        r.update(verdict="host_freeze", reasons=[
            "box frozen {:.0f} of the {:.0f} wall s since the last check".format(
                state_freeze, r["state_wall_s"])])
    elif freeze_yields:
        r.update(verdict="host_freeze", reasons=[
            "{} recent yield step(s) took >= {}x the usual wall time with normal dsteps"
            .format(freeze_yields, FREEZE_WALL_FACTOR)])
    elif stalled and not busy:
        r.update(verdict="stalled_unknown", confidence="medium", reasons=[
            "no line for {:.0f} s and the worker uses {} cores".format(
                r["last_line_age_s"], r["cores_now"])])
    elif stalled:
        excess = r["last_line_age_s"] - (expected or 0)
        ft = r.get("freeze_total_s")
        if ft is not None and ft >= excess:
            r.update(verdict="host_freeze", confidence="low", reasons=[
                "no line for {:.0f} s; the box was frozen {:.0f} s since the worker started, "
                "enough to explain it (use --state to confirm)".format(r["last_line_age_s"], ft)])
        elif ft is not None:
            r.update(verdict="timestep_collapse", confidence="low" if not new_fmt else "medium",
                     reasons=["no line for {:.0f} s while computing; at most {:.0f} s of it "
                              "can be a freeze".format(r["last_line_age_s"], ft)])
        else:
            r.update(verdict="stalled_unknown", confidence="low",
                     reasons=["no line for {:.0f} s; no start time to bound freezes".format(
                         r["last_line_age_s"])])
    else:
        r.update(verdict="normal")
        ft = r.get("freeze_total_s")
        if ft and r.get("proc_wall_age_s"):
            r["reasons"].append("box frozen {:.0f} s ({:.0%}) since the worker started".format(
                ft, ft / r["proc_wall_age_s"]))
    if not new_fmt and r["verdict"] in ("normal", "host_freeze", "timestep_collapse") \
            and state is None and r["confidence"] == "high":
        r["confidence"] = "medium"      # legacy log, single call: no dsteps / no snapshot
    return r


def _yieldstep(ys):
    d = [b["t"] - a["t"] for a, b in zip(ys, ys[1:]) if b["t"] > a["t"]]
    return _med(d) or 0.0


def _start_wall(workdir, log):
    """Wall time the worker started: anuga_job.npz's mtime (written just before the launch),
    else the first PROGRESS utc, else the log's ctime."""
    job = os.path.join(workdir, "anuga_job.npz")
    try:
        lm = os.path.getmtime(log)
        if os.path.isfile(job) and os.path.getmtime(job) <= lm:
            return os.path.getmtime(job)
    except OSError:
        pass
    try:
        with open(log, encoding="utf-8", errors="replace") as f:
            for ln in f:
                m = re.search(r"utc=(\S+)", ln)
                if ln.startswith("PROGRESS") and m:
                    t = _utc_epoch(m.group(1))
                    if t:
                        return t
    except OSError:
        pass
    return None


# ----------------------------------------------------------------- output
_LINE_KEYS = ("last_t_s", "last_pct", "last_line_age_s", "expected_yield_wall_s",
              "freeze_total_s", "state_freeze_s", "window_cpu_s", "cpu_s", "cores_now",
              "cpu_s_per_model_s", "state_cpu_s_per_model_s", "dsteps_recent",
              "dsteps_baseline", "dsteps_trend", "model_s_per_wall_s", "awake_model_s_per_s",
              "pid", "log_format")


def line(r):
    out = ["VERDICT=" + r["verdict"], "confidence=" + r["confidence"]]
    for k in _LINE_KEYS:
        if r.get(k) is not None:
            v = r[k]
            out.append("{}={}".format(k, "{:.6g}".format(v) if isinstance(v, float) else v))
    lim = r.get("limiting") or {}
    for k in ("tri", "x", "y", "lon", "lat", "area_m2", "depth_m", "speed_mps", "near_op"):
        if lim.get(k):
            out.append("lim_{}={}".format(k, str(lim[k]).replace(" ", "_")))
    if r.get("reasons"):
        out.append('reason="{}"'.format("; ".join(r["reasons"]).replace('"', "'")))
    return " ".join(out)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog="exit codes: " + ", ".join(
                                     "{} {}".format(v, k) for k, v in sorted(
                                         EXIT.items(), key=lambda kv: kv[1])))
    ap.add_argument("run", help="run folder (or its anuga folder, or anuga.log)")
    ap.add_argument("--pid", type=int, help="the worker's pid (default: found by its folder)")
    ap.add_argument("--window", default=str(DEFAULT_WINDOW),
                    help="yield steps looked at (N) or minutes of log (Nm); default 48")
    ap.add_argument("--state", help="snapshot file: compared with, then rewritten")
    ap.add_argument("--sample-s", type=float, default=1.0,
                    help="seconds of CPU sampling (0 = none); default 1")
    ap.add_argument("--json", action="store_true", help="print the full result as JSON")
    ap.add_argument("--proc", default="/proc", help=argparse.SUPPRESS)   # tests
    a = ap.parse_args(argv)
    window = a.window if a.window.endswith("m") else int(a.window)
    prev = None
    if a.state and os.path.isfile(a.state):
        try:
            with open(a.state, encoding="utf-8") as f:
                prev = json.load(f)
        except (OSError, ValueError):
            prev = None
    r = check(a.run, pid=a.pid, window=window, state=prev, sample_s=a.sample_s, proc=a.proc)
    if a.state and r.get("snapshot"):
        tmp = a.state + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(r["snapshot"], f)
        os.replace(tmp, a.state)
    if a.json:
        print(json.dumps(r, indent=1, sort_keys=True, default=str))
    else:
        print(line(r))
    return EXIT[r["verdict"]]


if __name__ == "__main__":
    sys.exit(main())
