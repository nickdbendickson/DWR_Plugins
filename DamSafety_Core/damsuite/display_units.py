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
    "ft²": ("m²", 0.09290304),
    "mph": ("km/h", 1.609344),
}
# spellings in the labels -> the stored unit above
ALIASES = {
    "ft": "ft", "feet": "ft", "foot": "ft",
    "ft/s": "ft/s", "fps": "ft/s",
    "miles": "miles", "mile": "miles", "mi": "miles",
    "acres": "acres", "acre": "acres", "ac": "acres",
    "ac-ft": "ac-ft", "acft": "ac-ft", "acre-ft": "ac-ft", "acre-feet": "ac-ft", "af": "ac-ft",
    "cfs": "cfs",
    "in": "in", "inches": "in", "inch": "in",
    "sq mi": "sq mi", "mi2": "sq mi", "mi²": "sq mi",
    "ft²": "ft²", "ft2": "ft²", "sq ft": "ft²", "sf": "ft²",
    "mph": "mph",
}
# decimals a spin box shows for each SI unit
DECIMALS = {"m": 3, "m/s": 3, "km": 3, "ha": 3, "m³": 1, "m³/s": 3, "mm": 1, "km²": 4, "m²": 1,
            "km/h": 1}

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


def resuffix(suffix, unit, system_value=None):
    """A spin box suffix under the display system: ' ft' -> ' m', ' ft (0 = half the typical
    cell size)' -> ' m (0 ft = half the typical cell size)'. A suffix that does not start with
    the stored unit comes back unchanged."""
    sysv = system() if system_value is None else normalize(system_value)
    if sysv != SI or not suffix:
        return suffix
    c = canonical(unit)
    if c is None:
        return suffix
    lead = suffix[:len(suffix) - len(suffix.lstrip())]
    body = suffix.lstrip()
    for alias in sorted((a for a, v in ALIASES.items() if v == c), key=len, reverse=True):
        if body.lower().startswith(alias) and (len(body) == len(alias)
                                               or not body[len(alias)].isalnum()):
            rest = body[len(alias):]
            if rest.strip().startswith("(") and rest.strip().endswith(")"):
                inner = relabel("(" + alias + "; " + rest.strip()[1:], sysv)
                return lead + SI_UNITS[c][0] + " " + "(" + inner[len("(" + SI_UNITS[c][0] + "; "):]
            return lead + SI_UNITS[c][0] + rest
    return suffix



# -- reports (Core 1.1.0) ------------------------------------------------------------------------
# A report is written from values in the stored units. Under SI its human-readable text is
# converted as the last step: every "<number> <unit>" quantity, "<a> x <b> mi" sizes, and the
# numeric cells of a table column whose header names a unit ("Depth (ft)", "Depth ft"). The data
# files a tool reads back (run_report.json, dam.json, CSVs with _ft / _ac columns, rasters) are
# never converted.

_TEXT_UNITS = {           # spelled in reports -> canonical stored unit
    "ft/s": "ft/s", "fps": "ft/s", "ft2/s": "ft²/s", "ft²/s": "ft²/s",
    "ac-ft": "ac-ft", "acft": "ac-ft", "acre-ft": "ac-ft", "acre-feet": "ac-ft", "af": "ac-ft",
    "acres": "acres", "acre": "acres", "ac": "acres",
    "cfs": "cfs", "sq mi": "sq mi", "mi²": "sq mi", "mi2": "sq mi",
    "ft²": "ft²", "ft2": "ft²", "sq ft": "ft²",
    "ft": "ft", "feet": "ft", "foot": "ft",
    "miles": "miles", "mile": "miles", "mi": "miles", "mph": "mph",
}
SI_UNITS.setdefault("ft²/s", ("m²/s", 0.09290304))
ALIASES.setdefault("ft²/s", "ft²/s")
ALIASES.setdefault("ft2/s", "ft²/s")
DECIMALS.setdefault("m²/s", 2)
_TU = "|".join(re.escape(u) for u in sorted(_TEXT_UNITS, key=len, reverse=True))
_N = r"-?\d{1,3}(?:,\d{3})+(?:\.\d+)?|-?\d+(?:\.\d+)?"
_SEP = r"\s*(?:x|×|by|to|vs\.?|and|or|/|-|–)\s*|,\s+"
# one quantity or a chain of them sharing the unit at the end: '0.1 / 1 / 2 ft',
# '4.86 / 13.09 vs 4.84 / 13.09 ft/s', '10.21 x 9.23 mi'
_QTY = re.compile(r"(?<![\w.\-/])((?:" + _N + r")(?:(?:" + _SEP + r")(?:" + _N + r"))*)"
                  r"(\s?)(" + _TU + r")(?![\w²])")
_KV = re.compile(r"(?<!\d )(?<!\d\()(?<=[\s(])(" + _TU + r")(:\s*)(" + _N + r")(?![\w.])")
_LEAD = re.compile(r"(\s*)((?:" + _N + r")(?:(?:\s*(?:x|×|/|-|–)\s*)(?:" + _N + r"))*)(?![\w.])")
_NUM_IN = re.compile(r"(?<![\w.])(" + _N + r")(?![\w.]*\d)")


def _fmt_like(original, value):
    """`value` written with about as much precision as `original` had (at least 3 significant
    figures), with thousands separators when the original had them."""
    o = original.replace(",", "")
    dec = len(o.split(".")[1]) if "." in o else 0
    mag = abs(value)
    if mag > 0:
        import math
        sig_dec = max(0, 2 - int(math.floor(math.log10(mag))))
        dec = max(dec, sig_dec)
    dec = min(dec, 4)
    txt = ("{:,.%df}" % dec).format(value) if "," in original or mag >= 10000 else \
        ("{:.%df}" % dec).format(value)
    if "." in txt:
        txt = txt.rstrip("0").rstrip(".")
    return txt


def convert_text(text, system_value=None):
    """Quantities in running text in the display system ('3,215 cfs' -> '91.04 m³/s',
    '4.0 x 2.5 mi' -> '6.44 x 4.02 km'). Under US the text comes back unchanged."""
    sysv = system() if system_value is None else normalize(system_value)
    if sysv != SI or not text:
        return text

    def one(m):
        unit = _TEXT_UNITS.get(m.group(3).lower())
        if unit is None or unit not in SI_UNITS:
            return m.group(0)
        si, f = SI_UNITS[unit]
        nums = re.sub(r"(?<![\w.])(" + _N + r")",
                      lambda n: _fmt_like(n.group(1), float(n.group(1).replace(",", "")) * f),
                      m.group(1))
        return nums + (m.group(2) or " ") + si
    def kv(m):                     # 'peak cfs: 28671.4' (a key named by its unit, then a value)
        unit = _TEXT_UNITS.get(m.group(1).lower())
        if unit is None or unit not in SI_UNITS:
            return m.group(0)
        si, f = SI_UNITS[unit]
        return "{}{}{}".format(si, m.group(2),
                               _fmt_like(m.group(3), float(m.group(3).replace(",", "")) * f))
    text = _KV.sub(kv, text)
    return _QTY.sub(one, text)


def header_unit(header):
    """The stored unit a table column header names: 'Depth (ft)', 'Depth ft', 'Vel ft/s',
    'Flooded area (acres)' -> canonical unit, else None."""
    h = (header or "").strip()
    m = re.search(r"\((" + _TU + r")\)\s*$", h, re.I) or \
        re.search(r"(?:^|\s)(" + _TU + r")\s*$", h, re.I)
    if not m:
        return None
    return _TEXT_UNITS.get(m.group(1).lower())


def convert_header(header, system_value=None):
    sysv = system() if system_value is None else normalize(system_value)
    u = header_unit(header)
    if sysv != SI or u is None or u not in SI_UNITS:
        return header
    si = SI_UNITS[u][0]
    return re.sub(r"(" + _TU + r")(\)?\s*)$", lambda m: si + m.group(2), header, count=1,
                  flags=re.I)


def convert_cell(cell, unit, system_value=None):
    """One value of a column (or row) whose header names `unit`: the number(s) the cell starts
    with ('12.3', '10.21 x 9.23', '0.0/5.89/2.9/0.0', '1,068 (from the DEM)') are converted;
    the rest of the text has only its own '<number> <unit>' quantities converted."""
    sysv = system() if system_value is None else normalize(system_value)
    if sysv != SI or unit not in SI_UNITS or cell is None:
        return cell
    t = str(cell)
    if _QTY.search(t):
        return convert_text(t, sysv)       # the cell names its own unit ('6,917 ac')
    f = SI_UNITS[unit][1]
    m = _LEAD.match(t)
    if not m or not m.group(2):
        return convert_text(t, sysv)
    lead = re.sub(r"(?<![\w.])(" + _N + r")(?![\w])",
                  lambda n: _fmt_like(n.group(1), float(n.group(1).replace(",", "")) * f),
                  m.group(2))
    return m.group(1) + lead + convert_text(t[m.end():], sysv)


def _row_unit(row):
    """The unit a key / value row names in its first cell ('Dam height (ft)'), else None."""
    if not row or not isinstance(row[0], str):
        return None
    m = re.search(r"\((" + _TU + r")\)\s*$", row[0].strip(), re.I)   # only '(ft)' at the end
    return _TEXT_UNITS.get(m.group(1).lower()) if m else None


def convert_table(head, rows, system_value=None):
    """(head, rows) with unit columns converted (headers relabelled, numeric cells converted)
    and every other cell through convert_text."""
    sysv = system() if system_value is None else normalize(system_value)
    if sysv != SI:
        return head, rows
    units = [header_unit(h) for h in head]
    new_head = [convert_header(h, sysv) for h in head]
    new_rows = []
    for r in rows:
        new_rows.append(convert_row(r, units, sysv))
    return new_head, new_rows


def convert_row(r, units, system_value=None):
    """One table row: cells under a unit column converted as values; a key / value row whose
    first cell names a unit ('Dam height (ft)' | '25.0') has its label and values converted;
    any other text cell has its quantities converted."""
    sysv = system() if system_value is None else normalize(system_value)
    ru = _row_unit(list(r))
    out = []
    for i, c in enumerate(r):
        u = units[i] if units and i < len(units) else None
        if not isinstance(c, str):
            out.append(c)
        elif u:
            out.append(convert_cell(c, u, sysv))
        elif ru and i == 0:
            out.append(convert_header(convert_text(c, sysv), sysv)
                       if header_unit(convert_text(c, sysv)) == ru else c)
        elif ru:
            out.append(convert_cell(c, ru, sysv))
        else:
            out.append(convert_text(c, sysv))
    return out


def convert_markdown(md, system_value=None):
    """A markdown report in the display system: pipe tables column-aware, other lines through
    convert_text. Under US the text comes back unchanged."""
    sysv = system() if system_value is None else normalize(system_value)
    if sysv != SI or not md:
        return md
    out, head_units = [], None
    lines = md.split("\n")
    for i, line in enumerate(lines):
        st = line.strip()
        if st.startswith("|") and st.endswith("|") and len(st) > 1:
            cells = st[1:-1].split("|")
            nxt = lines[i + 1].strip() if i + 1 < len(lines) else ""
            if re.fullmatch(r"\|?[\s:\-|]+\|?", nxt or "x") and head_units is None:
                head_units = [header_unit(c) for c in cells]          # the header row
                out.append("|" + "|".join(" " + convert_header(c.strip(), sysv) + " "
                                          if c.strip() else c for c in cells) + "|")
                continue
            if re.fullmatch(r"[\s:\-]*", "".join(cells)):
                out.append(line)                                       # the --- row
                continue
            stripped = [c.strip() for c in cells]
            new = convert_row(stripped, head_units or [], sysv)
            out.append("|" + "|".join(" " + n + " " if n else "" for n in new) + "|")
            continue
        head_units = None
        out.append(convert_text(line, sysv))
    return "\n".join(out)


def report_note(system_value=None):
    """One line for the top of a report written under SI."""
    sysv = system() if system_value is None else normalize(system_value)
    if sysv != SI:
        return ""
    return ("Units: SI (m, ha, m³, m³/s, km), converted for this report from the US customary "
            "values the tools compute and store; the data files keep US units.")


# -- CSV files (Core 1.4.0) ----------------------------------------------------------------------
# A result CSV keeps its US columns and its name: other tools read it back. Under SI a copy is
# written in the sub-folder SI_units beside it, under the same file name, with an SI column
# right after every column whose name says its unit ('Max_Depth_ft' -> 'Max_Depth_m',
# 'Peak (cfs)' -> 'Peak (m³/s)'). Nothing reads the copy back; it is for the engineer who opens
# the table. A sub-folder (not a second name in the same folder) keeps the copies out of the way
# of every tool that lists a folder's CSVs.

SI_CSV_DIR = "SI_units"
# column-name ending -> (stored unit, the ending of the SI column)
_CSV_ENDINGS = (
    ("_ft2s", "ft²/s", "_m2s"), ("_acft", "ac-ft", "_m3"), ("_ac_ft", "ac-ft", "_m3"),
    ("_acres", "acres", "_ha"), ("_miles", "miles", "_km"), ("_sqmi", "sq mi", "_km2"),
    ("_mi2", "sq mi", "_km2"), ("_sqft", "ft²", "_m2"), ("_ft2", "ft²", "_m2"),
    ("_fps", "ft/s", "_mps"), ("_cfs", "cfs", "_cms"), ("_mph", "mph", "_kmh"),
    ("_af", "ac-ft", "_m3"), ("_ac", "acres", "_ha"), ("_mi", "miles", "_km"),
    ("_ft", "ft", "_m"),
)
# '_in' ends many names that are not inches (built_in, also_in): only after these words
_CSV_INCH_WORDS = ("depth", "loss", "rain", "rainfall", "precip", "pmp", "runoff")
_CSV_NUM = re.compile(r"^\s*[-+]?(?:\d{1,3}(?:,\d{3})+|\d+)?(?:\.\d+)?(?:[eE][-+]?\d+)?\s*$")


def csv_column_unit(name):
    """(stored unit, SI column name) of a CSV column whose name says its unit, else
    (None, None): 'Max_Depth_ft' -> ('ft', 'Max_Depth_m'); 'Peak (cfs)' -> ('cfs',
    'Peak (m³/s)'). A column that is already metric, or names no unit, gives (None, None)."""
    n = (name or "").strip()
    low = n.lower()
    for end, unit, si_end in _CSV_ENDINGS:
        if low.endswith(end) and len(n) > len(end):
            return unit, n[:len(n) - len(end)] + (si_end.upper() if n[-len(end):].isupper()
                                                  else si_end)
    if low.endswith("_in") and low[:-3].rsplit("_", 1)[-1] in _CSV_INCH_WORDS:
        return "in", n[:-3] + ("_MM" if n[-3:].isupper() else "_mm")
    u = header_unit(n)
    if u in SI_UNITS and "(" in n:                 # only '(ft)' style: a bare 'ft' is a word
        return u, convert_header(n, SI)
    return None, None


def csv_cell_si(cell, unit):
    """One CSV value in SI: a plain number is converted (about the precision it had); a blank,
    a word or a mixed cell gives ''."""
    t = "" if cell is None else str(cell)
    if not t.strip() or not _CSV_NUM.match(t) or unit not in SI_UNITS:
        return ""
    try:
        v = float(t.replace(",", ""))
    except ValueError:
        return ""
    if v != v or v in (float("inf"), float("-inf")):
        return ""
    si = v * SI_UNITS[unit][1]
    if "e" in t.lower():
        return repr(si)
    # the decimals the value had, and at least four significant figures; no separators
    body = t.strip().replace(",", "")
    dec = len(body.split(".")[1]) if "." in body else 0
    if si:
        import math
        dec = max(dec, 3 - int(math.floor(math.log10(abs(si)))))
    txt = ("{:.%df}" % max(0, min(dec, 6))).format(si)
    return txt.rstrip("0").rstrip(".") if "." in txt else txt


def si_table(fieldnames, rows):
    """(fieldnames, rows) with an SI column right after each unit column. `rows` are lists in
    the order of `fieldnames`. A table with no unit column comes back as (None, None). An SI
    name the table already has is not added again, nor is a column with no number in it."""
    names = list(fieldnames)
    rows = [list(r) for r in rows]
    taken = {n for n in names}
    plan, out_names = [], []
    for i, n in enumerate(names):
        unit, si = csv_column_unit(n)
        if unit and si not in taken and any(
                csv_cell_si(r[i], unit) for r in rows if i < len(r)):   # a number to convert
            taken.add(si)
            plan.append((unit, si))
        else:
            plan.append((None, None))
        out_names.append(n)
        if plan[-1][0]:
            out_names.append(si)
    if not any(u for u, _ in plan):
        return None, None
    out_rows = []
    for r in rows:
        new = []
        for i, (unit, _si) in enumerate(plan):
            c = r[i] if i < len(r) else ""
            new.append(c)
            if unit:
                new.append(csv_cell_si(c, unit))
        new.extend(r[len(plan):])                  # a row longer than the header keeps its tail
        out_rows.append(new)
    return out_names, out_rows


def si_csv_path(path):
    """<folder>/SI_units/<name> for the CSV at <folder>/<name>."""
    return os.path.join(os.path.dirname(os.path.abspath(path)), SI_CSV_DIR,
                        os.path.basename(path))


def is_si_copy(path):
    """True for a file inside an SI_units folder."""
    return os.path.basename(os.path.dirname(os.path.abspath(path))).lower() == SI_CSV_DIR.lower()


def write_si_csv(path, system_value=None, header_row=0):
    """Write SI_units/<name> beside the CSV at `path`: the same table with an SI column after
    each column that names a US unit. Returns the path written, '' under US, when the table
    has no unit column, or when anything goes wrong (a copy for reading never stops a run).
    A copy left by an earlier run is removed when the table no longer has a unit column.
    `header_row` is the index of the header line (lines before it are copied as they are)."""
    import csv
    sysv = system() if system_value is None else normalize(system_value)
    if sysv != SI or not path:
        return ""
    try:
        if is_si_copy(path) or not os.path.isfile(path):
            return ""
        out = si_csv_path(path)
        with open(path, newline="", encoding="utf-8-sig") as f:
            table = list(csv.reader(f))
        names, rows = (None, None) if len(table) <= header_row else \
            si_table(table[header_row], table[header_row + 1:])
        if names is None:
            if os.path.isfile(out):                 # a stale copy would show old numbers
                os.remove(out)
            return ""
        os.makedirs(os.path.dirname(out), exist_ok=True)
        tmp = out + ".tmp"
        with open(tmp, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerows(table[:header_row])
            w.writerow(names)
            w.writerows(rows)
        os.replace(tmp, out)
        return out
    except Exception:                                             # noqa: BLE001
        return ""


def write_si_csvs(paths, system_value=None):
    """write_si_csv for each path (missing files and non-CSV names are skipped); the copies
    written."""
    sysv = system() if system_value is None else normalize(system_value)
    if sysv != SI:
        return []
    out = []
    for p in paths or []:
        if p and str(p).lower().endswith(".csv"):
            w = write_si_csv(str(p), sysv)
            if w:
                out.append(w)
    return out


def write_si_csvs_in(folder, system_value=None, recursive=False):
    """write_si_csv for every CSV in `folder` (and, with recursive, below it; never the
    copies themselves); the copies written."""
    sysv = system() if system_value is None else normalize(system_value)
    if sysv != SI or not folder or not os.path.isdir(folder):
        return []
    found = []
    for base, dirs, files in os.walk(folder):
        dirs[:] = sorted(d for d in dirs if d.lower() != SI_CSV_DIR.lower())
        if os.path.basename(base).lower() != SI_CSV_DIR.lower():
            found.extend(os.path.join(base, n) for n in sorted(files)
                         if n.lower().endswith(".csv"))
        if not recursive:
            break
    _prune_si_copies(folder, recursive)
    return write_si_csvs(found, sysv)


def _prune_si_copies(folder, recursive=False):
    """Remove the copies in <folder>/SI_units whose table is gone (a retired or renamed
    output), so the folder never shows numbers of an older run. Only .csv files in an
    SI_units folder are touched."""
    for base, dirs, _files in os.walk(folder):
        for d in list(dirs):
            if d.lower() != SI_CSV_DIR.lower():
                continue
            sub = os.path.join(base, d)
            try:
                for n in os.listdir(sub):
                    if n.lower().endswith(".csv") and not os.path.isfile(os.path.join(base, n)):
                        os.remove(os.path.join(sub, n))
                if not os.listdir(sub):
                    os.rmdir(sub)
            except OSError:
                pass
        dirs[:] = [d for d in dirs if d.lower() != SI_CSV_DIR.lower()]
        if not recursive:
            break


def write_si_csvs_of(results, system_value=None):
    """The SI copies for what a tool returned (`results`: its outputs, a dict or a list):
    every value that is a CSV file, and the CSVs in the folder of every report it names (a
    .md / .html / .docx / .pdf file). Other folders and files are left alone, so a tool that
    returns a terrain or an input never gets copies in the user's own data folders."""
    sysv = system() if system_value is None else normalize(system_value)
    if sysv != SI:
        return []
    vals = list(results.values()) if isinstance(results, dict) else list(results or [])
    files, folders = [], []
    for v in vals:
        if not isinstance(v, str) or not v or "\n" in v or len(v) > 1024:
            continue
        low = v.lower()
        try:
            if low.endswith(".csv") and os.path.isfile(v):
                files.append(v)
            elif low.endswith((".md", ".html", ".docx", ".pdf")) and os.path.isfile(v):
                d = os.path.dirname(os.path.abspath(v))
                if d not in folders:
                    folders.append(d)
        except (OSError, ValueError):
            continue
    out = write_si_csvs(files, sysv)
    for d in folders:
        out.extend(w for w in write_si_csvs_in(d, sysv) if w not in out)
    return out
