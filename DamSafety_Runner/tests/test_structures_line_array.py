"""ndbreach:rundam with a culvert / bridge STRUCTURES layer: a Structure's line is an ndarray,
and `line or []` raised 'truth value of an array is ambiguous' in the solve gate."""
import os
import tempfile

import numpy as np

from nd_breach_runner.tests.registry import test as register


@register
def structure_lines_that_are_ndarrays_feed_the_extent_gate():
    from nd_breach_runner.tests import fake_qgis
    fake_qgis.install()
    from nd_breach_runner import algorithms as A
    from nd_breach_runner.core import structures as S

    class Line(object):                       # what core.structures.Structure carries
        def __init__(self, xy):
            self.line = np.asarray(xy, dtype=float)
    s = [Line([(0.0, 0.0), (10.0, 5.0)]), Line(np.zeros((0, 2))), type("N", (), {"line": None})()]
    assert A.structure_points(s) == [(0.0, 0.0), (10.0, 5.0)]
    assert A.structure_points(None) == [] and A.structure_points([]) == []
    assert hasattr(S, "Structure")


@register
def structure_csv_writers_take_ndarray_lines():
    from nd_breach_runner.core import culvert_guess as cg
    d = tempfile.mkdtemp()
    p = cg.write_candidates_csv(os.path.join(d, "c.csv"),
                                [{"name": "c1", "kind": "culvert", "line": np.array([[0.0, 0.0], [5.0, 1.0]])}])
    txt = open(p).read()
    assert "c1" in txt and ",5.0,1.0," in txt, txt
