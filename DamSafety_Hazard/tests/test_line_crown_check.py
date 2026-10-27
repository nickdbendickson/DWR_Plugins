"""Hazard 2.18.65 (DWR-2801 review): a road / rail stretch counts as overtopped only where the
WSE (Max) is above the 1 m centreline crown (crown.py, the vendored Runner road_profile).

Tiny synthetic run (EPSG:5070): a 10 m depth / WSE / arrival grid and a 1 m DEM with a 2 m
embankment along y = 0 (crest 302 m, ground 300 m), _breach_model/run_report.json and
structures.json next to the rasters.

  * a rail toe cell (grid wet, WSE 301 m below the 302 m crest) is not counted;
  * WSE 303 m over the crest counts, with the centreline overtopping (~3.28 ft), not the grid;
  * a bridge cut through the embankment (DEM notched to 299 m) is read on the deck stand-in
    (structures.json) - without the cut record the notch would count;
  * a stretch with no arrival time, and one shallower than 0.1 ft, is skipped;
  * no WSE / no run_report -> the depth raster as before, with the note;
  * the rail / road tables carry Depth_Basis / Grid_Depth_ft / Crown_ft / WSE_ft /
    Freeboard_ft after Notes, and the Line_Crown_Check QA layer lists every wet stretch;
  * mark_lines_incremental compares grid depth with the baseline's grid depth;
  * the vendored road_profile gives the Runner's results.

    QT_QPA_PLATFORM=offscreen python3 DamSafety_Hazard/tests/test_line_crown_check.py
"""
import importlib.util
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

from qgis.core import (QgsApplication, QgsCoordinateReferenceSystem, QgsFeature,  # noqa: E402
                       QgsGeometry, QgsPointXY, QgsProject, QgsRasterLayer, QgsVectorLayer)
_app = QgsApplication.instance() or QgsApplication([], True)
_app.setPrefixPath(os.environ.get("QGIS_PREFIX_PATH", "/home/box/miniforge3/envs/qgis334"), True)
_app.initQgis()

from osgeo import gdal, osr  # noqa: E402
from DamSafety_Hazard import crown as CR, linear as LN, scenarios as SCN  # noqa: E402
from DamSafety_Hazard import road_profile as HRP  # noqa: E402
from DamSafety_Hazard.helpers import RasterSampler  # noqa: E402

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


def _dem(notch=False):
    """1 m DEM, 200 x 200 m: ground 300 m, embankment crest 302 m |y| <= 3, toe at |y| = 8;
    with notch, a 10 m open channel at 295 <= x <= 305... cut to 299 m (a bridge opening)."""
    xs = np.arange(200) + 0.5
    ys = 100.0 - (np.arange(200) + 0.5)
    X, Y = np.meshgrid(xs, ys)
    z = 300.0 + 2.0 * np.clip((8.0 - np.abs(Y)) / 5.0, 0.0, 1.0)
    if notch:
        z = np.where(np.abs(X - 100.0) <= 5.0, 299.0, z)
    return z


def make_run(folder, wse_m, notch=False, arrival=True, depth_ft=None, structs=True,
             wse_file=True, report=True):
    os.makedirs(os.path.join(folder, "_breach_model"), exist_ok=True)
    n = 20                                         # 10 m cells
    coarse = 300.5                                 # the 30 m model's averaged embankment
    wse_m = np.full((n, n), float(wse_m))
    d = (wse_m - coarse) / FT if depth_ft is None else np.full((n, n), float(depth_ft))
    _tif(os.path.join(folder, "Depth (Max).tif"), d, 10.0)
    _tif(os.path.join(folder, "Velocity (Max).tif"), np.full((n, n), 1.0), 10.0)
    _tif(os.path.join(folder, "Arrival Time.tif"),
         np.full((n, n), 1.5 if arrival else np.nan), 10.0)
    if wse_file:
        _tif(os.path.join(folder, "WSE (Max).tif"), wse_m / FT, 10.0)
    dem = os.path.join(folder, "dem_1m.tif")
    _tif(dem, _dem(notch), 1.0)
    if report:
        with open(os.path.join(folder, "_breach_model", "run_report.json"), "w") as f:
            json.dump({"dem_z_to_m": 1.0, "work_crs": CRS,
                       "settings": {"input_files": {"DEM": dem}, "cell_size_m": 10.0}}, f)
    with open(os.path.join(folder, "_breach_model", "structures.json"), "w") as f:
        json.dump([{"kind": "bridge", "width_m": 10.0, "name": "BNSF bridge",
                    "line": [[100.0, -40.0], [100.0, 40.0]]}] if structs else [], f)
    return folder


def rail_layer():
    lyr = QgsVectorLayer("LineString?crs=EPSG:5070&field=RROWNER1:string&field=SUBDIV:string",
                         "rail", "memory")
    f = QgsFeature(lyr.fields())
    f.setGeometry(QgsGeometry.fromPolylineXY([QgsPointXY(20.0, 0.0), QgsPointXY(180.0, 0.0)]))
    f.setAttributes(["BNSF", "KO"])
    lyr.dataProvider().addFeatures([f])
    return lyr


def run_rail(folder, crown=True):
    depth = os.path.join(folder, "Depth (Max).tif")
    s = {"depth": RasterSampler(QgsRasterLayer(depth, "d")),
         "vel": RasterSampler(QgsRasterLayer(os.path.join(folder, "Velocity (Max).tif"), "v")),
         "arr": RasterSampler(QgsRasterLayer(os.path.join(folder, "Arrival Time.tif"), "a"))}
    acrs = QgsCoordinateReferenceSystem(CRS)
    note = "off"
    if crown:
        s["crown"], note = CR.open_source(depth, acrs.toWkt())
        s["crown_note"] = note
    checked = []
    res = LN.analyze_rail(rail_layer(), s, acrs, QgsProject.instance().transformContext(),
                          s["depth"].layer.extent(), checked=checked)
    if s.get("crown") is not None:
        s["crown"].close()
    return res, checked, note


def main():
    tmp = tempfile.mkdtemp(prefix="crown_")
    try:
        # 1. rail toe: grid 1.64 ft deep, WSE 301 m, crest 302 m -> not overtopped
        res, chk, note = run_rail(make_run(os.path.join(tmp, "toe"), 301.0))
        check("crown source opened", note.startswith("centreline crown check"), note)
        check("toe: grid is wet (1.64 ft)", chk and abs(chk[0]["grid_max_depth"] - 1.64) < 0.05,
              [c.get("grid_max_depth") for c in chk])
        check("toe: rail stretch NOT counted", res == [], len(res))
        lc = chk[0]["line_check"] if chk else {}
        check("toe: decision below crown", lc.get("decision") == CR.DECISION_BELOW_CROWN, lc)
        check("toe: crown 302 m, WSE 301 m, freeboard ~3.28 ft",
              lc.get("crown_ft") and abs(lc["crown_ft"] - 302.0 / FT) < 0.1
              and abs(lc["wse_ft"] - 301.0 / FT) < 0.01 and abs(lc["freeboard_ft"] - 1 / FT) < 0.1,
              lc)
        lyr = LN.checked_to_layer(chk, QgsCoordinateReferenceSystem(CRS))
        f = next(lyr.getFeatures(), None)
        check("Line_Crown_Check row for the skipped stretch",
              lyr.featureCount() == 1 and f["Decision"] == CR.DECISION_BELOW_CROWN
              and f["Line_Type"] == "Rail" and abs(f["Freeboard_ft"] - 3.28) < 0.1,
              [lyr.featureCount(), f and f.attributes()])

        # 2. WSE 303 m: 1 m over the crest -> counted with the centreline overtopping
        res, chk, _n = run_rail(make_run(os.path.join(tmp, "over"), 303.0))
        check("over: counted", len(res) == 1, len(res))
        r = res[0] if res else {}
        check("over: Max_Depth = centreline overtopping 3.28 ft (grid 8.2 ft)",
              abs(r.get("max_depth", 0) - 1 / FT) < 0.1 and abs(r.get("grid_max_depth", 0)
                                                               - 2.5 / FT) < 0.05, r.get("max_depth"))
        rl = LN.rail_to_layer(res, QgsCoordinateReferenceSystem(CRS))
        names = [fd.name() for fd in rl.fields()]
        check("rail table: check fields after Notes",
              names[-6:] == ["Notes"] + [n for n, _t in LN.CHECK_FIELDS], names[-6:])
        ft = next(rl.getFeatures(), None)
        check("rail table: basis / crown / WSE / freeboard",
              ft is not None and ft["Depth_Basis"] == CR.BASIS_CROWN
              and abs(ft["Crown_ft"] - 990.81) < 0.1 and abs(ft["WSE_ft"] - 994.09) < 0.05
              and abs(ft["Freeboard_ft"] + 3.28) < 0.1 and "centreline crown" in ft["Notes"],
              ft and ft.attributes())
        check("ROAD_FIELDS end with the check fields",
              [n for n, _t in LN.ROAD_FIELDS][-6:] == ["Notes"] + [n for n, _t in LN.CHECK_FIELDS])

        # 3. bridge cut: DEM notched to 299 m under WSE 301 m; masked -> deck stand-in 302 m
        res, chk, note = run_rail(make_run(os.path.join(tmp, "cut"), 301.0, notch=True))
        check("cut: 1 cut corridor", "1 bridge / notch cut" in note, note)
        check("cut: bridge opening not counted", res == [], [r["line_check"] for r in res])
        lc = chk[0]["line_check"] if chk else {}
        check("cut: crown read on the deck stand-in, cut points recorded",
              lc.get("n_cut", 0) > 0 and lc.get("freeboard_ft", -1) > 0.5, lc)
        res, chk, _n = run_rail(make_run(os.path.join(tmp, "nocut"), 301.0, notch=True,
                                         structs=False))
        lc = chk[0]["line_check"] if chk else {}
        # an unmapped opening is read as it is (#198): the notch is wetter than the crown
        check("no cut record: the notch counts (read as it is)", len(res) == 1, lc)

        # 4. skips: no arrival time; shallower than 0.1 ft
        res, chk, _n = run_rail(make_run(os.path.join(tmp, "noarr"), 303.0, arrival=False))
        check("no arrival: skipped", res == [] and chk and
              chk[0]["line_check"]["decision"] == CR.DECISION_NO_ARRIVAL,
              [c["line_check"]["decision"] for c in chk])
        res, chk, _n = run_rail(make_run(os.path.join(tmp, "shallow"), 303.0, depth_ft=0.05))
        check("0.05 ft: skipped", res == [] and chk and
              chk[0]["line_check"]["decision"] == CR.DECISION_SHALLOW,
              [c["line_check"]["decision"] for c in chk])

        # 4b. a run folder copied from another computer: the recorded (Windows) DEM path does
        # not exist here; the DEM is found under the dam folder (Runner road_dem_of, #198)
        fold = make_run(os.path.join(tmp, "moved"), 303.0)
        os.makedirs(os.path.join(fold, "terrain"))
        shutil.move(os.path.join(fold, "dem_1m.tif"), os.path.join(fold, "terrain", "dem_1m.tif"))
        rp = os.path.join(fold, "_breach_model", "run_report.json")
        with open(rp) as f:
            rep = json.load(f)
        rep["settings"]["input_files"]["DEM"] = "D:\\Dams\\DWR-2801\\terrain\\dem_1m.tif"
        with open(rp, "w") as f:
            json.dump(rep, f)
        src, note = CR.open_source(os.path.join(fold, "Depth (Max).tif"),
                                   QgsCoordinateReferenceSystem(CRS).toWkt())
        check("moved run: DEM found relative to the dam folder, noted",
              src is not None and "relative to the dam folder" in note and
              src.info["dem"] == os.path.join(fold, "terrain", "dem_1m.tif"), note)
        if src is not None:
            src.close()
        os.remove(os.path.join(fold, "terrain", "dem_1m.tif"))
        src, note = CR.open_source(os.path.join(fold, "Depth (Max).tif"),
                                   QgsCoordinateReferenceSystem(CRS).toWkt())
        check("moved run, DEM nowhere here: no crown source", src is None and
              "not on this computer" in note, note)

        # 5. fallbacks: no WSE raster, no run_report -> depth raster, noted
        for tag, kw, want in (("nowse", {"wse_file": False}, "no 'WSE (Max).tif'"),
                              ("norep", {"report": False}, "no Runner run_report.json")):
            fold = make_run(os.path.join(tmp, tag), 301.0, **kw)
            src, note = CR.open_source(os.path.join(fold, "Depth (Max).tif"),
                                       QgsCoordinateReferenceSystem(CRS).toWkt())
            check("{}: no crown source ({})".format(tag, want), src is None and want in note, note)
            s = {"depth": RasterSampler(QgsRasterLayer(os.path.join(fold, "Depth (Max).tif"), "d")),
                 "vel": RasterSampler(QgsRasterLayer(os.path.join(fold, "Velocity (Max).tif"),
                                                     "v")),
                 "arr": RasterSampler(QgsRasterLayer(os.path.join(fold, "Arrival Time.tif"), "a")),
                 "crown": None, "crown_note": note}
            res = LN.analyze_rail(rail_layer(), s, QgsCoordinateReferenceSystem(CRS),
                                  QgsProject.instance().transformContext(),
                                  s["depth"].layer.extent())
            ft = next(LN.rail_to_layer(res, QgsCoordinateReferenceSystem(CRS)).getFeatures(),
                      None)
            check("{}: counted on the depth raster, basis + note".format(tag),
                  len(res) == 1 and ft["Depth_Basis"] == CR.BASIS_GRID and want in ft["Notes"],
                  ft and ft.attributes())

        # 6. incremental: grid depth against the baseline grid depth
        rows = [{"max_depth": 0.3, "grid_max_depth": 3.0, "base_depth": 2.9},
                {"max_depth": 0.3, "grid_max_depth": 3.0, "base_depth": 1.0}]
        SCN.mark_lines_incremental(rows, min_depth_ft=1.0, own_velocity=False)
        check("incremental: grid 3.0 vs baseline 2.9 not charged, vs 1.0 charged",
              [bool(r.get("incremental")) for r in rows] == [False, True],
              [(r.get("incremental"), r.get("incremental_note")) for r in rows])

        # 7. parity with the Runner's road_profile
        rp_path = os.path.join(ROOT, "DamSafety_Runner", "core", "road_profile.py")
        if os.path.isfile(rp_path):
            pkg = os.path.basename(os.path.dirname(os.path.dirname(rp_path)))
            try:
                rrp = __import__(pkg + ".core.road_profile", fromlist=["x"])
            except Exception:
                rrp = None
            if rrp is None:
                print("skip Runner parity (Runner package not importable)")
            else:
                z = _dem(True)
                g = lambda xy: z[np.clip((100.0 - xy[:, 1]).astype(int), 0, 199),  # noqa
                                 np.clip(xy[:, 0].astype(int), 0, 199)]
                w = lambda xy: np.full(len(xy), 301.0 / FT)                      # noqa
                cuts = HRP.cut_corridors([{"kind": "bridge", "width_m": 10.0,
                                           "line": [[100, -40], [100, 40]]}], 10.0)
                line = [np.array([[10.0, 0.5], [190.0, 0.5]])]
                a = HRP.road_crown_depth(line, w, g, cuts=cuts)
                b = rrp.road_crown_depth(line, w, g, cuts=cuts)
                same = all(a[k] == b[k] for k in b)
                check("vendored road_profile = Runner road_profile (common keys)", same,
                      {k: (a.get(k), b[k]) for k in b if a.get(k) != b[k]})
        else:
            print("skip Runner parity (no DamSafety_Runner next to this plugin)")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("\n{} failure(s)".format(len(FAILS)))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
