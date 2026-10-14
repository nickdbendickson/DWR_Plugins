"""'Download DEM' leaves no source tiles behind: they go to a temporary folder that is
removed after the local GeoTIFF is written, on success and on failure.

python3 DamSafety_Screening/tests/test_tile_workdir.py   or   python3 -m pytest <this file>"""
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "qgis_mock"))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))          # parent of the plugin package

import nd_dam_screening  # noqa: E402,F401
from nd_dam_screening.core.tile_workdir import tile_workdir  # noqa: E402
from nd_dam_screening.algorithms import download_dem as mod  # noqa: E402
from qgis.core import QgsProcessingContext, QgsProcessingFeedback  # noqa: E402


def _left(out):
    return sorted(os.listdir(out))


def test_tile_workdir_removed_on_success_and_failure():
    out = tempfile.mkdtemp()
    try:
        with tile_workdir(out) as w:
            assert os.path.dirname(w) == out
            open(os.path.join(w, "t0.tif"), "w").close()
        assert _left(out) == []
        try:
            with tile_workdir(out) as w:
                open(os.path.join(w, "t0.tif"), "w").close()
                raise IOError("network down")
        except IOError:
            pass
        assert _left(out) == []
    finally:
        shutil.rmtree(out, ignore_errors=True)


def _run(out, fail=False):
    seen = {}

    def fake_download(extent, cell, work, stem, feedback=None):
        seen["work"] = work
        for i in range(29):
            open(os.path.join(work, "{}_t{}.tif".format(stem, i)), "w").close()
        vrt = os.path.join(work, stem + ".vrt")
        open(vrt, "w").close()
        if fail:
            raise IOError("3DEP tile 12 failed")
        return vrt

    def fake_write(vrt, dest, cell, info, feedback=None):
        assert os.path.isfile(vrt)                   # tiles still there while warping
        open(dest, "w").close()
        side = os.path.splitext(dest)[0] + "_dem.txt"
        open(side, "w").close()
        return dest, side

    old = (mod.dd.download, mod.dd.write_local_dem, mod._warp, mod.dd.point_box)
    mod.dd.download, mod.dd.write_local_dem = fake_download, fake_write
    mod.dd.point_box = lambda lon, lat, buf: (0.0, 0.0, 1.0, 1.0)     # no osr in the mock
    mod._warp = lambda *a, **k: None
    try:
        alg = mod.DownloadDem() if hasattr(mod, "DownloadDem") else [
            v for k, v in vars(mod).items() if isinstance(v, type) and hasattr(v, "_usgs")][0]()
        alg.initAlgorithm()
        params = {"SOURCE": 0, "RES": 0, "SCOPE": 0, "DAM_POINT": "-97.4,46.9", "BUFFER_MI": 10.0,
                  "DAM_ID": "DWR-2801", "OUT": out, "COUNTY": 0}
        return alg.processAlgorithm(params, QgsProcessingContext(), QgsProcessingFeedback()), seen
    finally:
        mod.dd.download, mod.dd.write_local_dem, mod._warp, mod.dd.point_box = old


def test_download_leaves_only_the_dem_on_success():
    out = tempfile.mkdtemp()
    try:
        res, seen = _run(out)
        assert not os.path.exists(seen["work"])
        assert _left(out) == ["DWR-2801_10m_5070.tif", "DWR-2801_10m_5070_dem.txt"], _left(out)
        assert res["VRT"] == os.path.join(out, "DWR-2801_10m_5070.tif")
    finally:
        shutil.rmtree(out, ignore_errors=True)


def test_download_cleans_up_on_failure():
    out = tempfile.mkdtemp()
    try:
        try:
            _run(out, fail=True)
        except Exception:
            pass
        else:
            raise AssertionError("the failed download did not raise")
        assert _left(out) == [], _left(out)
    finally:
        shutil.rmtree(out, ignore_errors=True)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
