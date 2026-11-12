"""damsuite.display_units (Core 1.1.0), pure Python. Run: python3 DamSafety_Core/tests/test_display_units.py
(or pytest). The real-QGIS dialog check is qgis_unit_dialog_check.py beside this file."""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))

from DamSafety_Core.damsuite import display_units as DU   # noqa: E402


def test_system_default_and_env(monkeypatch=None):
    old = os.environ.pop(DU.ENV, None)
    try:
        assert DU.system() == DU.US
        os.environ[DU.ENV] = "metric"
        assert DU.system() == DU.SI
        os.environ[DU.ENV] = "nonsense"
        assert DU.system() == DU.US
    finally:
        os.environ.pop(DU.ENV, None)
        if old is not None:
            os.environ[DU.ENV] = old


def test_us_changes_nothing():
    for u in ("ft", "acres", "ac-ft", "cfs", "ft/s", "miles", "in", "m", "mm"):
        assert DU.factor(u, "us") == 1.0
        assert DU.display_unit(u, "us") == u
    d = "Crest width (ft; starting value 15 is the default)"
    assert DU.relabel(d, "us") == d


def test_si_factors_round_trip():
    cases = {"ft": (15.0, 4.572, "m"), "acres": (1.0, 0.40468564224, "ha"),
             "ac-ft": (1.0, 1233.48183754752, "m³"), "cfs": (1.0, 0.028316846592, "m³/s"),
             "ft/s": (10.0, 3.048, "m/s"), "miles": (1.0, 1.609344, "km"), "in": (1.0, 25.4, "mm")}
    for u, (stored, shown, du) in cases.items():
        assert abs(DU.to_display(stored, u, "si") - shown) < 1e-9, u
        assert abs(DU.from_display(shown, u, "si") - stored) < 1e-9, u
        assert DU.display_unit(u, "si") == du
    # metric units are not converted in either system
    assert DU.factor("m", "si") == 1.0 and DU.factor("mm", "si") == 1.0


def test_unit_in_label():
    assert DU.unit_in_label("Crest width (ft): TR-60") == "ft"
    assert DU.unit_in_label("Largest pond (acres; YOUR value)") == "acres"
    assert DU.unit_in_label("Storm depth (in): PMP") == "in"
    assert DU.unit_in_label("Cell size (in the plan's CRS)") is None
    assert DU.unit_in_label("Cell size (m)") is None
    assert DU.unit_in_label("Arrival (hours)") is None
    # a value whose unit depends on another choice is never converted
    d = "Flood scenario: failure time (hr) or pool elevation (ft)"
    assert DU.unit_in_label(d) is None and DU.relabel(d, "si") == d
    assert DU.unit_in_label("Breach (Froehlich 2008) width (ft)") is None


def test_relabel_keeps_meaning_of_numbers():
    r = DU.relabel
    assert r("Pool (ft)", "si") == "Pool (m)"
    assert r("Channels: bottom width (ft; starting value 15 is the default)", "si") == \
        "Channels: bottom width (m; starting value 15 ft is the default)"
    assert r("Span (ft; 0 or 99999.9 = not recorded)", "si") == \
        "Span (m; 0 ft or 99999.9 ft = not recorded)"
    assert r("Roads (ft; as tool 8)", "si") == "Roads (m; as tool 8)"
    assert r("Invert (ft; 1 ft to start)", "si") == "Invert (m; 1 ft to start)"
    assert r("Area (acres; 0 = no limit)", "si") == "Area (ha; 0 acres = no limit)"
    assert r("Cell size (in the plan)", "si") == "Cell size (in the plan)"


def test_fmt():
    assert DU.fmt(15, "ft", "si") == "4.572 m"
    assert DU.fmt(15, "ft", "us") == "15 ft"


def test_convert_text_quantities_and_chains():
    c = lambda t: DU.convert_text(t, "si")                         # noqa: E731
    assert c("3,215 cfs") == "91 m³/s"
    assert c("Maximum depth / velocity | 17.2 ft / 9.8 ft/s") == \
        "Maximum depth / velocity | 5.24 m / 2.99 m/s"
    assert c("10.21 x 9.23 mi") == "16.43 x 14.85 km"
    # a quantity before a colon is not a key named by its unit
    assert c("8,049.2 / 8,011.4 ac (at or above 1 ft: 2,198.9 / 2,169.9 ac)") == \
        "3,257.4 / 3,242.1 ha (at or above 0.305 m: 889.9 / 878.1 ha)"
    # every number of a chain carries the unit at its end
    assert c("at or above 0.1 / 1 / 2 ft") == "at or above 0.0305 / 0.305 / 0.61 m"
    assert c("4.86 / 13.09 vs 4.84 / 13.09 ft/s") == "1.48 / 3.99 vs 1.48 / 3.99 m/s"
    assert c("- peak cfs: 28671.4") == "- peak m³/s: 811.9"
    # numbers that are not quantities, and metric ones, are left alone
    for t in ("REG_05.2026 5.4(c)", "v3.10.84 at 10 m", "at (-106230, 2666610)", "5 min",
              "96 h", "10 in the pool"):
        assert c(t) == t, t
    assert DU.convert_text("3,215 cfs", "us") == "3,215 cfs"


def test_convert_table_and_markdown():
    head, rows = DU.convert_table(["ID", "Depth ft", "Vel ft/s", "Arrival h"],
                                  [["7", "12.3", "4.5", "1.25"]], "si")
    assert head == ["ID", "Depth m", "Vel m/s", "Arrival h"]
    assert rows == [["7", "3.75", "1.37", "1.25"]]
    md = ("| Pass | Size (mi) | Edge depth N/S/E/W (ft) |\n|---|---|---|\n"
          "| 1 | 4.21 x 3.23 | 0.0/5.89/2.93/0.0 |")
    out = DU.convert_markdown(md, "si").split("\n")
    assert out[0] == "| Pass | Size (km) | Edge depth N/S/E/W (m) |"
    assert out[2] == "| 1 | 6.78 x 5.2 | 0/1.8/0.893/0 |"
    assert DU.convert_markdown(md, "us") == md


if __name__ == "__main__":
    n = 0
    for k, f in sorted(globals().items()):
        if k.startswith("test_") and callable(f):
            f()
            n += 1
            print("ok  ", k)
    print("%d tests ok" % n)
