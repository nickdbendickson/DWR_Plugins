"""Plugin settings, stored with QgsSettings under nd_data_updater/.

Where the data goes (1.4.0, audit 1.22 item 7), first that applies:
  1. The Data folder set in this plugin's own settings (nd_data_updater/data_dir), when it is
     not the old default path.
  2. <suite data folder>/structures/ when the suite folder is set (NDSUITE_DATA, the QGIS
     setting ndsuite/data_folder, else ~/.ndsuite/config.json; the same lookup as the suite).
     Kept off: any folder inside (or named as) ND_Dam_Suite_Pack or another offline pack, and
     a folder that can't be written. If ~/Documents/ND Structure Data already holds downloads
     and structures/ holds none, the old folder stays in use and a notice says how to move it.
  3. ~/Documents/ND Structure Data.
Nothing is ever moved or deleted here. Data's manifest in that folder is
structures_manifest.json (core.MANIFEST_NAME), so it never replaces the suite's manifest.json.
"""

import json
import os

try:
    from qgis.core import QgsSettings
except ImportError:                     # outside QGIS (tests)
    QgsSettings = None

from .sources import SOURCE_INFO

PREFIX = "nd_data_updater/"
DEFAULT_DATA_DIR = os.path.join(os.path.expanduser("~"), "Documents", "ND Structure Data")
SUITE_KEY = "ndsuite/data_folder"
SUITE_ENV = "NDSUITE_DATA"
SUITE_CONFIG = os.path.join(os.path.expanduser("~"), ".ndsuite", "config.json")
STRUCTURES_SUB = "structures"
PACK_NAMES = ("nd_dam_suite_pack", "nd_offline_pack")       # compared lower-case
PACK_FORMAT = "nd-dam-hazard-offline-pack"
# the folder each source downloads into (sources.build_sources) and both manifest names
DATA_FOLDERS = ("nsi", "microsoft_footprints", "osm", "usa_structures", "nsi_generator_code",
                "overture_buildings", "address_points")
DATA_MANIFESTS = ("structures_manifest.json", "manifest.json")


def _norm(path):
    return os.path.normcase(os.path.realpath(os.path.expanduser(path)))


def in_pack(path):
    """True when `path` is an offline pack or inside one: a folder named ND_Dam_Suite_Pack /
    nd_offline_pack on the way, or a folder holding a pack's manifest or nd_offline_pack.sqlite.
    """
    p = _norm(path)
    while True:
        if os.path.basename(p).lower() in PACK_NAMES:
            return True
        if os.path.isfile(os.path.join(p, "nd_offline_pack.sqlite")):
            return True
        try:
            with open(os.path.join(p, "manifest.json"), encoding="utf-8") as f:
                m = json.load(f)
            if isinstance(m, dict) and m.get("format") == PACK_FORMAT:
                return True
        except (OSError, ValueError):
            pass
        parent = os.path.dirname(p)
        if parent == p:
            return False
        p = parent


def writable(path):
    """True when `path` (or, if it doesn't exist yet, the nearest folder above it) is writable."""
    p = os.path.abspath(os.path.expanduser(path))
    while not os.path.exists(p):
        parent = os.path.dirname(p)
        if parent == p:
            return False
        p = parent
    return os.path.isdir(p) and os.access(p, os.W_OK | os.X_OK)


def has_downloads(folder):
    """True when `folder` holds a Data manifest or a non-empty dataset folder."""
    if not folder or not os.path.isdir(folder):
        return False
    if any(os.path.isfile(os.path.join(folder, m)) for m in DATA_MANIFESTS):
        return True
    for name in DATA_FOLDERS:
        p = os.path.join(folder, name)
        try:
            if os.path.isdir(p) and os.listdir(p):
                return True
        except OSError:
            pass
    return False


def suite_folder(qsettings=None):
    """The suite data folder, "" when none is set (read only; Data never writes ndsuite/*)."""
    v = (os.environ.get(SUITE_ENV) or "").strip()
    if v:
        return v
    if qsettings is not None:
        try:
            v = str(qsettings.value(SUITE_KEY, "") or "").strip()
            if v:
                return v
        except Exception:                                        # noqa: BLE001
            pass
    try:
        with open(SUITE_CONFIG, encoding="utf-8") as f:
            d = json.load(f)
        return str((d.get(SUITE_KEY, "") if isinstance(d, dict) else "") or "").strip()
    except (OSError, ValueError, AttributeError):
        return ""


def resolve_data_dir(explicit, suite, default=None):
    """(folder, how, notice) for the explicit Data setting, the suite folder and the default.

    how: "explicit", "suite", "legacy" (old folder kept because it holds the downloads) or
    "default". notice: "" or a one-line message for the user (a guarded folder, or the offer
    to move the old downloads). Pure: looks at the files, never creates or moves anything.
    """
    default = default or DEFAULT_DATA_DIR
    notes = []
    explicit = (explicit or "").strip()
    if explicit and _norm(explicit) != _norm(default):
        if in_pack(explicit):
            notes.append(f"Data folder {explicit} is inside the offline pack, which is read "
                         f"only; using {default} instead.")
        else:
            return explicit, "explicit", ""
    suite = (suite or "").strip()
    if suite:
        cand = os.path.join(suite, STRUCTURES_SUB)
        if in_pack(suite) or in_pack(cand):
            notes.append(f"The suite data folder {suite} is the offline pack (read only), so "
                         f"downloads stay in {default}.")
        elif not writable(cand):
            notes.append(f"Can't write to {cand}, so downloads stay in {default}.")
        elif has_downloads(default) and not has_downloads(cand):
            notes.append(f"Your downloads are still in {default}. To share them with the suite, "
                         f"close QGIS and move that folder's contents to {cand}; until then "
                         f"Data keeps using {default}.")
            return default, "legacy", " ".join(notes)
        else:
            return cand, "suite", " ".join(notes)
    return default, "default", " ".join(notes)


class Settings:
    def __init__(self, qsettings=None):
        self.s = qsettings if qsettings is not None else QgsSettings()

    def _get(self, key, default, type_):
        value = self.s.value(PREFIX + key, default, type=type_)
        return default if value is None else value

    def _set(self, key, value):
        self.s.setValue(PREFIX + key, value)

    def resolve(self):
        """(folder, how, notice): see resolve_data_dir."""
        return resolve_data_dir(self._get("data_dir", "", str), suite_folder(self.s))

    @property
    def data_dir(self):
        return self.resolve()[0]

    @data_dir.setter
    def data_dir(self, v):
        """Set only when the user picks a folder; the old default path counts as not set."""
        v = (v or "").strip()
        if v and _norm(v) == _norm(DEFAULT_DATA_DIR):
            v = ""
        self._set("data_dir", v)

    @property
    def auto_check(self):
        return self._get("auto_check", False, bool)   # off until the engineer turns it on (security review 2026-10-14)

    @auto_check.setter
    def auto_check(self, v):
        self._set("auto_check", bool(v))

    @property
    def check_hours(self):
        return self._get("check_hours", 24, int)

    @check_hours.setter
    def check_hours(self, v):
        self._set("check_hours", int(v))

    @property
    def nsi_interval_days(self):
        return self._get("nsi_interval_days", 30, int)

    @nsi_interval_days.setter
    def nsi_interval_days(self, v):
        self._set("nsi_interval_days", int(v))

    @property
    def nsi_mode(self):
        value = self._get("nsi_mode", "merged", str)
        return value if value in ("merged", "nsi2026", "nsi2022") else "merged"

    @nsi_mode.setter
    def nsi_mode(self, v):
        self._set("nsi_mode", v)

    @property
    def nsi_match_m(self):
        return self._get("nsi_match_m", 30, int)

    @nsi_match_m.setter
    def nsi_match_m(self, v):
        self._set("nsi_match_m", int(v))

    @property
    def usa_structures_url(self):
        return self._get("usa_structures_url", "", str)

    @usa_structures_url.setter
    def usa_structures_url(self, v):
        self._set("usa_structures_url", v.strip())

    @property
    def last_auto_check(self):
        return self._get("last_auto_check", 0.0, float)

    @last_auto_check.setter
    def last_auto_check(self, v):
        self._set("last_auto_check", float(v))

    def enabled(self, source_id):
        return self._get("enabled/" + source_id, SOURCE_INFO[source_id][2], bool)

    def set_enabled(self, source_id, value):
        self._set("enabled/" + source_id, bool(value))

    def source_config(self):
        return {
            "enabled": {sid: self.enabled(sid) for sid in SOURCE_INFO},
            "usa_structures_url": self.usa_structures_url,
            "nsi_interval_days": self.nsi_interval_days,
            "nsi_mode": self.nsi_mode,
            "nsi_match_m": self.nsi_match_m,
        }
