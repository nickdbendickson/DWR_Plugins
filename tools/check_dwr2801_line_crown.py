"""READ-ONLY check of the Hazard 2.18.65 road / rail crown check on a finished run (DWR-2801).

    QT_QPA_PLATFORM=offscreen python tools/check_dwr2801_line_crown.py DAM_FOLDER [--rail F]
        [--roads F] [--all]

Reads DAM_FOLDER's 'Depth (Max).tif', 'Arrival Time.tif', 'WSE (Max).tif',
_breach_model/run_report.json (the 1 m DEM) and structures.json, the road / rail layers the
last Hazard run used (outputs/QA/Run_Settings.json, or --rail / --roads), and the last run's
outputs/Classification/Roads_Inundated.csv / Rail_Inundated.csv (BEFORE), and prints the
stretches the crown check counts (AFTER) with their crown, WSE and freeboard, and the
stretches it leaves out and why. Writes nothing (stdout only). Light: one process, the DEM
read in small windows, only the lines inside the wet extent."""
import argparse
import csv
import json
import os
import sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qgis.core import QgsApplication, QgsProject, QgsRasterLayer, QgsVectorLayer  # noqa: E402
_app = QgsApplication.instance() or QgsApplication([], True)
_app.setPrefixPath(os.environ.get("QGIS_PREFIX_PATH", "/home/box/miniforge3/envs/qgis334"), True)
_app.initQgis()

from DamSafety_Hazard import crown as CR, linear as LN  # noqa: E402
from DamSafety_Hazard.helpers import RasterSampler  # noqa: E402


def _before(folder, name):
    p = os.path.join(folder, "outputs", "Classification", name)
    try:
        with open(p, encoding="utf-8-sig") as f:
            return list(csv.DictReader(f))
    except OSError:
        return None


def _layer(path, folder, key):
    if path:
        return path
    try:
        with open(os.path.join(folder, "outputs", "QA", "Run_Settings.json"),
                  encoding="utf-8") as f:
            got = (json.load(f).get("inputs") or {}).get(key) or []
    except (OSError, ValueError):
        return None
    for p in got:
        if os.path.exists(p):
            return p
        alt = os.path.join(folder, "outputs", "_downloads", "_pack", os.path.basename(p))
        if os.path.exists(alt):
            return alt
    return None


def _f(v, fmt="{:.2f}"):
    return "-" if v is None else fmt.format(v)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("dam_folder")
    ap.add_argument("--rail")
    ap.add_argument("--roads")
    ap.add_argument("--all", action="store_true", help="list every road stretch")
    a = ap.parse_args(argv)
    folder = a.dam_folder
    depth = os.path.join(folder, "Depth (Max).tif")
    samplers = {"depth": RasterSampler(QgsRasterLayer(depth, "depth")),
                "vel": RasterSampler(QgsRasterLayer(os.path.join(folder, "Velocity (Max).tif"),
                                                    "vel")),
                "arr": RasterSampler(QgsRasterLayer(os.path.join(folder, "Arrival Time.tif"),
                                                    "arr"))}
    acrs = samplers["depth"].layer.crs()
    src, note = CR.open_source(depth, acrs.toWkt())
    print("crown source:", note)
    samplers["crown"], samplers["crown_note"] = src, note
    ext = samplers["depth"].layer.extent()
    ctx = QgsProject.instance().transformContext()
    for key, csvname, label in (("rail", "Rail_Inundated.csv", "rail line"),
                                ("roads", "Roads_Inundated.csv", "road")):
        path = _layer(getattr(a, key), folder, key)
        before = _before(folder, csvname)
        print("\n=== {} ({})".format(key, path))
        if before is not None:
            print("BEFORE ({}): {} stretches; {} with no arrival, {} below 0.1 ft; by class {}"
                  .format(csvname, len(before),
                          sum(1 for r in before if not r.get("First_Arrival_hr")),
                          sum(1 for r in before if float(r["Max_Depth_ft"] or 0) < 0.1),
                          dict(Counter(r["ND_Class"] for r in before))))
        if not path:
            print("no layer")
            continue
        lyr = QgsVectorLayer(path, key, "ogr")
        checked = []
        counted = list(LN.inundated_stretches(lyr, samplers, acrs, ctx, ext, None, label,
                                              checked))
        dec = Counter(st["line_check"]["decision"] for _f2, st in checked)
        basis = Counter(st["line_check"]["basis"] for _f2, st in counted)
        print("AFTER: {} wet stretches -> {} counted; decisions {}; counted basis {}".format(
            len(checked), len(counted), dict(dec), dict(basis)))
        show = checked if (key == "rail" or a.all) else \
            [c for c in checked if c[1]["line_check"]["decision"] == CR.DECISION_COUNTED]
        for feat, st in show:
            lc = st["line_check"]
            x0, y0 = st["xs"][0], st["ys"][0]
            nm = LN.AttrReader(lyr).road_name(feat.attributes()) if key == "roads" else ""
            print("  fid {} {} ({:.0f}, {:.0f}) grid {:.2f} ft arr {} | {} | crown {} WSE {} "
                  "freeboard {} overtop {}{}".format(
                      feat.id(), nm, x0, y0, st["grid_max_depth"], _f(st["arrival"]),
                      lc["decision"], _f(lc.get("crown_ft")), _f(lc.get("wse_ft")),
                      _f(lc.get("freeboard_ft")), _f(lc.get("overtop_ft")),
                      " cut pts {}".format(lc["n_cut"]) if lc.get("n_cut") else ""))
    if src is not None:
        src.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
