"""Plane geometry helpers that numpy 1.x and 2.x share.

audit R1 (2026-11-07): numpy 2.0 removed `np.cross` on 2-vectors (it raises 'Both input arrays
must be (arrays of) 3-dimensional vectors'); numpy 1.x returned the scalar z-component. The
HEC-RAS crest walk (hecras_crest._walk) and the weir-gap merge (hecras_structures.merge_gap)
used it for the signed distance of a point from a line, so under a QGIS that ships numpy 2
(QGIS 3.40+ on some platforms, QGIS 4) a crest trace died at the first junction. `cross2` is
that z-component, written out, for numpy arrays, tuples or lists. Only numpy.
"""
import numpy as np


def cross2(a, b):
    """z-component of a x b for 2-vectors `a` and `b` (the signed area of the parallelogram
    they span): positive when `b` lies to the left of `a`. Each may be a numpy array, a tuple
    or a list of two numbers; the result is a Python float. The same number numpy 1.x's
    np.cross(a, b) returned for two 2-vectors."""
    a = np.asarray(a, dtype=float).reshape(-1)
    b = np.asarray(b, dtype=float).reshape(-1)
    if a.shape[0] != 2 or b.shape[0] != 2:
        raise ValueError("cross2 takes two 2-vectors, got shapes {} and {}".format(
            a.shape, b.shape))
    return float(a[0] * b[1] - a[1] * b[0])
