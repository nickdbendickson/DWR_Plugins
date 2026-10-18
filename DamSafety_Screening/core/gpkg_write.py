"""sqlite3 writes to a GeoPackage that QGIS or GDAL wrote. Its spatial-index triggers call
ST_IsEmpty and ST_MinX/MaxX/MinY/MaxY, which only GDAL's SQLite has, so a plain sqlite3 INSERT
or UPDATE fails with "no such function: ST_IsEmpty". connect() adds them to the connection, so
the write goes through and the index stays right."""
import sqlite3

from ..damsuite.suitegpkg import bbox, parse_blob


def _is_empty(g):
    return None if g is None else int(bool(g[3] & 0x10))


def _bound(i):
    def f(g):
        b = None if g is None or g[3] & 0x10 else bbox(parse_blob(g)[0])
        return None if b is None else b[i]
    return f


def connect(path):
    con = sqlite3.connect(path)
    con.create_function("ST_IsEmpty", 1, _is_empty)
    for name, i in (("ST_MinX", 0), ("ST_MinY", 1), ("ST_MaxX", 2), ("ST_MaxY", 3)):
        con.create_function(name, 1, _bound(i))
    return con
