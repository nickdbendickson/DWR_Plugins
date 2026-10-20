"""Fill the danger-zone cells of an exported XLSX table with the zone colours of the ND Dam
Safety Standards REG_05.2026 Figures 5.1 / 5.2 (pp. 21-22): HIGH DANGER ZONE #EF9897,
JUDGMENT ZONE #F9F4A3, LOW DANGER ZONE #C4E0AD (2.18.47).

GDAL's XLSX driver writes values only, so after export_layer_to_formats writes a table that has
a zone column (ACER_Zone_Label, Danger_Zone) this adds three solid fills to xl/styles.xml and
points those cells at them. Values and every other cell are untouched; a workbook it cannot read
is left as it was. Standard library only.

    fill_zone_cells(path) -> number of cells filled
"""
import os
import re
import shutil
import tempfile
import zipfile
from xml.etree import ElementTree as ET

# REG_05.2026 Fig. 5.1 / 5.2 fills by ACER class (1 high, 2 judgment, 3 low); the same colours
# as styles/acer_zones.qml (tests/test_zone_colors_reg05.py)
ZONE_FILLS_REG05 = {1: "#EF9897", 2: "#F9F4A3", 3: "#C4E0AD"}
NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
ZONE_COLUMNS = ("ACER_Zone_Label", "Danger_Zone")
ZONE_OF_TEXT = {"HIGH": 1, "HIGH DANGER ZONE": 1, "JUDGMENT": 2, "JUDGMENT ZONE": 2,
                "LOW": 3, "LOW DANGER ZONE": 3}
_CELL = re.compile(r"<c\b([^>]*?)(/>|>(.*?)</c>)", re.S)
_ATTR = re.compile(r'(\w+)="([^"]*)"')


def _strings(z):
    if "xl/sharedStrings.xml" not in z.namelist():
        return []
    root = ET.fromstring(z.read("xl/sharedStrings.xml"))
    return ["".join(t.text or "" for t in si.iter(NS + "t")) for si in root.findall(NS + "si")]


def _sheet_path(z):
    names = sorted(n for n in z.namelist() if re.match(r"xl/worksheets/sheet\d+\.xml$", n))
    return names[0] if names else None


def _cell_text(attrs, body, strings):
    t = attrs.get("t", "")
    if t == "s":
        m = re.search(r"<v>\s*(\d+)\s*</v>", body or "")
        if m and int(m.group(1)) < len(strings):
            return strings[int(m.group(1))]
        return None
    if t in ("inlineStr", "str"):
        return "".join(re.findall(r"<t[^>]*>(.*?)</t>", body or "", re.S)) or \
            (re.search(r"<v>(.*?)</v>", body or "", re.S) or [None, None])[1]
    return None


def _col(ref):
    return re.match(r"[A-Z]+", ref).group(0)


def _add_styles(styles):
    """styles.xml with three solid fills and three cell formats using them; returns
    (new xml, {zone: style index})."""
    m = re.search(r"<fills\b[^>]*>(.*?)</fills>", styles, re.S)
    if not m:
        raise ValueError("no <fills> in styles.xml")
    n_fill = len(re.findall(r"<fill\b", m.group(1)))
    fills = "".join('<fill><patternFill patternType="solid"><fgColor rgb="FF{}"/>'
                    '<bgColor indexed="64"/></patternFill></fill>'.format(ZONE_FILLS_REG05[z][1:])
                    for z in (1, 2, 3))
    new_fills = '<fills count="{}">{}{}</fills>'.format(n_fill + 3, m.group(1), fills)
    styles = styles[:m.start()] + new_fills + styles[m.end():]
    m = re.search(r"<cellXfs\b[^>]*>(.*?)</cellXfs>", styles, re.S)
    if not m:
        raise ValueError("no <cellXfs> in styles.xml")
    xfs = re.findall(r"<xf\b[^>]*?(?:/>|>.*?</xf>)", m.group(1), re.S)
    base = re.search(r'numFmtId="(\d+)"', xfs[0]) if xfs else None
    num = base.group(1) if base else "0"
    add = "".join('<xf numFmtId="{}" fontId="0" fillId="{}" borderId="0" xfId="0" '
                  'applyFill="1"/>'.format(num, n_fill + i) for i in range(3))
    new_xfs = '<cellXfs count="{}">{}{}</cellXfs>'.format(len(xfs) + 3, m.group(1), add)
    styles = styles[:m.start()] + new_xfs + styles[m.end():]
    return styles, {1: len(xfs), 2: len(xfs) + 1, 3: len(xfs) + 2}


def fill_zone_cells(path, columns=ZONE_COLUMNS):
    """Colour the zone cells of the first sheet of `path`; returns how many were filled."""
    with zipfile.ZipFile(path) as z:
        sheet = _sheet_path(z)
        if not sheet or "xl/styles.xml" not in z.namelist():
            return 0
        strings = _strings(z)
        xml = z.read(sheet).decode("utf-8")
        styles = z.read("xl/styles.xml").decode("utf-8")
        entries = [(i, z.read(i.filename)) for i in z.infolist()]
    # header row: the columns to colour
    cols = set()
    m1 = re.search(r'<row\b[^>]*\br="1"[^>]*>(.*?)</row>', xml, re.S)
    for cm in _CELL.finditer(m1.group(1) if m1 else ""):
        attrs = dict(_ATTR.findall(cm.group(1)))
        if _cell_text(attrs, cm.group(3), strings) in columns and "r" in attrs:
            cols.add(_col(attrs["r"]))
    if not cols:
        return 0
    styles, style_of = _add_styles(styles)
    count = [0]

    def repl(cm):
        attrs = dict(_ATTR.findall(cm.group(1)))
        ref = attrs.get("r", "")
        if not ref or _col(ref) not in cols or ref[len(_col(ref)):] == "1":
            return cm.group(0)
        zone = ZONE_OF_TEXT.get((_cell_text(attrs, cm.group(3), strings) or "").strip().upper())
        if not zone:
            return cm.group(0)
        count[0] += 1
        head = re.sub(r'\s+s="\d+"', "", cm.group(1)) + ' s="{}"'.format(style_of[zone])
        return "<c" + head + cm.group(2)
    xml = _CELL.sub(repl, xml)
    if not count[0]:
        return 0
    fd, tmp = tempfile.mkstemp(suffix=".xlsx", dir=os.path.dirname(os.path.abspath(path)))
    os.close(fd)
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as out:
            for info, data in entries:
                if info.filename == sheet:
                    data = xml.encode("utf-8")
                elif info.filename == "xl/styles.xml":
                    data = styles.encode("utf-8")
                out.writestr(info, data)
        shutil.copymode(path, tmp)
        shutil.move(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return count[0]


def zone_fills(path, columns=ZONE_COLUMNS):
    """{cell text: fill hex} of the zone cells of `path` (for tests and checks)."""
    with zipfile.ZipFile(path) as z:
        strings = _strings(z)
        xml = z.read(_sheet_path(z)).decode("utf-8")
        st = ET.fromstring(z.read("xl/styles.xml"))
    fills = []
    for f in st.find(NS + "fills").findall(NS + "fill"):
        fg = f.find(NS + "patternFill/" + NS + "fgColor")
        fills.append(("#" + fg.get("rgb")[-6:].upper()) if fg is not None and fg.get("rgb")
                     else None)
    xfs = [int(x.get("fillId", "0")) for x in st.find(NS + "cellXfs").findall(NS + "xf")]
    cols, out = set(), {}
    for row in re.finditer(r'<row\b[^>]*\br="(\d+)"[^>]*>(.*?)</row>', xml, re.S):
        for cm in _CELL.finditer(row.group(2)):
            attrs = dict(_ATTR.findall(cm.group(1)))
            text = _cell_text(attrs, cm.group(3), strings)
            if row.group(1) == "1":
                if text in columns:
                    cols.add(_col(attrs["r"]))
                continue
            if _col(attrs.get("r", "A")) in cols and text:
                s = int(attrs.get("s", "0"))
                out.setdefault(text, set()).add(fills[xfs[s]] if s < len(xfs) else None)
    return out
