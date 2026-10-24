"""Runner 3.10.68: HEC-RAS 6.6 on Windows, run from the batch chain (and nothing else yet).

NOT TESTED ON WINDOWS YET (the tests mock every program). The chain only uses it on Windows
when Ras.exe is found; when a step fails, the chain falls back to waiting for a manual run
(RasMapper), so a dam is never lost to it.

The package written by ndbreach:hecrasexport has no terrain HDF: HEC-RAS needs the terrain made
once (RAS Mapper > Create a New RAS Terrain from Terrain/<name>.tif). run_plan() checks for it
first and raises TerrainMissing (-> the chain waits for the terrain or the results).

Order, each checked by its output (a plan .p##.hdf with unsteady results):
  1. headless:  Ras.exe -c "<project>.prj" "<project>.p##"   (HEC-RAS computes the plan,
                geometry preprocessing included, and exits);
     then, when Ras.exe left the run inputs (.b## .x## .p##.tmp.hdf) but no results:
                RasUnsteady.exe "<project>.p##.tmp.hdf" x##   in the project folder, and the
                .tmp.hdf becomes <project>.p##.hdf
  2. COM:       the HEC-RAS Controller ('RAS66.HECRASController', pywin32's win32com):
                Project_Open, Plan_SetCurrent(<plan title>), Compute_CurrentPlan(blocking),
                then Project_Close / QuitRas.

Settings (environment):
    NDBREACH_HECRAS_WINDOWS_DIR   the HEC-RAS 6.6 folder (default: the usual install folders)
    NDBREACH_HECRAS_TIMEOUT_S     wall-clock limit per program in s (unset/0 = none)
    NDBREACH_HECRAS_WIN_METHODS   'headless,com' (default), 'headless' or 'com'
"""
import glob
import os
import re
import shutil
import subprocess
import sys
import time

ENV_WINDIR = "NDBREACH_HECRAS_WINDOWS_DIR"
ENV_TIMEOUT = "NDBREACH_HECRAS_TIMEOUT_S"
ENV_METHODS = "NDBREACH_HECRAS_WIN_METHODS"
PROG_ID = "RAS66.HECRASController"
DEFAULT_DIRS = (r"C:\Program Files (x86)\HEC\HEC-RAS\6.6",
                r"C:\Program Files\HEC\HEC-RAS\6.6")
METHODS = ("headless", "com")


class WindowsRunError(RuntimeError):
    """A launcher step failed; .attempts lists what was tried."""

    def __init__(self, msg, attempts=None):
        RuntimeError.__init__(self, msg)
        self.attempts = list(attempts or [])


class TerrainMissing(WindowsRunError):
    pass


def _env(env):
    return os.environ if env is None else env


def _com_available():
    try:
        import win32com.client  # noqa: F401
        return True
    except Exception:                                            # noqa - not Windows / no pywin32
        return False


def detect(env=None, platform=None, isfile=os.path.isfile, com=None):
    """{available, platform, windows_dir, ras_exe, rasunsteady, com, reason}. Available on
    Windows when Ras.exe is found (the COM path needs pywin32 as well)."""
    e = _env(env)
    plat = platform or sys.platform
    out = {"available": False, "platform": plat, "windows_dir": "", "ras_exe": "",
           "rasunsteady": "", "com": False, "reason": ""}
    if not plat.startswith("win"):
        out["reason"] = "the Windows HEC-RAS launcher runs on Windows only"
        return out
    out["com"] = _com_available() if com is None else bool(com)
    cands = [(e.get(ENV_WINDIR) or "").strip()] + list(DEFAULT_DIRS)
    for d in [c for c in cands if c]:
        if isfile(os.path.join(d, "Ras.exe")):
            out["windows_dir"], out["ras_exe"] = d, os.path.join(d, "Ras.exe")
            for sub in ("", "x64"):
                p = os.path.join(d, sub, "RasUnsteady.exe") if sub else \
                    os.path.join(d, "RasUnsteady.exe")
                if isfile(p):
                    out["rasunsteady"] = p
                    break
            break
    if not out["ras_exe"]:
        out["reason"] = ("Ras.exe (HEC-RAS 6.6) not found in {}; set {} to the HEC-RAS folder"
                         .format(" or ".join(c for c in cands if c), ENV_WINDIR))
        return out
    out["available"] = True
    return out


def methods(env=None, log=None):
    raw = (_env(env).get(ENV_METHODS) or "").strip().lower()
    if not raw:
        return METHODS
    got = tuple(m.strip() for m in raw.split(",") if m.strip() in METHODS)
    if not got and log:                              # set but unrecognized: warn, keep default
        log("{}={!r} names no recognized method (expected {}); using {}".format(
            ENV_METHODS, raw, "/".join(METHODS), "/".join(METHODS)))
    return got or METHODS


def timeout_s(env=None, log=None):
    raw = (_env(env).get(ENV_TIMEOUT) or "").strip()
    if not raw:
        return None                                  # unset: no wall-clock limit
    try:
        v = float(raw)
    except ValueError:
        if log:                                      # a typo must not silently drop the limit
            log("{}={!r} is not a number of seconds; ignoring it (no wall-clock limit)"
                .format(ENV_TIMEOUT, raw))
        return None
    return v if v > 0 else None


# ------------------------------------------------------------------ the project ----
def find_project(folder):
    """The HEC-RAS .prj of a folder (not a GIS projection .prj): its path or ''."""
    for p in sorted(glob.glob(os.path.join(folder, "*.prj"))):
        try:
            with open(p, encoding="latin-1") as f:
                if f.readline().startswith("Proj Title="):
                    return p
        except OSError:
            pass
    return ""


def plan_files(prj):
    """The plan files (.p01 ...) of a project, in its 'Plan File=' order."""
    base = os.path.splitext(prj)[0]
    out = []
    try:
        with open(prj, encoding="latin-1") as f:
            for line in f:
                m = re.match(r"Plan File=(p\d\d)\s*$", line.strip())
                if m:
                    out.append(base + "." + m.group(1))
    except OSError:
        pass
    return [p for p in out if os.path.isfile(p)] or sorted(glob.glob(base + ".p[0-9][0-9]"))


def plan_title(plan_file):
    try:
        with open(plan_file, encoding="latin-1") as f:
            for line in f:
                if line.startswith("Plan Title="):
                    return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return ""


def terrain_state(folder):
    """(ready, note): a terrain HDF in the project and registered in its .rasmap."""
    hdfs = sorted(glob.glob(os.path.join(folder, "Terrain", "*.hdf")))
    if not hdfs:
        return False, ("no terrain yet: open the project in HEC-RAS, RAS Mapper > Project > "
                       "Create a New RAS Terrain from the Terrain folder's GeoTIFF, save")
    rasmaps = glob.glob(os.path.join(folder, "*.rasmap"))
    if rasmaps:
        try:
            with open(rasmaps[0], encoding="utf-8", errors="replace") as f:
                t = f.read()
        except OSError:
            t = ""
        if "TerrainLayer" not in t:
            if "<Terrains />" in t:
                from .hecras_engine import write_rasmap_terrain
                rel = os.path.relpath(hdfs[0], folder).replace("/", "\\")
                write_rasmap_terrain(rasmaps[0], rel,
                                     os.path.splitext(os.path.basename(hdfs[0]))[0])
                return True, "terrain {} registered in the .rasmap".format(rel)
            return False, "the terrain is not in the project's .rasmap (open RAS Mapper once)"
    return True, ""


def results_hdf(plan_file):
    return plan_file + ".hdf"


def _has_results(path, check):
    """True only when the plan HDF exists, is non-empty, and `check` positively confirms
    unsteady results (check(path) is True). 'cannot verify' - check returns None, e.g. h5py is
    not importable in this runtime - is NOT treated as success: the chain then waits for a
    verifiable result rather than attributing an unverified/partial/geometry-only plan to a dam."""
    if not os.path.isfile(path) or os.path.getsize(path) <= 0:
        return False
    return check(path) is True


def _unverifiable(path, check):
    """True when the file is there but `check` could not tell (returned None) - used only to
    explain in the log why the launcher is waiting instead of accepting the file."""
    try:
        return os.path.isfile(path) and os.path.getsize(path) > 0 and check(path) is None
    except Exception:                                            # noqa
        return False


# ------------------------------------------------------------------- launchers ----
def _kill_tree(p):
    """Best-effort kill of a process and its children (a HEC-RAS compute engine Ras.exe spawns
    must not be orphaned when we time out)."""
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(p.pid)],
                           capture_output=True)
        else:
            import signal
            try:
                os.killpg(os.getpgid(p.pid), signal.SIGTERM)
            except Exception:                                    # noqa
                pass
            try:
                p.kill()
            except Exception:                                    # noqa
                pass
    except Exception:                                            # noqa
        pass


def _popen_run(cmd, cwd=None, timeout=None, capture_output=True, text=True):
    """Like subprocess.run, but the child starts its own process group / tree, and on timeout the
    whole tree is killed (not just the direct child) before TimeoutExpired is re-raised. This is
    the default runner; the tests inject their own. Needs real Windows to verify fully."""
    kw = {}
    if os.name == "nt":
        kw["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        kw["start_new_session"] = True
    p = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         text=text, **kw)
    try:
        out, err = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_tree(p)
        try:
            p.communicate(timeout=10)
        except Exception:                                        # noqa
            pass
        raise
    return subprocess.CompletedProcess(cmd, p.returncode, out, err)


def _run(cmd, cwd, timeout, runner):
    r = runner(cmd, cwd=cwd, timeout=timeout, capture_output=True, text=True)
    return getattr(r, "returncode", 0), ((getattr(r, "stdout", "") or "") +
                                         (getattr(r, "stderr", "") or ""))[-2000:]


def run_headless(prj, plan_file, d, timeout=None, runner=_popen_run, check=None, log=None):
    """Ras.exe -c, then RasUnsteady.exe on the run inputs when needed. Returns the results
    HDF or raises WindowsRunError. A results HDF is accepted only when it is freshly produced
    by this launch (not a stale file from an earlier run) and positively verified."""
    log = log or (lambda m: None)
    folder = os.path.dirname(prj)
    out = results_hdf(plan_file)
    ext = os.path.splitext(plan_file)[1][1:]            # p01
    num = ext[1:]
    before = os.path.getmtime(out) if os.path.isfile(out) else None

    def fresh(path):
        """verified results AND produced by THIS launch - a pre-existing HDF left untouched by a
        failed run keeps its old mtime and must NOT be accepted (a real HEC-RAS compute advances
        the mtime; a same-instant rewrite falling through to wait is the safe failure)."""
        if not _has_results(path, check):
            return False
        if before is None:
            return True
        try:
            return os.path.getmtime(path) > before
        except OSError:
            return False

    try:
        rc, txt = _run([d["ras_exe"], "-c", prj, plan_file], folder, timeout, runner)
    except subprocess.TimeoutExpired:
        raise WindowsRunError("Ras.exe -c did not finish within {:g} s".format(timeout))
    except OSError as e:
        raise WindowsRunError("Ras.exe could not be started: {}".format(e))
    log("Ras.exe -c {} {}: exit {}".format(os.path.basename(prj),
                                           os.path.basename(plan_file), rc))
    if fresh(out):
        return out
    tmp = os.path.splitext(out)[0] + ".tmp.hdf"           # <name>.p01.tmp.hdf
    base = os.path.splitext(prj)[0]
    ins = [base + ".b" + num, base + ".x" + num, tmp]
    if not d.get("rasunsteady"):
        raise WindowsRunError("Ras.exe -c gave no results (exit {}); RasUnsteady.exe not found"
                              "{}".format(rc, (": " + txt.strip()) if txt.strip() else ""))
    missing = [os.path.basename(p) for p in ins if not os.path.isfile(p)]
    if missing:
        raise WindowsRunError("Ras.exe -c gave no results (exit {}) and no run inputs ({} "
                              "missing) for RasUnsteady.exe".format(rc, ", ".join(missing)))
    try:
        rc2, txt2 = _run([d["rasunsteady"], os.path.basename(tmp), "x" + num], folder,
                         timeout, runner)
    except subprocess.TimeoutExpired:
        raise WindowsRunError("RasUnsteady.exe did not finish within {:g} s".format(timeout))
    except OSError as e:
        raise WindowsRunError("RasUnsteady.exe could not be started: {}".format(e))
    log("RasUnsteady.exe {} x{}: exit {}".format(os.path.basename(tmp), num, rc2))
    # Promote the .tmp.hdf to the canonical results name ONLY when RasUnsteady succeeded and the
    # tmp positively verifies; otherwise delete the partial so a failed/partial run never leaves a
    # file at <name>.p##.hdf for the resume path to pick up as finished.
    if rc2 == 0 and os.path.isfile(tmp) and _has_results(tmp, check) and not _has_results(out, check):
        os.replace(tmp, out)
    elif os.path.isfile(tmp):
        try:
            os.remove(tmp)
        except OSError:                                          # noqa
            pass
    if rc2 == 0 and fresh(out):
        return out
    extra = ("; results could not be verified (h5py not available) - waiting for a manual run"
             if _unverifiable(out, check) or _unverifiable(tmp, check) else "")
    raise WindowsRunError("RasUnsteady.exe gave no results (exit {}){}{}".format(
        rc2, (": " + txt2.strip()) if txt2.strip() else "", extra))


def run_com(prj, plan_file, dispatch=None, check=None, log=None, timeout=None):
    """The HEC-RAS Controller: Project_Open, Plan_SetCurrent, Compute_CurrentPlan (blocking).
    Returns the results HDF or raises WindowsRunError. With `timeout`, the blocking compute runs
    in a watchdog thread; on expiry the controller is closed and WindowsRunError is raised so the
    chain can fall through to the manual-wait path instead of stalling on this dam forever. (A
    blocking COM call cannot be force-interrupted, so the background thread may linger until the
    controller actually returns; closing it is best-effort.)"""
    log = log or (lambda m: None)
    if dispatch is None:
        try:
            import win32com.client as wc
        except Exception as e:                                   # noqa
            raise WindowsRunError("pywin32 (win32com) is not in this Python: {}".format(e))
        dispatch = wc.Dispatch
    try:
        ras = dispatch(PROG_ID)
    except Exception as e:                                       # noqa
        raise WindowsRunError("{} could not be started ({}); run 'Ras.exe /regserver' once as "
                              "administrator".format(PROG_ID, e))
    out = results_hdf(plan_file)
    ok, msgs, timed_out = False, [], None
    try:
        ras.Project_Open(prj)
        title = plan_title(plan_file)
        if title:
            ras.Plan_SetCurrent(title)
        if timeout:
            import threading
            box = {}

            def _compute():
                try:
                    box["r"] = ras.Compute_CurrentPlan(None, None, True)
                except Exception as e:                           # noqa
                    box["e"] = e
            th = threading.Thread(target=_compute, daemon=True)
            th.start()
            th.join(timeout)
            if th.is_alive():
                timed_out = ("HEC-RAS Controller did not finish within {:g} s"
                             .format(timeout))
            elif "e" in box:
                raise box["e"]
            else:
                r = box.get("r")
        else:
            r = ras.Compute_CurrentPlan(None, None, True)
        if not timed_out:
            ok = r[0] if isinstance(r, (tuple, list)) else r
            msgs = list(r[2]) if isinstance(r, (tuple, list)) and len(r) > 2 and r[2] else []
            log("HEC-RAS Controller Compute_CurrentPlan ({}): {}".format(
                title or "current plan", "ok" if ok else "failed"))
    except Exception as e:                                       # noqa
        raise WindowsRunError("HEC-RAS Controller failed: {}".format(e))
    finally:
        for close in ("Project_Close", "QuitRas"):
            try:
                getattr(ras, close)()
            except Exception:                                    # noqa
                pass
    if timed_out:
        raise WindowsRunError(timed_out)
    if _has_results(out, check):
        return out
    raise WindowsRunError("HEC-RAS Controller gave no results{}".format(
        (": " + "; ".join(str(m) for m in msgs[-5:])) if msgs else ""))


def run_plan(folder, d=None, env=None, runner=_popen_run, dispatch=None, check=None,
             log=None, clock=time.time):
    """Runs the first plan of the HEC-RAS project in `folder`. Returns {plan_hdf, method,
    attempts, seconds}. Raises TerrainMissing (make the terrain first) or WindowsRunError."""
    log = log or (lambda m: None)
    if check is None:
        from .chain import plan_has_results as check
    d = d or detect(env)
    if not d.get("available"):
        raise WindowsRunError(d.get("reason") or "HEC-RAS 6.6 not found")
    prj = find_project(folder)
    if not prj:
        raise WindowsRunError("no HEC-RAS project (.prj with 'Proj Title=') in {}".format(folder))
    plans = plan_files(prj)
    if not plans:
        raise WindowsRunError("the project {} has no plan file".format(prj))
    plan = plans[0]
    ok, note = terrain_state(folder)
    if not ok:
        raise TerrainMissing(note)
    if note:
        log(note)
    t0, attempts = clock(), []
    to = timeout_s(env, log)
    for m in methods(env, log):
        try:
            if m == "headless":
                p = run_headless(prj, plan, d, to, runner, check, log)
            else:
                if dispatch is None and not d.get("com"):
                    attempts.append("com: pywin32 (win32com) is not installed")
                    continue
                p = run_com(prj, plan, dispatch, check, log, to)
            return {"plan_hdf": p, "method": m, "attempts": attempts,
                    "seconds": round(clock() - t0, 1), "project": prj}
        except WindowsRunError as e:
            attempts.append("{}: {}".format(m, e))
            log("HEC-RAS {} run failed: {}".format(m, e))
    raise WindowsRunError("HEC-RAS 6.6 did not compute {}: {}".format(
        os.path.basename(plan), " | ".join(attempts)), attempts)
