# -*- coding: utf-8 -*-
"""Hazard 2.20.2 (pack engine 1.3): an ArcGIS layer read area by area, another service filling
only the areas it cannot serve (build_nd_pack kind "arcgis_tiled" - the Data plugin's 3DHP
flowlines / waterbodies with NHD as the fill), the HTTP 400 page retry, the file's own date.

Fake ArcGIS services (BP.http_json replaced; no network): a grid of 150 short lines over the
test box, a point where object-id queries always fail (HTTP 504, at every split level), a point
where they fail only for big boxes (the split recovers it), features that cannot be read.
Checks:

  * a failing tile is quartered and the parts that answer are kept; only the area that never
    answers is read from the fill service;
  * one source per place: a feature crossing into the fill area is kept once (the source whose
    area holds its centre), the uid and properties["src"] say which source;
  * features that cannot be read: their area comes from the fill service, the main service's
    features there are removed (no doubles);
  * the fill service failing too: the area is a gap, the data set partial; both failing for
    everything: a clear error; the main layer description failing: everything from the fill;
  * a page refused with HTTP 400: smaller pages, a plain message when even those are refused;
  * dwr_files.source_date: the GeoPackage's own date, not the copy date.

    python3 DamSafety_Hazard/tests/test_pack_tiled_fill.py
"""
import json
import os
import sqlite3
import sys
import tempfile
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)
os.environ.pop("NDSUITE_SETTINGS", None)

from DamSafety_Hazard import build_nd_pack as BP             # noqa: E402
from DamSafety_Hazard import dwr_files as DF                 # noqa: E402
from DamSafety_Hazard import offline_pack as OP              # noqa: E402

BBOX = (-100.85, 46.75, -100.70, 46.85)
DEAD = (-100.782, 46.772)        # object ids here: always HTTP 504
SLOW = (-100.722, 46.822)        # object ids here: HTTP 504 for boxes wider than 0.03 degree
MAIN, FILL = "https://main.example.test/FeatureServer/0", "https://fill.example.test/MapServer/6"


def _line(x0, y0, x1, y1):
    return {"type": "LineString", "coordinates": [[x0, y0], [x1, y1]]}


def _grid(first_id):
    out = {}
    for j in range(10):
        for i in range(15):
            x, y = -100.848 + 0.01 * i, 46.752 + 0.01 * j
            out[first_id + i + 15 * j] = _line(x - 0.001, y - 0.001, x + 0.001, y + 0.001)
    return out


def _bb(g):
    xs = [c[0] for c in g["coordinates"]]
    ys = [c[1] for c in g["coordinates"]]
    return min(xs), min(ys), max(xs), max(ys)


def _err(what, code=504):
    e = BP.BuildError("{}: x (HTTP Error {}: {})".format(
        what, code, "Gateway Time-out" if code == 504 else "Bad Request"))
    e.http_code = code
    return e


class Svc(object):
    def __init__(self, geoms, dead=(), slow=(), bad_ids=(), desc_fail=False):
        self.geoms, self.dead, self.slow = geoms, list(dead), list(slow)
        self.bad_ids, self.desc_fail = set(bad_ids), desc_fail
        self.id_boxes, self.read = [], []

    def ids(self, box):
        x0, y0, x1, y1 = box
        inside = [p for p in self.dead + self.slow if x0 <= p[0] <= x1 and y0 <= p[1] <= y1]
        if any(p in self.dead for p in inside) or (
                any(p in self.slow for p in inside) and x1 - x0 > 0.03):
            raise _err("object ids")
        return [k for k, g in sorted(self.geoms.items()) if _meets(_bb(g), box)]

    def __call__(self, url, params=None, post=False, retries=None, what=""):
        p = params or {}
        if what == "layer description":
            if self.desc_fail:
                raise _err("layer description")
            return {"objectIdField": "OBJECTID", "maxRecordCount": 2000}
        if p.get("returnIdsOnly"):
            box = tuple(float(v) for v in p["geometry"].split(","))
            self.id_boxes.append(box)
            return {"objectIds": self.ids(box)}
        ids = [int(v) for v in p["objectIds"].split(",")]
        if self.bad_ids & set(ids):
            raise _err("features")
        self.read += ids
        return {"features": [{"type": "Feature", "geometry": json.loads(json.dumps(
            self.geoms[i])), "properties": {"OBJECTID": i, "svc": url[8:12]}} for i in ids]}


def _meets(a, b):
    return a[0] <= b[2] and b[0] <= a[2] and a[1] <= b[3] and b[1] <= a[3]


class Both(object):
    def __init__(self, main, fill):
        self.main, self.fill = main, fill

    def __call__(self, url, *a, **k):
        return (self.main if url.startswith(MAIN) else self.fill)(url, *a, **k)


SPEC = {"url": MAIN, "fields": "*", "tag": "3DHP", "source_field": "src", "tile_deg": 0.05,
        "split_depth": 2, "chunk": 40,
        "fill": {"url": FILL, "fields": "*", "tag": "NHD", "label": "NHD fill (test)"}}


def _run(main, fill, spec=SPEC, name="flow", db=None):
    db = db or os.path.join(tempfile.mkdtemp(), "w.sqlite")
    w = OP.PackWriter(db, BBOX)
    logs = []
    ctx = BP._Ctx(w, 2, logs.append, lambda *a, **k: None)
    old = BP.http_json, BP.SPLIT_PAUSE_S
    BP.http_json, BP.SPLIT_PAUSE_S = Both(main, fill), 0.0
    try:
        n = BP._run_one(ctx, name, "Flow", "arcgis_tiled", spec, BBOX)
    finally:
        BP.http_json, BP.SPLIT_PAUSE_S = old
    rows = {}
    for uid, blob in w.con.execute("SELECT uid, feat FROM {}".format(OP._table(name))):
        rows[uid] = json.loads(zlib.decompress(blob).decode("utf-8"))
    st = w.con.execute("SELECT status, note, gaps FROM datasets WHERE name=?",
                       (name,)).fetchone()
    return n, rows, ctx.tiled, st, logs


def _main_geoms():
    g = _grid(1)
    g[500] = _line(-100.790, 46.770, -100.776, 46.770)    # centre in the dead area
    g[501] = _line(-100.766, 46.770, -100.780, 46.770)    # crosses into it, centre outside
    return g


def _fill_geoms():
    g = _grid(1001)
    g[1500] = _line(-100.790, 46.770, -100.776, 46.770)
    g[1501] = _line(-100.766, 46.770, -100.780, 46.770)
    return g


def test_split_and_fill_one_source_per_place():
    main = Svc(_main_geoms(), dead=[DEAD], slow=[SLOW])
    fill = Svc(_fill_geoms())
    n, rows, rep, st, logs = _run(main, fill)
    # the slow tile was quartered and read whole from 3DHP; the dead area: 1 box of 1/16 tile
    assert any(abs((b[2] - b[0]) - 0.025) < 1e-9 and b[0] < SLOW[0] < b[2] and
               b[1] < SLOW[1] < b[3] for b in main.id_boxes), main.id_boxes
    assert rep["areas_not_listed"] == 1 and rep["missing_ids"] == 0, rep
    fb = rep["fill_areas"]
    assert len(fb) == 1 and abs((fb[0][2] - fb[0][0]) - 0.0125) < 1e-6, fb
    assert rep["fill_areas_read"] == 1 and rep["gap_areas"] == [], rep
    # grid feature 38 lies in the dead area: from NHD (1038); the crossing lines once each
    assert "3DHP:38" not in rows and "NHD:1038" in rows
    assert "NHD:1500" in rows and "3DHP:500" not in rows           # centre in the fill area
    assert "3DHP:501" in rows and "NHD:1501" not in rows           # centre outside it
    assert rep["count"] == {"3DHP": 150, "NHD": 2} and n == 152, (rep["count"], n)
    assert rep["removed_overlap"] == 1, rep
    assert all(f["properties"]["src"] == u.split(":")[0] for u, f in rows.items())
    assert st[0] == OP.STATUS_COMPLETE and "2 from NHD in 1 of 1 area" in st[1], st
    assert "HTTP Error 504" in rep["error"], rep
    assert any(m.startswith("  NHD fill: reading 1 area(s) from " + FILL) for m in logs), logs
    assert any("NHD fill: 1 of 1 area(s) read" in m for m in logs), logs


def test_features_not_read_come_from_the_fill():
    main = Svc(_grid(1), bad_ids=[77])           # 77: tile x -100.85..-100.80, y 46.80..46.85
    fill = Svc(_grid(1001))
    n, rows, rep, st, logs = _run(main, fill)
    assert rep["missing_ids"] == 1 and rep["areas_not_listed"] == 0, rep
    assert rep["count"] == {"3DHP": 125, "NHD": 25} and n == 150, rep
    assert rep["removed_overlap"] == 24, rep
    assert "NHD:1076" in rows and "3DHP:76" not in rows and "3DHP:70" in rows
    assert "1 feature(s) not read" in rep["why"], rep


def test_fill_fails_too_gap_partial():
    main = Svc(_grid(1), dead=[DEAD])
    fill = Svc(_grid(1001), dead=[(-100.778, 46.772)])     # NHD fails at feature 1038
    n, rows, rep, st, logs = _run(main, fill)
    assert rep["count"] == {"3DHP": 149, "NHD": 0}, rep
    assert "3DHP:38" not in rows and "NHD:1038" not in rows
    assert len(rep["gap_areas"]) == 1 and st[0] == OP.STATUS_PARTIAL and json.loads(st[2]), st
    assert any("1 area(s) not read by either service" in m for m in logs), logs


def test_partial_then_resume():
    """A partial result (NHD failed in the fill area) is continued by the next run: the 3DHP
    chunks are kept, only the fill is read again; nothing doubled."""
    db = os.path.join(tempfile.mkdtemp(), "w.sqlite")
    _run(Svc(_main_geoms(), dead=[DEAD]), Svc(_fill_geoms(), dead=[(-100.778, 46.772)]), db=db)
    main, fill = Svc(_main_geoms(), dead=[DEAD]), Svc(_fill_geoms())
    n, rows, rep, st, logs = _run(main, fill, db=db)
    assert st[0] == OP.STATUS_COMPLETE and rep["count"] == {"3DHP": 150, "NHD": 2}, (st, rep)
    assert "NHD:1038" in rows and "NHD:1500" in rows and "3DHP:500" not in rows, sorted(rows)
    # at most the one chunk that held the removed 3DHP:500 is read again (chunk = 40 ids)
    assert len(main.read) <= SPEC["chunk"], main.read
    assert any("continuing an interrupted download" in m for m in logs), logs


def test_main_down_all_from_fill_both_down_error():
    main = Svc(_grid(1), desc_fail=True)
    n, rows, rep, st, logs = _run(main, Svc(_grid(1001)))
    assert rep["count"] == {"3DHP": 0, "NHD": 150} and n == 150, rep
    assert "504" in rep["error"] and rep["areas_not_listed"] == 6, rep
    assert any("3DHP: the layer description failed" in m for m in logs), logs
    try:
        _run(Svc(_grid(1), desc_fail=True), Svc(_grid(1001), desc_fail=True))
    except BP.BuildError as e:
        assert "3DHP and NHD both failed for the whole area" in str(e), e
    else:
        raise AssertionError("no error")


def test_reason_text():
    assert BP._reason(_err("object ids")) == "HTTP Error 504: Gateway Time-out"
    assert BP._reason(BP.BuildError("x (The read operation timed out)")) == "timed out"


class Pages(object):
    """A paged service refusing pages bigger than `ok` (None: every page) with HTTP 400."""
    def __init__(self, n, ok=None):
        self.n, self.ok, self.sizes = n, ok, []

    def __call__(self, url, params=None, post=False, retries=None, what=""):
        p = params or {}
        if p.get("returnCountOnly"):
            return {"count": self.n}
        if p.get("outStatistics"):
            return {"features": [{"attributes": {"MX": self.n}}]}
        off, size = int(p["resultOffset"]), int(p["resultRecordCount"])
        self.sizes.append(size)
        if self.ok is None or size > self.ok:
            raise _err("page", 400)
        feats = [{"type": "Feature", "properties": {"OBJECTID": i + 1},
                  "geometry": {"type": "Point", "coordinates": [-100.8, 46.8]}}
                 for i in range(off, min(self.n, off + size))]
        return {"features": feats}


def _pages(svc):
    db = os.path.join(tempfile.mkdtemp(), "w.sqlite")
    w = OP.PackWriter(db, BBOX)
    w.begin("pages", "Pages", "http://svc", resume=True, signature="s")
    logs = []
    ctx = BP._Ctx(w, 1, logs.append, lambda *a, **k: None)
    old = BP.http_json, BP.PAGE_400_PAUSE_S
    BP.http_json, BP.PAGE_400_PAUSE_S = svc, 0.0
    try:
        BP._fetch_arcgis_pages(ctx, "http://svc", BBOX, "OBJECTID", 1000, "*", None,
                               BP._uid_arcgis("OBJECTID"), why="object ids: HTTP Error 400")
    finally:
        BP.http_json, BP.PAGE_400_PAUSE_S = old
    return w, logs


def test_page_400_smaller_pages():
    svc = Pages(700, ok=250)
    w, logs = _pages(svc)
    assert w.count == 700 and 250 in svc.sizes, (w.count, svc.sizes)
    assert any("refused (HTTP 400) - trying 500" in m for m in logs), logs


def test_page_400_plain_message():
    try:
        _pages(Pages(700))
    except BP.BuildError as e:
        txt = str(e)
        assert "HTTP 400 Bad Request, also with smaller pages (down to 100)" in txt, txt
        assert "object-id list failed too" in txt and "run the build again later" in txt, txt
    else:
        raise AssertionError("no error")


def test_source_date_is_the_files_own():
    d = tempfile.mkdtemp()
    p = os.path.join(d, "DWR_Dams.gpkg")
    con = sqlite3.connect(p)
    con.execute("CREATE TABLE gpkg_contents (table_name TEXT, last_change TEXT)")
    con.execute("INSERT INTO gpkg_contents VALUES ('DWR_Dams', '2026-08-14T09:30:00.000Z')")
    con.execute("INSERT INTO gpkg_contents VALUES ('other', '2026-09-01T00:00:00Z')")
    con.commit()
    con.close()
    assert DF.source_date(p, "DWR_Dams") == ("2026-08-14", "GeoPackage last change")
    assert DF.source_date(p) == ("2026-09-01", "GeoPackage last change")
    q = os.path.join(d, "plain.gpkg")
    open(q, "wb").write(b"not sqlite at all")
    day, how = DF.source_date(q)
    assert how == "file modified" and day == DF.file_date(q), (day, how)


if __name__ == "__main__":
    names = sorted(k for k in list(globals()) if k.startswith("test_"))
    for k in names:
        globals()[k]()
        print("ok", k)
    print("{} tests ok".format(len(names)))
