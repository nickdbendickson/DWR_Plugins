"""Per-folder run claim (audit 2026-11-20).

Nothing used to stop two drivers (two QGIS sessions, QGIS and a batch, two batch workers) from running
the same dam folder at once. Each wrote the same checkpoints, RUN_SETUP.json, hand-off and report files
over the other's, with no warning. claim(folder) now makes <folder>/.run_claim.lock with O_EXCL and
writes who holds it: pid, host, thread, start time and what is running. A second driver gets
RunClaimError naming the holder, before it touches anything.

A claim is taken over (the run that made it is gone) when it was made on this computer and its
process no longer exists, or when it is older than STALE_S on any computer (a crashed run elsewhere
on a shared folder). The same thread of the same process may claim a folder it already holds
(the Screener running the Runner on its own dam folder, a refine pass, a sensitivity case): the
claim is counted and released by the outermost caller. Another thread of the same process is
refused like another process.

Only the claim's owner removes it, and only while the file still carries its token, so a run that
was (wrongly) taken over never deletes the new holder's claim.

stdlib only: imported by QGIS plugins and by plain-Python batch drivers."""
import contextlib
import datetime
import json
import os
import socket
import threading
import time
import uuid

LOCK_NAME = ".run_claim.lock"
STALE_S = 7 * 24 * 3600.0          # generous: a long ANUGA run on another computer is not taken over sooner
EMPTY_GRACE_S = 60.0               # a claim file still being written (empty) is not stale before this

_guard = threading.Lock()
_held = {}                         # lock path -> {"thread": ident, "count": n, "token": str}


class RunClaimError(RuntimeError):
    """The folder is claimed by another run (the message names it)."""


def lock_path(folder, name=LOCK_NAME):
    return os.path.join(os.path.abspath(folder), name)


def read_claim(folder, name=LOCK_NAME):
    """The claim dict of `folder`, {} for an empty or unreadable claim file, None when there is none."""
    return _read(lock_path(folder, name))


def _read(path):
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except FileNotFoundError:
        return None
    except OSError:
        return {}
    try:
        d = json.loads(text)
        return d if isinstance(d, dict) else {}
    except ValueError:
        return {}


def pid_alive(pid):
    """True while process `pid` exists on this computer (never signals it)."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes
            k32 = ctypes.windll.kernel32
            h = k32.OpenProcess(0x1000, False, pid)          # PROCESS_QUERY_LIMITED_INFORMATION
            if not h:
                return False
            try:
                code = ctypes.c_ulong()
                return bool(k32.GetExitCodeProcess(h, ctypes.byref(code))) and code.value == 259
            finally:
                k32.CloseHandle(h)
        except Exception:                                    # noqa: BLE001 - cannot tell: alive
            return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:                                  # exists, another user's
        return True
    except OSError:
        return False
    try:                                                     # a zombie is not running
        with open("/proc/{}/stat".format(pid)) as fh:
            if fh.read().rsplit(")", 1)[-1].split()[0] == "Z":
                return False
    except (OSError, IndexError):
        pass
    return True


def describe(info):
    """'process 1234 on host X (what), started 2026-11-20 12:00:00' for an error message."""
    if not info:
        return "an unknown run (the claim file is empty or unreadable)"
    when = info.get("started") or "?"
    return "process {} on {} ({}), started {}".format(info.get("pid", "?"), info.get("host", "?"),
                                                      info.get("what") or "a run", when)


def is_stale(info, mtime=None, now=None):
    """True when the claim's run is gone: this computer and the pid is dead, or older than STALE_S."""
    now = time.time() if now is None else now
    if not info:
        return mtime is not None and now - mtime > EMPTY_GRACE_S
    try:
        age = now - float(info.get("started_epoch"))
    except (TypeError, ValueError):
        age = (now - mtime) if mtime is not None else 0.0
    if age > STALE_S:
        return True
    return info.get("host") == socket.gethostname() and not pid_alive(info.get("pid"))


def _mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return None


def _take_over(path, judged):
    """Move a stale claim aside, unless it changed since it was judged stale (then put it back)."""
    side = "{}.{}.{}.stale".format(path, os.getpid(), threading.get_ident())
    try:
        os.replace(path, side)
    except OSError:
        return
    if _read(side) == judged:
        try:
            os.remove(side)
        except OSError:
            pass
        return
    try:                                                     # someone's fresh claim: restore it
        os.link(side, path)
    except OSError:
        pass
    try:
        os.remove(side)
    except OSError:
        pass


def acquire(folder, what="a run", name=LOCK_NAME, attempts=5):
    """Claim `folder` (created when missing) for this thread. Returns the lock path; RunClaimError
    when another live run holds it."""
    os.makedirs(folder, exist_ok=True)
    path = lock_path(folder, name)
    me = threading.get_ident()
    with _guard:
        h = _held.get(path)
        if h is not None:
            if h["thread"] == me:
                h["count"] += 1
                return path
            raise RunClaimError("{} is already being run by another task in this QGIS / Python session "
                                "({}). Wait for it to finish, or use another output folder.".format(
                                    folder, describe(_read(path))))
        for _ in range(attempts):
            token = uuid.uuid4().hex
            info = {"pid": os.getpid(), "host": socket.gethostname(), "thread": me, "what": what,
                    "started": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "started_epoch": time.time(), "token": token, "folder": os.path.abspath(folder)}
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            except FileExistsError:
                other = _read(path)
                if other is None:
                    continue                                 # released meanwhile: try again
                if other.get("pid") == os.getpid() and other.get("host") == socket.gethostname() \
                        and other.get("thread") == me:
                    # this thread's own claim, made through another copy of this module (the
                    # Screener's and the Runner's): nested, and the outer holder removes it
                    _held[path] = {"thread": me, "count": 1, "token": None}
                    return path
                if is_stale(other, _mtime(path)):
                    _take_over(path, other)
                    continue
                raise RunClaimError(
                    "{} is already being run by {}. Two runs in one dam folder overwrite each other's "
                    "files: wait for that run to finish, or stop it, or use another output folder. If no "
                    "run is going, delete {}.".format(folder, describe(other), path))
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(info, fh, indent=1)
                    fh.flush()
                    try:
                        os.fsync(fh.fileno())
                    except OSError:
                        pass
            except BaseException:
                try:
                    os.remove(path)
                except OSError:
                    pass
                raise
            _held[path] = {"thread": me, "count": 1, "token": token}
            return path
        raise RunClaimError("{} could not be claimed for {} (the claim file kept changing): try again".format(
            folder, what))


def release(path):
    """Undo one acquire(); the outermost removes the claim file (only if it is still ours)."""
    with _guard:
        h = _held.get(path)
        if h is None:
            return
        h["count"] -= 1
        if h["count"] > 0:
            return
        del _held[path]
        if h["token"] is not None and (_read(path) or {}).get("token") == h["token"]:
            try:
                os.remove(path)
            except OSError:
                pass


@contextlib.contextmanager
def claim(folder, what="a run", name=LOCK_NAME):
    """with claim(dam_folder, "DamIT Runner"): ... - the folder is this run's until the block ends."""
    path = acquire(folder, what, name)
    try:
        yield path
    finally:
        release(path)
