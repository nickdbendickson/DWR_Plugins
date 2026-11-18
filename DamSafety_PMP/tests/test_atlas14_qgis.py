"""The five NOAA Atlas 14 Processing algorithms (algorithm_atlas14.py) run in real QGIS, offline: a fake PFDS client
and a fake downloader serve the recorded answers (fixtures_atlas14/) and NOAA-shaped grid zips made here.

  atlas14_point -> atlas14_hyetograph (JSON route and POINT route) -> ndpmp:inflow_hydrograph -> the Runner's
  read_inflow_csv; atlas14_basin; atlas14_fetchgrid -> atlas14_gridbasin (file and fetch routes). Also: DamIT's
  parameter names and choice lists, the unit-declaring labels, the cache setting, the fetchgrid sidecar when
  OUTPUT_JSON is set, and that the GDAL exception mode is left as it was (audit R3): by each run, by importing the
  tools, and by a fresh QGIS process loading the provider.

Exits 77 (SKIP in run_tests.py) without QGIS. Never touches the real QGIS profile (HOME is a temporary folder).
Run:  QT_QPA_PLATFORM=offscreen python3 -W error DamSafety_PMP/tests/test_atlas14_qgis.py
"""
import csv
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.pop("QGIS_CUSTOM_CONFIG_PATH", None)
os.environ["HOME"] = tempfile.mkdtemp(prefix="atlas14_qgis_home_")       # never the real profile

try:
    from qgis.core import QgsApplication, QgsProcessingContext, QgsProcessingFeedback, QgsSettings
except ImportError:
    print("SKIP: no QGIS")
    sys.exit(77)

from osgeo import gdal, ogr, osr  # noqa: E402

gdal.DontUseExceptions()                    # QGIS's default; the tools must leave it as it is

_APP = QgsApplication.instance()
if _APP is None:
    _APP = QgsApplication([], False)
    _APP.initQgis()

from DamSafety_PMP import algorithm_atlas14 as AL  # noqa: E402
from DamSafety_PMP.algorithm_hydro import InflowHydrographAlgorithm  # noqa: E402
from DamSafety_PMP.noaa14 import atlas14 as A  # noqa: E402
from DamSafety_PMP.noaa14 import temporal as T  # noqa: E402
from nd_breach_runner.core import adequacy as AD  # noqa: E402

FIX = os.path.join(HERE, "fixtures_atlas14")
FARGO_PT = "-96.7898,46.8772 [EPSG:4326]"


def modes():
    return (gdal.GetUseExceptions(), ogr.GetUseExceptions(), osr.GetUseExceptions())


MODES_AFTER_IMPORT = modes()     # importing the tools must leave QGIS's mode as it was (audit R3)

# a fresh QGIS process loads the provider as QGIS does at startup and prints the GDAL modes afterwards
PROVIDER_LOAD = """
import os, sys
sys.path.insert(0, sys.argv[1])
from qgis.core import QgsApplication
from osgeo import gdal, ogr, osr
gdal.DontUseExceptions()
app = QgsApplication([], False)
app.initQgis()
from DamSafety_PMP.provider import NDPMPProvider
prov = NDPMPProvider()
ok = QgsApplication.processingRegistry().addProvider(prov)
print("RESULT", gdal.GetUseExceptions(), ogr.GetUseExceptions(), osr.GetUseExceptions(), len(prov.algorithms()), int(ok))
sys.stdout.flush()
os._exit(0)
"""


class FakePFDS:
    def __init__(self):
        self.urls = []

    def get_bytes(self, url, timeout=60):
        self.urls.append(url)
        with open(os.path.join(FIX, "pfds_pds_english_fargo_nd.txt"), "rb") as fh:
            return fh.read()


# the .prj NOAA ships (full ESRI WKT). The two test scripts that wrote a cut-down
# GEOGCS["GCS_North_American_1983"] were the two that stalled at their first grid read on GDAL 3.4
# (QGIS 3.34 LTR image, CI 2026-11-18), so the fixtures use the real text.
NAD83_PRJ = ('GEOGCS["GCS_North_American_1983",DATUM["D_North_American_1983",SPHEROID["GRS_1980",6378137.0,'
             '298.257222101]],PRIMEM["Greenwich",0.0],UNIT["Degree",0.0174532925199433]]')


def noaa_zip(path, name, x0=-97.0, y0=46.6, n=60, cell=0.0083333, base=6000):
    """A NOAA-shaped Atlas 14 grid zip covering Fargo: base + column thousandths of an inch."""
    rows = "\n".join(" ".join(str(base + c) for c in range(n)) for _ in range(n))
    body = "ncols {n}\nnrows {n}\nxllcorner {x}\nyllcorner {y}\ncellsize {c}\nNODATA_value -9\n{r}\n".format(
        n=n, x=x0, y=y0, c=cell, r=rows)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(name + ".asc", body)
        z.writestr(name + ".prj", NAD83_PRJ)
        z.writestr(name + ".xml", "<metadata><title>test</title><pubdate>20130415</pubdate></metadata>")


def fake_download(url, dest):
    if url.endswith("_24h_temporal.csv"):
        shutil.copyfile(os.path.join(FIX, "mw_2_24h_temporal.csv"), dest)
    elif url.endswith(".zip") and "/pub/hdsc/data/mw/mw" in url:
        noaa_zip(dest, os.path.basename(url)[:-4], base=6000 if "100yr" in url else 9000)
    else:
        raise IOError("unexpected URL " + url)


def run(alg_class, params):
    alg = alg_class().create()
    ctx, fb = QgsProcessingContext(), QgsProcessingFeedback()
    res, ok = alg.run(params, ctx, fb)
    if not ok:
        raise AssertionError("{} failed: {}".format(alg.name(), res))
    return res


class Algorithms(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="atlas14_qgis_")
        cls.cache = os.path.join(cls.tmp, "cache")
        cls.pfds = FakePFDS()
        AL._Base.http_client = cls.pfds
        AL._Base.downloader = staticmethod(fake_download)
        AL._Base.pause_s = 0.0
        cls.basin = os.path.join(cls.tmp, "basin.geojson")
        with open(cls.basin, "w") as fh:
            json.dump({"type": "FeatureCollection", "features": [{"type": "Feature", "properties": {}, "geometry": {
                "type": "Polygon", "coordinates": [[[-96.85, 46.85], [-96.75, 46.85], [-96.75, 46.92], [-96.85, 46.92],
                                                    [-96.85, 46.85]]]}}]}, fh)

    @classmethod
    def tearDownClass(cls):
        AL._Base.http_client = None
        AL._Base.downloader = None
        AL._Base.pause_s = 1.0
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_registered_names_group_and_choices(self):
        from DamSafety_PMP.provider import NDPMPProvider
        prov = NDPMPProvider()
        self.assertTrue(QgsApplication.processingRegistry().addProvider(prov))
        try:
            names = {a.name() for a in prov.algorithms() if a.groupId() == "atlas14"}
            self.assertEqual(names, {"atlas14_point", "atlas14_basin", "atlas14_fetchgrid", "atlas14_gridbasin",
                                     "atlas14_hyetograph"})
            h = QgsApplication.processingRegistry().algorithmById("ndpmp:atlas14_hyetograph")
            self.assertEqual(h.group(), "NOAA Atlas 14")
            self.assertEqual(h.parameterDefinition("STORM").options(), A.DURATION_LABELS)
            self.assertEqual(h.parameterDefinition("PATTERN").options(), T.PATTERN_LABELS)
            self.assertTrue(h.parameterDefinition("STORM").allowMultiple())
            self.assertEqual(h.parameterDefinition("STEP_MIN").defaultValue(), 15)    # the PMP 24-hour storms' step
            self.assertIsNone(h.parameterDefinition("INITIAL_LOSS"))           # no losses here: inflow_hydrograph's
            g = QgsApplication.processingRegistry().algorithmById("ndpmp:atlas14_fetchgrid")
            self.assertEqual(g.parameterDefinition("DURATION").options(), A.DURATION_LABELS)
            for n in ("REGION", "SERIES", "FREQUENCY", "BOUND", "CACHE_DIR", "OFFLINE", "POINT", "EXTENT", "BASIN",
                      "MASK_TO_BASIN", "BUFFER_KM", "OUTPUT", "OUTPUT_JSON"):
                self.assertIsNotNone(g.parameterDefinition(n), n)
        finally:
            QgsApplication.processingRegistry().removeProvider(prov)

    def test_unit_declaring_labels(self):
        try:
            from DamSafety_Core.damsuite import display_units as DU
        except ImportError:
            self.skipTest("DamSafety Core not importable")
        h = AL.Atlas14HyetographAlgorithm().create()
        self.assertEqual(DU.unit_in_label(h.parameterDefinition("AREA_MI2").description()), "sq mi")
        for n in ("STEP_MIN", "AREAL_FACTOR", "PERCENT", "TDA"):
            self.assertIsNone(DU.unit_in_label(h.parameterDefinition(n).description()), n)

    def test_point_hyetograph_inflow_chain(self):
        before = modes()
        csv_p, js = os.path.join(self.tmp, "pt.csv"), os.path.join(self.tmp, "pt.json")
        r = run(AL.Atlas14PointAlgorithm, {"POINT": FARGO_PT, "CACHE_DIR": self.cache, "OUTPUT_CSV": csv_p,
                                          "OUTPUT_JSON": js})
        self.assertEqual(r["DEPTH_100YR_24HR"], 6.47)
        self.assertEqual(QgsSettings().value(AL.SETTING_CACHE, ""), self.cache)      # remembered
        out = os.path.join(self.tmp, "storms")
        r = run(AL.Atlas14HyetographAlgorithm, {"INPUT_JSON": js, "FREQUENCY": "100,500,1000", "STORM": [9],
                                               "PATTERN": [0, 1, 4], "STEP_MIN": 30, "TDA": 2, "AREA_MI2": 7.68,
                                               "CACHE_DIR": self.cache, "OUTPUT_FOLDER": out})
        self.assertEqual(r["STORMS"], 9)
        files = sorted(os.listdir(r["HYETOGRAPH_FOLDER"]))
        self.assertIn("atlas14_pds_0100yr_24hr_alternating.csv", files)
        self.assertIn("atlas14_pds_1000yr_24hr_atlas14temporal_q2_50pct.csv", files)
        with open(r["SUMMARY_CSV"], newline="") as fh:
            self.assertEqual(len(list(csv.DictReader(fh))), 9)
        with open(r["OUTPUT_JSON"], encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["areal_factor"], 1.0)
        flows = os.path.join(self.tmp, "inflow")
        r2 = run(InflowHydrographAlgorithm, {"HYETOGRAPHS": r["HYETOGRAPH_FOLDER"], "CN": 80, "TC_HR": 3.0,
                                             "OUTPUT_FOLDER": flows})
        with open(r2["SUMMARY_CSV"], newline="") as fh:
            rows = list(csv.DictReader(fh))
        self.assertEqual(len(rows), 9)
        self.assertEqual({x["area_mi2"] for x in rows}, {"7.68"})
        for p in AD.find_inflows(flows):
            t, q, meta = AD.read_inflow_csv(p)
            self.assertEqual((t[0], meta["warnings"], meta["storm_type"]), (0.0, [], "atlas14"))
        self.assertEqual(modes(), before)

    def test_hyetograph_point_route_and_areal_factor(self):
        before = modes()
        out = os.path.join(self.tmp, "storms_pt")
        r = run(AL.Atlas14HyetographAlgorithm, {"POINT": FARGO_PT, "FREQUENCY": "100", "STORM": [7, 9], "PATTERN": [0, 4],
                                               "STEP_MIN": 60, "AREA_MI2": 7.68, "AREAL_FACTOR": 0.9,
                                               "CACHE_DIR": self.cache, "OUTPUT_FOLDER": out})
        self.assertEqual(r["STORMS"], 3)                                   # 6-hr NRCS II skipped (24-hour only)
        self.assertTrue(os.path.exists(os.path.join(out, "atlas14_estimates.json")))
        with open(r["OUTPUT_JSON"], encoding="utf-8") as fh:
            doc = json.load(fh)
        self.assertEqual([(s["duration"], s["pattern"]) for s in doc["skipped"]], [("6-hr", "nrcs2")])
        s24 = [s for s in doc["storms"] if s["storm_id"] == "atlas14_pds_0100yr_24hr_alternating"][0]
        self.assertAlmostEqual(s24["gross_total"], 6.47 * 0.9, places=5)
        self.assertEqual(modes(), before)

    def test_hyetograph_refuses_bad_input(self):
        out = os.path.join(self.tmp, "bad")
        alg = AL.Atlas14HyetographAlgorithm().create()
        for params in ({"FREQUENCY": "100", "OUTPUT_FOLDER": out},                       # neither JSON nor point
                       {"POINT": FARGO_PT, "FREQUENCY": "100,300", "CACHE_DIR": self.cache, "OUTPUT_FOLDER": out}):
            res, ok = alg.run(params, QgsProcessingContext(), QgsProcessingFeedback())
            self.assertFalse(ok, params)

    def test_basin(self):
        r = run(AL.Atlas14BasinAlgorithm, {"BASIN": self.basin, "MAX_POINTS": 3, "CACHE_DIR": self.cache,
                                          "OUTPUT_CSV": os.path.join(self.tmp, "b.csv"),
                                          "OUTPUT_JSON": os.path.join(self.tmp, "b.json"), "POINTS": "TEMPORARY_OUTPUT"})
        self.assertEqual(r["DEPTH_100YR_24HR"], 6.47)
        with open(r["OUTPUT_JSON"], encoding="utf-8") as fh:
            doc = json.load(fh)
        self.assertEqual(len(doc["basin"]["points"]), 3)
        self.assertEqual(doc["schema"], "damit.pmp.atlas14")

    def test_fetchgrid_then_gridbasin(self):
        before = modes()
        tif = os.path.join(self.tmp, "g100.tif")
        r = run(AL.Atlas14FetchGridAlgorithm, {"POINT": FARGO_PT, "FREQUENCY": 100, "DURATION": 9, "BUFFER_KM": 3.0,
                                              "CACHE_DIR": self.cache, "OUTPUT": tif})
        self.assertEqual(r["URL"], "https://hdsc.nws.noaa.gov/pub/hdsc/data/mw/mw100yr24ha.zip")
        self.assertTrue(6.0 < r["POINT_VALUE"] < 6.06)
        with open(tif + ".json", encoding="utf-8") as fh:
            side = json.load(fh)
        self.assertEqual((side["schema"], side["units"]), ("damit.pmp.atlas14_gis", "in"))
        r = run(AL.Atlas14GridBasinAlgorithm, {"GRID": tif, "BASIN": self.basin,
                                              "OUTPUT_JSON": os.path.join(self.tmp, "gb1.json")})
        self.assertTrue(6.0 < r["MEAN"] < 6.06)                            # read in inches (sidecar), not x 0.001
        r2 = run(AL.Atlas14GridBasinAlgorithm, {"BASIN": self.basin, "FREQUENCY": 500, "DURATION": 9,
                                               "CACHE_DIR": self.cache, "OUTPUT_JSON": os.path.join(self.tmp, "gb2.json")})
        self.assertTrue(9.0 < r2["MEAN"] < 9.06)
        self.assertEqual(modes(), before)

    def test_fetchgrid_with_output_json_still_writes_the_sidecar(self):
        tif = os.path.join(self.tmp, "g100b.tif")
        rec = os.path.join(self.tmp, "records", "fetch_record.json")
        r = run(AL.Atlas14FetchGridAlgorithm, {"POINT": FARGO_PT, "FREQUENCY": 100, "DURATION": 9, "BUFFER_KM": 3.0,
                                              "CACHE_DIR": self.cache, "OUTPUT": tif, "OUTPUT_JSON": rec})
        self.assertEqual(r["OUTPUT_JSON"], rec)
        for p in (tif + ".json", rec):
            with open(p, encoding="utf-8") as fh:
                self.assertEqual(json.load(fh)["units"], "in", p)
        r = run(AL.Atlas14GridBasinAlgorithm, {"GRID": tif, "BASIN": self.basin,
                                              "OUTPUT_JSON": os.path.join(self.tmp, "gb3.json")})
        self.assertTrue(6.0 < r["MEAN"] < 6.06)                            # inches, not 1000 times too small


class GdalMode(unittest.TestCase):
    """Loading the plugin leaves the GDAL exception mode of QGIS as it was (audit R3). noaa14/gridstats.py turns
    exceptions on at import, so it may only be imported lazily inside gdalsafe.gdal_exceptions()."""

    def test_importing_the_tools_leaves_the_mode(self):
        self.assertEqual(MODES_AFTER_IMPORT, (0, 0, 0), "importing algorithm_atlas14 turned GDAL exceptions on: "
                                                        "import noaa14.gridstats only inside gdal_exceptions()")

    def test_loading_the_provider_in_a_fresh_qgis_leaves_the_mode(self):
        warn = ["-W" + w for w in sys.warnoptions]                      # the same warning filters as this run
        r = subprocess.run([sys.executable] + warn + ["-c", PROVIDER_LOAD, ROOT], capture_output=True, text=True,
                           timeout=300)
        line = [ln for ln in r.stdout.splitlines() if ln.startswith("RESULT ")]
        self.assertTrue(line, "no result: rc {}\n{}\n{}".format(r.returncode, r.stdout[-1500:], r.stderr[-2500:]))
        g, o, s, n, ok = (int(x) for x in line[-1].split()[1:])
        self.assertEqual(ok, 1)
        self.assertGreaterEqual(n, 11)
        self.assertEqual((g, o, s), (0, 0, 0), "loading the ndpmp provider turned GDAL exceptions on (audit R3)")


if __name__ == "__main__":
    prog = unittest.main(exit=False)
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0 if prog.result.wasSuccessful() else 1)      # QGIS can crash while Python tears it down
