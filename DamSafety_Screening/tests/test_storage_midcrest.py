"""v0.17.56 (damsuite 1.18.2, Nick 2026-10-28): tool 3 reads the crest at mid-crest of the dam
line (damsuite.midcrest) instead of the 98th percentile near the dam point; the dam-height cap
still applies. Synthetic bowl, no QGIS."""
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "core"))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)
import _optional  # noqa: E402
_optional.need("scipy", "below-pool volume")
import storage  # noqa: E402
from damsuite import midcrest as MC  # noqa: E402

cell, n = 5.0, 200
yy, xx = np.mgrid[0:n, 0:n]
r = np.hypot(xx - n / 2, yy - n / 2) * cell
bed = 100.0 + 0.002 * r ** 2
pool = 105.0
water = bed < pool
dem = np.where(water, pool, bed)

base = storage.estimate_storage(dem, water, cell, dam_xy_idx=(100, 160), max_depth_m=5.0)
res = storage.estimate_storage(dem, water, cell, dam_xy_idx=(100, 160), max_depth_m=5.0,
                               crest_z_m=107.5)
assert abs(res.crest_elev - 107.5) < 1e-9, res.crest_elev
assert any("mid-crest" in s for s in res.notes), res.notes
assert abs(base.crest_elev - 107.5) > 0.1 and not any("mid-crest" in s for s in base.notes)
# NaN / no-data / None: the old crest
for bad in (None, float("nan"), -9999.0):
    r2 = storage.estimate_storage(dem, water, cell, dam_xy_idx=(100, 160), max_depth_m=5.0,
                                  crest_z_m=bad)
    assert abs(r2.crest_elev - base.crest_elev) < 1e-9, (bad, r2.crest_elev)
# the dam-height cap still applies to the mid-crest crest
r3 = storage.estimate_storage(dem, water, cell, dam_xy_idx=(100, 160), max_depth_m=5.0,
                              crest_z_m=120.0, known_height_m=3.0)
assert abs(r3.crest_elev - (pool + 3.0)) < 1e-9, r3.crest_elev
# the vendored mid-crest on a ridge (the helper tool 3 / tool 4 use)
gt = (0.0, 1.0, 0.0, 40.0, 0.0, -1.0)
ys = 40.0 - (np.arange(80) + 0.5)
z = np.tile((10.0 - 0.2 * np.abs(ys - 22.5))[:, None], (1, 60))
rec = MC.midcrest([(10.0, 20.0), (50.0, 20.0)], MC.bilinear(z, gt), original_xy=(30.0, 20.0))
assert abs(rec["xy"][0] - 30.0) < 1e-6 and abs(rec["xy"][1] - 22.5) <= 0.05, rec
assert abs(rec["moved_m"] - 2.5) <= 0.06 and abs(rec["z_m"] - 10.0) < 0.02, rec
print("OK")
