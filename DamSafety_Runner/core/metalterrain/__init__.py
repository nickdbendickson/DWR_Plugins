"""BoannGrid terrain conditioner (core/metalterrain): GPU depression-fill (Planchon & Darboux
1984/2002) as Metal compute kernels for the Apple GPU, with a CPU build of the same kernel for
the cloud. It reuses core/metalflood's backend (backends.Spec) and shim. See README.md.

    from nd_breach_runner.core.metalterrain import fill
    filled, info = fill.fill_dem(dem, eps=1e-5, backend="auto")   # NaN / -9999 = NoData

This is the first harvested piece of the BoannGrid conditioner: the depression-fill, the one
GPU kernel of the Lumo/Terra Swift spike that reviewed as correct, reimplemented in the real
Python+Metal stack (not Swift) against core/terrain.priority_flood as the ground-truth oracle.
"""
from .fill import fill_dem, SPEC, TParams, NODATA  # noqa: F401
from ..metalflood.backends import metal_available, find_compiler  # noqa: F401


def available():
    """(True, device text) when the fill can run here: a Metal GPU, or a C++ compiler for the
    CPU build. (False, why) otherwise."""
    ok, why = metal_available()
    if ok:
        return True, "Apple GPU ({})".format(why)
    if find_compiler():
        return True, "CPU build of the fill kernel, one core (no Metal GPU: {})".format(why)
    return False, "no Metal GPU ({}) and no C++ compiler for the CPU build".format(why)
