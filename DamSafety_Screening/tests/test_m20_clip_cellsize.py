"""M20 regression: tool 16 '_dem_window' must not reuse a per-dam terrain clip that is COARSER
than the analysis cell. The clip is written at the clip cell (CLIP_CELL, often coarser than the
analysis CELL), so a rerun that reads the pond from the coarse clip reports a different (lower)
storage than the first run. The guard ported from nd_dam_screening only reuses a clip that is as
fine as the analysis cell.

Uses the QGIS/GDAL test doubles (no real QGIS/GDAL/scipy here); does not call analyze_pond.
"""
import os
import sys
import tempfile

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "qgis_mock"))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
from osgeo import gdal  # noqa: E402  (mock)
from DamSafety_Screening.algorithms import find_ponds as fp  # noqa: E402


def _make_raster(path, ext, cell):
    """Write a flat single-band raster covering ext at the given cell size (mock GeoTIFF)."""
    x0, y0, x1, y1 = ext
    nx, ny = int(round((x1 - x0) / cell)), int(round((y1 - y0) / cell))
    ds = gdal.GetDriverByName("GTiff").Create(path, nx, ny, 1, gdal.GDT_Float32)
    ds.SetGeoTransform((x0, cell, 0.0, y1, 0.0, -cell))
    ds.GetRasterBand(1).SetNoDataValue(-9999.0)
    ds.GetRasterBand(1).WriteArray(np.full((ny, nx), 100.0, "float32"))
    ds.FlushCache()


# ---------------------------------------------------------------- 1. _cell_of helper
work = tempfile.mkdtemp(prefix="m20_")
ext = (0.0, 0.0, 100.0, 100.0)
coarse = os.path.join(work, "coarse.tif")
fine = os.path.join(work, "fine.tif")
_make_raster(coarse, ext, 4.0)
_make_raster(fine, ext, 2.0)
assert abs(fp._cell_of(coarse) - 4.0) < 1e-9, fp._cell_of(coarse)
assert abs(fp._cell_of(fine) - 2.0) < 1e-9, fp._cell_of(fine)
assert fp._cell_of(os.path.join(work, "missing.tif")) == float("inf")   # unreadable -> never reused
print("OK (_cell_of: reads the clip cell size; inf when unreadable)")


# ---------------------------------------------------------------- 2. _dem_window source choice
def run_dem_window(clip_cell, analysis_cell):
    """Set up a FindPonds with a per-dam clip at clip_cell and a fine VRT, then record which
    source _dem_window hands to gdal.Warp for an analysis grid at analysis_cell."""
    terrain = tempfile.mkdtemp(prefix="m20_terr_", dir=work)
    alg = fp.FindPonds()
    alg._terrain = terrain
    alg._vrt = os.path.join(work, "_mosaic.vrt")        # the fine source (truthy; never read: Warp is faked)
    pid = "POND-DEADBEEF"
    clip = os.path.join(terrain, "dam_{}.tif".format(pid))
    _make_raster(clip, ext, clip_cell)

    captured = []

    def fake_warp(dst, src, **kw):
        captured.append(src)
        w, h = kw["width"], kw["height"]
        ob = kw["outputBounds"]
        cx = (ob[2] - ob[0]) / w
        arr = np.full((h, w), 100.0)
        return gdal.Dataset(arr, (ob[0], cx, 0.0, ob[3], 0.0, -cx), None, None)

    orig_warp, orig_cov = fp.gdal.Warp, fp.clip_covers
    fp.gdal.Warp = fake_warp
    fp.clip_covers = lambda path, ext: True             # isolate the cell-size guard from extent coverage
    try:
        alg._dem_window(ext, analysis_cell, pid, None, os.path.join(terrain, "_tmp"))
    finally:
        fp.gdal.Warp, fp.clip_covers = orig_warp, orig_cov
    return clip, alg._vrt, captured[-1]


# The regression: clip is COARSER (4 m) than the analysis cell (2 m) -> must NOT reuse the clip,
# must fall back to the fine VRT. (Before the fix this read the coarse clip and halved the storage.)
clip, vrt, src = run_dem_window(clip_cell=4.0, analysis_cell=2.0)
assert src == vrt, "coarse 4 m clip reused for a 2 m analysis grid (M20 regression): source={}".format(src)
print("OK (coarse clip NOT reused for a finer analysis grid; fell back to the VRT)")

# A clip as fine as the analysis cell IS reused (the guard does not over-reject).
clip, vrt, src = run_dem_window(clip_cell=2.0, analysis_cell=2.0)
assert src == clip, "an equally-fine clip should be reused, source={}".format(src)
print("OK (clip as fine as the analysis cell is reused)")

import shutil  # noqa: E402
shutil.rmtree(work)
print("OK")
