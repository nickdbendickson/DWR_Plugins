"""2.18.54 review 3 (Nick, 2026-10-21): the lone-911 300 ft rule reads the USDA NAIP 2025
flight date at each point (ND GIS Hub seamlines; the assumed Microsoft date is only the
fallback), lone 911 points far from their address on their own street are flagged
(possibly misplaced), and small NAIP 2025 clips are saved for the review points. All HTTP is
mocked.

    QT_QPA_PLATFORM=offscreen python3 DamSafety_Hazard/tests/test_lone911_naip.py
"""
import datetime
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import test_restored_911 as T  # noqa: E402  (inits the headless QgsApplication)

from qgis.core import QgsFeature, QgsField, QgsGeometry, QgsPointXY, QgsRectangle  # noqa
from DamSafety_Hazard import structures as ST  # noqa: E402
from DamSafety_Hazard import classification as CL  # noqa: E402
from DamSafety_Hazard import lone911 as LN  # noqa: E402
from DamSafety_Hazard import download as DL  # noqa: E402
from DamSafety_Hazard import footprints as FP  # noqa: E402
from DamSafety_Hazard.compat import FT_STRING, FT_INT, WKB_POLYGON, WKB_POINT, WKB_LINESTRING  # noqa
from DamSafety_Hazard.helpers import create_memory_layer  # noqa: E402

X0, Y0, CRS, CTX = T.X0, T.Y0, T.CRS, T.CTX
SEAM = [("IDATE", FT_STRING), ("SDATE", FT_STRING), ("EDATE", FT_STRING)]
ROAD = [("St_Name", FT_STRING), ("St_PosTyp", FT_STRING), ("St_PosDir", FT_STRING),
        ("FromAddr_L", FT_INT), ("ToAddr_L", FT_INT), ("FromAddr_R", FT_INT),
        ("ToAddr_R", FT_INT)]


def _seamlines(rows):
    return T._layer(WKB_POLYGON, LN.NAIP_DATES_NAME, SEAM, rows)


def _roads(rows):
    lyr = create_memory_layer("ND_roads_NG911", WKB_LINESTRING, CRS,
                              [QgsField(n, t) for n, t in ROAD])
    feats = []
    for pts, attrs in rows:
        f = QgsFeature(lyr.fields())
        f.setGeometry(QgsGeometry.fromPolylineXY([QgsPointXY(*p) for p in pts]))
        f.setAttributes(list(attrs))
        feats.append(f)
    lyr.dataProvider().addFeatures(feats)
    lyr.updateExtents()
    return lyr


def _ms():
    return T._layer(WKB_POLYGON, "MS_building_footprints", T.MSF,
                    [((X0, Y0, X0 + 12, Y0 + 12), ("4", "-1"))])


def _pt(nguid, x, y, upd, num=3550, street=("153RD", "AVE", "SE")):
    return ((x, y), (nguid, num) + tuple(street) + ("Unknown", "Unknown", "", upd))


def test_parse_dates_and_clip_url():
    assert LN.parse_flight_date("2025-07-13") == datetime.date(2025, 7, 13)
    assert LN.parse_flight_date("", "07/13/2025 09:28") == datetime.date(2025, 7, 13)
    assert LN.parse_flight_date(None, None) is None
    u = LN.clip_url(-97.25653, 46.90402)
    assert u.startswith("https://aerial.dwr.nd.gov/dwrwms?") and "VERSION=1.1.1" in u \
        and "LAYERS=NorthDakota_NAIP_2025_col_air" in u and "SRS=EPSG%3A4326" in u, u
    x0, y0, x1, y1 = LN.clip_bbox(-97.25653, 46.90402)
    assert abs((y1 - y0) * 111320 - 600) < 1 and abs(
        (x1 - x0) * 111320 * 0.6838 - 600) < 6, (x1 - x0, y1 - y0)


def test_naip_flight_date_is_the_imagery_date():
    # west seamline flown 2025-07-13, east 2025-09-01 (overlap: the earliest counts); one
    # point outside the seamlines uses the assumed date and says why
    seam = _seamlines([((X0 - 5000, Y0 - 5000, X0 + 2000, Y0 + 5000),
                        ("2025-07-13", "07/13/2025 09:28", "")),
                       ((X0 + 1000, Y0 - 5000, X0 + 9000, Y0 + 5000),
                        ("2025-09-01", "09/01/2025 10:00", ""))])
    dates = LN.ImageryDates(seam, CRS, CTX)
    assert len(dates) == 2 and dates.range == (datetime.date(2025, 7, 13),
                                               datetime.date(2025, 9, 1))
    assert dates.at(X0 + 1500, Y0)[0] == datetime.date(2025, 7, 13)       # overlap: earliest
    assert dates.at(X0 + 5000, Y0)[1] == "USDA NAIP 2025 flown 2025-09-01"
    assert dates.at(X0 + 20000, Y0) is None
    pts = T._layer(WKB_POINT, "NDGISHUB_address_points", T.NENA_D, [
        _pt("OLD", X0 + 600, Y0, "2022-04-13"),          # older than the flight: dropped
        _pt("NEW", X0 + 600, Y0 + 800, "2025-08-01"),    # after the 07-13 flight: kept
        _pt("LATE", X0 + 5000, Y0, "2025-08-01"),        # before the 09-01 flight: dropped
        _pt("OUT", X0 + 20000, Y0, "2024-05-01"),        # no seamline: assumed 2022-12-31
        ((X0 + 600, Y0 - 800), ("GAS", 3520, "153RD", "AVE", "SE", "Unknown", "Unknown",
                                "WBI Energy is building a natural gas transmission line "
                                "station", "2025-05-16"))])
    structs = ST.build_inventory([(FP.MS_FAMILY, _ms())], [("NDGISHUB", pts)], CRS, CTX,
                                 imagery_dates=dates)
    old, new, late, out = (T._by_nguid(structs, k) for k in ("OLD", "NEW", "LATE", "OUT"))
    assert old["habitable"] == "Unverified" and old["ms_dropped"]["img"] == "2025-07-13" \
        and not old["ms_dropped"]["assumed"], old.get("ms_dropped")
    assert "imagery USDA NAIP 2025 flown 2025-07-13" in "; ".join(ST.structure_notes(old))
    assert new["habitable"] == "Yes" and new["ms_kept_newer"]["img"] == "2025-07-13", \
        new.get("ms_kept_newer")
    assert "possibly built after" in "; ".join(ST.structure_notes(new))
    assert late["habitable"] == "Unverified" and late["ms_dropped"]["img"] == "2025-09-01"
    assert out["habitable"] == "Yes" and out["ms_kept_newer"]["assumed"] == "2022-12-31" \
        and "fallback: the point is outside the NAIP 2025 seamlines" in \
        out["ms_kept_newer"]["basis"], out.get("ms_kept_newer")
    gas = T._by_nguid(structs, "GAS")      # NOTES decide, not the rule: not listed as its drop
    assert gas["habitable"] == "Unverified" and not gas.get("ms_dropped") and \
        not gas.get("ms_kept_newer"), (gas["habitable"], gas.get("ms_dropped"))
    line = ST.ms_rule_source_line(structs)
    assert line.startswith("Lone 911 points: 2 not counted by the 300 ft no-building rule") \
        and "; 2 counted as possibly built after the imagery" in line \
        and "USDA NAIP 2025 flight date at each point (ND GIS Hub seamlines), flown " \
            "2025-07-13 to 2025-09-01, for 3" in line \
        and "ASSUMED 2022-12-31 (setting ms_imagery_assumed_date; Microsoft publishes no " \
            "capture dates for US footprints) for 1 - FALLBACK: the point is outside the " \
            "NAIP 2025 seamlines" in line, line
    # no flight dates at all (offline / failed download): every point says why
    s2 = ST.build_inventory([(FP.MS_FAMILY, _ms())], [("NDGISHUB", pts)], CRS, CTX,
                            imagery_why="the download failed and there is no earlier copy")
    assert "FALLBACK: the download failed and there is no earlier copy" in \
        ST.ms_rule_source_line(s2)
    assert T._by_nguid(s2, "OLD")["ms_dropped"]["assumed"] == "2022-12-31"


def test_possibly_misplaced_911_point():
    # Nick's example (#721): 3550 153RD AVE SE sits 531 m east of 153rd Ave SE; its range
    # 3500-3598 (right, even) runs north to south; a Microsoft footprint stands near the
    # expected address location (an industrial plant entrance) - a review item
    road_x = X0 + 2000
    roads = _roads([
        ([(road_x, Y0 + 1000), (road_x, Y0 - 1000)], ("153RD", "AVE", "SE", 3501, 3599,
                                                       3500, 3598)),
        ([(road_x - 3000, Y0 + 1000), (road_x, Y0 + 1000)], ("35TH", "ST", "SE", 15301,
                                                              15349, 15300, 15348))])
    streets = LN.StreetIndex(roads, CRS, CTX)
    assert streets.ok and len(streets) == 2
    ept, label = streets.expected(LN.street_key("153rd", "Ave", "SE"), 3550)
    assert label == "153RD AVE SE" and abs(ept.x() - road_x) < 1e-6 and \
        abs(ept.y() - (Y0 + 1000 - 2000 * 50 / 98.0)) < 1e-6, (ept.x(), ept.y())
    assert streets.expected(LN.street_key("153RD", "AVE", "SE"), 3700) is None
    # parity: odd numbers are on the left range (3501-3599) only
    assert LN._in_range(3551, 3501, 3599) and not LN._in_range(3551, 3500, 3598)
    assert LN._in_range(3551, 3500, 3598, "B")
    ey = Y0 + 1000 - 2000 * 50 / 98.0
    ms = T._layer(WKB_POLYGON, "MS_building_footprints", T.MSF,
                  [((X0, Y0, X0 + 12, Y0 + 12), ("4", "-1")),
                   ((road_x + 20, ey - 10, road_x + 60, ey + 10), ("6", "-1"))])
    pts = T._layer(WKB_POINT, "NDGISHUB_address_points", T.NENA_D, [
        _pt("{3F0FB7BC-A966-488D-87E4-2ECB2B816638}", road_x + 531, Y0, "2022-04-13"),
        _pt("NEAR", road_x + 60, Y0 + 520, "2022-04-13", num=3523)])
    structs = ST.build_inventory([(FP.MS_FAMILY, ms)], [("NDGISHUB", pts)], CRS, CTX,
                                 streets=streets, misplaced_m=150.0)
    k = T._by_nguid(structs, "{3F0FB7BC-A966-488D-87E4-2ECB2B816638}")
    m = k["misplaced"]
    assert m["far"] and m["ms_near_expected"] and 530 < m["dist_m"] < 533 and \
        m["street"] == "153RD AVE SE" and m["lat"] is not None, m
    assert k["habitable"] == "Unverified" and k["ms_dropped"]["review"], k.get("ms_dropped")
    notes = "; ".join(ST.structure_notes(k))
    assert "possibly misplaced: 531 m from its address on 153RD AVE SE; expected location " \
        "near " in notes and "review item, not a silent drop" in notes, notes
    near = T._by_nguid(structs, "NEAR")
    assert near.get("misplaced") and not near["misplaced"]["far"], near.get("misplaced")
    assert "of them are possibly misplaced" in ST.ms_rule_source_line(structs)
    # the judgment items: the dropped point (with its clip) and the misplaced list
    for r in structs:
        if r is k:
            r["outcome"], r["max_depth"], r["zone"] = ST.OUT_PROBABLE, 8.17, ST.CLASS_HIGH
        else:
            r["outcome"], r["max_depth"], r["zone"] = ST.OUT_DRY, None, None
    nd, tm = CL.classify_nd(structs, [], []), CL.classify_tm11(structs, [])
    msg = CL.ms_rule_judgment(nd, tm, structs, [], [], misplaced_m=150.0,
                              clip_fn=lambda r: "QA/imagery/{}_naip2025.jpg".format(r["id"]))
    assert "REVIEW: possibly misplaced, a Microsoft footprint is near its address location; " \
        "NAIP 2025 clip QA/imagery/{}_naip2025.jpg".format(k["id"]) in msg, msg
    assert "Lone 911 points possibly misplaced (wet; more than 150 m" in msg and \
        "possibly misplaced: 531 m from its address on 153RD AVE SE" in msg, msg
    assert sum(1 for j in nd["judgment"] if "possibly misplaced" in j) == 2


def _jpeg(color=(30, 120, 30)):
    from qgis.PyQt.QtGui import QImage, QColor
    from qgis.PyQt.QtCore import QBuffer, QByteArray, QIODevice
    img = QImage(64, 64, QImage.Format_RGB32)
    img.fill(QColor(*color))
    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QIODevice.WriteOnly)
    img.save(buf, "JPG")
    return bytes(ba)


def test_clips_are_capped_time_limited_and_fail_softly():
    from qgis.PyQt.QtGui import QImage
    calls = []
    good = _jpeg()
    orig = LN._http_get

    def fake(url, timeout_s):
        calls.append((url, timeout_s))
        return good
    LN._http_get = fake
    try:
        out = tempfile.mkdtemp()
        qa = os.path.join(out, "QA")
        logs = []
        cm = LN.ClipMaker(out, qa, cap=1, timeout_s=7, log=logs.append)
        rec = {"id": 776, "lonlat": (-97.25653, 46.90402),
               "misplaced": {"lon": -97.25655, "lat": 46.90402}}
        assert cm.clip(rec) == "QA/imagery/776_naip2025.jpg"
        assert cm.clip(rec) == "QA/imagery/776_naip2025.jpg" and len(calls) == 1  # cached
        assert calls[0][1] == 7 and "BBOX=" in calls[0][0]
        img = QImage(os.path.join(qa, "imagery", "776_naip2025.jpg"))
        c = img.pixelColor(img.width() // 2, img.height() // 2 - 10)   # the red ring
        assert c.red() > 180 and c.green() < 90, (c.red(), c.green(), c.blue())
        assert cm.clip({"id": 777, "lonlat": (-97.2, 46.9)}) == ""          # capped
        assert "1 more not clipped (cap 1 per run" in cm.summary(), cm.summary()

        def boom(url, timeout_s):
            raise TimeoutError("timed out after {} s".format(timeout_s))
        LN._http_get = boom
        cm2 = LN.ClipMaker(out, qa, cap=25, log=logs.append)
        for i in range(5):
            assert cm2.clip({"id": 900 + i, "lonlat": (-97.2, 46.9)}) == ""
        assert cm2.tried == LN.CLIP_STOP_AFTER_FAILS, cm2.tried       # stops trying
        assert any("not saved: timed out" in m for m in logs), logs
        LN._http_get = lambda url, timeout_s: b"<ServiceExceptionReport>bad layer"
        cm3 = LN.ClipMaker(out, qa, log=logs.append)
        assert cm3.clip({"id": 950, "lonlat": (-97.2, 46.9)}) == ""
        assert "did not return a JPEG" in logs[-1], logs[-1]
        assert LN.ClipMaker(out, qa, enabled=False).clip(rec) == ""
    finally:
        LN._http_get = orig


def test_fetch_naip_dates_one_query_per_run():
    # mocked ArcGIS answers: layer meta, count, one geojson page - then the cached file
    feats = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "id": 1, "properties": {"IDATE": "2025-07-13",
                                                     "SDATE": "07/13/2025 09:28"},
         "geometry": {"type": "Polygon", "coordinates": [[[-97.3, 46.8], [-97.2, 46.8],
                                                          [-97.2, 46.95], [-97.3, 46.95],
                                                          [-97.3, 46.8]]]}}]}
    urls = []

    def fake(url, user_agent=None, timeout_s=None):
        urls.append(url)
        assert "/MapServer/41" in url, url
        if "returnCountOnly" in url:
            return json.dumps({"count": 1}).encode()
        if "/query?" in url:
            return json.dumps(feats).encode()
        return json.dumps({"objectIdField": "OBJECTID", "maxRecordCount": 1000,
                           "advancedQueryCapabilities": {"supportsPagination": True}}).encode()
    orig = DL.http_get_bytes
    DL.http_get_bytes = fake
    try:
        out = tempfile.mkdtemp()
        lyr, n, path = LN.fetch_naip_dates((-97.4, 46.7, -97.1, 47.0), out)
        assert n == 1 and os.path.basename(path).startswith(LN.NAIP_DATES_NAME), (n, path)
        assert os.path.dirname(path) == os.path.join(out, "_downloads")
        k = len(urls)
        lyr2, n2, _p = LN.fetch_naip_dates((-97.4, 46.7, -97.1, 47.0), out)
        assert n2 == 1 and len(urls) == k                 # today's file: no new request
        from qgis.core import QgsCoordinateReferenceSystem
        d = LN.ImageryDates(lyr2, QgsCoordinateReferenceSystem("EPSG:4326"), CTX)
        assert d.at(-97.25653, 46.90402)[0] == datetime.date(2025, 7, 13)
        assert "flown 2025-07-13 to 2025-07-13" in LN.naip_source_line(d, path)
    finally:
        DL.http_get_bytes = orig


if __name__ == "__main__":

    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn) and fn.__module__ == __name__:
            try:
                fn()
                print("PASS", name)
            except Exception:
                fails += 1
                import traceback
                traceback.print_exc()
                print("FAIL", name)
    sys.stdout.flush()
    os._exit(1 if fails else 0)
