"""BoannGrid conditioning in one call (core/metalterrain/condition.py, Runner 3.58.0): 4-way from
one fill, and the keep-out mask built from building footprints / address points / town limits
(any GDAL vector, any CRS), found by name in a dam folder.
"""
import json
import os
import shutil
import tempfile

import numpy as np

from nd_breach_runner.core.metalterrain import audit as A
from nd_breach_runner.core.metalterrain import burn as B
from nd_breach_runner.core.metalterrain import cases
from nd_breach_runner.core.metalterrain import condition as C
from nd_breach_runner.core.metalterrain import delta_log as dl
from nd_breach_runner.core.metalterrain import fill as F
from nd_breach_runner.core.metalflood import backends as mfb
from nd_breach_runner.tests.registry import Skip, test

GT = (500000.0, 10.0, 0.0, 5200000.0, 0.0, -10.0)      # UTM 14N, 10 m cells


def _need_cpu_build():
    if os.name == "nt" or not mfb.find_compiler():
        raise Skip("no C++ compiler for the BoannGrid CPU build")


def _need_gdal():
    try:
        from osgeo import gdal, ogr, osr  # noqa: F401
    except ImportError:
        raise Skip("GDAL Python bindings not installed")


def _wkt():
    from osgeo import osr
    s = osr.SpatialReference()
    s.ImportFromEPSG(26914)
    return s.ExportToWkt()


def _geojson(path, geoms, epsg=None):
    fc = {"type": "FeatureCollection",
          "features": [{"type": "Feature", "properties": {}, "geometry": g} for g in geoms]}
    if epsg:
        fc["crs"] = {"type": "name", "properties": {"name": "urn:ogc:def:crs:EPSG::{}".format(epsg)}}
    with open(path, "w") as fh:
        json.dump(fc, fh)
    return path


def _xy(r, c):
    """Cell centre (row, col) -> UTM x, y on GT."""
    return GT[0] + (c + 0.5) * GT[1], GT[3] + (r + 0.5) * GT[5]


def _carve_cells():
    z = cases.channel_with_barrier(ny=50, nx=120, chan_depth=3.0, sill=1.0)
    out, recs, info = C.condition(z, cell_m=10.0, backend="cpu")
    return z, out, recs, info


@test
def condition_is_four_way_from_one_fill():
    """condition() fills once, 4-way, and flow needs no second fill (0 dead ends, no refill); it
    carves the blocked crossing exactly as reconnect_crossings does on the same 4-way inputs."""
    _need_cpu_build()
    calls = []
    real = F.fill_dem

    def counting(*a, **k):
        calls.append(k.get("conn", 8))
        return real(*a, **k)

    F.fill_dem = counting
    try:
        z, out, recs, info = _carve_cells()
    finally:
        F.fill_dem = real
    assert calls == [4], calls
    assert info["flow_dead_ends"] == 0 and not info["flow_refilled_d4"], info
    assert info["carved"] == 1 and info["cells_lowered"] > 0, info
    assert 1.0 < info["max_cut_m"] < B.DEFAULT_MAX_CUT_M, info
    ref, rrec = B.reconnect_crossings(z, min_accum_cells=20, cell_m=10.0, backend="cpu")
    assert np.array_equal(ref, out) and rrec == recs


@test
def census_four_way_finds_corner_draining_depressions():
    """A 4-way fill holds every cell that only drains across a corner, so the 4-way census holds
    at least the 8-way volume, and more on a noisy plain."""
    _need_cpu_build()
    rng = np.random.RandomState(5)
    yy, xx = np.mgrid[0:40, 0:60].astype(np.float64)
    z = 300.0 + 0.05 * xx + 0.02 * yy + 0.3 * rng.standard_normal(xx.shape)
    _l8, i8 = A.depression_census(z, cell_m=10.0, backend="cpu")
    _l4, i4 = A.depression_census(z, cell_m=10.0, backend="cpu", conn=4)
    v8 = sum(i["volume_m3"] for i in i8)
    v4 = sum(i["volume_m3"] for i in i4)
    assert v4 > v8 > 0, (v4, v8)


@test
def keepout_mask_burns_points_and_polygons_in_any_crs():
    """An address point in lon/lat grows to a lot of about 25 m around it; a footprint polygon in
    the grid's own CRS is burned where it lies; a feature far off the grid adds nothing."""
    _need_gdal()
    from osgeo import osr
    d = tempfile.mkdtemp()
    try:
        utm = osr.SpatialReference()
        utm.ImportFromEPSG(26914)
        utm.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        ll = osr.SpatialReference()
        ll.ImportFromEPSG(4326)
        ll.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        to_ll = osr.CoordinateTransformation(utm, ll)
        x, y = _xy(20, 30)
        lon, lat, _ = to_ll.TransformPoint(x, y)
        far_lon, far_lat, _ = to_ll.TransformPoint(x + 50000.0, y)
        pts = _geojson(os.path.join(d, "e911_points.geojson"),
                       [{"type": "Point", "coordinates": [lon, lat]},
                        {"type": "Point", "coordinates": [far_lon, far_lat]}])
        x0, y0 = _xy(5, 5)
        x1, y1 = _xy(8, 9)
        poly = _geojson(os.path.join(d, "buildings.geojson"), [{
            "type": "Polygon", "coordinates": [[[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]]}],
            epsg=26914)
        m, n, per = C.keepout_mask((50, 60), GT, _wkt(), [pts], buffer_m=25.0)
        assert n == 1 and per[pts] == 1, (n, per)                # the far point is dropped
        rows, cols = np.nonzero(m)
        assert m[20, 30] and 16 <= m.sum() <= 49, int(m.sum())
        assert rows.min() >= 17 and rows.max() <= 23 and cols.min() >= 27 and cols.max() <= 33
        m0, _n, _p = C.keepout_mask((50, 60), GT, _wkt(), [pts], buffer_m=0.0)
        assert m0.sum() <= 1, int(m0.sum())                      # a bare point: one cell at most
        mp, _n, _p = C.keepout_mask((50, 60), GT, _wkt(), [poly], buffer_m=0.0)
        assert mp[5:9, 5:10].all() and not mp[20, 30], int(mp.sum())
    finally:
        shutil.rmtree(d, True)


@test
def find_keepout_layers_by_name_in_a_dam_folder():
    d = tempfile.mkdtemp()
    try:
        dl_ = os.path.join(d, "outputs", "_downloads", "ms")
        os.makedirs(dl_)
        os.makedirs(os.path.join(d, "inputs"))
        want = [os.path.join(dl_, "MS_Buildings_quadkey_0230.geojson"),
                os.path.join(d, "inputs", "E911_address_points.gpkg"),
                os.path.join(d, "City_Limits.shp")]
        skip = [os.path.join(d, "inputs", "flowlines.geojson"),
                os.path.join(d, "inputs", "buildings_notes.txt"),
                os.path.join(dl_, "rebuildingsite.geojson")]
        for p in want + skip:
            open(p, "w").close()
        got = C.find_keepout_layers(d)
        assert got == sorted(os.path.abspath(p) for p in want), got
        assert C.find_keepout_layers(os.path.join(d, "missing")) == []
    finally:
        shutil.rmtree(d, True)


def _write_dem(path, z):
    C._write_geotiff(path, z, GT, _wkt())


@test
def condition_file_keeps_carves_out_of_house_lots():
    """End to end on GeoTIFFs: with no keep-out the crossing is carved; with a footprint over the
    notch the carve is refused whole, the output equals the input, and the refusal is FLAGGED in
    the delta-log written beside it. A dam folder's footprint file is found by name."""
    _need_cpu_build()
    _need_gdal()
    d = tempfile.mkdtemp()
    try:
        z = cases.channel_with_barrier(ny=50, nx=120, chan_depth=3.0, sill=1.0)
        src = os.path.join(d, "dem.tif")
        _write_dem(src, z)
        z32 = z.astype(np.float32).astype(np.float64)
        out1 = os.path.join(d, "open.tif")
        info1 = C.condition_file(src, out1, backend="cpu")
        assert info1["carved"] == 1 and os.path.exists(out1 + ".json")
        a1, _g, _w = C._read_raster(out1)
        low = (z32 - a1) > 1e-3
        r, c = np.argwhere(low)[0]
        folder = os.path.join(d, "dam", "outputs", "_downloads")
        os.makedirs(folder)
        x, y = _xy(r, c)
        _geojson(os.path.join(folder, "ms_buildings.geojson"), [{
            "type": "Polygon", "coordinates": [[[x - 3, y - 3], [x + 3, y - 3], [x + 3, y + 3],
                                                [x - 3, y + 3], [x - 3, y - 3]]]}], epsg=26914)
        out2 = os.path.join(d, "kept.tif")
        logs = []
        info2 = C.condition_file(src, out2, dam_folder=os.path.join(d, "dam"), backend="cpu",
                                 log=logs.append)
        assert info2["carved"] == 0 and info2["refused_keepout"] == 1, info2
        assert len(info2["keepout_layers"]) == 1 and info2["keepout_cells"] > 0, info2
        a2, _g, _w = C._read_raster(out2)
        assert np.allclose(a2, z32, atol=1e-6), "a refused carve changed the ground"
        with dl.DeltaLog(run_id="boanngrid:dem.tif", path=out2 + ".deltalog.sqlite") as log:
            assert [f[2] for f in log.flagged()] == [dl.REFUSE_LIMIT_ACTION], log.flagged()
        assert any("keep-out layers found" in m for m in logs), logs
        # the command line runs the same thing
        from nd_breach_runner.core.metalterrain import __main__ as M
        out3 = os.path.join(d, "cli.tif")
        assert M.main(["x", "condition", src, out3, "--backend", "cpu"]) == 0
        a3, _g, _w = C._read_raster(out3)
        assert np.array_equal(a3, a1)
    finally:
        shutil.rmtree(d, True)


@test
def condition_file_respects_the_freeze_export_gate():
    """A carve through a frozen embankment is refused, and the conditioned DEM is not written
    while that refusal is unresolved - unless --acknowledge records why."""
    _need_cpu_build()
    _need_gdal()
    d = tempfile.mkdtemp()
    try:
        z = cases.channel_with_barrier(ny=50, nx=120, chan_depth=3.0, sill=1.0)
        src = os.path.join(d, "dem.tif")
        _write_dem(src, z)
        raised = z - np.minimum(np.roll(z, 1, axis=1), np.roll(z, -1, axis=1))
        fz = os.path.join(d, "freeze.tif")
        _write_dem(fz, (raised > 1.0).astype(np.float64))
        out = os.path.join(d, "out.tif")
        try:
            C.condition_file(src, out, freeze_path=fz, backend="cpu")
        except dl.ExportRefused:
            pass
        else:
            raise AssertionError("exported with an unresolved freeze refusal")
        assert not os.path.exists(out)
        info = C.condition_file(src, out, freeze_path=fz, backend="cpu",
                                acknowledge="test: the embankment has a known culvert")
        assert info["refused_freeze"] == 1 and os.path.exists(out), info
    finally:
        shutil.rmtree(d, True)
