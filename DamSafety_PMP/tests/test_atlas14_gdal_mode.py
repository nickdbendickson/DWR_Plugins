"""audit 2026-11-20: the shared NOAA Atlas 14 core (noaa14/) never changes GDAL's process-wide exception mode.

gdal / ogr.UseExceptions() is process-wide in the GDAL bindings QGIS ships. noaa14/gridstats.py called it at
import, and gisgrid.read_clip / write_geotiff and temporal.locate_area inside the function, never switching back,
so an Atlas 14 tool changed GDAL for QGIS and every other plugin. They now raise inside atlas14.gdal_raising()
only. Each check runs in a fresh Python, in both starting modes, and asserts gdal / ogr.GetUseExceptions() is the
same after the import and after the calls (success and failure). GDAL + numpy needed (skipped without); no QGIS.

Run:  python3 DamSafety_PMP/tests/test_atlas14_gdal_mode.py
"""
import os
import re
import subprocess
import sys
import textwrap
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
CORE = os.path.join(os.path.dirname(HERE), "noaa14")

try:
    import numpy  # noqa: F401
    from osgeo import gdal, ogr  # noqa: F401
    HAVE_GDAL = True
except ImportError:                                            # pragma: no cover
    HAVE_GDAL = False

CHILD = textwrap.dedent("""
    import os, sys, tempfile, warnings
    warnings.simplefilter("ignore")
    sys.path.insert(0, {root!r})
    import numpy as np
    from osgeo import gdal, ogr, osr
    START = {start}
    for m in (gdal, ogr):
        m.UseExceptions() if START else m.DontUseExceptions()
    def same(where):
        got = (bool(gdal.GetUseExceptions()), bool(ogr.GetUseExceptions()))
        if got != (START, START):
            print("MODE CHANGED", where, got)
            sys.exit(3)
    def fails(fn, *a):
        try:
            fn(*a)
        except Exception:
            return True
        return False
    TMP = tempfile.mkdtemp()
    from DamSafety_PMP.noaa14 import gridstats as GS, gisgrid as G, temporal as T
    same("import")
    p = os.path.join(TMP, "mw100yr24ha.tif")
    G.write_geotiff(p, np.ones((4, 4), dtype="float32"), (-97.0, 0.01, 0.0, 47.0, 0.0, -0.01))
    same("write_geotiff")
    wgs = gdal.Open(p).GetProjection()
    assert GS.point_value(p, -96.985, 46.985, wgs, 1.0) == 1.0
    assert GS.basin_stats(p, "POLYGON((-96.999 46.961,-96.961 46.961,-96.961 46.999,-96.999 46.999,-96.999 46.961))",
                          wgs, 1.0)["cells"] > 0
    same("gridstats")
    assert fails(GS.point_value, os.path.join(TMP, "missing.tif"), 0, 0, wgs)
    same("failed point_value")
    d = os.path.join(TMP, "shp")
    s = osr.SpatialReference(); s.ImportFromEPSG(4269)
    ds = ogr.GetDriverByName("ESRI Shapefile").CreateDataSource(d)
    lyr = ds.CreateLayer("areas", s, ogr.wkbPolygon)
    lyr.CreateField(ogr.FieldDefn("Region", ogr.OFTInteger))
    f = ogr.Feature(lyr.GetLayerDefn()); f.SetField("Region", 2)
    f.SetGeometry(ogr.CreateGeometryFromWkt("POLYGON((-98 46,-96 46,-96 48,-98 48,-98 46))"))
    lyr.CreateFeature(f); f = lyr = ds = None
    same("test shapefile")
    assert T.locate_area(-97.0, 47.0, os.path.join(d, "areas.shp")) == 2
    assert fails(T.locate_area, -97.0, 47.0, os.path.join(TMP, "missing.shp"))
    same("locate_area")
    print("OK")
""")


@unittest.skipUnless(HAVE_GDAL, "GDAL + numpy are not installed")
class GdalModeUnchanged(unittest.TestCase):
    def test_import_and_calls_keep_the_mode(self):
        for start in (False, True):
            with self.subTest(start=start):
                p = subprocess.run([sys.executable, "-c", CHILD.format(root=ROOT, start=start)],
                                   capture_output=True, text=True, timeout=240)
                self.assertEqual(p.returncode, 0, "start={}:\n{}\n{}".format(start, p.stdout[-2000:], p.stderr[-4000:]))
                self.assertIn("OK", p.stdout)


class NoSwitchLeft(unittest.TestCase):
    def test_no_module_switches_gdal(self):
        """No gdal / ogr / osr.UseExceptions() call is left in the shared core (atlas14.gdal_raising switches
        and restores through a module variable)."""
        bad = []
        for f in sorted(os.listdir(CORE)):
            if f.endswith(".py"):
                with open(os.path.join(CORE, f), encoding="utf-8") as fh:
                    for i, ln in enumerate(fh, 1):
                        if re.search(r"\b(gdal|ogr|osr)\.UseExceptions\(\)", ln.split("#")[0]):
                            bad.append("{}:{}".format(f, i))
        self.assertEqual(bad, [])


if __name__ == "__main__":
    unittest.main()
