"""Hazard 2.21.2 (fulltest 2026-10-30): ndhazard:classify with every DOWNLOAD_* switch off
downloads nothing - before, it still fetched the OpenStreetMap mobile-home parks, the
Microsoft footprints and the NAIP flight dates (no switch of their own), and logged
'mobile-home park download failed' offline.

The algorithm runs on a tiny dam folder with pipeline.run_classification replaced by a stub
that records the settings it is given.

    QT_QPA_PLATFORM=offscreen python3 DamSafety_Hazard/tests/test_classify_no_downloads.py
"""
import os
import shutil
import sys
import tempfile

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qgis.core import (QgsApplication, QgsProcessingContext,  # noqa: E402
                       QgsProcessingFeedback)
_app = QgsApplication.instance() or QgsApplication([], True)
_app.setPrefixPath(os.environ.get("QGIS_PREFIX_PATH", "/home/box/miniforge3/envs/qgis334"), True)
_app.initQgis()

from osgeo import gdal, osr  # noqa: E402
from DamSafety_Hazard import pipeline as PL, processing_provider as PP  # noqa: E402

FAILS = []
PARK_ETC = ("download_trailer_parks", "download_ms_footprints", "naip_flight_dates",
            "naip_clips", "download_places", "download_flowlines")


def check(what, ok, got=""):
    print("{:4} {}{}".format("ok" if ok else "FAIL", what, "" if ok else "  -> {!r}".format(got)))
    if not ok:
        FAILS.append(what)


def _tif(path):
    sr = osr.SpatialReference()
    sr.ImportFromEPSG(5070)
    ds = gdal.GetDriverByName("GTiff").Create(path, 4, 4, 1, gdal.GDT_Float32)
    ds.SetGeoTransform((0.0, 10.0, 0.0, 40.0, 0.0, -10.0))
    ds.SetProjection(sr.ExportToWkt())
    ds.GetRasterBand(1).WriteArray(np.full((4, 4), 2.0, dtype=np.float32))
    ds = None


class _Stop(RuntimeError):
    pass


def run(dam, **params):
    seen = {}

    def stub(cfg, progress=None, log=None):
        seen.update(cfg)
        raise _Stop("stub")
    keep = PL.run_classification
    PL.run_classification = stub
    try:
        alg = PP.ClassifyAlgorithm()
        alg.initAlgorithm({})
        p = {d.name(): d.defaultValue() for d in alg.parameterDefinitions()}
        p.update(DAM_FOLDER=dam, REPORT=False, EAP=False)
        p.update(params)
        try:
            alg.processAlgorithm(p, QgsProcessingContext(), QgsProcessingFeedback())
        except Exception:                                   # the stub stops the run
            pass
    finally:
        PL.run_classification = keep
    return seen


def main():
    tmp = tempfile.mkdtemp(prefix="nodl_")
    try:
        _tif(os.path.join(tmp, "Depth (Max).tif"))
        off = {k: False for k in PP._DOWNLOAD_PARAMS}
        cfg = run(tmp, **off)
        check("the stub saw the settings", bool(cfg), cfg)
        check("all download switches off: parks / MS / NAIP / places / flowlines off",
              all(cfg.get(k) is False for k in PARK_ETC),
              {k: cfg.get(k) for k in PARK_ETC})
        check("all download switches off: every download key off",
              all(cfg.get(k) is False for k in PP._NO_DOWNLOADS),
              [k for k in PP._NO_DOWNLOADS if cfg.get(k) is not False])
        on = dict(off, DOWNLOAD_ROADS=True)
        cfg = run(tmp, **on)
        check("one download on: the parks download keeps its default (on)",
              cfg.get("download_trailer_parks") is True and cfg.get("download_roads") is True,
              {k: cfg.get(k) for k in PARK_ETC + ("download_roads",)})
        check("the no-download overrides cover the mobile-home parks",
              PP.no_download_overrides().get("download_trailer_parks") is False)
        check("every no-download key is a pipeline setting",
              all(k in PL.DEFAULTS for k in PP._NO_DOWNLOADS),
              [k for k in PP._NO_DOWNLOADS if k not in PL.DEFAULTS])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("\n{} failure(s)".format(len(FAILS)))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
