# -*- coding: utf-8 -*-
"""Hazard 2.25.3: run the hazard-creep watch on a schedule.

The watch (watch.py, ndhazard:creepwatch) re-classifies every dam of a folder against fresh
data and lists the dams that changed. It only ran when someone started it. This module puts
it on the computer's own scheduler, so the list arrives once a year (or every 3 months, or
every month) without anyone remembering:

    macOS     a launchd agent   ~/Library/LaunchAgents/org.damsafety.creepwatch.<id>.plist
    Linux     a systemd timer   ~/.config/systemd/user/damsafety-creepwatch-<id>.timer
              (no systemd: the crontab line is given, and nothing is registered)
    Windows   a scheduled task  "DamSafety hazard creep watch <id>" (schtasks)

<id> is 8 characters made from the folder's path, so several folders can each have a
schedule. What the scheduler starts is a small script in <folder>/_creep_watch/ ("_" = not a
dam folder) that runs `qgis_process run ndhazard:creepwatch` and appends everything it prints
to creep_watch_log.txt there. qgis_process opens the QGIS profile called 'default'; when the
suite lives in another profile the script points it at a small configuration folder whose
'default' is a link to that profile (as the Runner's batch workers do).

    plan(...)      what would be written and run; writes nothing
    apply(p)       write the files and register the schedule
    remove(...)    unregister it and delete the schedule's files (never a report)

    python3 watch_schedule.py FOLDER [--every year|quarter|month] [--month 1-12] [--day 1-28]
            [--hour 0-23] [--workers N] [--qgis-process PATH] [--profile-dir DIR]
            [--print | --remove]

Nothing here imports QGIS. Nothing is registered by plan(); the tests never call apply() or
remove() with the real command runner.
"""
import hashlib
import os
import subprocess
import sys

ALGORITHM = "ndhazard:creepwatch"
LABEL = "org.damsafety.creepwatch"
UNIT = "damsafety-creepwatch"
TASK = "DamSafety hazard creep watch"
WORK_FOLDER = "_creep_watch"            # "_": watch.dam_folders() does not take it for a dam
LOG_NAME = "creep_watch_log.txt"
EVERY = ("year", "quarter", "month")
EVERY_TEXT = {"year": "once a year", "quarter": "every 3 months", "month": "every month"}
MAX_WORKERS = 16
_MONTHS = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")
_MONTH_NAMES = ("January", "February", "March", "April", "May", "June", "July", "August",
                "September", "October", "November", "December")


class ScheduleError(Exception):
    """The schedule cannot be planned, written or registered; the text says why."""


def system_name(platform=None):
    plat = sys.platform if platform is None else platform
    if plat.startswith("win"):
        return "windows"
    return "macos" if plat == "darwin" else "linux"


def job_id(parent):
    """8 characters that stay the same for one folder (its real, absolute path)."""
    path = os.path.normcase(os.path.realpath(os.path.abspath(parent)))
    return hashlib.sha256(path.encode("utf-8", "replace")).hexdigest()[:8]


def work_folder(parent):
    return os.path.join(parent, WORK_FOLDER)


def months(every, month):
    """The months (1-12) the watch runs in, starting from `month`."""
    if every not in EVERY:
        raise ScheduleError("'every' must be one of: {}".format(", ".join(EVERY)))
    month = int(month)
    if not 1 <= month <= 12:
        raise ScheduleError("the month must be 1 to 12")
    step = {"year": 12, "quarter": 3, "month": 1}[every]
    return sorted(set((month - 1 + k * step) % 12 + 1 for k in range(12 // step)))


def when_text(every, month_list, day, hour):
    at = "on day {} at {:02d}:00".format(day, hour)
    if every == "month":
        return "every month {}".format(at)
    return "{} {}, in {}".format(EVERY_TEXT[every], at,
                                 ", ".join(_MONTH_NAMES[m - 1] for m in month_list))


# ---------------------------------------------------------------- the QGIS profile
def _ini_default_profile(root):
    try:
        with open(os.path.join(root, "profiles", "profiles.ini"), encoding="utf-8",
                  errors="replace") as f:
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


def profile_config(settings_dir, work):
    """(configuration folder for QGIS_CUSTOM_CONFIG_PATH, the profile link to make or None,
    note). `settings_dir` is QgsApplication.qgisSettingsDirPath() of the session that has the
    suite; '' = not known, and the job then opens whatever profile qgis_process opens."""
    prof = os.path.normpath(str(settings_dir or ""))
    parent = os.path.dirname(prof)
    if not settings_dir or os.path.basename(parent) != "profiles":
        return "", None, ""
    root = os.path.dirname(parent)
    if os.path.basename(prof) == "default" and _ini_default_profile(root) in ("", "default"):
        return root, None, ""
    cfg = os.path.join(work, "qgis_config")
    return cfg, (os.path.join(cfg, "profiles", "default"), prof), \
        "the watch opens the QGIS profile '{}'".format(os.path.basename(prof))


# ---------------------------------------------------------------- the script that runs
def _sh(text):
    return "'" + str(text).replace("'", "'\"'\"'") + "'"


def _cmd(text):
    text = str(text)
    if '"' in text or "%" in text:
        raise ScheduleError("a path with a double quote or a percent sign cannot be put in a "
                            "Windows script: {}".format(text))
    return '"' + text + '"'


def run_args(parent, workers, timeout_min, downloads):
    return ["run", ALGORITHM, "--PARENT=" + parent, "--DOWNLOADS=" + ("1" if downloads else "0"),
            "--WORKERS={}".format(int(workers)), "--TIMEOUT_MIN={}".format(int(timeout_min))]


def wrapper_text(system, exe, parent, workers=1, timeout_min=60, downloads=True, config="",
                 env=None):
    """The script the scheduler starts. `env`: extra variables (the offline pack choice)."""
    log = os.path.join(work_folder(parent), LOG_NAME)
    args = run_args(parent, workers, timeout_min, downloads)
    extra = dict(env or {})
    if config:
        extra["QGIS_CUSTOM_CONFIG_PATH"] = config
    if system == "windows":
        lines = ["@echo off", "rem DamSafety hazard creep watch: started by the Task Scheduler.",
                 "rem Made by 'Schedule the hazard creep watch'; run that tool again to change it."]
        lines += ["set {}={}".format(k, _cmd(v)[1:-1]) for k, v in sorted(extra.items())]
        lines += ["echo ==== %DATE% %TIME% hazard creep watch >> {}".format(_cmd(log)),
                  "call {} {} >> {} 2>&1".format(_cmd(exe), " ".join(_cmd(a) for a in args),
                                                 _cmd(log)),
                  "echo ==== ended with code %ERRORLEVEL% >> {}".format(_cmd(log)), ""]
        return "\r\n".join(lines)
    lines = ["#!/bin/sh", "# DamSafety hazard creep watch: started by the computer's scheduler.",
             "# Made by 'Schedule the hazard creep watch'; run that tool again to change it."]
    lines += ["export {}={}".format(k, _sh(v)) for k, v in sorted(extra.items())]
    lines += ["export QT_QPA_PLATFORM=offscreen", "LOG={}".format(_sh(log)),
              'echo "==== $(date) hazard creep watch" >> "$LOG"',
              '{} {} >> "$LOG" 2>&1'.format(_sh(exe), " ".join(_sh(a) for a in args)),
              'echo "==== ended with code $?" >> "$LOG"', ""]
    return "\n".join(lines)


# ---------------------------------------------------------------- the three schedulers
def _xml(text):
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def launchd_plist(label, wrapper, month_list, day, hour, every="year"):
    if every == "month":
        whens = ["<dict><key>Day</key><integer>{}</integer><key>Hour</key><integer>{}</integer>"
                 "<key>Minute</key><integer>0</integer></dict>".format(day, hour)]
    else:
        whens = ["<dict><key>Month</key><integer>{}</integer><key>Day</key><integer>{}</integer>"
                 "<key>Hour</key><integer>{}</integer><key>Minute</key><integer>0</integer>"
                 "</dict>".format(m, day, hour) for m in month_list]
    return "\n".join([
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">',
        '<plist version="1.0"><dict>',
        "  <key>Label</key><string>{}</string>".format(_xml(label)),
        "  <key>ProgramArguments</key><array><string>/bin/sh</string><string>{}</string>"
        "</array>".format(_xml(wrapper)),
        "  <key>StartCalendarInterval</key><array>",
    ] + ["    " + w for w in whens] + [
        "  </array>",
        "  <key>ProcessType</key><string>Background</string>",
        "</dict></plist>", ""])


def systemd_units(wrapper, month_list, day, hour, every="year"):
    cal = "*-{}-{:02d} {:02d}:00:00".format(
        "*" if every == "month" else ",".join("{:02d}".format(m) for m in month_list), day, hour)
    service = "\n".join(["[Unit]", "Description=DamSafety hazard creep watch", "",
                         "[Service]", "Type=oneshot", "Nice=10",
                         "ExecStart=/bin/sh {}".format(_systemd_arg(wrapper)), ""])
    timer = "\n".join(["[Unit]", "Description=DamSafety hazard creep watch (schedule)", "",
                       "[Timer]", "OnCalendar=" + cal, "Persistent=true", "",
                       "[Install]", "WantedBy=timers.target", ""])
    return service, timer


def _systemd_arg(path):
    path = str(path)
    if any(c in path for c in '"\\%$\n'):
        raise ScheduleError("this folder's path cannot be written into a systemd unit: "
                            "{}".format(path))
    return '"' + path + '"'


def cron_line(wrapper, month_list, day, hour, every="year"):
    return "0 {} {} {} * /bin/sh {}".format(
        hour, day, "*" if every == "month" else ",".join(str(m) for m in month_list),
        _sh(wrapper))


def schtasks_create(name, wrapper, month_list, day, hour, every="year"):
    cmd = ["schtasks", "/Create", "/F", "/TN", name, "/TR", _cmd(wrapper), "/SC", "MONTHLY",
           "/D", str(day), "/ST", "{:02d}:00".format(hour)]
    if every != "month":
        cmd += ["/M", ",".join(_MONTHS[m - 1] for m in month_list)]
    return cmd


# ---------------------------------------------------------------- plan, apply, remove
def _home(home):
    return home or os.path.expanduser("~")


def names(system, parent, home=None):
    """Where this folder's schedule lives: {"id", "wrapper", "log", and per system "plist" +
    "label", or "service" + "timer" + "unit", or "task"}."""
    jid = job_id(parent)
    work = work_folder(parent)
    out = {"id": jid, "work": work, "log": os.path.join(work, LOG_NAME),
           "wrapper": os.path.join(work, "run_creep_watch.cmd" if system == "windows"
                                   else "run_creep_watch.sh")}
    if system == "macos":
        out["label"] = "{}.{}".format(LABEL, jid)
        out["plist"] = os.path.join(_home(home), "Library", "LaunchAgents",
                                    out["label"] + ".plist")
    elif system == "linux":
        out["unit"] = "{}-{}".format(UNIT, jid)
        base = os.path.join(os.environ.get("XDG_CONFIG_HOME") or
                            os.path.join(_home(home), ".config"), "systemd", "user")
        if home:                                        # a named home: never the real one
            base = os.path.join(home, ".config", "systemd", "user")
        out["service"] = os.path.join(base, out["unit"] + ".service")
        out["timer"] = os.path.join(base, out["unit"] + ".timer")
    else:
        out["task"] = "{} {}".format(TASK, jid)
    return out


def plan(parent, exe, system=None, settings_dir="", every="year", month=1, day=15, hour=6,
         workers=1, timeout_min=60, downloads=True, env=None, home=None, uid=None,
         have_systemd=None):
    """Everything the schedule needs, as data; nothing is written or run.
    Returns {"system", "id", "when", "files": {path: text}, "exec": [paths made executable],
    "link": (link, target) or None, "register": [argv, ...], "unregister": [argv, ...],
    "wrapper", "log", "notes": [text], "manual": text or ""}."""
    system = system or system_name()
    if not parent or not os.path.isdir(parent):
        raise ScheduleError("Folder not found: {}".format(parent or "(none)"))
    parent = os.path.abspath(parent)
    if not exe:
        raise ScheduleError("qgis_process was not found: give its full path")
    day, hour, workers = int(day), int(hour), int(workers)
    if not 1 <= day <= 28:
        raise ScheduleError("the day must be 1 to 28 (a day every month has)")
    if not 0 <= hour <= 23:
        raise ScheduleError("the hour must be 0 to 23")
    workers = max(1, min(MAX_WORKERS, workers))
    month_list = months(every, month)
    n = names(system, parent, home)
    config, link, note = profile_config(settings_dir, n["work"])
    files = {n["wrapper"]: wrapper_text(system, exe, parent, workers, timeout_min, downloads,
                                        config, env)}
    out = {"system": system, "id": n["id"], "wrapper": n["wrapper"], "log": n["log"],
           "when": when_text(every, month_list, day, hour), "files": files, "link": link,
           "exec": [] if system == "windows" else [n["wrapper"]], "manual": "",
           "notes": [note] if note else [], "register": [], "unregister": []}
    if system == "macos":
        files[n["plist"]] = launchd_plist(n["label"], n["wrapper"], month_list, day, hour, every)
        dom = "gui/{}".format(os.getuid() if uid is None and hasattr(os, "getuid") else uid)
        out["unregister"] = [["launchctl", "bootout", dom + "/" + n["label"]]]
        out["register"] = [["launchctl", "bootstrap", dom, n["plist"]]]
        out["notes"].append("If the Mac is asleep at that time the watch runs when it wakes; "
                            "if it is switched off, that run is skipped.")
    elif system == "linux":
        cron = cron_line(n["wrapper"], month_list, day, hour, every)
        if have_systemd is None:
            import shutil
            have_systemd = bool(shutil.which("systemctl"))
        if have_systemd:
            files[n["service"]], files[n["timer"]] = systemd_units(n["wrapper"], month_list, day,
                                                                   hour, every)
            out["register"] = [["systemctl", "--user", "daemon-reload"],
                               ["systemctl", "--user", "enable", "--now", n["unit"] + ".timer"]]
            out["unregister"] = [["systemctl", "--user", "disable", "--now",
                                  n["unit"] + ".timer"]]
            out["notes"].append("A run missed while the computer was off is made up at the "
                                "next start. The timer runs while you are logged in; "
                                "`loginctl enable-linger` keeps it running when you are not.")
        else:
            out["manual"] = ("This computer has no systemd. Add this line to your crontab "
                             "(`crontab -e`):\n" + cron)
    else:
        out["register"] = [schtasks_create(n["task"], n["wrapper"], month_list, day, hour, every)]
        out["unregister"] = [["schtasks", "/Delete", "/F", "/TN", n["task"]]]
        out["notes"].append("The task runs only while you are logged in. To make up a run "
                            "missed while the computer was off, tick 'Run task as soon as "
                            "possible after a scheduled start is missed' in the Task Scheduler.")
    return out


def _run(cmd, run, must=True):
    try:
        r = run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=120)
    except (OSError, subprocess.SubprocessError) as e:
        if must:
            raise ScheduleError("could not run {}: {}".format(cmd[0], e))
        return ""
    text = r.stdout.decode("utf-8", "replace") if isinstance(r.stdout, bytes) else str(
        r.stdout or "")
    if r.returncode != 0 and must:
        raise ScheduleError("{} ended with code {}: {}".format(" ".join(cmd[:3]), r.returncode,
                                                               text.strip()[-400:]))
    return text


def apply(p, register=True, run=subprocess.run, link=None):
    """Write the plan's files and (register) hand the schedule to the system. Returns the
    lines to show. With register False the files are written and the commands are returned
    for the user to run. Raises ScheduleError."""
    out, held = [], []
    work = os.path.dirname(p["wrapper"])
    try:
        for path, text in p["files"].items():
            dest = path
            if not register and path != p["wrapper"]:
                # not scheduled: the agent / timer file waits beside the script, because a
                # file in the system's own folder would be picked up at the next login
                dest = os.path.join(work, os.path.basename(path))
                held.append((dest, path))
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "w", encoding="utf-8", newline="") as f:
                f.write(text)
        for path in p["exec"]:
            os.chmod(path, 0o755)
        if p["link"]:
            dst, target = p["link"]
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            if os.path.lexists(dst) and os.path.realpath(dst) != os.path.realpath(target):
                try:
                    os.remove(dst)
                except OSError:
                    os.rmdir(dst)                      # a junction
            if not os.path.lexists(dst):
                (link or _link_folder)(dst, target)
    except (OSError, subprocess.SubprocessError) as e:
        raise ScheduleError("the schedule's files could not be written: {}".format(e))
    out.append("Script: {}".format(p["wrapper"]))
    out.append("Log of every run: {}".format(p["log"]))
    if p["manual"]:
        out.append(p["manual"])
    elif register:
        for cmd in p["unregister"]:                    # replace a schedule made earlier
            _run(cmd, run, must=False)
        for cmd in p["register"]:
            _run(cmd, run)
        out.append("Scheduled: {}.".format(p["when"]))
    else:
        out.append("Not scheduled yet. To schedule it ({}):".format(p["when"]))
        out += ["  copy {} to {}".format(src, dst) for src, dst in held]
        out += ["  run: " + " ".join(_show(a) for a in cmd) for cmd in p["register"]]
    return out + p["notes"]


def _show(arg):
    return arg if arg and not any(c in arg for c in ' "\'') else '"{}"'.format(
        arg.replace('"', '\\"'))


def remove(parent, system=None, home=None, uid=None, run=subprocess.run):
    """Unregister this folder's schedule and delete its script and schedule files. The log
    and every report stay; the folder itself need not exist any more. Returns the lines to
    show."""
    system = system or system_name()
    parent = os.path.abspath(parent)
    n = names(system, parent, home)
    files, crontab = [n["wrapper"]], False
    if system == "macos":
        dom = "gui/{}".format(os.getuid() if uid is None and hasattr(os, "getuid") else uid)
        _run(["launchctl", "bootout", dom + "/" + n["label"]], run, must=False)
        files.append(n["plist"])
    elif system == "linux":
        if os.path.exists(n["timer"]):
            _run(["systemctl", "--user", "disable", "--now", n["unit"] + ".timer"], run,
                 must=False)
        else:
            crontab = True
        files += [n["service"], n["timer"]]
    else:
        _run(["schtasks", "/Delete", "/F", "/TN", n["task"]], run, must=False)
    # and the copies 'only write the script' left beside the script
    files += [os.path.join(n["work"], os.path.basename(f)) for f in files[1:]]
    gone = []
    for path in files:
        try:
            os.remove(path)
            gone.append(path)
        except OSError:
            pass
    if system == "linux" and not crontab:
        _run(["systemctl", "--user", "daemon-reload"], run, must=False)
    if not gone:
        return ["No schedule of this folder was found."]
    out = ["The schedule of {} was removed.".format(parent)]
    if crontab:
        out.append("If you added the crontab line, take it out with `crontab -e`.")
    return out + ["The log and the watch reports were kept."]


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="Run the hazard-creep watch on a schedule.")
    ap.add_argument("folder", help="the folder that holds the dam folders")
    ap.add_argument("--every", choices=EVERY, default="year")
    ap.add_argument("--month", type=int, default=1, help="the (first) month, 1-12")
    ap.add_argument("--day", type=int, default=15, help="day of the month, 1-28")
    ap.add_argument("--hour", type=int, default=6, help="hour of the day, 0-23")
    ap.add_argument("--workers", type=int, default=1, help="dams classified at the same time")
    ap.add_argument("--timeout-min", type=int, default=60, help="worker limit per dam")
    ap.add_argument("--qgis-process", default="", help="full path of qgis_process")
    ap.add_argument("--profile-dir", default="",
                    help="the QGIS profile folder that has the suite (…/profiles/<name>)")
    ap.add_argument("--print", action="store_true", dest="show",
                    help="write the files and print the command; schedule nothing")
    ap.add_argument("--remove", action="store_true", help="remove this folder's schedule")
    a = ap.parse_args(argv)
    try:
        if a.remove:
            lines = remove(a.folder)
        else:
            import shutil
            exe = a.qgis_process or os.environ.get("ND_QGIS_PROCESS", "") or \
                shutil.which("qgis_process") or ""
            lines = apply(plan(a.folder, exe, settings_dir=a.profile_dir, every=a.every,
                               month=a.month, day=a.day, hour=a.hour, workers=a.workers,
                               timeout_min=a.timeout_min), register=not a.show)
    except ScheduleError as e:
        print("Not scheduled: {}".format(e), file=sys.stderr)
        return 1
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
