"""The shared defaults file: the suite-wide decisions every tool starts from (Core 1.2.0).

Before this, each plugin held its own copy of a convention in code (Runner Manning's n 0.06,
Screening 0.05; the PMF Vw rule written once in each Runner, which is how the pool-convention bug
of ND 3.29.4 / DWR 3.16.4 happened). Now there is one small JSON file:

    DamSafety_Core/defaults.json        the suite's values (shipped, replaced on update; read only)
    ~/DamSafety/defaults.json           YOUR values (only the keys you changed; optional)

The suite file and yours are merged: a key in yours wins, a key you never changed follows the
suite file (so a later suite update reaches it). The path of your file is the suite setting
`defaults_file` (QgsSettings ndsuite/defaults_file, a project entry overrides it, env
NDSUITE_DEFAULTS for headless runs); empty = ~/DamSafety/defaults.json.

File layout (the same in DamIT Core and Crest, which read this format too):
    {"format": "dam-defaults", "format_version": 1, "name": "...", "note": "...",
     "values": {key: value}}

Keys (SCHEMA): sunny_day_start_pool, pmf_breach_volume, failure_mode, breach_method, manning_n.
Per-dam inputs (height, storage, crest, pools) are NOT here: they belong to the dam (dam.json).
Engine-only knobs (LISFLOOD CFL / theta, cell size) are NOT here: they stay in the Runner.

How a tool uses it:
  * Processing tools: ui/suite_settings_tools.settings_aware sets the bound parameters' defaults
    from these values (BINDINGS) when the tool is created, BEFORE the suite settings workbook, so
    the order is: built-in default < shared defaults < settings workbook row < the dialog.
  * Core code that has no parameter (the Runner's PMF Vw rule) calls value(key).
  * A run records what it used: assumptions(used, tool, version) -> the "assumptions" entry of
    dam.json, with the keys where the run differs from the shared file.

Pure Python: no QGIS needed (QgsSettings is used when it is there).
"""
import collections
import copy
import datetime
import json
import math
import os

FORMAT = "dam-defaults"
FORMAT_VERSION = 1
FILE_NAME = "defaults.json"
ENV_PATH = "NDSUITE_DEFAULTS"
CONFIG_KEY = "defaults_file"             # suite_config key (QgsSettings ndsuite/defaults_file)
USER_FOLDER = "DamSafety"                # ~/DamSafety/defaults.json, beside ~/DamSafety/Dams

# key -> spec. kind "choice": value is one of `choices` (id -> label); kind "number": a float in
# [minimum, maximum]. `tools` says which tools read it (for the window's help line).
SCHEMA = collections.OrderedDict([
    ("sunny_day_start_pool", {
        "kind": "choice", "label": "Sunny-day breach: starting pool",
        "choices": collections.OrderedDict([
            ("max_pool", "Maximum pool (top of dam; DWR maximum storage)"),
            ("normal_pool", "Normal pool")]),
        "help": "Where the pool stands when a sunny-day (no inflow) breach starts. Suite rule: "
                "the DWR maximum pool unless stated otherwise.",
        "tools": "Screening (run breach simulation), Runner, Crest"}),
    ("pmf_breach_volume", {
        "kind": "choice", "label": "PMF breach: reservoir volume Vw in the breach equations",
        "choices": collections.OrderedDict([
            ("storage_at_start_pool", "Storage at the pool when the breach starts (ND standards)"),
            ("max_storage", "Maximum storage (DWR)")]),
        "help": "The volume the breach equations (Froehlich, MLM, VTG, TR-60) take for a PMF / "
                "flood-inflow breach. A sunny-day breach always uses the storage at its starting "
                "pool (the maximum storage at the maximum pool).",
        "tools": "Runner (flood scenario), Crest"}),
    ("failure_mode", {
        "kind": "choice", "label": "Failure mode",
        "choices": collections.OrderedDict([
            ("piping", "Piping"), ("overtopping", "Overtopping")]),
        "help": "Piping is the ND standard for most dams. Used by the Froehlich equations; "
                "MLM, VTG and TR-60 do not use it.",
        "tools": "Screening (breach hydrograph), Runner"}),
    ("breach_method", {
        "kind": "choice", "label": "Breach width / time equation",
        "choices": collections.OrderedDict([
            ("froehlich2008", "Froehlich (2008)"),
            ("froehlich1995", "Froehlich (1995)"),
            ("mlm1984", "MacDonald & Langridge-Monopolis (1984)"),
            ("vtg1990", "Von Thun & Gillette (1990), erodible"),
            ("tr60", "NRCS TR-60, triangular hydrograph"),
            ("tr60_curvilinear", "NRCS TR-60, curvilinear hydrograph")]),
        "help": "The breach parameter equation a new run starts with.",
        "tools": "Screening (breach hydrograph), Runner"}),
    ("manning_n", {
        "kind": "number", "label": "Manning's n (where there is no n raster or land cover)",
        "minimum": 0.01, "maximum": 0.3, "decimals": 3,
        "help": "The roughness used for cells without an n raster / NLCD class.",
        "tools": "Screening (run breach simulation), Runner, Crest"}),
])
KEYS = tuple(SCHEMA)


class DefaultsError(Exception):
    """A defaults file that cannot be read or written; the message says what to do."""


# ------------------------------------------------------------------- paths --
def shipped_path():
    """DamSafety_Core/defaults.json (the suite's values)."""
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), FILE_NAME)


def default_user_path():
    return os.path.join(os.path.expanduser("~"), USER_FOLDER, FILE_NAME)


def user_path():
    """Your defaults file: the suite setting defaults_file (project, computer, env), else
    ~/DamSafety/defaults.json. The file need not exist."""
    p = ""
    try:
        from . import suite_config
        p = suite_config.get(CONFIG_KEY)
    except Exception:                                            # noqa - outside QGIS
        p = ""
    p = (p or os.environ.get(ENV_PATH, "") or "").strip()
    return os.path.expanduser(p) if p else default_user_path()


# ------------------------------------------------------------------ values --
def check(key, value):
    """(typed value, problem). problem is "" when the value is usable."""
    spec = SCHEMA.get(key)
    if spec is None:
        return None, "unknown key '{}'".format(key)
    if spec["kind"] == "choice":
        s = str(value).strip() if value is not None else ""
        if s in spec["choices"]:
            return s, ""
        for cid, label in spec["choices"].items():          # the label is accepted too
            if s.lower() == label.lower():
                return cid, ""
        return None, "'{}' is not one of {}".format(value, ", ".join(spec["choices"]))
    try:
        v = float(value) if not isinstance(value, bool) else None
    except (TypeError, ValueError):
        v = None
    if v is None or not math.isfinite(v):
        return None, "'{}' is not a number".format(value)
    if v < spec["minimum"] or v > spec["maximum"]:
        return None, "{} is outside {} - {}".format(value, spec["minimum"], spec["maximum"])
    return v, ""


def label_of(key, value):
    spec = SCHEMA.get(key) or {}
    if spec.get("kind") == "choice":
        return spec["choices"].get(value, str(value))
    return "{:g}".format(value) if isinstance(value, (int, float)) else str(value)


def read(path):
    """{key: typed value}, [problems] of one file. Unknown keys and bad values are reported and
    left out. Raises DefaultsError when the file cannot be read at all."""
    try:
        with open(path, encoding="utf-8-sig") as f:
            data = json.load(f)
    except OSError as e:
        raise DefaultsError("the defaults file {} cannot be read: {}".format(path, e))
    except ValueError as e:
        raise DefaultsError("the defaults file {} is not valid JSON: {}".format(path, e))
    if not isinstance(data, dict) or not isinstance(data.get("values", {}), dict):
        raise DefaultsError("the defaults file {} has no 'values' object".format(path))
    if data.get("format") not in (None, FORMAT):
        raise DefaultsError("{} is not a defaults file (format {!r}, expected {!r})".format(
            path, data.get("format"), FORMAT))
    fv = data.get("format_version", 1)
    if isinstance(fv, int) and fv > FORMAT_VERSION:
        raise DefaultsError("{} was written in defaults format {} by a newer tool; this one "
                            "reads format {}. Update DamSafety Core.".format(path, fv, FORMAT_VERSION))
    out, problems = {}, []
    for k, v in data.get("values", {}).items():
        typed, problem = check(k, v)
        if problem:
            problems.append("{}: {} ({})".format(os.path.basename(path), problem, k))
        else:
            out[k] = typed
    return out, problems


class Defaults(object):
    """The merged values. `values` {key: value}; `source` {key: "suite" | "yours"};
    `problems` [text]; `suite` the shipped values; `yours` your file's values."""

    def __init__(self, suite=None, yours=None, problems=None, suite_path="", user_path=""):
        self.suite = dict(suite or {})
        self.yours = dict(yours or {})
        self.values = dict(self.suite, **self.yours)
        self.source = {k: ("yours" if k in self.yours else "suite") for k in self.values}
        self.problems = list(problems or [])
        self.suite_path, self.user_path = suite_path, user_path

    def get(self, key, default=None):
        return self.values.get(key, default)

    def to_dict(self):
        return {"values": dict(self.values), "source": dict(self.source),
                "suite_file": self.suite_path, "your_file": self.user_path}


_cache = {"key": None, "obj": None}


def _mtime(p):
    try:
        return os.path.getmtime(p)
    except OSError:
        return None


def load(user=None, suite=None):
    """The shared defaults: the suite file merged with yours (cached until either file changes).
    Never raises: a file that cannot be read is listed in .problems and left out (a damaged suite
    file leaves every tool on its own built-in defaults, as before Core 1.2.0)."""
    suite = suite or shipped_path()
    user = user or user_path()
    key = (suite, _mtime(suite), user, _mtime(user))
    if _cache["key"] == key and _cache["obj"] is not None:
        return _cache["obj"]
    problems, sv, uv = [], {}, {}
    try:
        sv, p = read(suite)
        problems += p
    except DefaultsError as e:
        problems.append(str(e))
    if user and os.path.isfile(user):
        try:
            uv, p = read(user)
            problems += p
        except DefaultsError as e:
            problems.append(str(e) + " - your values are not used")
    obj = Defaults(sv, uv, problems, suite, user)
    _cache["key"], _cache["obj"] = key, obj
    return obj


def value(key, default=None):
    """One shared value (merged), or `default` when no file gives it."""
    return load().get(key, default)


def save_user(values, path=None):
    """Write YOUR file with the keys of `values` that differ from the suite file (a key equal to
    the suite value is dropped, so it follows later suite updates). Returns the path. Raises
    DefaultsError on a bad value or a write error. An empty result removes nothing: it writes a
    file with no values (Reset)."""
    path = path or user_path()
    suite = load(user=path).suite
    keep = {}
    for k, v in (values or {}).items():
        typed, problem = check(k, v)
        if problem:
            raise DefaultsError("{}: {}".format(k, problem))
        if k in suite and _same(suite[k], typed):
            continue
        keep[k] = typed
    data = {"format": FORMAT, "format_version": FORMAT_VERSION, "name": "Your defaults",
            "note": "Your changes to the suite defaults (DamSafety_Core/defaults.json). A key "
                    "not listed here follows the suite file. Edit in Plugins > Dam safety > "
                    "Shared defaults, or by hand.",
            "saved": datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            "values": {k: keep[k] for k in KEYS if k in keep}}
    folder = os.path.dirname(os.path.abspath(path))
    tmp = os.path.join(folder, ".{}.{}.tmp".format(os.path.basename(path), os.getpid()))
    try:
        os.makedirs(folder, exist_ok=True)
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(data, indent=1) + "\n")
        os.replace(tmp, path)
    except OSError as e:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise DefaultsError("the defaults file {} cannot be written: {}".format(path, e))
    _cache["key"] = None
    return path


def _same(a, b):
    if isinstance(a, float) or isinstance(b, float):
        try:
            return abs(float(a) - float(b)) < 1e-12
        except (TypeError, ValueError):
            return False
    return a == b


# ---------------------------------------------------- tools' parameters --
# The Runner's METHOD combines the failure mode with the equation.
RUNNER_METHOD = {("piping", "froehlich2008"): "piping",
                 ("overtopping", "froehlich2008"): "overtopping",
                 ("piping", "froehlich1995"): "froehlich1995_piping",
                 ("overtopping", "froehlich1995"): "froehlich1995_overtopping"}
RUNNER_METHOD_ANY = {"mlm1984": "mlm1984", "vtg1990": "vtg1990", "tr60": "tr60",
                     "tr60_curvilinear": "tr60_curvilinear"}
SCREEN_METHOD = {"froehlich2008": "F2008", "froehlich1995": "F1995", "vtg1990": "VTG-E",
                 "mlm1984": "MLM", "tr60": "TR60", "tr60_curvilinear": "TR60-C"}


def runner_method(values):
    mode, method = values.get("failure_mode"), values.get("breach_method")
    return RUNNER_METHOD_ANY.get(method) or RUNNER_METHOD.get((mode, method))


def from_runner_method(method_id):
    """(failure_mode, breach_method) of a Runner METHOD id; failure_mode None when the equation
    does not use it; (None, None) for your own hydrograph."""
    for (mode, m), rid in RUNNER_METHOD.items():
        if rid == method_id:
            return mode, m
    if method_id == "piping_orifice":
        return "piping", "froehlich2008"
    for m, rid in RUNNER_METHOD_ANY.items():
        if rid == method_id:
            return None, m
    return None, None


# (plugin, tool or None = every tool, parameter key, function(values) -> value). An enum value is
# an option ID: the tool class lists its options' IDs in SHARED_OPTION_IDS = {key: [ids in option
# order]}; a tool without that list (or with a list of another length) is left alone.
BINDINGS = (
    ("ndbreach", None, "MANNING", lambda v: v.get("manning_n")),
    ("ndbreach", None, "METHOD", runner_method),
    # Runner 3.19.0: the sunny-day starting pool is a choice of the run tools (START_POOL)
    ("ndbreach", None, "START_POOL",
     lambda v: {"max_pool": "top", "normal_pool": "normal"}.get(v.get("sunny_day_start_pool"))),
    ("ndscreen", "runbreachsim", "NDEF", lambda v: v.get("manning_n")),
    ("ndscreen", "runbreachsim", "SCEN",
     lambda v: {"max_pool": "td", "normal_pool": "sd"}.get(v.get("sunny_day_start_pool"))),
    ("ndscreen", "breachhydrograph", "MODE", lambda v: v.get("failure_mode")),
    ("ndscreen", "breachhydrograph", "METHOD", lambda v: SCREEN_METHOD.get(v.get("breach_method"))),
)


def bindings_for(plugin, tool):
    """[(parameter key, function)] bound for one tool."""
    return [(k, fn) for p, t, k, fn in BINDINGS if p == plugin and (t is None or t == tool)]


# ------------------------------------------------------------ the record --
def assumptions(used, tool, version, defaults=None, note=""):
    """The "assumptions" entry a run writes into dam.json: what it used, and where that differs
    from the shared file. `used` {key: value} (keys of SCHEMA; a value None = not used by this
    run, e.g. failure_mode for TR-60, pmf_breach_volume for a sunny-day run)."""
    d = defaults if defaults is not None else load()
    used_ok = {k: v for k, v in (used or {}).items() if k in SCHEMA}
    differs = {}
    for k, v in used_ok.items():
        shared = d.get(k)
        if v is not None and shared is not None and not _same(v, shared):
            differs[k] = {"used": v, "shared": shared}
    return {"used": used_ok, "differs": differs,
            "shared_values": copy.deepcopy(d.values), "shared_source": dict(d.source),
            "suite_file": d.suite_path,
            "your_file": d.user_path if any(s == "yours" for s in d.source.values()) else "",
            "tool": str(tool or ""), "version": str(version or ""),
            "when": datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S"), "note": str(note or "")}


def differs_text(entry):
    """Plain sentences for a run report / log: where the run differs from the shared file."""
    out = []
    for k, dv in sorted((entry or {}).get("differs", {}).items()):
        out.append("{} = {} for this run; the shared defaults say {}.".format(
            SCHEMA[k]["label"], label_of(k, dv["used"]), label_of(k, dv["shared"])))
    return out
