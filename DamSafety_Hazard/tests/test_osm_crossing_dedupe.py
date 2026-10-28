"""Hazard 2.18.67: one OpenStreetMap crossing mapped as a node and a way is counted once
(DWR-3034 REVIEW 9.8 item 5, FORD-DUP).

DWR-3034: ford node/11121114805 and way/1198845155 (1.57 m apart, changeset 139990619) were two
rows in Bridges_Culverts_Crossings.csv and two crossings in the Summary. facilities.dedupe now
merges two OSM records of one source that share a node, or lie within 5 m with the same category
or the same changeset; the kept row lists both ids. Twin structures of one source further apart
(or of other categories / changesets) stay apart, and NBI / OSM pairs work as before.

    QT_QPA_PLATFORM=offscreen python3 DamSafety_Hazard/tests/test_osm_crossing_dedupe.py
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qgis.core import (QgsApplication, QgsCoordinateReferenceSystem, QgsCoordinateTransformContext,  # noqa: E402,E501
                       QgsGeometry, QgsPointXY, QgsVectorLayer)
_app = QgsApplication.instance() or QgsApplication([], True)
_app.setPrefixPath(os.environ.get("QGIS_PREFIX_PATH", "/home/box/miniforge3/envs/qgis334"), True)
_app.initQgis()

from DamSafety_Hazard import facilities as FAC, osm_parse as O  # noqa: E402
KIND_CROSSING = FAC.KIND_CROSSING

FAILS = []
CRS = QgsCoordinateReferenceSystem("EPSG:5070")


def check(what, ok, got=""):
    print("{:4} {}{}".format("ok" if ok else "FAIL", what, "" if ok else "  -> {!r}".format(got)))
    if not ok:
        FAILS.append(what)


def rec(osm_id, x, y, cat="Low-water crossing / ford", source="OpenStreetMap", nodes="",
        changeset=""):
    return {"name": "(unnamed)", "category": cat, "struct_no": osm_id, "carried": "",
            "crosses": "", "adt": None, "adt_year": None, "owner": "", "built": None,
            "scour": "", "osm_nodes": nodes, "osm_changeset": changeset, "kind": KIND_CROSSING,
            "source": source, "geom": QgsGeometry.fromPointXY(QgsPointXY(x, y))}


# DWR-3034's pair, as the CSV has it (EPSG:5070 metres), from the pack: no nodes / changeset
x0, y0 = -384828.743770497, 2676566.35429406
k = FAC.dedupe([rec("node/11121114805", x0, y0),
                rec("way/1198845155", -384827.229258853, 2676566.77261798)], CRS)
check("DWR-3034 ford node + way 1.57 m apart: one record", len(k) == 1, k)
check("the kept record lists both OSM ids",
      k and k[0]["struct_no"] == "node/11121114805; way/1198845155", k and k[0]["struct_no"])
check("no also_in for a same-source merge", k and "also_in" not in k[0], k and k[0])

# 4 m, same category: one; 6 m, same category: two; 4 m other category: two
check("4 m same category -> 1", len(FAC.dedupe([rec("node/1", 0, 0),
                                               rec("way/2", 4, 0)], CRS)) == 1)
check("6 m same category -> 2", len(FAC.dedupe([rec("node/1", 0, 0),
                                               rec("way/2", 6, 0)], CRS)) == 2)
check("4 m other category, no changeset -> 2",
      len(FAC.dedupe([rec("node/1", 0, 0), rec("way/2", 4, 0, cat="Culvert")], CRS)) == 2)
check("4 m other category, same changeset -> 1",
      len(FAC.dedupe([rec("node/1", 0, 0, changeset="139990619"),
                      rec("way/2", 4, 0, cat="Culvert", changeset="139990619")], CRS)) == 1)
check("4 m other category, other changeset -> 2",
      len(FAC.dedupe([rec("node/1", 0, 0, changeset="1"),
                      rec("way/2", 4, 0, cat="Culvert", changeset="2")], CRS)) == 2)
# a way that contains the node: one, however far its centre is
check("way containing the node, 40 m off -> 1",
      len(FAC.dedupe([rec("node/77", 0, 0), rec("way/2", 40, 0, nodes="76,77,78")], CRS)) == 1)
check("two ways sharing a node, 40 m apart -> 1",
      len(FAC.dedupe([rec("way/1", 0, 0, nodes="5,6"), rec("way/2", 40, 0, nodes="6,7")],
                     CRS)) == 1)
check("ways with no shared node 40 m apart -> 2",
      len(FAC.dedupe([rec("way/1", 0, 0, nodes="5,6"), rec("way/2", 40, 0, nodes="7,8")],
                     CRS)) == 2)
# twin railroad bridges 20 m apart: stay apart (as before)
check("twin rail bridges 20 m apart -> 2",
      len(FAC.dedupe([rec("way/1", 0, 0, cat="Railroad bridge"),
                      rec("way/2", 20, 0, cat="Railroad bridge")], CRS)) == 2)
# the same OSM id twice is not merged by this rule (it is not a node / way pair)
check("same id twice -> 2 (not this rule)",
      len(FAC.dedupe([rec("node/1", 0, 0), rec("node/1", 1, 0)], CRS)) == 2)
# NBI vs OSM within 100 ft, same category: merged as before (also_in)
n = FAC.dedupe([rec("0025008224", 0, 0, cat="Culvert", source="National Bridge Inventory"),
                rec("way/9", 20, 0, cat="Culvert")], CRS)
check("NBI + OSM culvert 20 m apart: one, OSM in also_in",
      len(n) == 1 and n[0].get("also_in") == ["OpenStreetMap"], n)

# the Overpass parser keeps a way's nodes and the changeset; the query asks for them
data = {"elements": [
    {"type": "node", "id": 11121114805, "lat": 46.9880069, "lon": -101.0379721,
     "changeset": 139990619, "tags": {"ford": "yes"}},
    {"type": "way", "id": 1198845155, "center": {"lat": 46.9880114, "lon": -101.0379526},
     "nodes": [11121114804, 11121114805], "changeset": 139990619,
     "tags": {"highway": "track", "ford": "yes"}}]}
feats = O.crossing_features(data)
check("parser: osm_nodes / osm_changeset",
      [f["properties"]["osm_nodes"] for f in feats] == ["", "11121114804,11121114805"]
      and {f["properties"]["osm_changeset"] for f in feats} == {"139990619"}, feats)
check("query asks for meta (nodes + changeset)", "out center meta;" in O.crossings_query(
    (-101.1, 46.9, -101.0, 47.0)), O.crossings_query((-101.1, 46.9, -101.0, 47.0)))

# end to end through read_crossings: a GeoJSON like the download (and an old pack file)
d = tempfile.mkdtemp()
p = os.path.join(d, "osm.geojson")
with open(p, "w") as f:
    json.dump({"type": "FeatureCollection", "features": feats}, f)
lyr = QgsVectorLayer(p, "osm", "ogr")
recs = FAC.read_crossings(lyr, "OpenStreetMap", CRS, QgsCoordinateTransformContext())
check("read_crossings reads osm_nodes / changeset",
      len(recs) == 2 and recs[1]["osm_nodes"] == "11121114804,11121114805"
      and recs[0]["osm_changeset"] == "139990619", [(r["osm_nodes"], r["osm_changeset"])
                                                    for r in recs])
out = FAC.dedupe(recs, CRS)
check("downloaded ford pair -> one crossing", len(out) == 1
      and out[0]["struct_no"] == "node/11121114805; way/1198845155", [r["struct_no"] for r in out])
for ft in feats:
    for key in ("osm_nodes", "osm_changeset"):
        ft["properties"].pop(key)
p2 = os.path.join(d, "old_pack.geojson")
with open(p2, "w") as f:
    json.dump({"type": "FeatureCollection", "features": feats}, f)
old = FAC.read_crossings(QgsVectorLayer(p2, "old", "ogr"), "OpenStreetMap", CRS,
                         QgsCoordinateTransformContext())
check("an old pack file (no nodes / changeset): merged by distance + category",
      len(FAC.dedupe(old, CRS)) == 1, [r["struct_no"] for r in old])

print("\n{} failure(s)".format(len(FAILS)))
sys.stdout.flush()
# os._exit before the real-QgsApplication offscreen teardown (CI: std::bad_alloc at shutdown)
os._exit(1 if FAILS else 0)
