"""M11 regression: flooded road / rail length on a feet-CRS dam folder.

Receptors.add_lines measures each line with core.conditioning.polyline_length, which returns a
length in the layer's own coordinate units. On a feet CRS those units are feet, so without the
`unit_m` ground-metres-per-unit scaling the stored `length_m` is feet treated as metres and the
consequences report reads 1 / 0.3048 ~= 3.28x too high. The scaling is ported from
nd_breach_runner (nd_hazard) 3.10.0, core/impacts.py add_lines."""
import numpy as np

from nd_breach_runner.core import impacts
from nd_breach_runner.core.units import FT
from nd_breach_runner.tests.registry import test as register   # "test" would be collected by pytest


@register
def road_lengths_are_metres_in_a_feet_crs():
    """The same 1 km road expressed in metre coordinates and in feet coordinates must yield the
    same metre length and about the same number of sample points."""
    rm, rf = impacts.Receptors(), impacts.Receptors()
    line_m = np.array([[0.0, 0.0], [1000.0, 0.0]])
    rm.add_lines([line_m], ids=["r"])
    rf.add_lines([line_m / FT], ids=["r"], unit_m=FT)     # feet CRS: 1000 m == 1000/FT ft
    assert abs(rm.length_m.sum() - rf.length_m.sum()) < 1.0, (rm.length_m.sum(),
                                                              rf.length_m.sum())
    assert abs(len(rm) - len(rf)) <= 1, (len(rm), len(rf))


@register
def feet_crs_road_length_is_not_scaled_by_3_28():
    """A 1000 m road in a feet CRS reports the same length as in a metre CRS, not the ~3.28x
    the missing-scaling bug produced (unscaled feet treated as metres)."""
    coords_ft = np.array([[0.0, 0.0], [1000.0, 0.0]]) / FT     # 1 km of road in feet coordinates
    rm, rf = impacts.Receptors(), impacts.Receptors()
    rm.add_lines([np.array([[0.0, 0.0], [1000.0, 0.0]])], ids=["r"])   # metre CRS baseline
    rf.add_lines([coords_ft], ids=["r"], unit_m=FT)
    km, km_metre = rf.length_m.sum() / 1000.0, rm.length_m.sum() / 1000.0
    assert abs(km - km_metre) < 0.01, (km, km_metre)           # agrees with metre CRS (fixed)
    assert km < 2.0, km                                        # bug reported ~3.28x (~3.4 km)
