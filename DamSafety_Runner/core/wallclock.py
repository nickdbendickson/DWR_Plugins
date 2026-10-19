"""Wall-clock deadlines and a graceful stop for long subprocesses (Runner 3.10.24, released in 3.10.26).

Why: Python's subprocess timeouts (Popen.wait(timeout=...), subprocess.run(timeout=...)) run
on time.monotonic(). On a VM that is paused, snapshotted or forked, CLOCK_MONOTONIC (and
CLOCK_BOOTTIME) do not advance while the guest is stopped; the wall clock is stepped forward
on resume. On this box monotonic was ~32,000 s behind the wall clock since boot, and a 5 h
(18,099 s) HEC-RAS timeout set at 19:03:57Z fired at 04:43:06Z, after 34,749 s of wall time.

Deadline        elapsed = max(wall elapsed, monotonic elapsed):
                - a pause / suspend counts (the wall clock governs), so a timeout fires at a
                  predictable wall time;
                - a BACKWARDS wall-clock step can never make it fire early (it only lowers the
                  wall part) nor push it past the monotonic deadline (monotonic is the floor);
                - a forward wall-clock step looks the same as a pause and counts. Every time
                  wall and monotonic disagree by more than `margin_s` more than last reported,
                  that is logged (and kept in Deadline.events) so the run log says why.
wait()          polls a Popen until it exits or the Deadline expires (None).
stop_group()    SIGTERM to the process group, `grace_s` (NDBREACH_HECRAS_KILL_GRACE_S, default
                60 s) on the wall clock for it to exit, then SIGKILL. Returns what happened.
run()           subprocess.run(capture_output=True, text=True) with a wall-clock timeout and
                stop_group(); raises Timeout (a subprocess.TimeoutExpired) on expiry.

Every clock, sleep and kill function can be passed in (the tests use fakes)."""
import os
import signal
import subprocess
import tempfile
import time

ENV_GRACE = "NDBREACH_HECRAS_KILL_GRACE_S"
DEFAULT_GRACE_S = 60.0
JUMP_MARGIN_S = 60.0
AFTER_KILL_WAIT_S = 30.0


def grace_s(env=None, default=DEFAULT_GRACE_S):
    """NDBREACH_HECRAS_KILL_GRACE_S in seconds (>= 0), else `default`."""
    e = os.environ if env is None else env
    try:
        v = float(e.get(ENV_GRACE) or default)
    except (TypeError, ValueError):
        return float(default)
    return v if v >= 0 else float(default)


def _utc(t):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


class Deadline:
    """A timeout measured on the wall clock; elapsed() = max(wall, monotonic) (module doc)."""

    def __init__(self, timeout_s, what="", log=None, wall=time.time, mono=time.monotonic,
                 margin_s=JUMP_MARGIN_S):
        self.timeout_s = float(timeout_s)
        self.what = what
        self._log = log or (lambda m: None)
        self._wall, self._mono = wall, mono
        self.margin_s = float(margin_s)
        self.t0_wall, self.t0_mono = wall(), mono()
        self._reported_skew = 0.0
        self.events = []                      # [{wall_s, mono_s, skew_s, at}]
        self.last = {"wall_s": 0.0, "mono_s": 0.0}

    @property
    def deadline_wall(self):
        return self.t0_wall + self.timeout_s

    def elapsed(self):
        w = self._wall() - self.t0_wall
        m = self._mono() - self.t0_mono
        self.last = {"wall_s": w, "mono_s": m}
        skew = w - m
        if abs(skew - self._reported_skew) > self.margin_s:
            self._reported_skew = skew
            ev = {"wall_s": round(w, 1), "mono_s": round(m, 1), "skew_s": round(skew, 1),
                  "at": _utc(self.t0_wall + w)}
            self.events.append(ev)
            self._log(
                "WARNING: {}clock jump: {:.0f} s of wall time but {:.0f} s monotonic ({:+.0f} "
                "s; {}). The deadline is on the wall clock{}.".format(
                    (self.what + ": ") if self.what else "", w, m, skew,
                    "the machine was paused or the clock was stepped forward" if skew > 0 else
                    "the wall clock was stepped back",
                    "" if skew > 0 else " but never earlier than the monotonic one"))
        return max(w, m)

    def remaining(self):
        return self.timeout_s - self.elapsed()

    def expired(self):
        return self.remaining() <= 0.0

    def summary(self):
        """Dict for logs and reports (call after elapsed())."""
        return {"timeout_s": self.timeout_s, "started": _utc(self.t0_wall),
                "deadline": _utc(self.deadline_wall),
                "wall_elapsed_s": round(self.last["wall_s"], 1),
                "monotonic_elapsed_s": round(self.last["mono_s"], 1),
                "clock_jumps": list(self.events)}


def wait(proc, deadline, poll_s=1.0, sleep=time.sleep):
    """proc's return code, or None when `deadline` expired first (proc is left running)."""
    while True:
        rc = proc.poll()
        if rc is not None:
            return rc
        left = deadline.remaining()
        if left <= 0.0:
            return None
        sleep(max(0.01, min(poll_s, left)))


def _signal_group(proc, sig, killpg):
    try:
        killpg(proc.pid, sig)
        return "process group"
    except ProcessLookupError:
        return "gone"
    except (OSError, AttributeError):          # not a group leader / no killpg (Windows)
        try:
            (proc.kill if sig == getattr(signal, "SIGKILL", None) else proc.terminate)()
            return "process"
        except OSError:
            return "gone"


def stop_group(proc, grace=DEFAULT_GRACE_S, log=None, wall=time.time, mono=time.monotonic,
               sleep=time.sleep, killpg=getattr(os, "killpg", None), poll_s=0.5):
    """SIGTERM to proc's process group (start_new_session=True), up to `grace` s (wall clock)
    for it to exit, then SIGKILL. Returns {'sent': [..], 'exited_after': 'SIGTERM' |
    'SIGKILL' | None, 'returncode', 'term_wait_s'}. Never raises for a process that is gone."""
    log = log or (lambda m: None)
    if killpg is None:
        def killpg(pid, sig):
            raise AttributeError("os.killpg is not available here")
    out = {"sent": [], "exited_after": None, "returncode": None, "grace_s": float(grace),
           "term_wait_s": 0.0}
    term = getattr(signal, "SIGTERM", 15)
    kill = getattr(signal, "SIGKILL", 9)
    if proc.poll() is not None:
        out.update(exited_after="before SIGTERM", returncode=proc.returncode)
        return out
    out["sent"].append("SIGTERM to the " + _signal_group(proc, term, killpg))
    log("Stopping: SIGTERM sent; waiting up to {:.0f} s for it to exit.".format(grace))
    d = Deadline(grace, wall=wall, mono=mono)
    rc = wait(proc, d, poll_s=poll_s, sleep=sleep)
    d.elapsed()
    out["term_wait_s"] = round(d.last["wall_s"], 1)
    if rc is not None:
        out.update(exited_after="SIGTERM", returncode=rc)
        log("Stopped: it exited {:.0f} s after SIGTERM (code {}).".format(out["term_wait_s"], rc))
        return out
    out["sent"].append("SIGKILL to the " + _signal_group(proc, kill, killpg))
    log("Stopping: still running {:.0f} s after SIGTERM; SIGKILL sent.".format(grace))
    rc = wait(proc, Deadline(AFTER_KILL_WAIT_S, wall=wall, mono=mono), poll_s=poll_s,
              sleep=sleep)
    out.update(exited_after="SIGKILL" if rc is not None else None, returncode=rc)
    return out


class Timeout(subprocess.TimeoutExpired):
    """run() hit its wall-clock deadline. .stop is stop_group()'s result, .deadline the
    Deadline.summary()."""

    def __init__(self, cmd, timeout, output=None, stop=None, deadline=None):
        super().__init__(cmd, timeout, output=output)
        self.stop, self.deadline = stop or {}, deadline or {}


def run(cmd, cwd=None, env=None, timeout=None, grace=DEFAULT_GRACE_S, log=None, poll_s=0.5,
        what=""):
    """subprocess.run(cmd, capture_output=True, text=True, errors='replace') with a wall-clock
    timeout; on expiry the process group gets stop_group() and Timeout is raised. Output goes
    through temporary files (no pipe can fill up). OSError when it cannot start."""
    with tempfile.TemporaryFile() as o, tempfile.TemporaryFile() as e:
        p = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=o, stderr=e,
                             start_new_session=(os.name == "posix"))
        rc = None
        if timeout is None:
            rc = p.wait()
        else:
            dl = Deadline(timeout, what=what, log=log)
            rc = wait(p, dl, poll_s=poll_s)
            if rc is None:
                dl.elapsed()
                stop = stop_group(p, grace, log=log)
                o.seek(0)
                raise Timeout(cmd, timeout, output=o.read().decode("utf-8", "replace"),
                              stop=stop, deadline=dl.summary())

        def text(f):
            f.seek(0)
            return f.read().decode("utf-8", "replace")
        return subprocess.CompletedProcess(cmd, rc, text(o), text(e))
