"""3.17.7: the model terrain saved for review (core/model_terrain.py).

LISFLOOD-FP's grid is written per pass as a georeferenced GeoTIFF (the .dem.asc it runs on
sits at a local (0, 0) origin with no projection); each gmsh mesh as a GeoPackage of triangles
with their bed elevation; publish() copies the newest of each next to the results."""
import os
import tempfile
import time

import numpy as np

from nd_breach_runner.core import model_terrain as mt
from nd_breach_runner.core.grid import Grid
from nd_breach_runner.tests.registry import test, need_osgeo

UTM14 = "EPSG:26914"


def _dem():
    # 4 x 3 cells of 10 m, top-left at a real UTM position; elevation = 100 + column
    data = np.tile(np.arange(4, dtype=float) + 100.0, (3, 1))
    data[2, 3] = -9999.0
    return Grid(data, 500000.0, 5200030.0, 10.0, -9999.0, UTM14)


def _mesh():
    pts = np.array([[500005.0, 5200025.0], [500035.0, 5200025.0], [500005.0, 5200005.0],
                    [500035.0, 5200005.0]])
    return {"points": pts, "triangles": np.array([[0, 1, 2], [1, 3, 2]]), "stats": {}}


@test
def model_terrain_mesh_layers_bed_and_area():
    spec = {"outer": [[500000.0, 5200000.0], [500040.0, 5200000.0], [500040.0, 5200030.0]],
            "lines": [[[500000.0, 5200015.0], [500040.0, 5200015.0]]], "channel": [],
            "size_lines": [{"line": [[500010.0, 5200000.0], [500010.0, 5200030.0]]}]}
    layers = mt.mesh_layers(_mesh(), _dem(), spec)
    kind, tris = layers["mesh_triangles"]
    assert kind == "polygon" and len(tris) == 2
    p0 = tris[0][1]
    assert p0["area_m2"] == 300.0, p0
    # centroid x = 500015 -> 1.0 cells right of the first centre -> 101.0 m
    assert abs(p0["bed_m"] - 101.0) < 1e-6, p0
    assert abs(p0["bed_ft"] - 101.0 / 0.3048) < 0.01, p0
    ring = tris[0][0][0][0]
    assert ring[0] == ring[-1] and ring[0] == (500005.0, 5200025.0)   # real coordinates
    kinds = sorted(p["kind"] for _, p in layers["mesh_lines"][1])
    assert kinds == ["lines", "outer", "size_line"], kinds


@test
def model_terrain_off_by_env():
    old = os.environ.get(mt.ENV)
    try:
        os.environ[mt.ENV] = "0"
        assert not mt.enabled()
        del os.environ[mt.ENV]
        assert mt.enabled()

        class S:
            export_model_terrain = False
        assert not mt.enabled(S())
    finally:
        if old is not None:
            os.environ[mt.ENV] = old


@test
def model_terrain_publish_copies_newest():
    d = tempfile.mkdtemp()
    model = os.path.join(d, "_breach_model")
    for sub, age in (("lisflood", 10), ("lisflood_refine_1", 0), ("anuga", 0)):
        os.makedirs(os.path.join(model, sub))
    for sub, name, body, age in (("lisflood", mt.TERRAIN_NAME, b"old", 100),
                                 ("lisflood_refine_1", mt.TERRAIN_NAME, b"new", 0),
                                 ("anuga", mt.MESH_NAME, b"mesh", 0)):
        p = os.path.join(model, sub, name)
        with open(p, "wb") as f:
            f.write(body)
        t = time.time() - age
        os.utime(p, (t, t))
    got = mt.publish(model, d)
    assert sorted(os.path.basename(g) for g in got) == sorted([mt.PUBLISHED_TERRAIN,
                                                                mt.PUBLISHED_MESH])
    with open(os.path.join(d, mt.PUBLISHED_TERRAIN), "rb") as f:
        assert f.read() == b"new"
    # nothing to publish -> nothing copied, no error
    assert mt.publish(os.path.join(d, "none"), d) == []


@test
def model_terrain_geotiff_and_gpkg_are_georeferenced():
    need_osgeo()
    from osgeo import gdal, ogr
    d = tempfile.mkdtemp()
    dem = _dem()
    p = mt.write_lisflood_terrain(dem, d)
    assert p and os.path.isfile(p)
    ds = gdal.Open(p)
    gt = ds.GetGeoTransform()
    assert gt[0] == 500000.0 and gt[3] == 5200030.0 and gt[1] == 10.0, gt
    assert "26914" in ds.GetSpatialRef().GetAuthorityCode(None)
    b = ds.GetRasterBand(1)
    a = b.ReadAsArray()
    assert a[0, 2] == 102.0 and a[2, 3] == b.GetNoDataValue(), a
    ds = None
    g = mt.write_gmsh_mesh(_mesh(), dem, {"outer": [[500000.0, 5200000.0], [500040.0, 5200000.0],
                                                    [500040.0, 5200030.0]]}, d)
    assert g and os.path.isfile(g)
    v = ogr.Open(g)
    lyr = v.GetLayerByName("mesh_triangles")
    assert lyr.GetFeatureCount() == 2
    f = lyr.GetNextFeature()
    assert abs(f.GetField("bed_m") - 101.0) < 1e-6
    assert v.GetLayerByName("mesh_lines").GetFeatureCount() == 1
