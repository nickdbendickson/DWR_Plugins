"""Hazard 2.25.3: the hazard-creep watch on a schedule, and the one-page changed-dams list.

  1. watch.write_reports also writes Hazard_Creep_Changed_Dams_<date>.html and
     Hazard_Creep_Changed_Dams.csv: only the dams that changed, alerts first, then the dams
     that could not be checked; a run with no change says so
  2. watch_schedule.plan: the script, the launchd agent / systemd timer / Windows task and
     the commands that register them, for a year, a quarter and a month
  3. the QGIS profile: 'default' needs nothing; another profile gets a configuration folder
     whose 'default' is a link to it
  4. apply() and remove() with a command runner that records instead of running, in a
     temporary home folder: NOTHING is registered on this computer by this test
  5. bad input is a ScheduleError with a plain message

    python3 DamSafety_Hazard/tests/test_creep_schedule.py
"""
import os
import shutil
import sys
import tempfile
import time
import types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))

from DamSafety_Hazard import watch as W  # noqa: E402
from DamSafety_Hazard import watch_schedule as S  # noqa: E402

fails = []
T = tempfile.mkdtemp(prefix="hz_sched_")


def check(cond, msg):
    print(("ok   " if cond else "FAIL ") + msg)
    if not cond:
        fails.append(msg)


def row(dam, alert, **kw):
    r = dict.fromkeys(W.WATCH_FIELDS, "")
    r.update({"Dam": dam, "Alert": alert, "Status": "done", "ND_Before": "Low", "ND_Now": "Low",
              "TM11_Before": "Low", "TM11_Now": "Low", "Message": "no change"})
    r.update(kw)
    return r


class Recorder:
    """Stands in for subprocess.run: records the command, runs nothing."""

    def __init__(self, code=0):
        self.calls, self.code = [], code

    def __call__(self, cmd, **kw):
        self.calls.append(list(cmd))
        return types.SimpleNamespace(returncode=self.code, stdout=b"said no")


# ---- 1. the changed-dams page -------------------------------------------------------
parent = os.path.join(T, "dams")
os.makedirs(parent)
rows = [row("Quiet 1", W.ALERT_NONE), row("Quiet 2", W.ALERT_NONE),
        row("New one", W.ALERT_NEW, Message="no earlier run to compare with"),
        row("Up <b>", W.ALERT_CLASS_UP, ND_Now="High", Newly_Flooded=3,
            Message="ND Low -> High; 3 structure(s) newly flooded or cut off",
            Compared_With="2025-11-01"),
        row("Creeper", W.ALERT_CREEP, Newly_Flooded=1, Roads_Newly_Overtopped=2,
            Message="1 structure(s) newly flooded or cut off"),
        row("Look", W.ALERT_REVIEW, Message="2 structure(s) no longer flooded"),
        row("Broken", W.ALERT_FAILED, Status="failed", Message="worker timed out")]
when = time.strptime("2026-11-13", "%Y-%m-%d")
paths = W.write_reports(parent, rows, when=when, mode="one dam after another")
check(set(paths) == {"csv", "dated", "html", "changed_html", "changed_csv"}, "the two new files are returned with the old three")
check(os.path.basename(paths["changed_html"]) == "Hazard_Creep_Changed_Dams_2026-11-13.html", "the page is named by date")
page = open(paths["changed_html"], encoding="utf-8").read()
check("3 of 7 dam(s) changed" in page, "the page starts with how many dams changed")
check("1 class change(s) (1 to a higher class)" in page and "1 with new flooding" in page and "1 not checked" in page, "and the count of each kind")
check("Quiet 1" not in page and "New one" not in page, "unchanged and baseline dams are not on the page")
check(page.index("Up &lt;b&gt;") < page.index("Creeper") < page.index("Look") < page.index("Broken"), "alerts first, the dams not checked last")
check("<b>" not in page.split("Up &lt;b&gt;")[0].split("<table>")[-1], "a dam name is escaped")
check("Low \u2192 High" in page, "a class change is shown as before and now")
check("Not checked (1)" in page and "worker timed out" in page, "a failed dam is listed with why")
check("not a classification decision" in page, "the page says it is a list to look at")
check("@page" in page, "the page has a print layout")
import csv  # noqa: E402
with open(paths["changed_csv"], encoding="utf-8-sig", newline="") as fh:
    crows = list(csv.DictReader(fh))
check([r["Dam"] for r in crows] == ["Up <b>", "Creeper", "Look", "Broken"], "the CSV holds the same dams in the same order")
full = open(paths["html"], encoding="utf-8").read()
check("Quiet 1" in full, "the full table still lists every dam")
quiet = W.changed_html([row("Quiet 1", W.ALERT_NONE)], "2026-11-13")
check("0 of 1 dam(s) changed" in quiet and "No dam changed class" in quiet and "<table>" not in quiet, "a run with no change says so")
check(W.changed_rows(rows)[0]["Dam"] == "Up <b>" and len(W.failed_rows(rows)) == 1, "changed_rows / failed_rows")

# ---- 2. the plan on each system -----------------------------------------------------
home = os.path.join(T, "home")
os.makedirs(home)
exe = "/opt/qgis/bin/qgis_process"
p = S.plan(parent, exe, system="macos", home=home, uid=501, workers=4, timeout_min=45)
jid = S.job_id(parent)
check(len(jid) == 8 and jid == S.job_id(parent + os.sep) and jid != S.job_id(home), "the id is 8 characters of the folder's path")
check(p["when"] == "once a year on day 15 at 06:00, in January", "the default is once a year, 15 January, 06:00: " + p["when"])
wrapper = os.path.join(parent, "_creep_watch", "run_creep_watch.sh")
plist = os.path.join(home, "Library", "LaunchAgents", "org.damsafety.creepwatch.{}.plist".format(jid))
check(sorted(p["files"]) == sorted([wrapper, plist]), "macOS: a script in <folder>/_creep_watch and a launchd agent")
sh = p["files"][wrapper]
check(sh.startswith("#!/bin/sh\n") and "'/opt/qgis/bin/qgis_process' 'run' 'ndhazard:creepwatch'" in sh, "the script runs qgis_process run ndhazard:creepwatch")
check("'--PARENT={}'".format(parent) in sh and "'--WORKERS=4'" in sh and "'--TIMEOUT_MIN=45'" in sh and "'--DOWNLOADS=1'" in sh, "with the folder, the workers, the limit and the downloads")
check('>> "$LOG" 2>&1' in sh and "creep_watch_log.txt" in sh, "and appends what it prints to the log")
check("QGIS_CUSTOM_CONFIG_PATH" not in sh, "no profile is named when none is known")
pl = p["files"][plist]
check("<key>Month</key><integer>1</integer><key>Day</key><integer>15</integer><key>Hour</key><integer>6</integer>" in pl, "the agent's calendar: month 1, day 15, hour 6")
check("<string>/bin/sh</string><string>{}</string>".format(wrapper) in pl and "org.damsafety.creepwatch." + jid in pl, "the agent starts the script")
check(p["register"] == [["launchctl", "bootstrap", "gui/501", plist]], "registered with launchctl bootstrap")
check(p["unregister"] == [["launchctl", "bootout", "gui/501/org.damsafety.creepwatch." + jid]], "and removed with launchctl bootout")
check(not os.path.exists(wrapper) and not os.path.exists(plist), "plan() writes nothing")

q = S.plan(parent, exe, system="linux", home=home, every="quarter", month=2, day=1, hour=22, have_systemd=True)
check(q["when"] == "every 3 months on day 1 at 22:00, in February, May, August, November", "a quarter from February: " + q["when"])
timer = [k for k in q["files"] if k.endswith(".timer")][0]
service = [k for k in q["files"] if k.endswith(".service")][0]
check(timer == os.path.join(home, ".config", "systemd", "user", "damsafety-creepwatch-{}.timer".format(jid)), "Linux: a systemd user timer")
check("OnCalendar=*-02,05,08,11-01 22:00:00" in q["files"][timer] and "Persistent=true" in q["files"][timer], "that catches up a missed run")
check('ExecStart=/bin/sh "{}"'.format(wrapper) in q["files"][service], "its service starts the script")
check(q["register"][-1] == ["systemctl", "--user", "enable", "--now", "damsafety-creepwatch-{}.timer".format(jid)], "registered with systemctl --user enable --now")
c = S.plan(parent, exe, system="linux", home=home, have_systemd=False)
check(not c["register"] and "0 6 15 1 * /bin/sh '{}'".format(wrapper) in c["manual"], "no systemd: the crontab line is given and nothing is registered")
check(sorted(c["files"]) == [wrapper], "and only the script is written")

w = S.plan(parent, r"C:\QGIS\bin\qgis_process-qgis-ltr.bat", system="windows", home=home, every="month", day=28, hour=0, downloads=False)
cmdf = os.path.join(parent, "_creep_watch", "run_creep_watch.cmd")
check(sorted(w["files"]) == [cmdf] and w["exec"] == [], "Windows: a .cmd script")
bat = w["files"][cmdf]
check('call "C:\\QGIS\\bin\\qgis_process-qgis-ltr.bat" "run" "ndhazard:creepwatch"' in bat and '"--DOWNLOADS=0"' in bat and "\r\n" in bat, "that calls qgis_process")
check(w["register"] == [["schtasks", "/Create", "/F", "/TN", "DamSafety hazard creep watch " + jid, "/TR", '"{}"'.format(cmdf), "/SC", "MONTHLY", "/D", "28", "/ST", "00:00"]], "every month: schtasks /SC MONTHLY /D 28")
y = S.plan(parent, "q.exe", system="windows", home=home, every="quarter", month=3)
check(y["register"][0][-2:] == ["/M", "MAR,JUN,SEP,DEC"], "a quarter names its months")
check(S.months("year", 12) == [12] and S.months("month", 5) == list(range(1, 13)), "months()")

# ---- 3. the QGIS profile ------------------------------------------------------------
cfg = os.path.join(T, "QGIS3")
os.makedirs(os.path.join(cfg, "profiles", "default"))
os.makedirs(os.path.join(cfg, "profiles", "damsafety"))
d = S.plan(parent, exe, system="linux", home=home, have_systemd=True, settings_dir=os.path.join(cfg, "profiles", "default") + os.sep)
check("export QGIS_CUSTOM_CONFIG_PATH='{}'".format(cfg) in d["files"][wrapper] and d["link"] is None, "the 'default' profile: its configuration folder, no link")
n = S.plan(parent, exe, system="linux", home=home, have_systemd=True, settings_dir=os.path.join(cfg, "profiles", "damsafety"),
           env={"ND_HAZARD_PACK": "/data/pack"})
own = os.path.join(parent, "_creep_watch", "qgis_config")
check(n["link"] == (os.path.join(own, "profiles", "default"), os.path.join(cfg, "profiles", "damsafety")), "another profile: a 'default' that links to it")
check("export QGIS_CUSTOM_CONFIG_PATH='{}'".format(own) in n["files"][wrapper] and "export ND_HAZARD_PACK='/data/pack'" in n["files"][wrapper], "the script points QGIS at it, and carries the pack choice")
check(any("damsafety" in t for t in n["notes"]), "and the profile is named in the notes")
with open(os.path.join(cfg, "profiles", "profiles.ini"), "w") as fh:
    fh.write("[core]\ndefaultProfile=damsafety\n")
d2 = S.plan(parent, exe, system="linux", home=home, have_systemd=True, settings_dir=os.path.join(cfg, "profiles", "default"))
check(d2["link"] is not None, "'default' while profiles.ini names another: linked too, so a later QGIS cannot open the other one")

# ---- 4. apply and remove, nothing registered for real -------------------------------
rec = Recorder()
lines = S.apply(n, register=True, run=rec)
check(rec.calls == n["unregister"] + n["register"], "apply: the old schedule is removed, then the new one registered")
check(os.access(wrapper, os.X_OK) and os.path.isfile(timer), "the script (executable) and the timer are written")
check(os.path.realpath(os.path.join(own, "profiles", "default")) == os.path.realpath(os.path.join(cfg, "profiles", "damsafety")), "the profile link is made")
check(any(ln.startswith("Scheduled: once a year") for ln in lines) and any("creep_watch_log.txt" in ln for ln in lines), "it says when, and where the log is")
check(W.dam_folders(parent) == [], "_creep_watch is not taken for a dam folder")
S.apply(n, register=True, run=Recorder())
check(os.path.islink(os.path.join(own, "profiles", "default")), "scheduling again keeps the link")
rec = Recorder()
lines = S.apply(p, register=False, run=rec)
check(rec.calls == [] and any("run: launchctl bootstrap gui/501" in ln for ln in lines) and any(ln.startswith("Not scheduled yet") for ln in lines), "'only write the script': nothing is run, the command is shown")
held = os.path.join(parent, "_creep_watch", os.path.basename(plist))
check(os.path.isfile(held) and not os.path.exists(plist), "and the agent file waits beside the script, not in LaunchAgents (macOS would load it at the next login)")
check(any(ln.strip() == "copy {} to {}".format(held, plist) for ln in lines), "with where to copy it")
os.remove(held)
try:
    S.apply(q, register=True, run=Recorder(code=1))
    check(False, "a scheduler that says no is an error")
except S.ScheduleError as e:
    check("systemctl --user daemon-reload ended with code 1" in str(e) and "said no" in str(e), "a scheduler that says no is a ScheduleError with its answer")
rec = Recorder()
lines = S.remove(parent, system="linux", home=home, run=rec)
check(["systemctl", "--user", "disable", "--now", "damsafety-creepwatch-{}.timer".format(jid)] in rec.calls, "remove: the timer is disabled")
check(not os.path.exists(wrapper) and not os.path.exists(timer) and not os.path.exists(service), "and the script and the units are deleted")
check(os.path.isfile(paths["changed_html"]) and "kept" in lines[-1], "the reports are kept")
check(S.remove(parent, system="linux", home=home, run=Recorder()) == ["No schedule of this folder was found."], "removing twice says there is none")
rec = Recorder()
S.apply(p, register=True, run=rec)
gone = os.path.join(T, "gone")
check(S.remove(gone, system="macos", home=home, uid=501, run=Recorder()) == ["No schedule of this folder was found."], "a folder that never had a schedule")
check(S.remove(parent, system="macos", home=home, uid=501, run=rec)[0].startswith("The schedule of") and not os.path.exists(plist), "macOS: the agent is removed")

# ---- 4b. the script itself runs, with a stand-in for qgis_process ----------------------
if os.name != "nt":
    import subprocess  # noqa: E402
    odd = os.path.join(T, "it's a folder", "dams")           # a space and an apostrophe
    os.makedirs(odd)
    fake = os.path.join(T, "it's a folder", "qgis_process")
    with open(fake, "w") as fh:
        fh.write('#!/bin/sh\nfor a in "$@"; do echo "ARG[$a]"; done\necho "CFG[$QGIS_CUSTOM_CONFIG_PATH]"\nexit 3\n')
    os.chmod(fake, 0o755)
    o = S.plan(odd, fake, system="linux", home=home, have_systemd=True, workers=2,
               settings_dir=os.path.join(cfg, "profiles", "damsafety"))
    S.apply(o, register=False, run=Recorder())
    r = subprocess.run(["/bin/sh", o["wrapper"]], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=60)
    log = open(o["log"], encoding="utf-8").read()
    check(r.returncode == 0 and r.stdout == b"", "the script runs and prints nothing itself")
    check("ARG[run]" in log and "ARG[ndhazard:creepwatch]" in log and "ARG[--PARENT={}]".format(odd) in log and "ARG[--WORKERS=2]" in log,
          "qgis_process gets the folder as one argument, spaces and apostrophe and all")
    check("CFG[{}]".format(os.path.join(odd, "_creep_watch", "qgis_config")) in log, "and the profile's configuration folder")
    check("hazard creep watch" in log.splitlines()[0] and log.strip().endswith("==== ended with code 3"), "the log has the start and how the run ended")
    lines = S.remove(odd, system="linux", home=home, run=Recorder())
    left = sorted(os.listdir(os.path.join(odd, "_creep_watch")))
    check(left == ["creep_watch_log.txt", "qgis_config"], "remove also takes the unit files 'only write' left beside the script; the log stays: {}".format(left))

# ---- 5. bad input -------------------------------------------------------------------
for kw, word in ((dict(day=31), "day must be 1 to 28"), (dict(hour=24), "hour must be 0 to 23"),
                 (dict(month=13), "month must be 1 to 12"), (dict(every="week"), "must be one of")):
    try:
        S.plan(parent, exe, system="linux", home=home, have_systemd=True, **kw)
        check(False, "refused: " + word)
    except S.ScheduleError as e:
        check(word in str(e), "refused: " + word)
for args, word in (((os.path.join(T, "nowhere"), exe), "Folder not found"), ((parent, ""), "qgis_process was not found")):
    try:
        S.plan(*args, system="linux", home=home, have_systemd=True)
        check(False, word)
    except S.ScheduleError as e:
        check(word in str(e), word)
try:
    S.plan(parent, 'C:\\a"b\\q.exe', system="windows", home=home)
    check(False, "a Windows path with a quote is refused")
except S.ScheduleError:
    check(True, "a Windows path with a quote is refused")
check(S.main([os.path.join(T, "nowhere"), "--print"]) == 1, "the command line returns 1 on an error")

shutil.rmtree(T, ignore_errors=True)
print("\n{} failed".format(len(fails)))
sys.exit(1 if fails else 0)
