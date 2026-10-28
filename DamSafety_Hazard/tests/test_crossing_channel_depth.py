"""Hazard 2.18.69 (DWR-3034 review, item 8): a crossing's depth in the channel on the run's
1 m DEM (crown.channel_depth / facilities.channel_depths).

DWR-3034's ford read 3.24 ft on its 30 m 'Depth (Max)' cell; the 1 m channel bed under the same
WSE gave ~9.4 ft. Tiny synthetic run (EPSG:5070): a 20 m depth / WSE grid (WSE 301 m, the coarse
cell bed 300.2 m -> 2.62 ft) and a 1 m DEM, ground 300 m with a 4 m channel at 297 m along
x = 100.

  * a ford on the channel gets Channel_Depth_1m_ft = 301 - 297 m = 13.12 ft (bed 974.4 ft);
    a crossing 60 m off the channel gets 1 m over the 300 m ground (3.28 ft);
  * Max_Depth_ft (the grid) and Flooded are not changed;
  * a dry crossing gets nothing; no crown source -> the basis says why;
  * WSE below every 1 m ground in the radius -> no channel depth;
  * the Bridges_Culverts_Crossings layer carries the three new fields after Source, and the
    summary list names the 1 m channel depth.

    QT_QPA_PLATFORM=offscreen python3 DamSafety_Hazard/tests/test_crossing_channel_depth.py
"""
import json
import os
import shutil
import sys
import tempfile

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qgis.core import (QgsApplication, QgsCoordinateReferenceSystem,  # noqa: E402
                       QgsGeometry, QgsPointXY)
_app = QgsApplication.instance() or QgsApplication([], True)
_app.setPrefixPath(os.environ.get("QGIS_PREFIX_PATH", "/home/box/miniforge3/envs/qgis334"), True)
_app.initQgis()

from osgeo import gdal, osr  # noqa: E402
from DamSafety_Hazard import crown as CR, facilities as FAC  # noqa: E402

FAILS = []
FT = 0.3048
CRS = "EPSG:5070"


def check(what, ok, got=""):
    print("{:4} {}{}".format("ok" if ok else "FAIL", what, "" if ok else "  -> {!r}".format(got)))
    if not ok:
        FAILS.append(what)


def _tif(path, arr, cell, nodata=-9999.0):
    sr = osr.SpatialReference()
    sr.ImportFromEPSG(5070)
    ds = gdal.GetDriverByName("GTiff").Create(path, arr.shape[1], arr.shape[0], 1,
                                              gdal.GDT_Float32)
    ds.SetGeoTransform((0.0, cell, 0.0, 100.0, 0.0, -cell))
    ds.SetProjection(sr.ExportToWkt())
    b = ds.GetRasterBand(1)
    b.SetNoDataValue(nodata)
    b.WriteArray(np.where(np.isfinite(arr), arr, nodata).astype(np.float32))
    ds = None


def make_run(folder, wse_m=301.0):
    os.makedirs(os.path.join(folder, "_breach_model"), exist_ok=True)
    n = 10                                          # 20 m cells, 200 x 200 m
    _tif(os.path.join(folder, "Depth (Max).tif"), np.full((n, n), (wse_m - 300.2) / FT), 20.0)
    _tif(os.path.join(folder, "WSE (Max).tif"), np.full((n, n), wse_m / FT), 20.0)
    xs = np.arange(200) + 0.5
    X, _Y = np.meshgrid(xs, xs)
    dem = os.path.join(folder, "dem_1m.tif")
    _tif(dem, np.where(np.abs(X - 100.0) <= 2.0, 297.0, 300.0), 1.0)
    with open(os.path.join(folder, "_breach_model", "run_report.json"), "w") as f:
        json.dump({"dem_z_to_m": 1.0, "work_crs": CRS,
                   "settings": {"input_files": {"DEM": dem}, "cell_size_m": 20.0}}, f)
    with open(os.path.join(folder, "_breach_model", "structures.json"), "w") as f:
        json.dump([], f)
    return os.path.join(folder, "Depth (Max).tif")


def crossing(i, x, y, name):
    return {"id": i, "kind": FAC.KIND_CROSSING, "name": name, "category": "Crossing",
            "struct_no": "node/{}".format(i), "carried": "", "crosses": "Square Butte Ck",
            "adt": None, "adt_year": None, "owner": "", "built": None, "scour": "",
            "source": "OpenStreetMap", "geom": QgsGeometry.fromPointXY(QgsPointXY(x, y))}


def main():
    tmp = tempfile.mkdtemp(prefix="xdepth_")
    acrs = QgsCoordinateReferenceSystem(CRS)
    try:
        src, note = CR.open_source(make_run(os.path.join(tmp, "wet")), acrs.toWkt())
        check("crown source opened", src is not None, note)
        grid = (301.0 - 300.2) / FT
        recs = [crossing(1, 100.0, 0.0, "Ford"), crossing(2, 40.0, 0.0, "Track"),
                crossing(3, 160.0, 60.0, "Dry")]
        ev = {1: {"depth": grid, "arrival": 1.64}, 2: {"depth": grid, "arrival": 1.7},
              3: {"depth": None, "arrival": None}}
        n = FAC.channel_depths(recs, ev, src)
        check("two wet crossings get a 1 m depth", n == 2, n)
        e1 = ev[1]
        check("ford: 1 m channel depth 13.12 ft (grid 2.62 ft)",
              abs(e1.get("channel_depth", 0) - 4.0 / FT) < 0.01, e1)
        check("ford: bed 297 m in ft", abs(e1.get("channel_bed", 0) - 297.0 / FT) < 0.01, e1)
        check("ford: basis names the 1 m ground", e1.get("channel_basis") ==
              CR.BASIS_CHANNEL.format(50.0), e1.get("channel_basis"))
        check("ford: grid depth unchanged", e1["depth"] == grid, e1["depth"])
        check("off-channel track: 1 m over the ground (3.28 ft)",
              abs(ev[2].get("channel_depth", 0) - 1.0 / FT) < 0.01, ev[2])
        check("dry crossing: nothing added", set(ev[3]) == {"depth", "arrival"}, ev[3])

        got = CR.channel_depth(src, 100.0, 0.0, 50.0)
        check("channel_depth: thalweg point inside the 4 m channel",
              got and abs(got["x"] - 100.0) <= 2.5 and got["n"] > 700, got)

        lyr = FAC.facilities_layer(recs, FAC.KIND_CROSSING, ev, None, None, acrs, "x")
        names = [f.name() for f in lyr.fields()]
        check("layer: new fields after Source",
              names[-4:] == ["Source", "Channel_Depth_1m_ft", "Channel_Bed_ft", "Channel_Basis"],
              names[-4:])
        rows = {f["Structure"]: f for f in lyr.getFeatures()}
        check("layer: ford row 13.12 ft channel, 2.62 ft grid, Flooded Yes",
              abs(rows["Ford"]["Channel_Depth_1m_ft"] - 13.12) < 0.01
              and abs(rows["Ford"]["Max_Depth_ft"] - 2.62) < 0.01
              and rows["Ford"]["Flooded"] == "Yes", rows["Ford"].attributes())
        check("layer: dry row has no channel depth",
              rows["Dry"]["Channel_Depth_1m_ft"] in (None, "") or
              str(rows["Dry"]["Channel_Depth_1m_ft"]) == "NULL", rows["Dry"].attributes())
        txt = FAC.describe([recs[0]], ev)
        check("summary names the 1 m channel depth", "2.6 ft" in txt and
              "13.1 ft in the 1 m channel" in txt, txt)
        src.close()

        # no crown source: the basis says why, nothing else changes
        ev2 = {1: {"depth": grid}}
        n = FAC.channel_depths(recs[:1], ev2, None, "no 'WSE (Max).tif' next to the depth raster")
        check("no source: 0 crossings, basis says why",
              n == 0 and "channel_depth" not in ev2[1]
              and ev2[1]["channel_basis"].endswith("no 'WSE (Max).tif' next to the depth raster"),
              ev2)

        # WSE below the ground everywhere in the radius: no channel depth
        src2, _n = CR.open_source(make_run(os.path.join(tmp, "low"), wse_m=296.0), acrs.toWkt())
        check("WSE below every 1 m ground -> None", CR.channel_depth(src2, 100.0, 0.0, 50.0) is None)
        ev3 = {1: {"depth": 0.5}}
        FAC.channel_depths(recs[:1], ev3, src2)
        check("WSE below ground: basis says no ground below the WSE",
              "no 1 m ground below the WSE" in ev3[1].get("channel_basis", ""), ev3)
        src2.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("\n{} failure(s)".format(len(FAILS)))
    return 1 if FAILS else 0


if __name__ == "__main__":
    _rc = main()
    sys.stdout.flush()
    # os._exit before the real-QgsApplication offscreen teardown (it can abort at shutdown)
    os._exit(_rc)
