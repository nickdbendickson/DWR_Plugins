#!/usr/bin/env python3
"""What the three batch scripts share: finding dam_chain.py and its Python, reading a dam's
chain state, the NDDOT Center Line Pipes download, the data files that are not in the
repository, and starting the batch watcher.

These scripts travelled in zip files until 2026-11-13, three copies of the same helpers, and
went stale without anything noticing: they looked for a conda env and a watcher under names
the local AI had not had for two renames. Here they are next to the code they drive, with
tests (tests/test_batch.py) that run every script's --dry-run against a stand-in dam_chain.
"""
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
CLP_URL = "https://www.dot.nd.gov/gis/downloads/centerlinepipes.gdb.zip"
# The conda env that holds dam_chain's Python. Named after the local AI, which was Lumo, then
# Pierre, and is Heim since 2026-11-13: newest first, so an old install still works.
AI_ENVS = ("ndheim", "ndpierre", "ndlumo")
_lock = threading.Lock()


def say(msg):
    with _lock:
        print(time.strftime("%H:%M:%S ") + msg, flush=True)


def conda_exe():
    return shutil.which("conda") or os.path.expanduser("~/miniforge3/bin/conda")


def env_python(names=AI_ENVS, listing=None):
    """bin/python of the first of `names` that `conda env list` knows, or None.
    `listing` is the text of `conda env list` (tests pass it; otherwise conda is asked)."""
    if listing is None:
        try:
            listing = subprocess.run([conda_exe(), "env", "list"], capture_output=True, text=True,
                                     timeout=60).stdout
        except Exception:                                                    # noqa
            return None
    envs = {}
    for ln in listing.splitlines():
        parts = ln.split()
        if parts and not parts[0].startswith("#"):
            envs[parts[0]] = parts[-1]
    for name in names:
        cand = os.path.join(envs.get(name, ""), "bin", "python")
        if name in envs and os.path.exists(cand):
            return cand
    return None


def find_damchain(use_env=True):
    """(python, dam_chain.py): $DAMCHAIN_PY or ~/local-ai/dam_chain.py, run with the local-AI
    env's Python when there is one (the one the damchain alias uses), else this Python."""
    tool = os.environ.get("DAMCHAIN_PY") or os.path.expanduser("~/local-ai/dam_chain.py")
    if not os.path.isfile(tool):
        sys.exit("dam_chain.py not found at %s. Run install_all.zsh first, or set DAMCHAIN_PY "
                 "to the dam_chain.py path." % tool)
    return ((env_python() if use_env else None) or sys.executable), tool


def chain_status(folder, did, rc):
    """One dam's result from its own state.json. dam_chain before 1.5.2 exited 0 when a dam's
    step had failed, and a batch has many dams per exit code anyway, so the file decides."""
    if rc:
        return "FAILED (exit %d)" % rc
    try:
        with open(os.path.join(folder, did, "_chain_nd", "state.json"), encoding="utf-8") as fh:
            st = json.load(fh)
    except (OSError, ValueError):
        return "FAILED (no state.json)"
    if st.get("status") != "done":
        return "FAILED: " + (st.get("error") or "see log")[:300].replace("\n", " ")
    return "ok, ND class %s" % (st.get("nd_class") or "?")


def get_clp(dest):
    """NDDOT Center Line Pipes -> <dest>/nddot_clp.gpkg (ogr2ogr from the ndqgis env)."""
    os.makedirs(dest, exist_ok=True)
    gpkg = os.path.join(dest, "nddot_clp.gpkg")
    if os.path.exists(gpkg):
        say("CLP already there: " + gpkg)
        return gpkg
    z = os.path.join(dest, "centerlinepipes.gdb.zip")
    say("downloading " + CLP_URL)
    req = urllib.request.Request(CLP_URL, headers={"User-Agent": "nd-dam-suite-batch"})
    with urllib.request.urlopen(req, timeout=600) as r, open(z, "wb") as fh:   # nosec B310 - fixed https URL
        shutil.copyfileobj(r, fh)
    with zipfile.ZipFile(z) as zf:
        zf.extractall(dest)                                                    # nosec B202 - NDDOT's own archive
    gdb = next((os.path.join(root, d) for root, dirs, _f in os.walk(dest) for d in dirs
                if d.lower().endswith(".gdb")), None)
    if not gdb:
        sys.exit("No .gdb folder in " + z)
    cmd = [conda_exe(), "run", "-n", "ndqgis", "ogr2ogr", "-f", "GPKG", gpkg, gdb]
    say(" ".join(cmd))
    subprocess.run(cmd, check=True)
    return gpkg


def data_dirs():
    """Where a data file that is not in the repository may be: $BATCH_DATA_DIR, next to the
    scripts, then the folders the batch zips unpack to."""
    out = []
    if os.environ.get("BATCH_DATA_DIR"):
        out.append(os.path.expanduser(os.environ["BATCH_DATA_DIR"]))
    out.append(HERE)
    for base in ("~", "~/Downloads", "~/Documents"):
        for name in ("batch-44-dams-2026-11-06", "batch-44-dams", "county-batch-2026-11-06", "county-batch"):
            out.append(os.path.expanduser(os.path.join(base, name)))
    return out


def find_data(name):
    """Full path of data file `name`, or None."""
    for d in data_dirs():
        p = os.path.join(d, name)
        if os.path.isfile(p):
            return p
    return None


def need_data(name, why):
    """find_data, or stop with where the file comes from."""
    p = find_data(name)
    if p:
        return p
    sys.exit("%s not found. %s\nIt is not in the repository (it is a copy of the State's dam records). "
             "Put it next to this script, or set BATCH_DATA_DIR to the folder that holds it. Looked in:\n  %s"
             % (name, why, "\n  ".join(data_dirs())))


def start_watcher(folder):
    """Start batch_watch.py on a folder; returns the Popen. Rule findings only: the watcher
    asks the local model nothing unless it is started by hand with --with-model."""
    cmd = [sys.executable, os.path.join(HERE, "batch_watch.py"), folder]
    w = subprocess.Popen(cmd)
    say("watcher: " + " ".join(cmd) + "  (findings in %s/BATCH_FINDINGS.md)" % folder)
    return w
