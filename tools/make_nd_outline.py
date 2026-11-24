#!/usr/bin/env python3
"""Write DamSafety_Screening/core/nd_outline_5070.json: the North Dakota outline tool 6's
corridor check uses for its state-line rule (core/nd_counties.state_outline_5070).

    python3 tools/make_nd_outline.py <pack>/boundaries/ND_counties.gpkg [--out <json>]

The union of the pack's county outlines (NDGISHub), Buffer(0) to close slivers between
counties, to EPSG:5070, SimplifyPreserveTopology(50 m): one ring of about 2,000 vertices,
46 KB, coordinates to 0.1 m. Needs GDAL (python3 -c "from osgeo import ogr").
"""
import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "DamSafety_Screening", "core", "nd_outline_5070.json")


def outline(counties_gpkg, simplify_m=50.0):
    """[[x, y], ...] rings (exteriors of the union, largest first) in EPSG:5070."""
    from osgeo import ogr, osr
    ogr.UseExceptions()
    ds = ogr.Open(counties_gpkg)
    if ds is None:
        raise SystemExit("cannot open " + counties_gpkg)
    lyr = ds.GetLayer(0)
    union = None
    for f in lyr:
        g = f.GetGeometryRef()
        if g is not None:
            union = g.Clone() if union is None else union.Union(g)
    if union is None:
        raise SystemExit("no county outlines in " + counties_gpkg)
    union = union.Buffer(0)
    alb = osr.SpatialReference()
    alb.ImportFromEPSG(5070)
    alb.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    src = lyr.GetSpatialRef().Clone()
    src.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    union.AssignSpatialReference(src)
    union.TransformTo(alb)
    union = union.SimplifyPreserveTopology(simplify_m)
    polys = [union.GetGeometryRef(i) for i in range(union.GetGeometryCount())] \
        if union.GetGeometryName() == "MULTIPOLYGON" else [union]
    rings = [[[round(x, 1), round(y, 1)] for x, y, *_z in p.GetGeometryRef(0).GetPoints()] for p in polys]
    return sorted(rings, key=len, reverse=True)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("counties", help="the pack's boundaries/ND_counties.gpkg")
    ap.add_argument("--out", default=OUT)
    a = ap.parse_args(argv)
    rings = outline(a.counties)
    with open(a.out, "w", encoding="utf-8") as fh:
        json.dump({"crs": "EPSG:5070", "source": "ND_counties (NDGISHub) union, simplified 50 m",
                   "rings": rings}, fh, separators=(", ", ": "))
    print("{}: {} ring(s), {} vertices".format(a.out, len(rings), sum(len(r) for r in rings)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
