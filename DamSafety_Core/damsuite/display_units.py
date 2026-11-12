"""The unit system the windows SHOW and ACCEPT (Core 1.1.0): US customary (the default) or SI.

Presentation only. Every tool keeps its parameters, its files and its computations in the units
it has always used: the parameter keys say so (HEAD_FT, NORMAL_POOL_FT, ...), dam.json and the
settings workbook keep feet / acres / acre-feet / cfs, and a qgis_process call, a Processing
model, a batch run and a run log mean exactly what they meant before. Choosing SI changes what
an engineer sees in a window and types into it: a value is converted from the stored unit for
display and back to the stored unit when the tool runs. No datum changes - an elevation in feet
on a dam's local datum is shown as the same elevation in metres on the same datum.

    system()                  'us' or 'si' (suite_config key unit_system: project, then
                              computer setting ndsuite/unit_system, then env NDSUITE_UNITS)
    display_unit('ft')        'm' under SI, 'ft' under US
    to_display(15, 'ft')      4.572 under SI
    from_display(4.572, 'ft') 15.0 under SI
    unit_in_label(desc)       the stored unit a parameter label declares, e.g. 'ft' for
                              'Crest width (ft): TR-60 ...'
    relabel(desc)             the label as shown under SI: the unit of the FIRST parenthesis
                              swapped, bare numbers in the
                              same parentheses marked with the stored unit ('starting value 15'
                              -> 'starting value 15 ft') so nothing reads as metres that is not

Under US nothing is converted and no label changes. Units that are already metric (cell sizes in
m, rainfall in mm) are left alone in both systems; the US system does not turn metres into feet.

Pure Python: no QGIS, no Qt (suite_config is imported lazily and only reads settings).
"""
import os
import re

US = "us"
SI = "si"
SYSTEMS = (US, SI)
LABELS = {US: "US customary (ft, acres, ac-ft, cfs)", SI: "SI / metric (m, ha, m³, m³/s)"}
KEY = "unit_system"                 # suite_config key -> QgsSettings ndsuite/unit_system
ENV = "NDSUITE_UNITS"

# stored unit -> (SI display unit, SI units per stored unit). Exact factors (international foot,
# acre from its definition in square feet; see damsuite.units).
_FT = 0.3048
SI_UNITS = {
    "ft": ("m", _FT),
    "ft/s": ("m/s", _FT),
    "miles": ("km", 1.609344),
    "mi": ("km", 1.609344),
    "acres": ("ha", 0.40468564224),
    "ac-ft": ("m³", 1233.48183754752),
    "cfs": ("m³/s", 0.028316846592),
    "in": ("mm", 25.4),
    "sq mi": ("km²", 2.589988110336),
}
# spellings in the labels -> the stored unit above
ALIASES = {
    "ft": "ft", "feet": "ft", "foot": "ft",
    "ft/s": "ft/s", "fps": "ft/s",
    "miles": "miles", "mile": "miles", "mi": "miles",
    "acres": "acres", "acre": "acres", "ac": "acres",
    "ac-ft": "ac-ft", "acre-ft": "ac-ft", "acre-feet": "ac-ft", "af": "ac-ft",
    "cfs": "cfs",
    "in": "in", "inches": "in", "inch": "in",
    "sq mi": "sq mi", "mi2": "sq mi", "mi²": "sq mi",
}
# decimals a spin box shows for each SI unit
DECIMALS = {"m": 3, "m/s": 3, "km": 3, "ha": 3, "m³": 1, "m³/s": 3, "mm": 1, "km²": 4}

# '(ft)' '(ft;' '(ft,' '(ft:' at the start of a parenthesis; longest spellings first
_ALT = "|".join(re.escape(a) for a in sorted(ALIASES, key=len, reverse=True))
_PAREN_UNIT = re.compile(r"\((" + _ALT + r")(?=[);,:])")
# a bare number: not part of a word, a version (3.10.84), a range (TR-60) or followed by a unit
_NUM = re.compile(r"(?<![\w.\-/])(\d+(?:,\d{3})*(?:\.\d+)?)(?![\w.\-/%]|\.\d|\s*(?:" + _ALT
                  + r"|m\b|%|hr|h\b|hours|cells|x\b))")

# a number after these words names something (tool 8, item 49, REG 5.4), not a quantity
_NOT_A_QUANTITY = ("tool", "tools", "item", "step", "fig.", "figure", "table", "section", "reg",
                   "version", "v", "class", "tier", "pass", "nbi", "tr-")


def normalize(system_value):
    v = str(system_value or "").strip().lower()
    if v in ("si", "metric", "m", "meters", "metres"):
        return SI
    return US


def system(project=None):
    """The chosen system: suite_config (project entry, computer setting, env NDSUITE_UNITS)."""
    v = ""
    try:
        from . import suite_config
        v = suite_config.get(KEY, project=project)
    except Exception:                                             # noqa - no QGIS, bad project
        v = ""
    return normalize(v or os.environ.get(ENV, ""))


def set_system(value, scope="computer", project=None):
    from . import suite_config
    suite_config.set(KEY, normalize(value), scope=scope, project=project)


def canonical(unit):
    """'feet' -> 'ft'; an unknown unit -> None."""
    if unit is None:
        return None
    return ALIASES.get(str(unit).strip().lower())


def display_unit(unit, system_value=None):
    """The unit a stored `unit` is shown in. Unknown units come back unchanged."""
    sysv = system() if system_value is None else normalize(system_value)
    c = canonical(unit)
    if sysv != SI or c is None:
        return unit
    return SI_UNITS[c][0]


def factor(unit, system_value=None):
    """Display units per stored unit (1.0 when nothing is converted)."""
    sysv = system() if system_value is None else normalize(system_value)
    c = canonical(unit)
    if sysv != SI or c is None:
        return 1.0
    return SI_UNITS[c][1]


def to_display(value, unit, system_value=None):
    if value is None:
        return None
    return float(value) * factor(unit, system_value)


def from_display(value, unit, system_value=None):
    if value is None:
        return None
    return float(value) / factor(unit, system_value)


def decimals(unit, system_value=None, default=2):
    du = display_unit(unit, system_value)
    return DECIMALS.get(du, default) if du != unit else default


def fmt(value, unit, system_value=None, places=None):
    """'4.57 m' / '15 ft' - a stored value written in the display system."""
    if value is None:
        return ""
    v = to_display(value, unit, system_value)
    p = decimals(unit, system_value) if places is None else places
    txt = ("{:,.%df}" % p).format(v)
    if "." in txt:
        txt = txt.rstrip("0").rstrip(".")
    return "{} {}".format(txt, display_unit(unit, system_value))


def _first_unit_paren(desc):
    """The match of the label's FIRST parenthesis when it opens with a convertible unit, else
    None. Only the first counts: 'failure time (hr) or pool elevation (ft)' is a value whose
    unit depends on another choice, and it must not be converted."""
    desc = desc or ""
    i = desc.find("(")
    if i < 0:
        return None
    m = _PAREN_UNIT.match(desc, i)
    return m


def unit_in_label(desc):
    """The stored unit a label declares in its first parenthesis ('(ft', '(acres;' ...),
    canonical, or None (no parenthesis, or the first one is not a convertible unit)."""
    m = _first_unit_paren(desc)
    return canonical(m.group(1)) if m else None


def _paren_end(text, start):
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return i
    return len(text)


def relabel(desc, system_value=None):
    """The label shown under SI. The first unit parenthesis gets the SI unit; bare numbers in
    that parenthesis (starting values, sentinels) keep their meaning by naming the stored unit.
    Under US, or for a label with no convertible unit, `desc` comes back unchanged."""
    sysv = system() if system_value is None else normalize(system_value)
    if sysv != SI or not desc:
        return desc
    m = _first_unit_paren(desc)
    if not m:
        return desc
    stored = canonical(m.group(1))
    si = SI_UNITS[stored][0]
    end = _paren_end(desc, m.start())
    rest = desc[m.end():end]
    stored_word = stored if stored != "miles" else "mi"

    def _mark(n):
        before = rest[:n.start()].rstrip().lower()
        if before.endswith(_NOT_A_QUANTITY):
            return n.group(0)
        return "{} {}".format(n.group(1), stored_word)
    rest = _NUM.sub(_mark, rest)
    return desc[:m.start()] + "(" + si + rest + desc[end:]
