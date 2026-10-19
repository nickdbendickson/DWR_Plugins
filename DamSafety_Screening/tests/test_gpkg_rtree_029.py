"""0.29.0: plain-sqlite3 writes to a GeoPackage that QGIS/GDAL wrote (one with an R*Tree spatial
index whose triggers call ST_IsEmpty) must not fail.

The _gpkg test fake writes an index-less GeoPackage, so the Screening tests passed while the real
tool 18 merge and the Review tab's "Move breach point" died on every real work folder with
"no such function: ST_IsEmpty". This test adds the real GDAL R*Tree index + triggers to the fake's
output, proves the write fails with a plain sqlite3 connection (ST_* unregistered, the pre-fix
state), and that merge (H1), move_point (H2) and the far-move guard (L2) behave once
core/gpkg_write.connect registers ST_* (#77 keeps the spatial index rather than dropping it).

Run: PYTHONPATH=tests/qgis_mock:<parent of the plugin> python3 tests/test_gpkg_rtree_029.py
"""
import os
import sqlite3
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "qgis_mock"))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
sys.path.insert(0, HERE)

import _gpkg  # noqa: E402
from nd_dam_screening.core import merge as mg, review as rv, workflow as wf, gpkg_write  # noqa: E402

DF = [("dam_id", "TEXT"), ("name", "TEXT"), ("height_m", "REAL"), ("stor_method", "TEXT")]
PF = DF + [("pond_type", "TEXT"), ("no_flood", "TEXT")]
BF = [("dam_id", "TEXT"), ("flow_az", "REAL"), ("dir_conf", "TEXT"), ("dir_method", "TEXT")]
AF = [("dam_id", "TEXT")]
X0, Y0 = 500000.0, 2000000.0        # a breach point in EPSG:5070 metres


def add_rtree(path, table, gcol="geom"):
    """Give a feature table the real GDAL gpkg_rtree_index: the rtree virtual table, its six
    triggers (each guarded by ST_IsEmpty, a function only GDAL's SQLite dialect registers) and
    the gpkg_extensions row. This is exactly what QGIS/GDAL leaves in a .gpkg it has written."""
    con = sqlite3.connect(path)
    try:
        con.execute("CREATE TABLE IF NOT EXISTS gpkg_extensions (table_name TEXT, column_name TEXT, "
                    "extension_name TEXT, definition TEXT, scope TEXT)")
        con.execute("INSERT INTO gpkg_extensions VALUES (?,?,?,?,?)",
                    (table, gcol, "gpkg_rtree_index",
                     "http://www.geopackage.org/spec120/#extension_rtree", "write-only"))
        rt = "rtree_{}_{}".format(table, gcol)
        con.execute('CREATE VIRTUAL TABLE "{}" USING rtree(id, minx, maxx, miny, maxy)'.format(rt))
        for (fid,) in con.execute('SELECT fid FROM "{}"'.format(table)).fetchall():
            con.execute('INSERT INTO "{}" VALUES (?,?,?,?,?)'.format(rt), (fid, 0.0, 0.0, 0.0, 0.0))
        ins = ('INSERT OR REPLACE INTO "{r}" VALUES (NEW.fid, ST_MinX(NEW.{c}), ST_MaxX(NEW.{c}), '
               'ST_MinY(NEW.{c}), ST_MaxY(NEW.{c}));').format(r=rt, c=gcol)
        for sql in (
                'CREATE TRIGGER "{r}_insert" AFTER INSERT ON "{t}" WHEN (new.{c} NOT NULL AND NOT '
                'ST_IsEmpty(NEW.{c})) BEGIN {ins} END',
                'CREATE TRIGGER "{r}_update1" AFTER UPDATE OF {c} ON "{t}" WHEN OLD.fid = NEW.fid AND '
                '(NEW.{c} NOTNULL AND NOT ST_IsEmpty(NEW.{c})) BEGIN {ins} END',
                'CREATE TRIGGER "{r}_update2" AFTER UPDATE OF {c} ON "{t}" WHEN OLD.fid = NEW.fid AND '
                '(NEW.{c} ISNULL OR ST_IsEmpty(NEW.{c})) BEGIN DELETE FROM "{r}" WHERE id = OLD.fid; END',
                'CREATE TRIGGER "{r}_update3" AFTER UPDATE ON "{t}" WHEN OLD.fid != NEW.fid AND (NEW.{c} '
                'NOTNULL AND NOT ST_IsEmpty(NEW.{c})) BEGIN DELETE FROM "{r}" WHERE id = OLD.fid; {ins} END',
                'CREATE TRIGGER "{r}_update4" AFTER UPDATE ON "{t}" WHEN OLD.fid != NEW.fid AND (NEW.{c} '
                'ISNULL OR ST_IsEmpty(NEW.{c})) BEGIN DELETE FROM "{r}" WHERE id IN (OLD.fid, NEW.fid); END',
                'CREATE TRIGGER "{r}_delete" AFTER DELETE ON "{t}" WHEN old.{c} NOT NULL BEGIN DELETE '
                'FROM "{r}" WHERE id = OLD.fid; END'):
            con.execute(sql.format(r=rt, t=table, c=gcol, ins=ins))
        con.commit()
    finally:
        con.close()


def populate(W):
    """A work folder with the dam side (3/4) and the pond side (16), where the 3/4 layers carry a
    GDAL R*Tree index (the real-QGIS state) and so do the 18_* copies the merge makes from them."""
    _gpkg.write(wf.path(W, "3", "OUT"), {"dams": ("Point", DF, [
        (("Point", (-100.1, 47.1)), {"dam_id": "DWR-1", "name": "A", "height_m": 5.0, "stor_method": "calibrated"}),
        (("Point", (-100.2, 47.2)), {"dam_id": "DWR-2", "name": "B", "height_m": 7.0, "stor_method": "estimated"})])})
    add_rtree(wf.path(W, "3", "OUT"), "dams")
    _gpkg.write(wf.path(W, "16", "OUT"), {"ponds": ("Point", PF, [
        (("Point", (-100.3, 47.3)), {"dam_id": "POND-1", "name": "", "height_m": 2.0, "stor_method": "estimated",
                                     "pond_type": "embankment pond", "no_flood": ""}),
        (("Point", (-100.4, 47.4)), {"dam_id": "POND-2", "name": "", "height_m": 0.2, "stor_method": "estimated",
                                     "pond_type": "dugout", "no_flood": "YES"})])})
    _gpkg.write(wf.path(W, "4", "OUTP"), {"bp": ("Point", BF, [
        (("Point", (490000.0, 2010000.0)), {"dam_id": "DWR-1", "flow_az": 90.0, "dir_conf": "high", "dir_method": "line"}),
        (("Point", (X0, Y0)), {"dam_id": "DWR-2", "flow_az": 180.0, "dir_conf": "low", "dir_method": "rim"})])})
    add_rtree(wf.path(W, "4", "OUTP"), "bp")
    _gpkg.write(wf.path(W, "16", "OUTP"), {"bp": ("Point", BF, [
        (("Point", (470000.0, 2020000.0)), {"dam_id": "POND-1", "flow_az": 45.0, "dir_conf": "medium", "dir_method": "line"})])})
    _gpkg.write(wf.path(W, "4", "OUTL"), {"ar": ("LineString", AF, [
        (("LineString", [(1, 2), (2, 3)]), {"dam_id": "DWR-1"})])})
    add_rtree(wf.path(W, "4", "OUTL"), "ar")
    _gpkg.write(wf.path(W, "16", "OUTL"), {"ar": ("LineString", AF, [
        (("LineString", [(5, 6), (6, 7)]), {"dam_id": "POND-1"})])})


def triggers(path):
    con = sqlite3.connect(path)
    try:
        return [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'rtree_%'")]
    finally:
        con.close()


def count(path, table):
    con = sqlite3.connect(path)
    try:
        return con.execute('SELECT COUNT(*) FROM "{}"'.format(table)).fetchone()[0]
    finally:
        con.close()


# -- the hazard is real: a plain sqlite3 connection (no ST_*) dies on the rtree triggers (H1)
Wbad = tempfile.mkdtemp(prefix="rtree_bad_")
populate(Wbad)
real_connect = gpkg_write.connect
gpkg_write.connect = lambda path: sqlite3.connect(path)      # pre-fix: ST_* not registered
try:
    try:
        mg.merge(Wbad)
        raise AssertionError("merge succeeded without the ST_* functions registered")
    except sqlite3.OperationalError as e:
        assert "ST_IsEmpty" in str(e), e
finally:
    gpkg_write.connect = real_connect

# -- H1: with gpkg_write the merge appends the ponds and the 18_* copies KEEP their rtree index
# (#77 registers ST_* on the connection instead of dropping the index, so QGIS keeps a usable one)
W = tempfile.mkdtemp(prefix="rtree_merge_")
populate(W)
r = mg.merge(W)
assert r["n_dams"] == 2 and r["n_ponds"] == 2, r
assert count(r["dams"], "dams") == 4 and count(r["bpts"], "bp") == 3, (r["dams"], r["bpts"])
assert all(triggers(p) for p in (r["dams"], r["bpts"], r["arrows"]))   # index preserved, not dropped
assert any("added column pond_type" in n for n in r["notes"])
# running it again replaces (no doubling) - the fresh 18 copy has the rtree again, still handled
r2 = mg.merge(W)
assert count(r2["dams"], "dams") == 4

# -- the hazard is real for the move too: a plain sqlite3 connection (no ST_*) UPDATE dies (H2)
Wmv = tempfile.mkdtemp(prefix="rtree_move_")
populate(Wmv)
bp4 = wf.path(Wmv, "4", "OUTP")
gpkg_write.connect = lambda path: sqlite3.connect(path)
try:
    try:
        rv.SqliteApply().move_point(bp4, "DWR-2", X0 + 10.0, Y0 + 10.0)
        raise AssertionError("move_point succeeded without the ST_* functions registered")
    except sqlite3.OperationalError as e:
        assert "ST_IsEmpty" in str(e), e
finally:
    gpkg_write.connect = real_connect
# the point never moved in the broken state
assert rv.SqliteApply().point_of(bp4, "DWR-2") == (X0, Y0)

# -- H2: with gpkg_write move_point actually moves the point and keeps the rtree index (#77)
n = rv.SqliteApply().move_point(bp4, "DWR-2", X0 + 10.0, Y0 + 10.0)
assert n == 1 and rv.SqliteApply().point_of(bp4, "DWR-2") == (X0 + 10.0, Y0 + 10.0)
assert triggers(bp4) != []                                     # index preserved, not dropped

# -- L2: a far move (wrong CRS or wrong dam) is refused and never logged; a near move is applied
Wg = tempfile.mkdtemp(prefix="rtree_guard_")
_gpkg.write(wf.path(Wg, "16", "OUTP"), {"bp": ("Point", BF, [
    (("Point", (X0, Y0)), {"dam_id": "D1", "flow_az": 90.0, "dir_conf": "low", "dir_method": "rim"})])})
assert rv.SqliteApply().point_of(wf.path(Wg, "16", "OUTP"), "D1") == (X0, Y0)
assert rv._gpkg_point_xy(_gpkg.blob(("Point", (1.5, -2.5)))) == (1.5, -2.5)
for far in ("-99.0, 47.0", "{:.1f}, {:.1f}".format(X0 + 6000.0, Y0)):   # degrees typed; 6 km off
    try:
        rv.decide(Wg, "D1", "move_breach", far, "JD")
        raise AssertionError("a far move was accepted: " + far)
    except ValueError as e:
        assert "refused" in str(e) and "EPSG:5070" in str(e), e
assert rv.read_log(Wg) == []                                   # a refused move is not logged
try:
    rv.apply(Wg, {"dam_id": "D1", "action": "move_breach", "value": "0, 0", "reviewer": "JD", "when": "x"})
    raise AssertionError("apply accepted a far move")
except ValueError:
    pass
row, done = rv.decide(Wg, "D1", "move_breach", "{:.1f}, {:.1f}".format(X0 + 120.0, Y0 - 40.0), "JD")
assert done and done[0][1] == 1
assert rv.SqliteApply().point_of(wf.path(Wg, "16", "OUTP"), "D1") == (X0 + 120.0, Y0 - 40.0)
assert [r["action"] for r in rv.read_log(Wg)] == ["move_breach"]
# a dam with no current breach point is not distance-checked
_gpkg.write(wf.path(Wg, "4", "OUTP"), {"bp": ("Point", BF, [])})
assert rv.move_xy(Wg, "NEW", "10, 20") == (10.0, 20.0)

print("OK (rtree ST_IsEmpty handled on merge + move; far-move guard refuses a wrong-CRS point)")
