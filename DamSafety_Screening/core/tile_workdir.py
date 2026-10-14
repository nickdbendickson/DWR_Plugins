"""A temporary folder for downloaded DEM source tiles. No QGIS / GDAL here."""
import contextlib
import shutil
import tempfile


@contextlib.contextmanager
def tile_workdir(out_dir):
    """Yield a new temporary folder (tempfile.mkdtemp, inside `out_dir` so the tiles stay on
    the same disk as the result) and remove it with everything in it afterwards, whether the
    block succeeded or raised."""
    work = tempfile.mkdtemp(prefix="_dem_tiles_", dir=out_dir)
    try:
        yield work
    finally:
        shutil.rmtree(work, ignore_errors=True)
