"""Hazard 2.23.0: OpenStreetMap culverts are placed on a stream x road / rail crossing only
within osm_search_m (60 m; the point is the middle of the mapped culvert way), with source / src_id so
the Runner labels them mapped-not-surveyed. Synthetic fixture modelled on DWR-10554's
way/492688522, 3.6 m off the BNSF rail and 46.3 m from the 3DHP stream x rail crossing.

    python3 DamSafety_Hazard/tests/test_runnerculverts_osm.py
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))

from DamSafety_Hazard import runnerculverts as RC  # noqa: E402

fails = []


def check(cond, msg):
    print(("ok   " if cond else "FAIL ") + msg)
    if not cond:
        fails.append(msg)


def osm(oid, x, y, category="Culvert", railway=""):
    rec, why = RC.osm_record({"category": category, "waterway": "stream", "railway": railway,
                              "osm_id": oid, "name": ""}, (x, y))
    assert rec is not None, why
    return rec


# a 3DHP-like stream (west to east, digitised downstream) under a rail line (x = 0) and,
# 400 m east, a road (x = 400)
streams = [{"parts": [[(-1000.0, 0.0), (1000.0, 0.0)]], "name": "3DHP flowline"}]
rails = [{"parts": [[(0.0, -500.0), (0.0, 500.0)]], "name": "BNSF ZAP SPUR"}]
roads = [{"parts": [[(400.0, -500.0), (400.0, 500.0)]], "name": "county road"}]

check(RC.DEFAULTS["osm_search_m"] == 60.0 and RC.DEFAULTS["search_m"] == 150.0,
      "defaults: OSM culverts snap within 60 m, other records 150 m")

near = osm("way/492688522", 3.6, 46.2)                # 46.3 m from the crossing, 3.6 m off the rail
far = osm("way/1", 54.0, 72.0)                        # 90 m from it: beyond 60 m
placed, not_placed = RC.place([near, far], streams, roads, rails)
check(len(placed) == 1, "the 46 m OSM culvert is placed, the 90 m one is not")
geom, a = placed[0][:2]
check(a["source"] == "OSM" and a["src_id"] == "way/492688522", "source / src_id recorded")
check(a["embankment"] == "rail" and a["emb_name"] == "BNSF ZAP SPUR",
      "an OSM culvert with no railway tag snaps to the nearest crossing (rail here, not the road)")
check(abs(a["match_m"] - 46.3) < 0.1, "match_m is the 46.3 m snap ({})".format(a["match_m"]))
check(a["span_ft"] is None and a["rise_ft"] is None, "no size from OSM (Runner default)")
check("not surveyed" in a["notes"] and "way/492688522" in a["notes"], "notes say mapped only")
xs = [p[0] for p in geom["coordinates"]]
check(min(xs) < 0.0 < max(xs), "the line crosses the rail embankment along the stream")
check(len(not_placed) == 1 and not_placed[0][1]["src_id"] == "way/1" and
      "60 m" in not_placed[0][1]["reason"], "the far one: 'no ... within 60 m'")

# the snap is a parameter
placed, _ = RC.place([osm("way/1", 54.0, 72.0)], streams, roads, rails,
                     {"osm_search_m": 100.0})
check(len(placed) == 1, "osm_search_m = 100 m places the 90 m point")
# never looser than search_m
placed, _ = RC.place([osm("way/1", 54.0, 72.0)], streams, roads, rails,
                     {"osm_search_m": 500.0, "search_m": 50.0})
check(not placed, "the smaller of search_m and osm_search_m wins")
# OSM bridges keep search_m
placed, _ = RC.place([osm("way/9", 54.0, 72.0, category="Railway bridge")], streams, roads, rails)
check(len(placed) == 1 and placed[0][1]["kind"] == "bridge", "OSM bridges keep search_m (150 m)")
# a culvert tagged under a railway prefers the rail line even when a road crossing is nearer
placed, _ = RC.place([osm("way/7", 390.0, 0.0, railway="rail")], streams, roads, rails,
                     {"osm_search_m": 500.0, "search_m": 500.0})
check(placed and placed[0][1]["embankment"] == "rail", "railway tag: prefers the rail line")

if fails:
    print("{} FAILED".format(len(fails)))
    sys.exit(1)
print("all ok")
