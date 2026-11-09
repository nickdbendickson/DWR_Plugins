# -*- coding: utf-8 -*-
"""Suite readiness checks used by the setup wizard and the status panel."""
import os

from .suite_config import get, default_dams_folder


def checks(data_folder="", dams_folder=""):
    data_folder = data_folder or get("data_folder")
    dams_folder = dams_folder or get("dams_folder") or default_dams_folder()
    rows = []
    rows.append(_data(data_folder))
    rows.append(_dams(dams_folder, data_folder))
    exe = get("lisflood_exe")
    rows.append(("ok" if exe and os.path.isfile(exe) else "warn",
                 "LISFLOOD-FP",
                 exe or "not set (needed for the Runner)"))
    py = get("anuga_python")
    rows.append(("ok" if py and os.path.isfile(py) else "warn",
                 "ANUGA",
                 py or "optional; not set"))
    book = get("settings_file")
    rows.append(("ok" if book else "info",
                 "Settings workbook",
                 book or "built-in starting values"))
    return rows


def _data(folder):
    if not folder:
        return ("fail", "Suite data folder", "not set")
    if not os.path.isdir(folder):
        return ("fail", "Suite data folder", "missing: " + folder)
    if os.path.isfile(os.path.join(folder, "LAYERS.txt")) or os.path.isfile(os.path.join(folder, "manifest.json")):
        return ("ok", "Suite data folder", folder)
    return ("warn", "Suite data folder", "no LAYERS.txt or manifest.json in " + folder)


def _dams(folder, data_folder):
    if not folder:
        return ("fail", "Dams folder", "not set")
    if data_folder and os.path.abspath(folder) == os.path.abspath(data_folder):
        return ("fail", "Dams folder", "must not be the suite data folder")
    if os.path.isfile(os.path.join(folder, "LAYERS.txt")) or os.path.isfile(os.path.join(folder, "manifest.json")):
        return ("fail", "Dams folder", "looks like a data pack; pick a results folder")
    return ("ok" if os.path.isdir(folder) else "warn", "Dams folder", folder)
