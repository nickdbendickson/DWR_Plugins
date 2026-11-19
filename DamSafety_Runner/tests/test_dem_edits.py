"""S3: the DEM-edits record. model_dem keeps the structure-cut counts it used to discard, and
the engine-DEM-minus-source footprint covers the UNION of the per-edit masks (overlapping cuts
are counted once), not the SUM of the per-cut counts."""
import numpy as np

from nd_breach_runner.tests.registry import test as register
from nd_breach_runner.core.grid import Grid
from nd_breach_runner.core import structures as structmod
from nd_breach_runner.core.structures import KIND_NOTCH
from nd_breach_runner.core import workflow


class _FlatRidge:
    """A flat embankment at 560 m; read() ignores the bounds and returns the same grid."""
    z_to_m = 1.0

    def __init__(self, n=40, cell=1.0, z=560.0):
        self.base = np.full((n, n), float(z))
        self.cell = cell
        self.n = n

    def read(self, bounds, cell):
        return Grid(self.base.copy(), 0.0, self.n * self.cell, cell)


def _changed_mask(notch, src_grid):
    """Cells one notch lowers when applied on its own to a fresh source grid."""
    before = src_grid.masked()
    z = before.copy()
    init = np.zeros_like(z)
    notch.apply(src_grid, z, init)
    with np.errstate(invalid="ignore"):
        return np.isfinite(z) & np.isfinite(before) & (z != before)


@register
def dem_edits_footprint_is_union_not_sum_of_cut_counts():
    terr = _FlatRidge()
    cell = terr.cell
    line = np.array([[5.0, 20.0], [35.0, 20.0]])
    # two cuts on the same corridor; the second is deeper, so it re-cuts the same cells
    s1 = structmod.Structure(kind=KIND_NOTCH, line=line, width_m=3.0,
                             invert_up_m=550.0, invert_dn_m=550.0, name="n1")
    s2 = structmod.Structure(kind=KIND_NOTCH, line=line, width_m=3.0,
                             invert_up_m=540.0, invert_dn_m=540.0, name="n2")

    info = {}
    g = workflow.model_dem(terr, [s1, s2], "lisflood", (0.0, 0.0, 40.0, 40.0), cell,
                           edits_out=info)

    # the counts model_dem used to discard are kept, one per cut, all > 0
    counts = info["counts"]
    assert set(counts) == {"notches:n1", "notches:n2"}, counts
    assert all(v > 0 for v in counts.values()), counts

    # the edits footprint = where the engine DEM differs from the plain source DEM
    model, source = info["model"], info["source"]
    with np.errstate(invalid="ignore"):
        footprint = model.valid() & source.valid() & (model.data != source.data)

    # the union of the per-cut masks, each cut applied on its own to the plain source
    union = (_changed_mask(s1.notch(cell), terr.read(None, cell))
             | _changed_mask(s2.notch(cell), terr.read(None, cell)))

    # the overlap is real: the summed counts double-count the shared cells
    assert sum(counts.values()) > int(union.sum()), (counts, int(union.sum()))
    # the footprint matches the UNION, not the SUM
    assert np.array_equal(footprint, union)
    assert int(footprint.sum()) == int(union.sum())
    # model_dem returned exactly the grid it recorded as the model
    assert g is model

    # no structures -> no edits recorded and no footprint
    none = {}
    workflow.model_dem(terr, [], "lisflood", (0.0, 0.0, 40.0, 40.0), cell, edits_out=none)
    assert none["counts"] == {}
    with np.errstate(invalid="ignore"):
        empty = none["model"].valid() & none["source"].valid() & \
            (none["model"].data != none["source"].data)
    assert not empty.any()
