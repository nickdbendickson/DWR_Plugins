"""Runner 3.20.0: several dams of a batch at the same time.

A batch runs one dam after another, and LISFLOOD-FP gains little from more threads on one dam
(its OpenMP scaling is poor on these grids). Running the dams side by side, each in its own
child process with a share of the cores, uses the computer far better. The children are the
same tool run for ONE dam: `qgis_process run ndbreach:batch` from QGIS, `ndbreach_cli.py batch
--only-row N` without QGIS. Each child writes its own one-row summary; the parent merges them
in the order of the dams and writes the usual Batch_Breach_Runs.csv.

Nothing here imports QGIS (the two lookups that need it import it inside the function), so the
command line and the tests use it as it is."""
import csv
import json
import os
import shutil
import signal
import subprocess
import sys
import time

ENV_QGIS_PROCESS = "NDBREACH_QGIS_PROCESS"
MAX_WORKERS = 16
WORK_FOLDER = "_batch_workers"          # inside the Dams folder; "_" = not a dam folder
KILL_GRACE_S = 5.0
POLL_S = 0.5
PROBE_TIMEOUT_S = 300.0


class WorkersUnavailable(Exception):
    """Workers cannot be used here; the text says why. The batch then runs one dam after
    another, as before."""


def clamp_workers(n):
    """1 .. MAX_WORKERS (anything unreadable is 1)."""
    try:
        return max(1, min(MAX_WORKERS, int(n)))
    except (TypeError, ValueError, OverflowError):
        return 1


def cpu_count():
    """The CPUs this process may use (an affinity mask counts), at least 1."""
    try:
        return max(1, len(os.sched_getaffinity(0)))
    except (AttributeError, OSError):
        return max(1, os.cpu_count() or 1)


def plan(workers, n_dams, threads=0, cpus=None):
    """(workers used, threads for each child, text). No more workers than dams to run or
    CPUs. `threads` > 0 is the user's own count for each child and is kept; otherwise each
    child gets an equal share of the CPUs, at least one."""
    cpus = cpus or cpu_count()
    want = clamp_workers(workers)
    used = max(1, min(want, max(1, int(n_dams)), cpus))
    each = int(threads) if threads and int(threads) > 0 else max(1, cpus // used)
    text = "{} dam(s) at a time, {} thread(s) each ({} CPUs)".format(used, each, cpus)
    if used < want:
        text += "; {} were asked for".format(want)
    if each * used > cpus:
        text += "; that is more threads than CPUs, the runs will slow each other down"
    return used, each, text


def work_folder(dams_folder):
    path = os.path.join(dams_folder, WORK_FOLDER)
    os.makedirs(path, exist_ok=True)
    return path


def job_paths(work, index, safe_id):
    """(summary csv, log) of one child. The index keeps two dams whose IDs clean to the same
    file name apart."""
    stem = os.path.join(work, "{:04d}_{}".format(index, safe_id))
    return stem + ".csv", stem + ".log"


# ------------------------------------------------------------------ qgis_process --
def _qgis_process_names():
    if os.name == "nt":
        return ["qgis_process-qgis-ltr.bat", "qgis_process-qgis.bat", "qgis_process.bat",
                "qgis_process.exe"]
    return ["qgis_process"]


def _is_exe(path):
    return bool(path) and os.path.isfile(path) and os.access(path, os.X_OK)


def find_qgis_process(prefixes=None, env=None):
    """(path, '') or (None, why not). Looked for in: NDBREACH_QGIS_PROCESS (a full path), the
    QGIS install folders (`prefixes`, else asked from QgsApplication), the folder of the
    running program, PATH."""
    env = os.environ if env is None else env
    note = ""
    given = str(env.get(ENV_QGIS_PROCESS) or "").strip().strip('"')
    if given:
        if _is_exe(given):
            return given, ""
        note = "{} points to {}, which is not a program; ".format(ENV_QGIS_PROCESS, given)
    if prefixes is None:
        prefixes = []
        try:
            from qgis.core import QgsApplication
            for base in (QgsApplication.prefixPath(),
                         os.path.join(QgsApplication.pkgDataPath(), os.pardir, os.pardir)):
                if base:
                    prefixes.append(os.path.normpath(str(base)))
        except Exception:                                         # noqa: BLE001
            pass
    dirs = []
    for base in prefixes:
        dirs += [os.path.join(base, "bin"), base,
                 os.path.join(base, os.pardir, os.pardir, "bin")]
    exe_dir = os.path.dirname(sys.executable or "")
    if exe_dir:
        dirs.append(exe_dir)
    for d in dirs:
        for name in _qgis_process_names():
            path = os.path.normpath(os.path.join(d, name))
            if _is_exe(path):
                return path, ""
    for name in _qgis_process_names():
        path = shutil.which(name)
        if path:
            return path, ""
    return None, note + "qgis_process was not found (set {} to its full path)".format(
        ENV_QGIS_PROCESS)


def _ini_default_profile(root):
    """The default profile profiles.ini names under the configuration folder `root` ('' when
    it names none)."""
    ini = os.path.join(root, "profiles", "profiles.ini")
    try:
        with open(ini, encoding="utf-8", errors="replace") as f:
            for line in f:
                key, _eq, val = line.partition("=")
                if key.strip() == "defaultProfile" and val.strip():
                    return val.strip()
    except OSError:
        pass
    return ""


def _link_folder(link, target):
    """A folder link: a symlink, on Windows a junction (no administrator rights needed)."""
    if os.name == "nt":
        r = subprocess.run(["cmd", "/c", "mklink", "/J", link, target],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
        if r.returncode != 0:
            raise OSError("mklink /J failed")
    else:
        os.symlink(target, link, target_is_directory=True)


def profile_env(env, settings_dir, work, link=None):
    """The children's environment so they open the SAME QGIS profile as this session (its
    plugins and settings). qgis_process has no profile option: QGIS 3.34 opens the profile
    called 'default' of its configuration folder whatever profiles.ini says. When this
    session's profile is that one nothing more is needed; otherwise a small configuration
    folder is made in `work` whose 'default' profile is a link to this session's profile, and
    QGIS_CUSTOM_CONFIG_PATH points the children at it. `settings_dir` is
    QgsApplication.qgisSettingsDirPath(). Raises WorkersUnavailable."""
    env = dict(env)
    prof = os.path.normpath(str(settings_dir or ""))
    parent = os.path.dirname(prof)
    if not prof or os.path.basename(parent) != "profiles":
        return env, ""                    # not a profile folder (a script, a test): inherit
    root = os.path.dirname(parent)
    # 'default', and no profiles.ini that a later QGIS might follow to another profile
    if os.path.basename(prof) == "default" and _ini_default_profile(root) in ("", "default"):
        env["QGIS_CUSTOM_CONFIG_PATH"] = root
        return env, ""
    cfg = os.path.join(work, "qgis_config")
    target = os.path.join(cfg, "profiles", "default")
    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        if os.path.lexists(target):
            if os.path.realpath(target) != os.path.realpath(prof):
                try:
                    os.remove(target)
                except OSError:
                    os.rmdir(target)              # a junction
                (link or _link_folder)(target, prof)
        else:
            (link or _link_folder)(target, prof)
    except (OSError, subprocess.SubprocessError) as e:
        raise WorkersUnavailable(
            "the QGIS profile '{}' could not be shared with the worker processes ({})".format(
                os.path.basename(prof), e))
    env["QGIS_CUSTOM_CONFIG_PATH"] = cfg
    return env, "workers use this session's QGIS profile '{}'".format(os.path.basename(prof))


def probe(exe, alg_id, env, run=None, timeout_s=PROBE_TIMEOUT_S):
    """Raises WorkersUnavailable unless `qgis_process help <alg_id>` works in the children's
    environment (the plugin is installed and enabled in that profile)."""
    run = run or subprocess.run
    try:
        r = run([exe, "help", alg_id], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, env=env, timeout=timeout_s)
    except (OSError, subprocess.SubprocessError) as e:
        raise WorkersUnavailable("{} could not be started ({})".format(exe, e))
    if r.returncode != 0:
        raise WorkersUnavailable(
            "qgis_process does not offer {} (is the plugin enabled for qgis_process? "
            "'qgis_process plugins' lists them)".format(alg_id))


# ------------------------------------------------------------------- the children --
def tail(path, lines=4, chars=400):
    """The last lines of a text file on one line ('' when unreadable)."""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - 8192))
            text = fh.read().decode("utf-8", "replace")
    except OSError:
        return ""
    got = [ln.strip() for ln in text.splitlines() if ln.strip()][-lines:]
    out = " | ".join(got)
    return out[-chars:] if len(out) > chars else out


def _terminate(proc, grace=KILL_GRACE_S, sleep=time.sleep, clock=time.time):
    """End a child and what it started (the OSGeo4W .bat starts the real program; the Runner
    starts LISFLOOD-FP): terminate, then kill after `grace` seconds."""
    real = type(proc).__module__ == "subprocess"        # never signal a test double's pid
    try:
        if real and os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
        elif real:
            os.killpg(proc.pid, signal.SIGTERM)          # started in its own session
    except Exception:                                    # noqa: BLE001
        pass
    try:
        proc.terminate()
    except Exception:                                    # noqa: BLE001
        pass
    t0 = clock()
    while proc.poll() is None and clock() - t0 < grace:
        sleep(0.05)
    if proc.poll() is None:
        try:
            if real and os.name != "nt":
                os.killpg(proc.pid, signal.SIGKILL)
        except Exception:                                # noqa: BLE001
            pass
        try:
            proc.kill()
        except Exception:                                # noqa: BLE001
            pass


CANCELLED, TIMED_OUT, NOT_STARTED = "cancelled", "timed out", "not started"


def run_children(jobs, workers, log=None, progress=None, cancelled=None, timeout_s=0,
                 env=None, popen=None, sleep=time.sleep, clock=time.time, poll_s=POLL_S):
    """Run `jobs` as child processes, at most `workers` at a time, and wait for them.

    A job is {"key", "name", "cmd", "log" (a file path for its output)[, "stdin" (bytes)]}.
    Returns {key: (code, text)}: the child's exit code, or CANCELLED / TIMED_OUT /
    NOT_STARTED with the reason. A job never started because the batch was cancelled is not
    in the result. Every child still running is ended before this returns or raises."""
    popen = popen or subprocess.Popen
    workers = max(1, int(workers))
    pending, running, results = list(jobs), [], {}
    total = len(pending)
    kw = {"start_new_session": True} if os.name != "nt" else {
        "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)}

    def close(job):
        for key in ("out", "inp"):
            try:
                job[key].close()
            except Exception:                            # noqa: BLE001
                pass

    def end(job, code, text):
        close(job)
        running.remove(job)
        results[job["key"]] = (code, text)
        if log:
            mins = (clock() - job["t0"]) / 60.0
            log("{} ({}/{}): {} after {:.1f} min".format(
                job["name"], len(results), total,
                "finished" if code == 0 else text or "ended with code {}".format(code), mins))
        if progress:
            progress(100.0 * len(results) / max(1, total))

    try:
        while pending or running:
            if cancelled and cancelled():
                for job in list(running):
                    _terminate(job["proc"], sleep=sleep, clock=clock)
                    end(job, CANCELLED, "cancelled")
                break
            while pending and len(running) < workers:
                job = dict(pending.pop(0))
                job["t0"] = clock()
                running.append(job)
                try:
                    job["out"] = open(job["log"], "wb")
                    data = job.get("stdin")
                    if data is not None:
                        # a file, not a pipe: nothing to feed while the child runs
                        inp = job["log"] + ".in"
                        with open(inp, "wb") as f:
                            f.write(data)
                        job["inp"] = open(inp, "rb")
                    job["proc"] = popen(job["cmd"], stdin=job.get("inp", subprocess.DEVNULL),
                                        stdout=job["out"], stderr=subprocess.STDOUT,
                                        env=env, **kw)
                except OSError as e:
                    end(job, NOT_STARTED, "the worker process could not be started: {}".format(e))
                    continue
                if log:
                    log("{}: started ({} running)".format(job["name"], len(running)))
            sleep(poll_s)
            for job in list(running):
                code = job["proc"].poll()
                if code is None:
                    if timeout_s and clock() - job["t0"] > timeout_s:
                        _terminate(job["proc"], sleep=sleep, clock=clock)
                        end(job, TIMED_OUT, "stopped after {:.0f} min (the worker time "
                                            "limit)".format(timeout_s / 60.0))
                    continue
                end(job, code, "" if code == 0 else "ended with code {}: {}".format(
                    code, tail(job["log"]) or "no output"))
    finally:
        for job in list(running):                 # an error: leave nothing running
            _terminate(job["proc"], sleep=sleep, clock=clock)
            close(job)
    return results


def read_row(path, dam_id=None):
    """The child's row for `dam_id` from its summary CSV (the first row when there is one
    row), or None."""
    try:
        with open(path, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
    except (OSError, csv.Error, UnicodeDecodeError):
        return None
    for r in rows:
        if dam_id is None or str(r.get("Dam", "")).strip() == str(dam_id).strip():
            return r
    return rows[0] if len(rows) == 1 and dam_id is None else None


def fill_row(row, fields, result, child_csv, log_path):
    """Put a child's outcome into the parent's summary row (a dict with `fields` keys).
    The child's own row is used whenever it wrote one; otherwise the row says the worker
    failed and where its log is."""
    code, text = result if result else (NOT_STARTED, "not run")
    got = read_row(child_csv, row.get("Dam"))
    if got and got.get("Status") and code not in (CANCELLED, TIMED_OUT):
        for k in fields:
            if k in got and k not in ("Dam", "Folder"):
                row[k] = got[k]
        return row
    if code == CANCELLED:
        row["Status"] = "cancelled"
        return row
    row["Status"] = "failed"
    why = text or "the worker wrote no result"
    row["Message"] = "{} (worker log: {})".format(why, log_path)[:500]
    return row


def stdin_json(params):
    """The JSON `qgis_process run <id> -` reads."""
    return json.dumps({"inputs": params}, default=str).encode("utf-8")
