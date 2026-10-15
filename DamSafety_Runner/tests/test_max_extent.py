"""Max extent (MAX_EXTENT_MI, 'Largest domain side') is in the main dialog of the run tools,
not under Advanced, and defaults to 20 mi."""
from nd_breach_runner.tests.registry import test as register  # "test" would be collected by pytest


def _algs():
    from nd_breach_runner.tests import fake_qgis
    fake_qgis.install()
    import importlib
    import nd_breach_runner.algorithms as algs
    importlib.reload(algs)
    from nd_breach_runner.compat import FLAG_ADVANCED
    return algs, FLAG_ADVANCED


@register
def test_max_extent_is_visible_with_a_20_mi_default():
    algs, advanced = _algs()
    assert algs.DEFAULT_MAX_EXTENT_MI == 20.0
    for cls in (algs.RunDamAlgorithm, algs.SensitivityAlgorithm):
        a = cls()
        a.initAlgorithm()
        p = a.parameterDefinition("MAX_EXTENT_MI")
        if p is None:
            continue
        assert p.defaultValue() == 20.0, (cls.__name__, p.defaultValue())
        assert not (p.flags() & advanced), cls.__name__ + ": MAX_EXTENT_MI is Advanced"


@register
def max_extent_cut_off_warning_says_truncated_and_how_to_fix():
    """Audit 2026-10-15: a run cut at the (20 mi) cap must say TRUNCATED, name the cap, and point
    at the setting (the default was 60 mi before 2026-10)."""
    from nd_breach_runner.core import workflow
    w = workflow.max_extent_warning(20.0, ["east", "south"])
    assert w.startswith("TRUNCATED") and "20 mi" in w and "east, south" in w, w
    assert "Largest domain side" in w and "60 mi" in w, w
    src = open(workflow.__file__, encoding="utf-8").read()
    assert 'stop_reason = "maximum extent' in src          # recorded in report["cut_off"]
