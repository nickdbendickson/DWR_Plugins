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
