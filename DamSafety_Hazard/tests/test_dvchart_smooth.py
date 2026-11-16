# -*- coding: utf-8 -*-
"""Hazard 2.25.6 / Core 1.5.2 / nd_hazard 1.4.2: the depth-velocity chart lines are DRAWN smooth.

Nick, 2026-11-16: the chart lines in the Hazard report were not smooth. The REG_05.2026 table
is a hand digitization joined by straight lines; the charts now draw a smoothed line through the
same table. Classification (zone(), the table, table_sha256) must not change.

QGIS-free, no data.

    python3 DamSafety_Hazard/tests/test_dvchart_smooth.py
"""
import copy
import importlib.util
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)

from DamSafety_Hazard import dvchart as DV  # noqa: E402
from DamSafety_Core.damsuite import tm11_curves as TM11  # noqa: E402

FAILS = []


def check(name, ok, info=""):
    print("{} {}{}".format("ok  " if ok else "FAIL", name, "" if ok else "  -> {}".format(info)))
    if not ok:
        FAILS.append(name)


def roughness(pts):
    """Sum of |second difference| of depth on an even velocity grid (kinks add to it)."""
    d = [p[1] for p in pts]
    return sum(abs(d[i - 1] - 2 * d[i] + d[i + 1]) for i in range(1, len(d) - 1))


def table_on_grid(kind, level, n=TM11.DRAW_POINTS):
    vmax = TM11.AXES[kind][0]
    return [(vmax * k / (n - 1), TM11.depth_at(kind, level, vmax * k / (n - 1))) for k in range(n)]


# ---------------------------------------------------------------- Core: the table is untouched --
SHA_BEFORE = "2fb490ca2f08"            # table_sha256() of the signed-off table (Core 1.5.1)
check("table_sha256 unchanged", TM11.table_sha256().startswith(SHA_BEFORE), TM11.table_sha256())
check("table check() clean", TM11.check() == [], TM11.check())
CURVES_BEFORE = copy.deepcopy(TM11.CURVES)
for kind in ("house", "mobile"):
    vmax, dmax = TM11.AXES[kind]
    for level in ("low", "high"):
        line = TM11.drawn_line(kind, level)
        check("{} {}: {} points from 0 to {:g} ft/s".format(kind, level, len(line), vmax),
              len(line) == TM11.DRAW_POINTS and line[0][0] == 0.0 and abs(line[-1][0] - vmax) < 1e-9)
        check("{} {}: within the axes".format(kind, level),
              all(0.0 <= d <= dmax for _v, d in line))
        worst = max(abs(d - TM11.depth_at(kind, level, v)) for v, d in line)
        lim = 0.03 if kind == "house" else 0.01
        check("{} {}: within {} ft of the table (worst {:.4f})".format(kind, level, lim, worst),
              worst <= lim, worst)
        r_s, r_t = roughness(line), roughness(table_on_grid(kind, level))
        check("{} {}: smoother than straight segments ({:.3f} < {:.3f})".format(
            kind, level, r_s, r_t), r_s < 0.8 * r_t, (r_s, r_t))
    lo, hi = TM11.drawn_line(kind, "low"), TM11.drawn_line(kind, "high")
    check("{}: drawn low line never above the high line".format(kind),
          all(a[1] <= b[1] for a, b in zip(lo, hi)))
    check("{}: drawn_offset_ft is the worst of both lines".format(kind),
          abs(TM11.drawn_offset_ft(kind) - max(
              max(abs(d - TM11.depth_at(kind, lv, v)) for v, d in TM11.drawn_line(kind, lv))
              for lv in ("low", "high"))) < 1e-12)
    p = TM11.drawn_polygons(kind)
    check("{}: drawn polygons low/judgment/high".format(kind), set(p) == {"low", "judgment", "high"})
    check("{}: high polygon closes on the top axis".format(kind),
          p["high"][-2:] == [(vmax, dmax), (0.0, dmax)])

# the house low line reaches the axis at 19 ft/s in the table; the drawing stays on it from there
tail = [(v, d) for v, d in TM11.drawn_line("house", "low") if v >= 19.0]
check("house low: 0 from 19 ft/s (the table's zero tail)", tail and all(d == 0.0 for _v, d in tail))
near = [d for v, d in TM11.drawn_line("house", "low") if 18.0 <= v < 19.0]
check("house low: reaches the axis without a step (<= 0.03 ft just before 19 ft/s)",
      near and near[-1] <= 0.03, near[-3:])

# zones still come from the table: a point just above the table line but under the drawn one
# (or the other way round) keeps the table's zone
for kind in ("house", "mobile"):
    for level in ("low", "high"):
        for v in (1.25, 7.3, 12.6):
            t = TM11.depth_at(kind, level, v)
            z_on = TM11.zone(kind, t, v)
            want = TM11.HIGH if level == "high" else TM11.JUDGMENT
            check("{} {} v={}: a depth on the table line keeps the table's zone".format(
                kind, level, v), z_on == want or (t <= 0 and z_on == TM11.LOW), z_on)
check("CURVES not modified by drawing", TM11.CURVES == CURVES_BEFORE)

# smooth_points is generic (DamIT and other tables)
pts = TM11.smooth_points([0, 1, 2, 3, 4, 5, 6, 7, 8], [2, 1.95, 1.8, 1.5, 1.0, 0.5, 0.0, 0.0, 0.0],
                         d_max=3.0, n=81)
check("smooth_points: zero tail kept", all(d == 0.0 for v, d in pts if v >= 6.0))
check("smooth_points: starts near the first point", abs(pts[0][1] - 2.0) < 0.05, pts[0])
check("smooth_points: two points come back as given",
      TM11.smooth_points([0, 1], [1, 0]) == [(0.0, 1.0), (1.0, 0.0)])

# ---------------------------------------------------------------- Hazard dvchart --
for kind in ("house", "mobile"):
    dp = DV.drawn_zone_polygons(kind)
    check("dvchart {}: draws the smoothed polygons".format(kind),
          dp["judgment"] == TM11.drawn_polygons(kind)["judgment"] and
          (dp["v_max"], dp["d_max"]) == TM11.AXES[kind])
note = DV.smoothing_note()
check("dvchart: caption says the lines are drawn smoothed", "smoothed" in note and "0.02 ft" in note,
      note)

saved = TM11.drawn_polygons
try:                                      # an older DamSafety Core: straight segments, no note
    del TM11.drawn_polygons
    old_off = TM11.drawn_offset_ft
    del TM11.drawn_offset_ft
    check("dvchart: older Core draws the table polygons",
          DV.drawn_zone_polygons("house")["judgment"] == DV.zone_polygons("house")["judgment"])
    check("dvchart: older Core, no smoothing note", DV.smoothing_note() == "")
finally:
    TM11.drawn_polygons = saved
    TM11.drawn_offset_ft = old_off


def _has_pyplot():
    try:
        DV._pyplot()
        return True
    except Exception:                                                # noqa: BLE001
        return False


if _has_pyplot():
    with tempfile.TemporaryDirectory() as tmp:
        pts = []
        path = os.path.join(tmp, "chart.png")
        DV.draw(pts, {"dam_id": "T-1", "source": "test", "run_date": "2026-11-16"}, path)
        check("dvchart: PNG drawn with smoothed lines", os.path.getsize(path) > 10000)
else:
    print("skip PNG (matplotlib not available)")

# ---------------------------------------------------------------- nd_hazard chart_svg --
for plugin in ("DamSafety_Runner", "DamSafety_Screening"):
    path = os.path.join(ROOT, plugin, "core", "nd_hazard.py")
    spec = importlib.util.spec_from_file_location("_t_nd_hazard_" + plugin, path)
    nh = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(nh)
    check("{}: nd_hazard 1.4.2".format(plugin), nh.MODULE_VERSION == "1.4.2", nh.MODULE_VERSION)
    cs = nh.load_charts()
    ch = cs.chart(False)
    j, h = nh._drawn_zones(ch)
    check("{}: shipped chart drawn smoothed".format(plugin),
          j == TM11.drawn_polygons(ch.table_kind)["judgment"] and len(j) == 2 * TM11.DRAW_POINTS)
    svg = nh.chart_svg(ch, [])
    check("{}: SVG has the zones".format(plugin), svg.count("<polygon") == 2 and "</svg>" in svg)
    other = copy.copy(ch)
    other.judgment = [[v, d + 0.2] for v, d in ch.judgment]       # a chart file of other polygons
    other.high = [[v, min(d + 0.2, ch.d_max_ft)] for v, d in ch.high]
    j2, h2 = nh._drawn_zones(other)
    check("{}: a chart file of other polygons is drawn as given".format(plugin),
          j2 is other.judgment and h2 is other.high)
    z = [ch.zone(d, v)[0] for d, v in ((1.0, 2.0), (4.0, 3.0), (7.0, 3.0), (0.5, 21.0))]
    check("{}: zones unchanged".format(plugin),
          [TM11.zone("house", d, v) for d, v in ((1.0, 2.0), (4.0, 3.0), (7.0, 3.0), (0.5, 21.0))]
          == ["low", "judgment", "high", "judgment"], z)

print("\n{} failure(s)".format(len(FAILS)))
sys.exit(1 if FAILS else 0)
