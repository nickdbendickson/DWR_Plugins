"""Hazard 2.18.46: the ACER TM 11 danger-zone colours are the ND Dam Safety Standards' own.

REG_05.2026 Figures 5.1 / 5.2 (pp. 21-22) fill the zones High #EF9897, Judgment #F9F4A3 and
Low #C4E0AD (sampled from the published figures; the text names no colours) and label them
HIGH DANGER ZONE / JUDGMENT ZONE / LOW DANGER ZONE. Every copy that draws the zones must use
those colours at full opacity:

  * DamSafety_Hazard/helpers.py apply_acer11_style() (zone rasters), labels spelled JUDGMENT
  * DamSafety_Hazard/styles.py COLORS (structure points by ACER zone)
  * {Hazard,Runner,Screening}/styles/acer_zones.qml (zone rasters, opacity 1)
  * {Hazard,Runner,Screening}/styles/acer_zone_points.qml (structure points)
  * DamSafety_Hazard/chartsign.py overlay_svg() (the low zone is filled too)
  * {Runner,Screening}/core/nd_hazard.py chart_svg() (ND Fig 5.1 / 5.2 review charts + legend)

check_text(root) is QGIS-free and is also run by tools/ci_checks.py. The sibling plugins' copies
are checked when they are next to this plugin (the repo); a lone plugin zip checks its own.

    python3 DamSafety_Hazard/tests/test_zone_colors_reg05.py
"""
import importlib.util
import os
import re
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))

STD = {"high": "#EF9897", "judgment": "#F9F4A3", "low": "#C4E0AD"}
OLD = ("#F44336", "#FFEB3B", "#4CAF50", "#FBE7B5", "#F4B6B0", "#E8F3E8",
       "244,67,54", "255,235,59", "76,175,80")
PLUGINS = ("DamSafety_Hazard", "DamSafety_Runner", "DamSafety_Screening")


def _rgb(hexcol):
    h = hexcol.lstrip("#")
    return "{},{},{}".format(int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


def _read(root, rel):
    p = os.path.join(root, rel)
    if not os.path.exists(p):
        return None
    with open(p, encoding="utf-8") as f:
        return f.read()


def check_text(root=ROOT):
    """Problems (list of str) with the zone colours in the source files under `root`."""
    probs = []
    s = _read(root, "DamSafety_Hazard/helpers.py")
    if s is None:
        return ["DamSafety_Hazard/helpers.py missing"]
    for cls, key, label in (("CLASS_HIGH", "high", "HIGH DANGER ZONE"),
                            ("CLASS_JUDGMENT", "judgment", "JUDGMENT ZONE"),
                            ("CLASS_LOW", "low", "LOW DANGER ZONE")):
        pat = r'ColorRampItem\({}, QColor\("{}"\), "{}"\)'.format(cls, STD[key], label)
        if not re.search(pat, s, re.I):
            probs.append("helpers.py apply_acer11_style: {} is not {} '{}'".format(cls, STD[key],
                                                                                  label))
    if "JUDGEMENT" in s.upper():
        probs.append("helpers.py still spells JUDGEMENT (REG_05.2026: JUDGMENT)")
    s = _read(root, "DamSafety_Hazard/styles.py")
    for key in ("HIGH", "JUDGMENT", "LOW"):
        if not re.search(r'"{}": "{}"'.format(key, STD[key.lower()]), s, re.I):
            probs.append("styles.py COLORS[{!r}] is not {}".format(key, STD[key.lower()]))
    for p in PLUGINS:
        q = _read(root, p + "/styles/acer_zones.qml")
        if q is None:
            continue
        m = re.search(r'<rasterrenderer[^>]*\sopacity="([0-9.]+)"', q)
        if not m or float(m.group(1)) != 1.0:
            probs.append("{}/styles/acer_zones.qml opacity is {} (must be 1: a lighter tint is "
                         "not the standard's colour)".format(p, m.group(1) if m else "?"))
        for val, key in (("1", "high"), ("2", "judgment"), ("3", "low")):
            if not re.search(r'<paletteEntry value="{}" color="{}"[^>]*alpha="255"'
                             .format(val, STD[key]), q, re.I):
                probs.append("{}/styles/acer_zones.qml value {} is not {}".format(p, val,
                                                                                  STD[key]))
        q = _read(root, p + "/styles/acer_zone_points.qml")
        if q is None:
            probs.append(p + "/styles/acer_zone_points.qml missing")
            continue
        for cat, key in (("HIGH", "high"), ("JUDGMENT", "judgment"), ("LOW", "low")):
            m = re.search(r'<category value="{}" symbol="(\d+)"'.format(cat), q)
            sym = m and re.search(r'<symbol[^>]*name="{}"[^>]*>.*?<prop k="color" v="([0-9,]+)"'
                                  .format(m.group(1)), q, re.S)
            if not sym or sym.group(1) != _rgb(STD[key]) + ",255":
                probs.append("{}/styles/acer_zone_points.qml {} is {} (want {},255)".format(
                    p, cat, sym.group(1) if sym else "?", _rgb(STD[key])))
    for p in ("DamSafety_Runner", "DamSafety_Screening"):
        s = _read(root, p + "/core/nd_hazard.py")
        if s is None:
            continue
        body = s[s.index("def chart_svg"):]
        for key in STD:
            if body.lower().count(STD[key].lower()) < 2:      # zone fill + legend swatch
                probs.append("{}/core/nd_hazard.py chart_svg lacks {} for {}".format(
                    p, STD[key], key))
    s = _read(root, "DamSafety_Hazard/chartsign.py")
    body = s[s.index("def overlay_svg"):]
    for key in STD:
        if STD[key].lower() not in body.lower():
            probs.append("chartsign.py overlay_svg lacks {} for {}".format(STD[key], key))
    for rel in (["DamSafety_Hazard/helpers.py", "DamSafety_Hazard/styles.py"] +
                [p + "/styles/" + n for p in PLUGINS
                 for n in ("acer_zones.qml", "acer_zone_points.qml")]):
        s = _read(root, rel)
        if s is None:
            continue
        if rel.endswith("helpers.py"):
            s = s[s.index("def apply_acer11_style"):s.index("def apply_class_style")]
        if rel.endswith("styles.py"):
            s = s[s.index("COLORS = {"):]
            s = s[:s.index("}")]
            s = "\n".join(l for l in s.splitlines() if '"HIGH"' in l)
        for old in OLD:
            if old.lower() in s.lower():
                probs.append("{} still has the old zone colour {}".format(rel, old))
    return probs


def _fills(svg):
    return [m.lower() for m in re.findall(r'fill="(#[0-9a-fA-F]{6})"', svg)]


def check_rendered(root=ROOT):
    """Render the review charts and the sign-off overlay; the zone fills are the standard's."""
    probs = []
    for p in ("DamSafety_Runner", "DamSafety_Screening"):
        path = os.path.join(root, p, "core", "nd_hazard.py")
        if not os.path.exists(path):
            continue
        spec = importlib.util.spec_from_file_location("_ndh_" + p, path)
        nh = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = nh                # dataclasses resolve the module by name
        spec.loader.exec_module(nh)
        cs = nh.load_charts()
        for key, ch in cs.charts.items():
            svg = nh.chart_svg(ch, [nh.Structure("a", 1.0, 1.0)])
            f = _fills(svg)
            # plot area (low) first, then judgment and high polygons, then the 3 legend swatches
            want = [STD["low"].lower(), STD["judgment"].lower(), STD["high"].lower()]
            zone = [c for c in f if c not in ("#ffffff", "#1f4e9c")][:3]
            if zone != want:
                probs.append("{} chart_svg({}) zone fills {} != {}".format(p, key, zone, want))
            if not all(c in f[3:] or f.count(c) >= 2 for c in want):
                probs.append("{} chart_svg({}) legend swatches do not match".format(p, key))
    try:
        sys.path.insert(0, root)
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from DamSafety_Hazard import chartsign as CS
    except Exception as e:                         # no QGIS: the text check above still ran
        print("  chartsign not importable here ({}); overlay render skipped".format(e))
        return probs
    d = tempfile.mkdtemp()
    for k in ("house", "mobile"):
        out = os.path.join(d, k + ".svg")
        CS.overlay_svg(None, "", out, k)
        with open(out, encoding="utf-8") as fh:
            svg = fh.read()
        f = _fills(svg)
        want = [STD["low"].lower(), STD["judgment"].lower(), STD["high"].lower()]
        zone = [c for c in f if c != "#ffffff"][:3]
        if zone != want:
            probs.append("chartsign overlay ({}) zone fills {} != {}".format(k, zone, want))
        if re.search(r'fill="#(c4e0ad|f9f4a3|ef9897)" fill-opacity', svg, re.I):
            probs.append("chartsign overlay ({}) zone fills are not opaque".format(k))
    return probs


def main():
    probs = check_text(ROOT)
    print("text check: {} problem(s)".format(len(probs)))
    rp = check_rendered(ROOT)
    print("render check: {} problem(s)".format(len(rp)))
    probs += rp
    for p in probs:
        print("  FAIL " + p)
    if probs:
        return 1
    print("OK: REG_05.2026 Fig. 5.1 / 5.2 zone colours in every copy")
    return 0


if __name__ == "__main__":
    rc = main()
    sys.stdout.flush()
    os._exit(rc)
