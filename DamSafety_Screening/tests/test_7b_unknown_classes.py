"""Tool 7b's dam selection: Unknown / Undetermined / TBD / ... count as no class (like an empty
hz_class), and the 'High, Medium, Low and unknown (all classified dams)' mode."""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(HERE, "qgis_mock"))
sys.path.insert(0, os.path.dirname(PLUGIN))
from nd_dam_screening.core import handoff as ho  # noqa: E402

A = lambda cls: {"hz_class": cls, "hz_border": "no", "hz_vs_existing": "agree"}
UNKNOWN = ("", None, "NoData", "NULL", "Unknown", "UNKNOWN", " undetermined ", "Undetermined",
           "Not determined", "TBD", "N/A", "?", "Unclassified", "nan")


def test_unknown_like_classes_are_sent_like_blank():
    for u in UNKNOWN:
        cls, unknown = ho.class_of(A(u))
        assert unknown, u
        ok, why = ho.selected(A(u), ho.SELECT_HIGH_MEDIUM)
        assert ok and "no screening class" in why, (u, why)
    # the class names in any letter case
    assert ho.class_of(A("high")) == ("High", False) and ho.class_of(A(" LOW ")) == ("Low", False)
    assert ho.selected(A("medium"), ho.SELECT_HIGH_MEDIUM)[0]
    assert not ho.selected(A("Low"), ho.SELECT_HIGH_MEDIUM)[0]
    assert not ho.selected(A("Unknown"), ho.SELECT_HIGH)[0]     # High only stays High only
    assert ho.selected(A("high"), ho.SELECT_HIGH)[0]


def test_all_classified_mode():
    m = ho.SELECT_CLASSIFIED
    assert ho.SELECT_LABELS[m] == "High, Medium, Low and unknown (all classified dams)"
    assert m == len(ho.SELECT_LABELS) - 1 and m in ho.NEEDS_TOOL8
    # the earlier modes keep their numbers (saved settings and scripts use them)
    assert (ho.SELECT_HIGH_MEDIUM, ho.SELECT_HIGH, ho.SELECT_CHANGED, ho.SELECT_ALL,
            ho.SELECT_SELECTED) == (0, 1, 2, 3, 4)
    for c in ("High", "Medium", "Low", "low"):
        ok, why = ho.selected(A(c), m)
        assert ok and why.startswith("screening class"), (c, why)
    for u in UNKNOWN:
        ok, why = ho.selected(A(u), m)
        assert ok and "unknown, so sent" in why, (u, why)
    for other in ("Excluded", "Not a dam", "Removed"):
        ok, why = ho.selected(A(other), m)
        assert not ok and "is not High, Medium, Low or unknown" in why, (other, why)


def test_tool_parameter_offers_the_new_mode():
    from nd_dam_screening.algorithms.runner_handoff import RunnerHandoff
    a = RunnerHandoff()
    a.initAlgorithm()
    d = a.parameterDefinition("WHICH")
    opts = d.options() if callable(getattr(d, "options", None)) else d.options       # real QGIS / mock
    assert list(opts) == ho.SELECT_LABELS, opts


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok  ", name)
    print("test_7b_unknown_classes: all passed")
