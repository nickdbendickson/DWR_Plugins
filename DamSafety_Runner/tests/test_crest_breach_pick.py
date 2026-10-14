"""Crest breach placement: pick this dam's line within 500 m, not a far decoy."""
from nd_breach_runner.tests import fake_qgis as _fq
_fq.install()

from nd_breach_runner.algorithms import (
    _crest_line_near, _point_along_line, _point_along_nearest)
from nd_breach_runner.tests.registry import test


@test
def point_along_line_frac_ends_and_mid():
    line = [(0.0, 0.0), (100.0, 0.0)]
    assert _point_along_line(line, 0.0) == (0.0, 0.0)
    assert _point_along_line(line, 0.5) == (50.0, 0.0)
    assert _point_along_line(line, 1.0) == (100.0, 0.0)


@test
def crest_line_near_picks_own_not_decoy_with_closer_far_vertex():
    # Own crest: every vertex ~100 m from the dam.
    own = [(100.0, -50.0), (100.0, 50.0)]
    # Decoy: one vertex 10 m from the dam, but the rest of the line is miles away —
    # _point_along_nearest would prefer it by that single near vertex.
    decoy = [(10.0, 0.0), (20000.0, 0.0), (20000.0, 100.0)]
    dam = (0.0, 0.0)
    assert _point_along_nearest([decoy, own], dam, 0.5)[0] > 1000.0
    picked = _crest_line_near([decoy, own], dam)
    assert picked is own
    assert _point_along_line(picked, 0.5) == (100.0, 0.0)


@test
def crest_line_near_refuses_lines_beyond_500m():
    far = [(600.0, 0.0), (700.0, 0.0)]
    farther = [(5000.0, 0.0), (5100.0, 0.0)]
    assert _crest_line_near([far, farther], (0.0, 0.0)) is None
    assert _crest_line_near([far], (0.0, 0.0), max_m=500.0) is None
    near = [(400.0, 0.0), (450.0, 0.0)]
    assert _crest_line_near([near, far], (0.0, 0.0)) is near


@test
def crest_line_near_empty():
    assert _crest_line_near([], (0.0, 0.0)) is None
