"""Step 18's merge and the Review tab's writes on GeoPackages that GDAL wrote (audit 1.22, #4):
their spatial-index triggers call ST_* functions plain sqlite3 lacks, so these writes used to fail
with "no such function: ST_IsEmpty". Needs GDAL (the QGIS Python); prints SKIP otherwise."""
import os
import sqlite3
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
sys.path.insert(0, HERE)

import _optional  # noqa: E402
_optional.need("osgeo", "GDAL writes the GeoPackages here")
from osgeo import gdal, ogr  # noqa: E402
from nd_dam_screening.core import merge as mg, review as rv  # noqa: E402

gdal.UseExceptions()
W = tempfile.mkdtemp(prefix="gpkg_gdal_")


def gdal_gpkg(name, gtype, rows):
    path = os.path.join(W, name + ".gpkg")
    ds = ogr.GetDriverByName("GPKG").CreateDataSource(path)
    lyr = ds.CreateLayer(name, None, gtype)
    lyr.CreateField(ogr.FieldDefn("dam_id", ogr.OFTString))
    for did, wkt in rows:
        f = ogr.Feature(lyr.GetLayerDefn())
        f.SetField("dam_id", did)
        f.SetGeometry(ogr.CreateGeometryFromWkt(wkt))
        lyr.CreateFeature(f)
    ds = None
    return path


def rtree(path, table):
    con = sqlite3.connect(path)
    try:
        return con.execute('SELECT id, minx, maxx, miny, maxy FROM "rtree_{}_geom" ORDER BY id'.format(table)).fetchall()
    finally:
        con.close()


def found(path, x0, y0, x1, y1):
    ds = ogr.Open(path)
    lyr = ds.GetLayer(0)
    lyr.SetSpatialFilterRect(x0, y0, x1, y1)
    return sorted(f.GetField("dam_id") for f in lyr)


# merge appends points and lines, and the index covers the new rows
dams = gdal_gpkg("dams", ogr.wkbPoint, [("D1", "POINT (100 200)"), ("D2", "POINT (300 400)")])
ponds = gdal_gpkg("ponds", ogr.wkbPoint, [("P1", "POINT (500 600)")])
assert mg.append_gpkg(dams, ponds) == 1
assert rtree(dams, "dams")[-1] == (3, 500.0, 500.0, 600.0, 600.0)
assert found(dams, 499, 599, 501, 601) == ["P1"]
arrows = gdal_gpkg("arrows", ogr.wkbLineString, [("D1", "LINESTRING (0 0, 10 10)")])
more = gdal_gpkg("more", ogr.wkbLineString, [("P1", "LINESTRING (20 30, 40 -5)")])
assert mg.append_gpkg(arrows, more) == 1
assert rtree(arrows, "arrows")[-1] == (2, 20.0, 40.0, -5.0, 30.0)

# Review: a decision is stamped and a moved breach point moves in the index too
bpts = gdal_gpkg("bpts", ogr.wkbPoint, [("D1", "POINT (100 200)"), ("D2", "POINT (300 400)")])
ap = rv.SqliteApply()
assert ap.set_attributes(bpts, "D2", {"rev_decision": "exclude"}) == 1
assert ap.move_point(bpts, "D1", 111.0, 222.0) == 1
assert rtree(bpts, "bpts")[0] == (1, 111.0, 111.0, 222.0, 222.0)
assert found(bpts, 110, 221, 112, 223) == ["D1"] and found(bpts, 99, 199, 101, 201) == []
con = sqlite3.connect(bpts)
assert con.execute("SELECT rev_decision FROM bpts WHERE dam_id='D2'").fetchone()[0] == "exclude"
con.close()
print("ok")
