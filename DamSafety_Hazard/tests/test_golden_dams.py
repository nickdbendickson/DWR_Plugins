"""Real-dam golden regression (audit P1, 2026-11-08): every fixture folder under
tests/data/golden_dams/<dam_id>/ (written by tools/freeze_golden_dam.py from a finished Runner
dam folder and the Hazard classification of it) is re-classified here with the CURRENT code -
structures.evaluate_structures on the frozen structures over the frozen (clipped) rasters,
then classification.classify_nd / classify_tm11 - and compared with expected.json:

  * every structure's ACER zone and ND outcome (the zone of record of that dam's run);
  * its peak depth / velocity (within 0.01 ft / 0.01 ft/s; a downsampled fixture raster is
    noted in expected.json and compared on the zone and outcome only);
  * the ND class, the TM 11 class and the lives in jeopardy of the structures
    (roads, railroads and lifelines are not in the fixture: a fixture's ND class is compared
    only when the frozen run's ND class came from its structures, i.e. the class the
    structures alone give equals the frozen class; otherwise the structure-level checks stand
    and the summary line says so).

No fixture (the state of a fresh checkout): prints a notice and exits 0. The maintainer runs
the freeze tool on DWR-2801 / DWR-3161 / DWR-8278 and commits the fixtures; from then on a
change that moves any zone of those dams fails this test, and the change must say why.

    QT_QPA_PLATFORM=offscreen python3 DamSafety_Hazard/tests/test_golden_dams.py

Needs PyQGIS and GDAL (SKIP without them). Set GOLDEN_DAMS=/other/root to read fixtures
elsewhere.
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
FIXTURES = os.environ.get("GOLDEN_DAMS") or os.path.join(HERE, "data", "golden_dams")
DEPTH_TOL_FT = 0.01
VEL_TOL_FPS = 0.01


def fixture_folders(root=FIXTURES):
    if not os.path.isdir(root):
        return []
    return sorted(os.path.join(root, d) for d in os.listdir(root)
                  if os.path.isfile(os.path.join(root, d, "expected.json")))


def main():
    folders = fixture_folders()
    if not folders:
        print("NOTICE: no golden dam fixtures under {} - nothing to compare. Freeze one with "
              "tools/freeze_golden_dam.py <Runner dam folder> (DWR-2801 / DWR-3161 / DWR-8278) "
              "and commit tests/data/golden_dams/<dam_id>/.".format(
                  os.path.relpath(FIXTURES, ROOT)))
        return 0
    try:
        from osgeo import gdal  # noqa: F401
        from qgis.core import QgsApplication
    except ImportError as e:
        print("SKIP: {} (needs PyQGIS and GDAL; CI's plugin-suites job runs this)".format(e))
        return 0
    app = QgsApplication.instance() or QgsApplication([], False)
    app.setPrefixPath(os.environ.get("QGIS_PREFIX_PATH", "/home/box/miniforge3/envs/qgis334"),
                      True)
    app.initQgis()
    fails = []
    for folder in folders:
        fails += check_fixture(folder)
    if fails:
        print("\n".join(fails))
        print("\n{} failure(s) over {} fixture(s)".format(len(fails), len(folders)))
        return 1
    print("ALL OK: {} golden dam fixture(s) re-classify to their frozen zones and classes"
          .format(len(folders)))
    return 0


def _rec_from_feature(feat, names, idx):
    """A structures.py record with what evaluate_structures and classify_nd read, from a
    frozen Habitable_Structures feature."""
    from DamSafety_Hazard import structures as ST
    from DamSafety_Hazard.compat import WKB_POINT
    a = dict(zip(names, feat.attributes()))

    def g(k):
        v = a.get(k)
        try:
            if v is None or v.isNull():
                return None
        except AttributeError:
            pass
        return None if type(v).__name__ in ("_Null", "QVariant", "NULL") else v
    geom = feat.geometry()
    try:
        is_point = geom.wkbType() == WKB_POINT or geom.type() == 0
    except Exception:
        is_point = False
    acer = g("ACER_Type") or ST.TYPE_HOUSE
    hab = g("Habitable") or "Yes"
    try:
        persons = int(float(g("Persons") or 0))
    except (TypeError, ValueError):
        persons = 0
    return {"id": idx, "geom": geom, "footprint": not is_point, "families": {"GOLDEN"},
            "sources": [], "habitable": hab, "acer_type": acer, "type_basis": "frozen",
            "type_conflict": False, "lifeline": False, "persons": persons,
            "persons_basis": "frozen", "category": ST.CAT_NOT_HABITABLE if hab == "No" else
            (ST.CAT_UNVERIFIED if hab == "Unverified" else ST.CAT_DWELLING),
            "occupancy": g("Occupancy") or "", "fema_category": g("FEMA_Category") or "",
            "area_ft2": g("Area_ft2"), "sqft": g("Area_ft2"),
            "_frozen": {"id": g("Struct_ID"), "zone": g("ACER_Zone"),
                        "zone_label": g("ACER_Zone_Label"), "outcome": g("ND_Outcome"),
                        "max_depth": g("Max_Depth_ft"), "max_vel": g("Max_Vel_fps"),
                        "near": g("Near_Threshold")}}


def check_fixture(folder):
    from qgis.core import (QgsCoordinateTransformContext, QgsRasterLayer, QgsVectorLayer)
    from DamSafety_Hazard import classification as CL, structures as ST
    from DamSafety_Hazard.constants import ZONE_LABELS
    from DamSafety_Hazard.processing import make_samplers
    name = os.path.basename(folder)
    with open(os.path.join(folder, "expected.json"), encoding="utf-8") as f:
        exp = json.load(f)
    out = []
    lyr = QgsVectorLayer(os.path.join(folder, "structures.gpkg"), "structures", "ogr")
    if not lyr.isValid():
        return ["{}: structures.gpkg could not be opened".format(name)]
    names = [f.name() for f in lyr.fields()]
    recs = [_rec_from_feature(f, names, i + 1) for i, f in enumerate(lyr.getFeatures())]
    rasters = {k: QgsRasterLayer(os.path.join(folder, k + ".tif"), k)
               for k in ("depth", "velocity", "arrival")}
    for k, r in rasters.items():
        if not r.isValid():
            return ["{}: {}.tif could not be opened".format(name, k)]
    downsampled = any((exp.get("rasters") or {}).get(k, {}).get("downsample_factor", 1) > 1
                      for k in ("depth", "velocity"))
    smp = make_samplers(rasters["depth"], rasters["velocity"], rasters["arrival"], 0)
    st = exp.get("hazard_settings") or {}
    kw = {}
    if st.get("near_ft") is not None:
        kw["near_ft"] = float(st["near_ft"])
    if st.get("borderline_higher") is not None:
        kw["borderline_higher"] = bool(st["borderline_higher"])
    ST.evaluate_structures(recs, smp, lyr.crs(), QgsCoordinateTransformContext(),
                           one_d=bool(st.get("one_d")), **kw)
    n_cmp = 0
    for rec in recs:
        fz = rec["_frozen"]
        sid = fz["id"]
        got_zone = rec.get("zone")
        want_zone = None if fz["zone"] in (None, "") else int(float(fz["zone"]))
        got_out = rec.get("outcome")
        want_out = fz["outcome"] or ST.OUT_DRY
        if got_zone != want_zone or got_out != want_out:
            out.append("{} structure {}: zone {} / {} now, frozen {} / {}".format(
                name, sid, ZONE_LABELS.get(got_zone, "DRY"), got_out,
                ZONE_LABELS.get(want_zone, "DRY"), want_out))
        if not downsampled and fz["max_depth"] not in (None, "") and rec.get("max_depth") \
                is not None:
            dd = abs(float(rec["max_depth"]) - float(fz["max_depth"]))
            dv = abs(float(rec.get("max_vel") or 0.0) - float(fz["max_vel"] or 0.0))
            if dd > DEPTH_TOL_FT or dv > VEL_TOL_FPS:
                out.append("{} structure {}: {:.2f} ft / {:.2f} ft/s now, frozen {} / {}".format(
                    name, sid, rec["max_depth"], rec.get("max_vel") or 0.0, fz["max_depth"],
                    fz["max_vel"]))
        n_cmp += 1
    judg = bool(st.get("nd_judgment_as_probable", False))
    nd = CL.classify_nd(recs, [], [], judgment_as_probable=judg, one_d=bool(st.get("one_d")),
                        roads_evaluated=False, rail_evaluated=False)
    tm11 = CL.classify_tm11(recs, [], judgment_as_probable=bool(
        st.get("judgment_as_probable", True)))
    want_nd, want_tm, want_lij = exp.get("nd_class"), exp.get("tm11_class"), exp.get(
        "lives_in_jeopardy")
    nd_note = ""
    if want_nd and nd["cls"] != want_nd:
        # roads / rail / lifelines are not in the fixture: a frozen class above the
        # structures' class may come from them; a class BELOW the structures' class cannot
        rank = {"Low": 0, "Medium": 1, "High": 2}
        if rank.get(nd["cls"], -1) > rank.get(want_nd, -1):
            out.append("{}: the structures alone now give ND {}, the frozen run said {}".format(
                name, nd["cls"], want_nd))
        else:
            nd_note = " (frozen ND {} came from roads / rail / lifelines, not in the fixture)".format(
                want_nd)
    if want_tm and tm11["cls"] != want_tm:
        out.append("{}: TM 11 {} now, frozen {}".format(name, tm11["cls"], want_tm))
    if want_lij is not None and abs(float(tm11["lij"]) - float(want_lij)) > 0.5:
        out.append("{}: lives in jeopardy {:.1f} now, frozen {}".format(name, tm11["lij"],
                                                                        want_lij))
    print("{:4} {}: {} structure(s) compared, ND {} / TM 11 {} (LIJ {:.1f}){}{}".format(
        "FAIL" if out else "ok", name, n_cmp, nd["cls"], tm11["cls"], tm11["lij"], nd_note,
        " [rasters downsampled: zones and outcomes only]" if downsampled else ""))
    return out


if __name__ == "__main__":
    sys.exit(main())
