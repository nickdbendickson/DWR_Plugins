"""Runner 3.12.2: the ANUGA worker runs detached from QGIS (DWR-2801: the GUI run died twice
when desktop :2 was shut down under it).

Before 3.12.2 the worker's stdout was a pipe that a QGIS thread read and copied into
anuga.log. When QGIS died, the worker's next PROGRESS print hit a broken pipe and the run
ended (the SIGTERM partial flush never ran). Now:

- launch()        the worker writes its own log: stdout and stderr go straight to
                  <workdir>/anuga.log, stdin is /dev/null, and it starts in a new session
                  (POSIX setsid: no SIGHUP / terminal signal from the QGIS session) or, on
                  Windows, in a new process group without a console, breaking away from a job
                  object when allowed. QGIS only tails the log (LogTail) for PROGRESS lines.
- worker_entry()  the worker's __main__: ignores SIGPIPE and SIGHUP, wraps stdout / stderr so a
                  failed write never kills the run, turns SIGTERM into SystemExit (the
                  checkpoint code's own handler still flushes anuga_partial.npz) and keeps the
                  status file <workdir>/anuga_worker.json (pid, job, result, log, state
                  running / finished / failed / stopped, start and end times).
- worker_state()  what a later QGIS session finds in a workdir: none, running (the pid is
                  alive and is the worker), finished (result written), failed, stopped, or died
                  (the status says running but the pid is gone).
- Attached        a Popen-like handle on a worker this session did not start (poll /
                  terminate / kill / wait by pid), so anuga_model can wait for it like its own.

stdlib only: the worker imports it from the ANUGA environment, QGIS from its own."""
import json
import os
import signal
import socket
import subprocess
import sys
import time

STATUS_NAME = "anuga_worker.json"
LOG_NAME = "anuga.log"
# set by the launcher: the status file to write and the launch's id (a worker started by hand
# writes <folder of the result>/anuga_worker.json)
ENV_STATUS = "NDBREACH_ANUGA_STATUS"
ENV_LAUNCH = "NDBREACH_ANUGA_LAUNCH_ID"
ENV_TAG = "NDBREACH_ANUGA_TAG"            # JSON the launcher wants kept in the status (buffer_m)

RUNNING, FINISHED, FAILED, STOPPED, DIED, NONE = (
    "running", "finished", "failed", "stopped", "died", "none")

# Windows process creation flags
CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_BREAKAWAY_FROM_JOB = 0x01000000
STILL_ACTIVE = 259


def utc_now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# --- status file --------------------------------------------------------------------------
def status_path(workdir):
    return os.path.join(workdir, STATUS_NAME)


def write_json_atomic(path, obj):
    """Write to <path>.<pid>.tmp, then rename over `path` (readers never see half a file)."""
    tmp = "{}.{}.tmp".format(path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=1, default=str)
        f.flush()
        try:
            os.fsync(f.fileno())
        except OSError:
            pass
    os.replace(tmp, path)


def read_status(workdir_or_path):
    """The status dict, or None (no file, or unreadable)."""
    p = workdir_or_path
    if os.path.isdir(p):
        p = status_path(p)
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


def update_status(path, **kw):
    """Merge `kw` into the status file (best effort: never raises)."""
    try:
        d = read_status(path) or {}
        d.update(kw)
        write_json_atomic(path, d)
        return d
    except Exception:                                    # noqa: BLE001 - never stop a run
        return None


def create_status(path, **kw):
    """The launcher's first record: written only when the worker has not written its own yet
    (O_EXCL), so a fast worker's record is never overwritten. True when written."""
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except OSError:
        return False
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(kw, f, indent=1, default=str)
        return True
    except Exception:                                    # noqa: BLE001
        return False


# --- is a pid still the worker? --------------------------------------------------------------
def pid_alive(pid):
    """True while process `pid` exists (and, on Linux, is not a zombie). Never signals it:
    on Windows os.kill(pid, 0) would TerminateProcess, so OpenProcess is used there."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name == "nt":
        return _win_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:          # exists, owned by someone else
        return True
    except OSError:
        return False
    try:                             # a zombie (exited, not reaped yet) is not running
        with open("/proc/{}/stat".format(pid)) as f:
            if f.read().rsplit(")", 1)[-1].split()[0] == "Z":
                return False
    except (OSError, IndexError):
        pass
    return True


def _win_alive(pid):
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(0x1000, False, pid)          # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        try:
            code = ctypes.c_ulong()
            if not k32.GetExitCodeProcess(h, ctypes.byref(code)):
                return False
            return code.value == STILL_ACTIVE
        finally:
            k32.CloseHandle(h)
    except Exception:                                    # noqa: BLE001
        return False


def _cmdline(pid):
    try:
        with open("/proc/{}/cmdline".format(int(pid)), "rb") as f:
            return f.read().replace(b"\0", b" ").decode("utf-8", "replace")
    except (OSError, ValueError):
        return None


def is_worker(status):
    """True while the status's pid is alive and (where /proc shows it) still runs the worker's
    script - a recycled pid of another program is not the worker."""
    if not status:
        return False
    pid = status.get("pid")
    if not pid_alive(pid):
        return False
    if status.get("host") and status.get("host") != socket.gethostname():
        return True                  # another computer's worker: cannot tell, assume alive
    cmd = _cmdline(pid)
    script = os.path.basename(str(status.get("script") or ""))
    if cmd is not None and script:
        return script in cmd
    return True


def worker_state(workdir, result=None):
    """{"state": none | running | finished | failed | stopped | died, "status": dict or None,
    "result": path, "have_result": bool, "processed": bool}."""
    st = read_status(workdir)
    res = result or (st or {}).get("result") or ""
    if res and not os.path.isabs(res):
        res = os.path.join(workdir, res)
    have = bool(res) and os.path.isfile(res)
    out = {"state": NONE, "status": st, "result": res, "have_result": have,
           "processed": bool((st or {}).get("processed_utc")), "workdir": workdir}
    if not st:
        return out
    s = st.get("state")
    if s == RUNNING:
        out["state"] = RUNNING if is_worker(st) else (FINISHED if have and _fresh(st, res)
                                                      else DIED)
    elif s == FINISHED:
        out["state"] = FINISHED if have else DIED
    elif s in (FAILED, STOPPED):
        out["state"] = s
    else:
        out["state"] = DIED
    return out


def _fresh(st, res):
    """The result was written after this launch started (a died-at-the-end worker)."""
    try:
        return os.path.getmtime(res) >= float(st.get("started_epoch") or 0) - 1
    except (OSError, ValueError, TypeError):
        return False


def pending(workdir):
    """A worker whose run the Runner has not built products from yet: running, or finished
    and not marked processed. None, or worker_state()'s dict."""
    w = worker_state(workdir)
    if w["state"] == RUNNING or (w["state"] == FINISHED and not w["processed"]):
        return w
    return None


# --- launching ------------------------------------------------------------------------------
def popen_flags():
    """Popen keyword arguments that detach the worker from the launching session."""
    if os.name == "nt":
        return {"creationflags": CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
                | CREATE_BREAKAWAY_FROM_JOB}
    return {"start_new_session": True}


def launch(cmd, workdir, env, log_name=LOG_NAME, tag=None, script=None):
    """Start `cmd` detached, its stdout + stderr appended to <workdir>/<log_name> (created
    fresh). Returns (Popen, log path, status path)."""
    log_path = os.path.join(workdir, log_name)
    spath = status_path(workdir)
    for p in (spath,):
        try:
            os.remove(p)
        except OSError:
            pass
    launch_id = "{}-{}".format(os.getpid(), int(time.time() * 1000))
    env = dict(env)
    env[ENV_STATUS] = spath
    env[ENV_LAUNCH] = launch_id
    env.setdefault("PYTHONUNBUFFERED", "1")          # a file is block-buffered: keep PROGRESS live
    if tag:
        env[ENV_TAG] = json.dumps(tag, default=str)
    kw = popen_flags()
    with open(log_path, "wb") as logf:               # the child gets its own copy of the fd
        try:
            proc = subprocess.Popen(cmd, cwd=workdir, env=env, stdin=subprocess.DEVNULL,
                                    stdout=logf, stderr=subprocess.STDOUT, **kw)
        except OSError:
            if os.name != "nt":
                raise
            # a job object that forbids breakaway: start inside it (dies with QGIS's job)
            kw["creationflags"] &= ~CREATE_BREAKAWAY_FROM_JOB
            proc = subprocess.Popen(cmd, cwd=workdir, env=env, stdin=subprocess.DEVNULL,
                                    stdout=logf, stderr=subprocess.STDOUT, **kw)
    create_status(spath, state=RUNNING, pid=proc.pid, launch_id=launch_id, log=log_path,
                  script=os.path.basename(script or (cmd[2] if len(cmd) > 2 else cmd[-1])),
                  job=cmd[-2] if len(cmd) >= 2 else "", result=cmd[-1],
                  started_utc=utc_now(), started_epoch=time.time(),
                  host=socket.gethostname(), launcher_pid=os.getpid(),
                  tag=tag or {}, written_by="launcher")
    return proc, log_path, spath


def finish_status(spath, proc_pid, returncode, launch_id=None):
    """After the worker exited: when its status still says running (it was killed, or it is
    not our worker script - the tests' fakes), record the exit code."""
    st = read_status(spath)
    if not st or st.get("state") != RUNNING or int(st.get("pid") or -1) != int(proc_pid):
        return st
    if launch_id and st.get("launch_id") not in (None, launch_id):
        return st
    state = FINISHED if returncode == 0 and os.path.isfile(str(st.get("result") or "")) \
        else STOPPED if returncode is not None and returncode < 0 else FAILED
    return update_status(spath, state=state, returncode=returncode, ended_utc=utc_now(),
                         ended_by="launcher")


def mark_processed(workdir):
    """The Runner built its products from this result: no longer 'pending'."""
    if read_status(workdir):
        update_status(status_path(workdir), processed_utc=utc_now())


# --- watching ---------------------------------------------------------------------------------
class LogTail:
    """New complete lines of a growing text file (a partial last line waits for its end)."""

    def __init__(self, path, start_at_end=False):
        self.path = path
        self.pos = 0
        self.buf = b""
        if start_at_end:
            try:
                self.pos = os.path.getsize(path)
            except OSError:
                pass

    def lines(self, final=False):
        try:
            with open(self.path, "rb") as f:
                f.seek(self.pos)
                data = f.read()
                self.pos = f.tell()
        except OSError:
            data = b""
        self.buf += data
        *done, self.buf = self.buf.split(b"\n")
        if final and self.buf:
            done.append(self.buf)
            self.buf = b""
        return [d.decode("utf-8", "replace").rstrip("\r") + "\n" for d in done]


def parse_progress(line):
    """(pct, message) of a 'PROGRESS <pct> <msg>' line, else None."""
    if not line.startswith("PROGRESS"):
        return None
    parts = line.split(None, 2)
    try:
        return float(parts[1]), (parts[2].strip() if len(parts) > 2 else "")
    except (IndexError, ValueError):
        return None


def last_progress(log_path):
    """The last PROGRESS (pct, message) in a log, or None."""
    last = None
    for ln in LogTail(log_path).lines(final=True):
        p = parse_progress(ln)
        if p is not None:
            last = p
    return last


class Attached:
    """Popen-like handle on a worker started by an earlier session (by its pid)."""

    def __init__(self, status, spath):
        self.pid = int(status["pid"])
        self.status = status
        self.spath = spath
        self.returncode = None

    def poll(self):
        if self.returncode is not None:
            return self.returncode
        if is_worker(self.status):
            return None
        st = read_status(self.spath) or {}
        s = st.get("state")
        self.returncode = (0 if s == FINISHED else -15 if s == STOPPED
                           else int(st.get("returncode") or 1) if s == FAILED else 1)
        if s == RUNNING and os.path.isfile(str(st.get("result") or "")) and _fresh(
                st, st.get("result")):
            self.returncode = 0
        return self.returncode

    def _signal(self, sig):
        if self.poll() is not None:
            return
        try:
            os.kill(self.pid, sig)
        except OSError:
            pass

    def terminate(self):
        self._signal(signal.SIGTERM)

    def kill(self):
        self._signal(getattr(signal, "SIGKILL", signal.SIGTERM))

    def wait(self, timeout=None):
        t0 = time.time()
        while self.poll() is None:
            if timeout is not None and time.time() - t0 > timeout:
                raise subprocess.TimeoutExpired("anuga worker {}".format(self.pid), timeout)
            time.sleep(0.2)
        return self.returncode


# --- the worker's side ----------------------------------------------------------------------
class SafeStream:
    """A text stream whose write / flush never raise (a closed pipe, a full disk, a log that
    went away): the run goes on, the output is lost."""

    def __init__(self, stream):
        self._s = stream
        self.broken = False

    def write(self, s):
        if self.broken or self._s is None:
            return len(s)
        try:
            return self._s.write(s)
        except (OSError, ValueError):
            self.broken = True
            return len(s)

    def flush(self):
        if self.broken or self._s is None:
            return
        try:
            self._s.flush()
        except (OSError, ValueError):
            self.broken = True

    def __getattr__(self, name):
        return getattr(self._s, name)


def harden_process():
    """Ignore SIGPIPE / SIGHUP and make stdout / stderr failure-proof. SIGTERM -> SystemExit
    (so finally blocks and the partial flush run; the checkpoint loop installs its own)."""
    for name in ("SIGPIPE", "SIGHUP"):
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                signal.signal(sig, signal.SIG_IGN)
            except (ValueError, OSError):
                pass

    def _term(signum, frame):
        raise SystemExit(128 + signum)
    try:
        signal.signal(signal.SIGTERM, _term)
    except (ValueError, OSError, AttributeError):
        pass
    if not isinstance(sys.stdout, SafeStream):
        sys.stdout = SafeStream(sys.stdout)
    if not isinstance(sys.stderr, SafeStream):
        sys.stderr = SafeStream(sys.stderr)


def worker_entry(main, job, out):
    """The worker's __main__: harden, record the status, run main(job, out), record the end.
    Exit codes: 0 finished, 1 failed, 128 + signal when stopped."""
    harden_process()
    out_abs = os.path.abspath(out)
    spath = os.environ.get(ENV_STATUS) or status_path(os.path.dirname(out_abs))
    try:
        tag = json.loads(os.environ.get(ENV_TAG) or "{}")
    except ValueError:
        tag = {}
    base = dict(read_status(spath) or {})
    if base.get("launch_id") not in (None, os.environ.get(ENV_LAUNCH)):
        base = {}
    base.update(state=RUNNING, pid=os.getpid(), launch_id=os.environ.get(ENV_LAUNCH),
                job=os.path.abspath(job), result=out_abs,
                script=os.path.basename(sys.argv[0] or "anuga_worker.py"),
                started_utc=base.get("started_utc") or utc_now(),
                started_epoch=base.get("started_epoch") or time.time(),
                host=socket.gethostname(), python=sys.executable, tag=tag,
                written_by="worker")
    base.setdefault("log", os.path.join(os.path.dirname(out_abs), LOG_NAME))
    try:
        write_json_atomic(spath, base)
    except Exception:                                    # noqa: BLE001 - run anyway
        pass
    code = 0
    try:
        main(job, out)
        update_status(spath, state=FINISHED, returncode=0, ended_utc=utc_now())
    except SystemExit as e:
        c = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
        code = c
        state = FINISHED if c == 0 and os.path.isfile(out_abs) else STOPPED if c >= 128 \
            else FAILED
        partial = os.path.join(os.path.dirname(out_abs), "anuga_partial.npz")
        update_status(spath, state=state, returncode=c, ended_utc=utc_now(),
                      partial=partial if os.path.isfile(partial) else None)
    except KeyboardInterrupt:
        code = 130
        update_status(spath, state=STOPPED, returncode=130, ended_utc=utc_now())
    except BaseException as e:                           # noqa: BLE001
        import traceback
        try:
            traceback.print_exc()
        except Exception:                                # noqa: BLE001
            pass
        code = 1
        update_status(spath, state=FAILED, returncode=1, ended_utc=utc_now(),
                      error="{}: {}".format(type(e).__name__, e)[:2000])
    try:
        sys.stdout.flush()
    except Exception:                                    # noqa: BLE001
        pass
    # never die on a closed stdout at interpreter exit (exit code 120)
    try:
        sys.stdout = SafeStream(None)
        sys.stderr = SafeStream(None)
    except Exception:                                    # noqa: BLE001
        pass
    return code


def main_cli(argv=None):
    """python -m ... anuga_detach STATUS <workdir>: print the worker status (for the CLI)."""
    argv = sys.argv[1:] if argv is None else argv
    for wd in argv:
        w = worker_state(wd)
        lp = (w["status"] or {}).get("log") or os.path.join(wd, LOG_NAME)
        print(json.dumps({"workdir": wd, "state": w["state"], "processed": w["processed"],
                          "result": w["result"], "have_result": w["have_result"],
                          "pid": (w["status"] or {}).get("pid"),
                          "last_progress": last_progress(lp)}, default=str))
    return 0
