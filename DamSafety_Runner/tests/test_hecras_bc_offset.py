"""3.10.84: the normal-depth BC line lies just outside the 2D perimeter.

Run in HEC-RAS 6.6 (Ras.exe -c under Wine with .NET 4.8) the package of DWR-2801 stopped in
the geometry writer: "BC Line 'DS Normal Depth' intersects the perimeter of 2D Flow Area
'Downstream'. This is not allowed in HEC-RAS: if this is an external BC Line, then it must be
entirely outside the 2D Area Perimeter." The line was the perimeter's own points. It is now
drawn BC_OFFSET_CELLS of a cell outside, along the perimeter's outward normal (the same fix as
nd_hazard Runner 3.26.4).

Synthetic data only (test_hecras_p0's valley), GDAL from the QGIS Python; runs in the Runner
suite (run_tests.py) and under pytest."""
import os
import shutil
import tempfile

import numpy as np

from nd_breach_runner.core import conditioning as cond
from nd_breach_runner.core import hecras_model as hm
from nd_breach_runner.tests.registry import test
from nd_breach_runner.tests import test_hecras_p0 as p0


@test
def hecras_bc_offset_follows_the_ring_orientation():
    sq = np.array([(0, 0), (10, 0), (10, 10), (0, 10)], float)
    ring = np.asarray(hm.densify(sq, 1.0, closed=True))
    for r_ in (ring, ring[::-1]):
        k = int(np.argmin(np.hypot(*(r_ - (5.0, 0.0)).T)))
        p = hm._offset_outward(r_, [k], 2.0)[0]
        assert np.allclose(p, (5.0, -2.0)), p


@test
def hecras_bc_line_is_outside_the_2d_perimeter():
    if p0._skip():
        return
    d = tempfile.mkdtemp()
    try:
        p0._run(d)
        r = hm.build_model(d, out_folder=os.path.join(d, "model"))
        m = r["model"]
        per = np.asarray(m.perimeter, float)
        bc = np.asarray(m.bc_line, float)
        assert len(bc) >= 2
        inside = cond.points_in_polygon(bc[:, 0], bc[:, 1], per)
        assert not np.asarray(inside).any(), "BC line points inside the 2D area"
        dist = hm.point_segment_distance(bc[:, 0], bc[:, 1],
                                         list(map(tuple, per)) + [tuple(per[0])])
        cell = float(r["info"]["cell"])
        lim = hm.BC_OFFSET_CELLS * cell
        assert dist.min() > 0.25 * lim and dist.max() < 3.0 * lim, (dist.min(), dist.max(), lim)
    finally:
        shutil.rmtree(d, ignore_errors=True)
