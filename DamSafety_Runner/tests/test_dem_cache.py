"""3.16.9 (audit P3, 2026-11-07): GdalTerrain keeps a small LRU of warped DEM windows
(gdal_io.WarpCache) so the crest window, the inflow window, the planning grid and the model
domain are not re-warped from disk on every call. The cache is checked here on a fake warp
(no GDAL in the stub harness): hit / miss rules, the cell snap in the key, copies on the way
out, the 3-entry eviction and the source-stamp invalidation."""
import os
import shutil
import tempfile
import time

import numpy as np

from nd_breach_runner.tests.registry import test
from nd_breach_runner.core.grid import Grid


def _fake_warp(calls):
    def warp(bounds, cell):
        calls.append((tuple(bounds), cell))
        xmin, ymin, xmax, ymax = bounds
        nx = int(round((xmax - xmin) / cell))
        ny = int(round((ymax - ymin) / cell))
        xs = xmin + (np.arange(nx) + 0.5) * cell
        ys = ymax - (np.arange(ny) + 0.5) * cell
        X, Y = np.meshgrid(xs, ys)
        return Grid(100.0 + 0.01 * X - 0.02 * Y, xmin, ymax, cell, crs_wkt="FAKE")
    return warp


@test
def dem_cache_hits_the_same_window_and_hands_out_copies():
    from nd_breach_runner.core import gdal_io as G
    calls = []
    c = G.WarpCache(_fake_warp(calls), 3)
    b = (0.0, 0.0, 1000.0, 500.0)
    g1 = c.read(b, 10.0, "average", 1.0, None)
    g2 = c.read(b, 10.0, "average", 1.0, None)
    assert len(calls) == 1 and (c.hits, c.misses) == (1, 1)
    assert np.array_equal(g1.data, g2.data) and g1.data is not g2.data
    assert g2.crs_wkt == "FAKE" and g2.dx == 10.0 and g2.x0 == 0.0 and g2.y0 == 500.0
    # a caller editing its copy (raise_crest / the crest burn / conditioning do) never
    # changes what the next caller gets
    g2.data[:] = -1.0
    g3 = c.read(b, 10.0, "average", 1.0, None)
    assert np.array_equal(g3.data, g1.data) and len(calls) == 1
    # the key carries the method, the z factor and the source nodata: each is another window
    c.read(b, 10.0, "bilinear", 1.0, None)
    c.read(b, 10.0, "average", 0.3048, None)
    assert len(calls) == 3 and c.misses == 3
    # a window that differs by a fraction of a cell is another window (other cell centres)
    c.read((0.3, 0.0, 1000.3, 500.0), 10.0, "average", 1.0, None)
    assert len(calls) == 4
    # another cell size too
    c.read(b, 20.0, "average", 1.0, None)
    assert len(calls) == 5


@test
def dem_cache_keeps_three_entries_most_recently_used():
    from nd_breach_runner.core import gdal_io as G
    calls = []
    c = G.WarpCache(_fake_warp(calls), 3)
    boxes = [(0.0, 0.0, 100.0 * k, 100.0) for k in (1, 2, 3, 4)]
    for b in boxes[:3]:
        c.read(b, 10.0)
    assert len(calls) == 3
    c.read(boxes[0], 10.0)                     # touch the oldest: it is the newest now
    assert len(calls) == 3
    c.read(boxes[3], 10.0)                     # a fourth evicts boxes[1], the least recent
    assert len(calls) == 4 and len(c.items) == 3
    c.read(boxes[0], 10.0)
    c.read(boxes[2], 10.0)
    c.read(boxes[3], 10.0)
    assert len(calls) == 4                     # all three still there
    c.read(boxes[1], 10.0)
    assert len(calls) == 5                     # the evicted one is warped again
    # zero entries = no caching at all (every read warps)
    calls2 = []
    c0 = G.WarpCache(_fake_warp(calls2), 0)
    c0.read(boxes[0], 10.0)
    c0.read(boxes[0], 10.0)
    assert len(calls2) == 2 and c0.items == []


@test
def dem_cache_empties_when_the_source_file_changes():
    from nd_breach_runner.core import gdal_io as G
    d = tempfile.mkdtemp(prefix="ndbr_demcache_")
    try:
        p = os.path.join(d, "dem.vrt")
        with open(p, "w") as f:
            f.write("<VRTDataset/>\n")
        calls = []
        c = G.WarpCache(_fake_warp(calls), 3, stamp=lambda: G.file_stamp(p))
        b = (0.0, 0.0, 1000.0, 500.0)
        c.read(b, 10.0)
        c.read(b, 10.0)
        assert len(calls) == 1
        # the file is rewritten (a refill, a rebuilt mosaic): the next read warps again
        time.sleep(0.02)
        with open(p, "w") as f:
            f.write("<VRTDataset>changed</VRTDataset>\n")
        os.utime(p, None)
        c.read(b, 10.0)
        assert len(calls) == 2, calls
        c.read(b, 10.0)
        assert len(calls) == 2
        # a vanished file also empties it (and does not raise)
        os.remove(p)
        c.read(b, 10.0)
        assert len(calls) == 3
        assert G.file_stamp(p) == (p, None, None)
    finally:
        shutil.rmtree(d)


@test
def dem_cache_key_snaps_the_window_as_the_warp_does():
    from nd_breach_runner.core import gdal_io as G
    k1 = G.WarpCache.key_of((0.0, 0.0, 1000.0, 500.0), 10.0)
    k2 = G.WarpCache.key_of((0.0, 0.0, 1000.0, 500.0), 10.0)
    assert k1 == k2 == (0.0, 500.0, 100, 50, 10.0)
    # the warp fixes xmin / ymax and rounds the cell counts: a far edge off by less than half a
    # cell gives the same grid, so the same key; the near edge is part of the key
    assert G.WarpCache.key_of((0.0, 0.0, 1003.0, 500.0), 10.0) == k1
    assert G.WarpCache.key_of((0.0, 0.0, 1006.0, 500.0), 10.0) != k1
    assert G.WarpCache.key_of((1.0, 0.0, 1001.0, 500.0), 10.0) != k1
    assert G.WarpCache.key_of((0.0, 0.0, 1000.0, 500.0), 10.0, "average") != k1
