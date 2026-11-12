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


if __name__ == "__main__":
    n = 0
    for k, f in sorted(globals().items()):
        if k.startswith("test_") and callable(f):
            f()
            n += 1
            print("ok  ", k)
    print("%d tests ok" % n)
