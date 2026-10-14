# -*- coding: utf-8 -*-
"""One store for suite setup. Computer settings, optional project override, then env.

Keys: data_folder, dams_folder, lisflood_exe, anuga_python, settings_file,
offline_only, setup_version.
"""
import os

KEYS = (
    "data_folder", "dams_folder", "lisflood_exe", "anuga_python",
    "settings_file", "offline_only", "setup_version",
)
ENV = {
    "data_folder": "NDSUITE_DATA_FOLDER",
    "dams_folder": "NDSUITE_DAMS_FOLDER",
    "lisflood_exe": "NDBREACH_LISFLOOD",
    "anuga_python": "NDBREACH_ANUGA_PYTHON",
    "settings_file": "NDSUITE_SETTINGS",
    "offline_only": "NDSUITE_OFFLINE_ONLY",
}
PREFIX = "ndsuite/"


def _settings():
    try:
        from qgis.core import QgsSettings
        return QgsSettings()
    except Exception:
        return None


def _project():
    try:
        from qgis.core import QgsProject
        return QgsProject.instance()
    except Exception:
        return None


def get(key, project=None):
    project = project or _project()
    if project is not None:
        try:
            val, ok = project.readEntry("NDDamSuite", key)
            if ok and str(val).strip():
                return str(val)
        except Exception:
            pass
    s = _settings()
    if s is not None:
        val = s.value(PREFIX + key, "")
        if val not in (None, ""):
            return str(val)
    env = os.environ.get(ENV.get(key, ""), "")
    return env or ""


def set(key, value, scope="computer", project=None):
    if scope == "project":
        project = project or _project()
        if project is not None:
            project.writeEntry("NDDamSuite", key, str(value or ""))
            return
    s = _settings()
    if s is not None:
        s.setValue(PREFIX + key, str(value or ""))


def source(key, project=None):
    project = project or _project()
    if project is not None:
        try:
            val, ok = project.readEntry("NDDamSuite", key)
            if ok and str(val).strip():
                return "project"
        except Exception:
            pass
    s = _settings()
    if s is not None and s.value(PREFIX + key, "") not in (None, ""):
        return "computer"
    if os.environ.get(ENV.get(key, ""), ""):
        return "env"
    return ""


def default_dams_folder():
    return os.path.join(os.path.expanduser("~"), "DamSafety", "Dams")

def dams_folder():
    """Saved dams folder, or the default under the home directory."""
    return get("dams_folder") or default_dams_folder()


def data_folder():
    """Saved suite data folder, or empty."""
    return get("data_folder") or ""


# The read-only data pack. Nothing in the suite writes into it: results, DEM downloads
# and dam folders go to the Dams folder (default ~/DamSafety/Dams).
PACK_NAME = "ND_Dam_Suite_Pack"


def in_suite_pack(path):
    """True when `path` is the ND_Dam_Suite_Pack folder or anything inside it (also its
    _work folder). Case-insensitive, either separator, '..' and symlinks resolved."""
    text = str(path or "").strip()
    if not text:
        return False
    try:
        full = os.path.realpath(os.path.abspath(os.path.expanduser(text)))
    except (OSError, ValueError):
        full = text
    for raw in (text, full):
        for part in raw.replace("\\", "/").split("/"):
            if part.lower().startswith(PACK_NAME.lower()):
                return True
    return False


def safe_dams_folder(candidate=""):
    """`candidate`, else the saved Dams folder, else the default - but never a folder in
    the read-only pack: then the default under the home directory."""
    for folder in (str(candidate or "").strip(), get("dams_folder")):
        if folder and not in_suite_pack(folder):
            return folder
    return default_dams_folder()
