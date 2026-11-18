"""NOAA Atlas 14 shared core (noaa14/atlas14.py, temporal.py, storm.py): DamIT PMP's test_atlas14.py and
test_temporal_storm.py, ported to the DWR copy of the core. Recorded PFDS answers and NOAA's own HDSC temporal file
(fixtures_atlas14/), a fake downloader: no network, no QGIS. numpy needed; GDAL for the shapefile test.

Run:  python3 -W error DamSafety_PMP/tests/test_atlas14_core.py
"""
import json
import os
import re
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))          # the repository: DamSafety_PMP is a package

import numpy as np  # noqa: E402

from DamSafety_PMP.noaa14 import atlas14 as A  # noqa: E402
from DamSafety_PMP.noaa14 import storm as S  # noqa: E402
from DamSafety_PMP.noaa14 import temporal as T  # noqa: E402

try:
    from osgeo import ogr
    HAVE_OGR = True
except ImportError:                                            # pragma: no cover
    HAVE_OGR = False

FIX = os.path.join(HERE, "fixtures_atlas14")
CORE = os.path.join(os.path.dirname(HERE), "noaa14")


def fixture(name):
    with open(os.path.join(FIX, name), "rb") as fh:
        return fh.read()


FARGO = fixture("pfds_pds_english_fargo_nd.txt")
LINCOLN = fixture("pfds_ams_metric_lincoln_ne.txt")
PORTLAND = fixture("pfds_no_coverage_portland_or.txt")


def fargo_pf():
    return A.parse(FARGO)


class FakePFDS(object):
    def __init__(self, answer):
        self.answer, self.urls = answer, []

    def get_bytes(self, url, timeout=60):
        self.urls.append(url)
        return self.answer(url) if callable(self.answer) else self.answer


# ------------------------------------------------------------------------------------------- atlas14.py
class Parse(unittest.TestCase):
    def test_pds_english(self):
        pf = A.parse(FARGO)
        self.assertEqual((pf["volume"], pf["version"], pf["region"]), ("8", "2", "Midwestern States"))
        self.assertEqual(pf["frequencies"], [1, 2, 5, 10, 25, 50, 100, 200, 500, 1000])
        self.assertEqual(len(pf["quantiles"]), 19)
        self.assertEqual(pf["quantiles"][A.duration_index("24-hr")][A.freq_index(pf, 100)], 6.47)
        self.assertEqual(pf["upper"][0][0], 0.418)
        self.assertEqual(pf["unit_label"], "in")
        self.assertIn("NOAA Atlas 14 Volume 8 Version 2", A.citation(pf))
        self.assertEqual(len(A.table_rows(pf)), 190)

    def test_ams_metric(self):
        pf = A.parse(LINCOLN)
        self.assertEqual(pf["series"], "ams")
        self.assertEqual(pf["frequencies"], [2, 5, 10, 25, 50, 100, 200, 500, 1000])
        self.assertEqual(pf["unit_label"], "mm")
        with self.assertRaises(A.Atlas14Error):
            A.freq_index(pf, 1)                       # no 1-year AEP in AMS

    def test_no_coverage_fails_clearly(self):
        with self.assertRaises(A.CoverageError) as cm:
            A.parse(PORTLAND)
        self.assertIn("does not cover this location", str(cm.exception))

    def test_garbage(self):
        with self.assertRaises(A.Atlas14Error):
            A.parse(b"<html>503</html>")
        with self.assertRaises(A.Atlas14Error):
            A.parse(b"result = 'values';\nquantiles = [['1']];\n")


class Fetch(unittest.TestCase):
    def setUp(self):
        self.cache = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.cache)

    def test_url_and_cache(self):
        http = FakePFDS(FARGO)
        pf, src = A.fetch(46.8772, -96.7898, http, self.cache)
        self.assertTrue(http.urls[0].startswith("https://hdsc.nws.noaa.gov/cgi-bin/new/cgi_readH5.py?lat=46.8772"))
        self.assertIn("series=pds", http.urls[0])
        self.assertFalse(src["from_cache"])
        self.assertEqual(src["source"], "NOAA Atlas 14 (PFDS)")
        pf2, src2 = A.fetch(46.8772, -96.7898, http, self.cache)
        self.assertTrue(src2["from_cache"])
        self.assertEqual(len(http.urls), 1)
        self.assertEqual(pf2["quantiles"], pf["quantiles"])
        _, off = A.fetch(46.8772, -96.7898, None, self.cache, offline=True)
        self.assertTrue(off["from_cache"])
        with self.assertRaises(A.Atlas14Error):
            A.fetch(47.0, -97.0, None, self.cache, offline=True)

    def test_no_coverage_not_cached(self):
        http = FakePFDS(PORTLAND)
        with self.assertRaises(A.CoverageError):
            A.fetch(45.52, -122.68, http, self.cache)
        self.assertEqual(os.listdir(self.cache), [])

    def test_polite_many(self):
        waits = []
        http = FakePFDS(FARGO)
        A.fetch_many([(46.0, -97.0), (46.1, -97.0), (46.0, -97.0)], http, self.cache, pause_s=1.0, sleep=waits.append)
        self.assertEqual(len(http.urls), 2)          # third point from the cache
        self.assertEqual(waits, [1.0, 1.0])

    def test_bad_arguments(self):
        with self.assertRaises(A.Atlas14Error):
            A.query_url(95, 0)
        with self.assertRaises(A.Atlas14Error):
            A.query_url(45, -100, series="hms")


class Derived(unittest.TestCase):
    def test_interpolation_exact_and_between(self):
        pf = A.parse(FARGO)
        j = A.freq_index(pf, 100)
        self.assertEqual(A.depth_at(pf, 1440, 100), 6.47)
        d90 = A.depth_at(pf, 90, 100)
        self.assertTrue(pf["quantiles"][4][j] < d90 < pf["quantiles"][5][j])
        with self.assertRaises(A.Atlas14Error):
            A.depth_at(pf, 2, 100)
        with self.assertRaises(A.Atlas14Error):
            A.depth_at(pf, 100000, 100)

    def test_alternating_block(self):
        pf = A.parse(FARGO)
        rows = A.alternating_block(pf, 100, 1440, 60)
        self.assertEqual(len(rows), 24)
        self.assertAlmostEqual(rows[-1][2], 6.47, places=3)
        incs = [r[1] for r in rows]
        self.assertEqual(incs.index(max(incs)), 11)
        self.assertAlmostEqual(max(incs), A.depth_at(pf, 60, 100), places=3)
        # every centred 3-step window holds the 3-hour depth
        self.assertAlmostEqual(sum(incs[10:13]), A.depth_at(pf, 180, 100), places=3)
        with self.assertRaises(A.Atlas14Error):
            A.alternating_block(pf, 100, 1440, 7)

    def test_one_duration_list_everywhere(self):
        from DamSafety_PMP.noaa14 import gisgrid as G
        self.assertIs(G.DURATION_LABELS, A.DURATION_LABELS)                 # one constant, not copies
        self.assertEqual(len(A.DURATION_LABELS), 19)
        self.assertEqual(A.DURATION_LABELS[A.DEFAULT_DURATION_INDEX], "24-hr")
        self.assertEqual(A.DEFAULT_DURATION_INDEX, 9)
        self.assertEqual(A.DURATION_LABELS, [d for d, _ in A.DURATIONS])
        self.assertEqual((A.duration_minutes("5-min"), A.duration_minutes("24-hr"), A.duration_minutes("60-day")),
                         (5, 1440, 86400))

    def test_every_duration_builds_or_says_why(self):
        pf = A.parse(FARGO)
        for label, minutes in A.DURATIONS:
            with self.assertRaises(A.Atlas14Error) as cm:                   # a 90-min step never fits the short ones
                A.alternating_block(pf, 100, minutes, 90 if minutes % 90 else minutes * 2)
            self.assertTrue(any(w in str(cm.exception) for w in ("shorter than the time step", "whole number")), label)
            step = 5 if minutes < 60 else 60
            rows = A.alternating_block(pf, 100, minutes, step)
            self.assertEqual(len(rows), minutes // step, label)
            self.assertAlmostEqual(rows[-1][2], A.depth_at(pf, minutes, 100), places=3, msg=label)

    def test_check_storm_messages(self):
        with self.assertRaises(A.Atlas14Error) as cm:
            A.check_storm(5, 60, "5-min")
        self.assertIn("5-min (5 min) is shorter than the time step of 60 min", str(cm.exception))
        self.assertIn("5 min or less", str(cm.exception))
        with self.assertRaises(A.Atlas14Error) as cm:
            A.check_storm(1440, 7, "24-hr")
        self.assertIn("divides 1440 min", str(cm.exception))
        with self.assertRaises(A.Atlas14Error) as cm:
            A.check_storm(86400, 5, "60-day")                               # 17280 blocks is fine ...
            A.check_storm(86400 * 2, 5)                                     # ... 34560 is not
        self.assertIn("at most 20000", str(cm.exception))
        with self.assertRaises(A.Atlas14Error):
            A.check_storm(60, 2)

    def test_combine(self):
        a, b = A.parse(FARGO), A.parse(FARGO)
        b = dict(b, quantiles=[[v * 2 for v in row] for row in b["quantiles"]])
        m = A.combine([a, b], "mean")
        self.assertAlmostEqual(m["quantiles"][9][6], 6.47 * 1.5, places=3)
        self.assertEqual(A.combine([a, b], "max")["quantiles"][9][6], 12.94)
        with self.assertRaises(A.Atlas14Error):
            A.combine([a, A.parse(LINCOLN)])


# ------------------------------------------------------------------------------------------- temporal.py
class NrcsCurves(unittest.TestCase):
    def test_every_type_is_a_cumulative_0_to_1_24_hour_curve(self):
        for kind in ("I", "IA", "II", "III"):
            h, f = T.nrcs_curve(kind)
            self.assertEqual((h[0], h[-1], f[0], f[-1]), (0.0, 24.0, 0.0, 1.0), kind)
            self.assertTrue((np.diff(f) > 0).all(), kind)

    def test_type_ii_published_ordinates(self):
        h, f = T.nrcs_curve("II")
        at = dict(zip(h, f))
        self.assertEqual((at[11.5], at[12.0], at[12.5]), (0.283, 0.663, 0.735))        # TR-55 Exhibit 4-II
        self.assertAlmostEqual(at[12.0] - at[11.75], 0.306)
        self.assertAlmostEqual(at[12.0] - at[11.0], 0.428, places=3)                   # the steep hour

    def test_type_peaks_are_where_they_should_be(self):
        peak_hour = {}
        for kind in ("I", "IA", "II", "III"):
            h, f = T.nrcs_curve(kind)
            peak_hour[kind] = h[1:][np.argmax(np.diff(f) / np.diff(h))]
        self.assertEqual(peak_hour["II"], 12.0)
        self.assertEqual(peak_hour["III"], 12.0)
        self.assertLessEqual(peak_hour["I"], 11.0)
        self.assertLessEqual(peak_hour["IA"], 9.0)

    def test_record_says_shape_only_and_cites(self):
        rec = T.nrcs_record("II")
        self.assertIn("shape option only", rec["note"])
        self.assertIn("Technical Release 55", rec["citation"])


class Patterns(unittest.TestCase):
    def test_check_pattern_names_the_durations(self):
        T.check_pattern("alternating", 5)
        T.check_pattern("atlas14", 1440)
        T.check_pattern("atlas14", 360, "6-hr")
        T.check_pattern("nrcs2", 1440)
        with self.assertRaises(T.TemporalError) as cm:
            T.check_pattern("atlas14", 180, "3-hr")
        self.assertIn("6-, 12-, 24- and 96-hour", str(cm.exception))
        with self.assertRaises(T.TemporalError) as cm:
            T.check_pattern("nrcs3", 720, "12-hr")
        self.assertIn("Type III", str(cm.exception))
        with self.assertRaises(T.TemporalError):
            T.check_pattern("nope", 1440)

    def test_url_is_the_hdsc_pattern(self):
        self.assertEqual(T.temporal_url("mw", 2, 24), "https://hdsc.nws.noaa.gov/pub/hdsc/data/mw/mw_2_24h_temporal.csv")
        with self.assertRaises(T.TemporalError):
            T.temporal_url("se", 2, 24)                       # Volume 2: not wired, says so
        with self.assertRaises(T.TemporalError):
            T.temporal_url("mw", 2, 48)

    def test_parse_the_real_hdsc_file(self):
        p = T.parse_temporal(fixture("mw_2_24h_temporal.csv").decode("utf-8"))
        self.assertIn("Volume 8", p["title"])
        self.assertEqual(sorted(p["cases"]), ["all", "first", "fourth", "second", "third"])
        c = p["cases"]["second"]
        self.assertEqual(c["pcts"], [90, 80, 70, 60, 50, 40, 30, 20, 10])
        self.assertEqual((c["hours"][0], c["hours"][-1], len(c["hours"])), (0.0, 24.0, 49))
        h, f = T.curve(p, "second", 50)
        self.assertEqual((f[0], f[-1]), (0.0, 1.0))
        self.assertTrue((np.diff(f) >= 0).all())
        h1, f1 = T.curve(p, "first", 50)
        self.assertGreater(f1[8], f[8])                       # first quartile: more of the rain early
        with self.assertRaises(T.TemporalError):
            T.curve(p, "second", 55)
        self.assertEqual(T.curve(p, "all", 50)[1][-1], 1.0)
        del p["cases"]["third"]
        with self.assertRaises(T.TemporalError):
            T.curve(p, "third", 50)

    def test_junk_is_refused(self):
        with self.assertRaises(T.TemporalError):
            T.parse_temporal("hello\nworld\n")

    def test_rows_from_curve_sums_to_the_atlas_depth(self):
        h, f = T.nrcs_curve("II")
        rows = T.rows_from_curve(h, f, 6.34, 1440, 30)
        self.assertEqual(len(rows), 48)
        self.assertEqual(rows[-1][0], 1440)
        self.assertEqual(rows[-1][2], 6.34)                   # exactly the storm total, not 6.3401
        self.assertAlmostEqual(sum(r[1] for r in rows), 6.34, places=3)
        peak = max(rows, key=lambda r: r[1])
        self.assertEqual(peak[0], 720)                        # the half hour ending at hour 12: 0.283 -> 0.663 of Type II
        with self.assertRaises(T.TemporalError):
            T.rows_from_curve(h, f, 6.34, 720, 30)            # a 24 h curve for a 12 h storm


class FakeDownload:
    """The downloader hook: writes the real HDSC file (any area) or a zip holding a tiny shapefile."""
    def __init__(self, shp_zip=None):
        self.urls = []
        self.shp_zip = shp_zip

    def __call__(self, url, dest):
        self.urls.append(url)
        if url.endswith(".csv"):
            shutil.copyfile(os.path.join(FIX, "mw_2_24h_temporal.csv"), dest)
        elif url.endswith(".zip") and self.shp_zip:
            shutil.copyfile(self.shp_zip, dest)
        else:
            raise IOError("unexpected URL " + url)


NAD83_PRJ = ('GEOGCS["GCS_North_American_1983",DATUM["D_North_American_1983",SPHEROID["GRS_1980",6378137.0,'
             '298.257222101]],PRIMEM["Greenwich",0.0],UNIT["Degree",0.0174532925199433]]')


def make_area_zip(path, boxes):
    """A zip with Vol_8_Temporal_Regions.shp: one polygon per (region, (x0, y0, x1, y1)). The .prj is written as text
    (no osr call: under -W error GDAL >= 3.7 warns about an osr call made before any exception mode was chosen, and
    the test must not choose one for the code it checks)."""
    import zipfile
    d = tempfile.mkdtemp()
    ds = ogr.GetDriverByName("ESRI Shapefile").CreateDataSource(d)
    lyr = ds.CreateLayer("Vol_8_Temporal_Regions", None, ogr.wkbPolygon)
    lyr.CreateField(ogr.FieldDefn("Region", ogr.OFTInteger))
    for region, (x0, y0, x1, y1) in boxes:
        f = ogr.Feature(lyr.GetLayerDefn())
        f.SetField("Region", region)
        f.SetGeometry(ogr.CreateGeometryFromWkt("POLYGON(({0} {1},{2} {1},{2} {3},{0} {3},{0} {1}))".format(x0, y0, x1, y1)))
        lyr.CreateFeature(f)
    ds = None
    with open(os.path.join(d, "Vol_8_Temporal_Regions.prj"), "w") as fh:
        fh.write(NAD83_PRJ)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:     # as NOAA ships them
        for fn in os.listdir(d):
            z.write(os.path.join(d, fn), fn)
    shutil.rmtree(d)


class Atlas14Files(unittest.TestCase):
    def setUp(self):
        self.cache = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.cache, ignore_errors=True)

    def test_fetch_caches_and_records_a_hash(self):
        dl = FakeDownload()
        url = T.temporal_url("mw", 2, 24)
        p, rec = T.fetch_file(url, self.cache, dl)
        self.assertFalse(rec["from_cache"])
        self.assertEqual(len(rec["sha256"]), 64)
        p2, rec2 = T.fetch_file(url, self.cache, dl)
        self.assertTrue(rec2["from_cache"])
        self.assertEqual(rec2["sha256"], rec["sha256"])
        self.assertEqual(len(dl.urls), 1)
        with self.assertRaises(T.TemporalError):
            T.fetch_file(T.temporal_url("mw", 3, 24), self.cache, dl, offline=True)

    def test_atlas14_curve_with_the_area_given(self):
        h, f, rec = T.atlas14_curve(-96.9, 46.9, "mw", 24, "second", 50, self.cache, FakeDownload(), area=2)
        self.assertEqual((h[-1], f[-1]), (24.0, 1.0))
        self.assertEqual((rec["area"], rec["quartile"], rec["percent_of_occurrence"], rec["duration_hr"]), (2, "second", 50, 24))
        self.assertEqual(rec["area_from"], "user choice")
        self.assertIn("Shape only", rec["note"])
        self.assertIn("hdsc.nws.noaa.gov", rec["file"]["url"])
        self.assertIn("Atlas 14 Volume 8", rec["citation"])
        json.dumps(rec)                                        # goes into the JSON as it is

    @unittest.skipUnless(HAVE_OGR, "GDAL not installed")
    def test_area_is_found_from_the_shapefile(self):
        z = os.path.join(self.cache, "shp.zip")
        make_area_zip(z, [(1, (-98, 45, -97.5, 48)), (2, (-97.5, 45, -96, 48)), (3, (-96, 45, -94, 48))])
        dl = FakeDownload(shp_zip=z)
        h, f, rec = T.atlas14_curve(-96.9, 46.9, "mw", 24, "second", 50, self.cache, dl)
        self.assertEqual(rec["area"], 2)
        self.assertEqual(rec["area_from"], "HDSC shapefile point-in-polygon")
        self.assertTrue(any(u.endswith("mw_2_24h_temporal.csv") for u in dl.urls))
        self.assertTrue(any("Vol8_Temporals_Shapefiles.zip" in u for u in dl.urls))
        with self.assertRaises(T.TemporalError) as cm:
            T.atlas14_curve(-120.0, 46.9, "mw", 24, "second", 50, self.cache, dl)
        self.assertIn("outside", str(cm.exception))

    def test_unwired_volume_says_so(self):
        with self.assertRaises(T.TemporalError) as cm:
            T.atlas14_curve(-90.0, 30.0, "se", 24, "second", 50, self.cache, FakeDownload(), area=1)
        self.assertIn("Volume 8", str(cm.exception))


# ------------------------------------------------------------------------------------------- storm.py
class Losses(unittest.TestCase):
    """storm.py's losses are part of the shared core (DamIT's hyetograph / raingrid use them). The DWR
    atlas14_hyetograph writes GROSS rain and never calls them: ndpmp:inflow_hydrograph applies the losses."""
    ROWS = [(30, 0.5, 0.5), (60, 1.0, 1.5), (90, 2.0, 3.5), (120, 0.5, 4.0)]

    def test_zero_losses_change_nothing(self):
        t = S.apply_losses(self.ROWS)
        self.assertEqual([r[3] for r in t], [r[1] for r in self.ROWS])
        self.assertEqual([r[2] for r in t], [0.0] * 4)

    def test_initial_loss_then_constant_rate(self):
        t = S.apply_losses(self.ROWS, initial=0.8, constant_rate=0.4)      # in / hr: 0.2 in per 30 min step
        self.assertEqual([r[2] for r in t], [0.5, 0.5, 0.2, 0.2])
        self.assertEqual([r[3] for r in t], [0.0, 0.5, 1.8, 0.3])
        s = S.loss_summary(t, "in")
        self.assertEqual((s["gross_in"], s["loss_in"], s["excess_in"]), (4.0, 1.4, 2.6))
        self.assertAlmostEqual(s["runoff_fraction"], 0.65)

    def test_constant_loss_never_exceeds_the_rain(self):
        t = S.apply_losses(self.ROWS, initial=0.0, constant_rate=10.0)
        self.assertEqual([r[3] for r in t], [0.0, 0.0, 0.0, 0.0])
        self.assertEqual(sum(r[2] for r in t), 4.0)

    def test_loss_never_goes_negative_or_nonsense(self):
        with self.assertRaises(S.LossError):
            S.apply_losses(self.ROWS, initial=-1)
        with self.assertRaises(S.LossError):
            S.apply_losses(self.ROWS, constant_rate=-0.1)

    def test_units(self):
        self.assertEqual(S.to_unit(1.0, "in", "mm"), 25.4)
        self.assertAlmostEqual(S.to_unit(25.4, "mm", "in"), 1.0)
        self.assertEqual(S.to_unit(3.0, "in", "in"), 3.0)


class Build(unittest.TestCase):
    def test_alternating_is_the_existing_function(self):
        pf = fargo_pf()
        self.assertEqual(S.build(pf, 100, 1440, 60), A.alternating_block(pf, 100, 1440, 60))

    def test_every_pattern_has_the_atlas_14_depth(self):
        pf = fargo_pf()
        total = A.depth_at(pf, 1440, 100)
        h, f = T.nrcs_curve("II")
        for pat in ("alternating", "nrcs2"):
            rows = S.build(pf, 100, 1440, 60, pat, "24-hr", (h, f) if pat != "alternating" else None)
            self.assertAlmostEqual(rows[-1][2], total, places=3, msg=pat)
        with self.assertRaises(T.TemporalError):
            S.build(pf, 100, 1440, 60, "atlas14", "24-hr", None)             # a curve is required
        with self.assertRaises(T.TemporalError):
            S.build(pf, 100, 720, 60, "nrcs2", "12-hr", (h, f))              # NRCS is 24 h only

    def test_pattern_changes_the_shape_not_the_depth(self):
        pf = fargo_pf()
        h, f = T.nrcs_curve("II")
        a = S.build(pf, 100, 1440, 30)
        n = S.build(pf, 100, 1440, 30, "nrcs2", "24-hr", (h, f))
        self.assertAlmostEqual(a[-1][2], n[-1][2], places=3)
        self.assertNotEqual([r[1] for r in a], [r[1] for r in n])


# ------------------------------------------------------------------------------------------- sources
class OnlyNoaa(unittest.TestCase):
    def test_the_temporal_module_names_no_other_depth_source(self):
        with open(os.path.join(CORE, "temporal.py"), encoding="utf-8") as fh:
            src = fh.read()
        for h in set(re.findall(r"https?://([\w.\-]+)", src)):
            self.assertIn(h, ("hdsc.nws.noaa.gov", "www.nrcs.usda.gov", "directives.sc.egov.usda.gov", "www.nws.noaa.gov"), h)

    def test_the_atlas14_tools_reach_only_noaa(self):
        """Every byte the Atlas 14 tools fetch comes from hdsc.nws.noaa.gov (the shared core, the DWR wrappers)."""
        plugin = os.path.dirname(HERE)
        files = [os.path.join(CORE, f) for f in os.listdir(CORE) if f.endswith(".py")]
        files += [os.path.join(plugin, f) for f in ("algorithm_atlas14.py", "atlas14_outputs.py", "noaa14_http.py")]
        hosts = set()
        for p in files:
            with open(p, encoding="utf-8") as fh:
                hosts |= set(re.findall(r"https?://([^/\s\"'<>]+)", fh.read()))
        self.assertEqual(hosts, {"hdsc.nws.noaa.gov"})

    def test_no_awa_material_in_the_shared_core(self):
        """The shared five go to DamIT too, which ships none of the AWA-derived tool: keep them free of it."""
        bad = re.compile(r"(\bAWA\b|Applied Weather|\bHMR\b|\.gdb\b|pmp_evaluation|ND_PMP|\bSWC\b)", re.I)
        for f in ("atlas14.py", "temporal.py", "storm.py", "gisgrid.py", "gridstats.py"):
            with open(os.path.join(CORE, f), encoding="utf-8") as fh:
                self.assertIsNone(bad.search(fh.read()), f)


if __name__ == "__main__":
    unittest.main()
