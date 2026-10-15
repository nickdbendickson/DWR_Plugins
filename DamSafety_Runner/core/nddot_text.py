"""The NDDOT culvert description parser ("DOUBLE, 10X7X88' RCB" -> barrels, span, rise,
barrel length, shape), copied verbatim from ND Dam Hazard's runnerculverts.py so the Runner
can read the NDDOT bridge inventory without the Hazard plugin installed.

Do not edit here: edit DamSafety_Hazard/runnerculverts.py and copy the functions across.
tests/test_culvert_match.py checks that both copies are the same, function by function.
"""

import math
import re

def _txt(v):
    if v is None:
        return ""
    s = str(v).strip().strip("'").strip()
    return "" if s.upper() == "NULL" else s


def _num(v):
    try:
        x = float(str(v).strip()) if isinstance(v, str) else float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _pos(v):
    x = _num(v)
    return x if x is not None and x > 0 else None


_COUNT_WORDS = {"SINGLE": 1, "ONE": 1, "DOUBLE": 2, "DOUBLES": 2, "DBL": 2, "TWO": 2, "TWIN": 2,
                "TRIPLE": 3, "TRIPPLE": 3, "TRPL": 3, "TPL": 3, "THREE": 3, "QUAD": 4,
                "QUADRUPLE": 4, "QUARD": 4, "FOUR": 4, "QUINT": 5, "QUINTUPLE": 5, "PENTA": 5,
                "FIVE": 5, "SIX": 6, "SEXTUPLE": 6, "SEPTUPLE": 7, "SEVEN": 7, "EIGHT": 8,
                "NINE": 9, "TEN": 10}


_ARCH_WORDS = ("SPPA", "CMPA", "CSPA", "CMPAS", "RCPA", "RCAP", "ESPP", "ARCH", "ELLIP",
               "CON-SPAN", "CONSPAN", "CON/SPAN")


_BOX_WORDS = ("RCB", "RCBC", "PCRCB", "PRCB", "PRBC", "PCBC", "PCRBC", "RBC", "PRCBC", "RPCB",
              "BOX", "PCB", "PRECAST", "PRECASE", "PRECASR", "PREACST", "PREC", "CIP", "RCBS")


_PIPE_WORDS = ("SPP", "CSP", "CMP", "SMP", "SSP", "RCP", "PIPE", "MULTIPLATE", "MULTI-PLATE",
               "STEEL CULVERT", "CMP'S", "PRCSP")


_MEASURE = (r"(?P<a>\d+(?:\.\d+)?)"
            r"(?:\s*(?P<au>'|FT\.?|\"|IN\.?)(?:\s*-?\s*(?P<b>\d{1,2})\s*(?P<bu>\"|IN\.?)(?!\d))?"
            r"|-(?P<b2>\d{1,2})(?!\d)(?P<bu2>'|\")?)?")


_SEP = r"\s*X\s*(?:'\s*)?"


_SIZE_FT = (1.0, 60.0)                          # plausible span / rise


_LEN_FT = (5.0, 600.0)                          # plausible barrel length


def _norm_text(s):
    s = _txt(s).upper().replace("&#39;", "'").replace("’", "'").replace("‘", "'")
    s = s.replace("“", '"').replace("”", '"').replace("''", "'")
    return " ".join(s.split())


def _measure_ft(m):
    """Feet from a _MEASURE match: the unit on the first number (' or FT = feet, " or IN =
    inches, none = feet), a second number = inches."""
    a = float(m.group("a"))
    au = (m.group("au") or "").replace(".", "")
    if au in ('"', "IN"):
        return None if m.group("b") else a / 12.0
    b = m.group("b") or m.group("b2")
    return a + (float(b) / 12.0 if b else 0.0)


def _measures(text, start=0):
    """The first run of two or three sizes joined by X in text[start:]:
    (values in ft, the unit written on each first number, end index) or ([], [], start)."""
    mre, sep = re.compile(_MEASURE), re.compile(_SEP)
    pos = start
    while pos < len(text):
        m = mre.search(text, pos)
        if not m:
            break
        if m.start() > start and (text[m.start() - 1].isdigit() or text[m.start() - 1] in ".-"):
            pos = m.end() if m.end() > pos else pos + 1
            continue
        vals, units, end = [_measure_ft(m)], [m.group("au") or ""], m.end()
        while len(vals) < 3:
            sm = sep.match(text, end)
            m2 = mre.match(text, sm.end()) if sm else None
            if not m2:
                break
            vals.append(_measure_ft(m2))
            units.append(m2.group("au") or "")
            end = m2.end()
        if len(vals) >= 2:
            return vals, units, end
        pos = end if end > pos else pos + 1
    return [], [], start


def _count_prefix(s):
    """(barrels or None, index where the sizes may start) from the start of a description:
    "DOUBLE, ", "DBL", "(2)-", "3-81\"", "2-10'x", "5 73\"", "3 DOUBLES" / "3-DOUBLE" (= 6)."""
    n, at = None, 0
    m = re.match(r"^\((\d{1,2})\)\s*-?\s*", s) or \
        re.match(r"^(\d{1,2})\s*-\s*(?=[A-Z]|\d+(?:\.\d+)?\s*(?:'|\"|FT|IN))", s) or \
        re.match(r"^(\d{1,2})\s+(?=\d)", s)
    if m is None:
        m = re.match(r"^(\d{1,2})\s+(?=[A-Z]+)", s)
        if m and re.match(r"[A-Z]+", s[m.end():]).group(0) not in _COUNT_WORDS:
            m = None                                         # "12 X 14 X 56": a size, not a count
    if m:
        n, at = int(m.group(1)), m.end()
    words = list(re.finditer(r"[A-Z]+", s[at:at + 24]))
    for w in words[:2]:
        if w.group(0) in _COUNT_WORDS:
            k = _COUNT_WORDS[w.group(0)]
            n = n * k if n else k
            at = at + w.end()
            break
    bm = re.search(r"([A-Z]+)-BARREL", s)
    if bm and bm.group(1) in _COUNT_WORDS:
        n = _COUNT_WORDS[bm.group(1)]
    return n, at


def _shape_words(s):
    if any(w in s for w in _ARCH_WORDS):
        return "arch"
    if any(re.search(r"\b{}\b".format(re.escape(w)), s) for w in _BOX_WORDS) or "CATTLE" in s:
        return "box"
    if any(re.search(r"\b{}\b".format(re.escape(w)), s) for w in _PIPE_WORDS):
        return "pipe"
    return None


def _material(s):
    known = set(_ARCH_WORDS) | set(_BOX_WORDS) | set(_PIPE_WORDS) | {
        "ALUMINUM", "ALUM", "STEEL", "TIMBER", "MC", "CONCRETE"}
    for w in re.findall(r"[A-Z][A-Z/'.-]*[A-Z]", s):
        ww = w.strip(".'")
        if ww in ("PRECAST", "PRECASE", "PRECASR", "PREACST", "CIP", "PC", "PREC"):
            continue
        if ww in known:
            return ww
    return ""


def parse_culvert_text(text):
    """The NDDOT CULVERT description ("DOUBLE, 10X7X88' RCB", "SINGLE, 10-6X39' SPP",
    "TRIPLE 66\" X 40' CMPA" ...) as a dict: barrels, span_ft, rise_ft, barrel_len_ft, shape
    (box / pipe / arch), material (the code as written), notes (list), raw. What the text does
    not state stays None; when several openings or extensions are described the first size is
    read and a note says so. Sizes are checked for plausibility (span and rise 1-60 ft, barrel
    length 5-600 ft) and left empty otherwise, with a note."""
    raw = _txt(text)
    out = {"barrels": None, "span_ft": None, "rise_ft": None, "barrel_len_ft": None,
           "shape": None, "material": "", "notes": [], "raw": raw}
    s = _norm_text(raw)
    if not s or s == "-":
        return out
    out["barrels"], at = _count_prefix(s)
    vals, units, _end = _measures(s, at)
    shape = _shape_words(s)
    out["material"] = _material(s)
    span = rise = length = None
    if len(vals) == 3 and None not in vals:
        span, rise, length = vals
    elif len(vals) == 2 and None not in vals:
        a, b = vals
        both_in = all(u.replace(".", "") in ('"', "IN") for u in units)
        if shape == "box" or both_in or (shape == "arch" and b < 3.0 * a and b <= _SIZE_FT[1]):
            span, rise = a, b                                # span x rise, no length
        else:
            span, length = a, b                              # diameter x length
            if shape == "arch":
                out["notes"].append("one size given for an arch / ellipse: written as a round "
                                    "pipe of that diameter")
            shape = "pipe" if shape in (None, "arch") else shape
    elif vals:
        out["notes"].append("size not read from '{}'".format(raw))
    if shape in ("arch", "pipe") and span is not None and rise is not None and \
            span > _SIZE_FT[1] and rise > 24.0 and not units[0] and not units[1]:
        span, rise = span / 12.0, rise / 12.0               # 117X79 CSPA: inches without the mark
        out["notes"].append("sizes above 60 read as inches")
    if span is not None and not (_SIZE_FT[0] <= span <= _SIZE_FT[1]):
        out["notes"].append("span {:g} ft in '{}' not plausible: left empty".format(span, raw))
        span = None
    if rise is not None and not (_SIZE_FT[0] <= rise <= _SIZE_FT[1]):
        out["notes"].append("rise {:g} ft in '{}' not plausible: left empty".format(rise, raw))
        rise = None
    if length is not None and not (_LEN_FT[0] <= length <= _LEN_FT[1]):
        out["notes"].append("length {:g} ft in '{}' not plausible: left empty".format(length, raw))
        length = None
    if shape == "pipe" and span is not None and rise is not None and abs(rise - span) > 0.01:
        shape = "arch"                                       # two sizes: not a round pipe
        out["notes"].append("two sizes for a pipe: written as an arch (span x rise)")
    if shape == "arch" and span is not None and rise is not None and rise > span:
        span, rise = rise, span                              # an arch is wider than high
        out["notes"].append("arch sizes given rise-first: swapped")
    if shape == "pipe" and span is not None and rise is None:
        rise = span
    out["shape"] = shape
    out["span_ft"] = round(span, 2) if span is not None else None
    out["rise_ft"] = round(rise, 2) if rise is not None else None
    out["barrel_len_ft"] = round(length, 1) if length is not None else None
    if re.search(r"\s(&|AND|\+|TO)\s", " " + s + " ") or re.search(r"\bEXT", s) or \
            re.search(r"WITH\s+(?:DOUBLE|SINGLE|TRIPLE|\d)", s):
        out["notes"].append("several openings or extensions described in '{}': the first size "
                            "is used - verify".format(raw))
    return out
