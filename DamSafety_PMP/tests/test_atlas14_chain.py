"""Atlas 14 storms through the DWR chain, with no QGIS and no network:

  Fargo PFDS answer (fixtures_atlas14/) -> atlas14_outputs.make_storms (what ndpmp:atlas14_hyetograph writes)
  -> core.hydro read_hyetograph / run_folder (ndpmp:inflow_hydrograph) -> DamSafety_Runner core.adequacy
  read_inflow_csv / assess (ndbreach:spillwayadequacy) on a toy dam.

Also: the nd_pmp hyetograph contract (header lines, time at the end of each step, no t = 0 row, gross depth = the
Atlas 14 depth), storm ids, skipped duration / pattern pairs, the areal factor, mm answers, frequency parsing, a
rerun into the same folder, that the GDAL exception mode is left as it was and is off during downloads (audit R3),
and that inflow_hydrograph's StreamStats check takes no Atlas 14 storm for the PMF. numpy + GDAL (the temporal-area
shapefile).

Run:  python3 -W error DamSafety_PMP/tests/test_atlas14_chain.py
"""
import csv
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)

from osgeo import gdal, ogr, osr  # noqa: E402

gdal.DontUseExceptions()            # QGIS's default mode; the tools must leave it as it is (audit R3)

from DamSafety_PMP import atlas14_outputs as O  # noqa: E402
from DamSafety_PMP.core import gdalsafe as GS  # noqa: E402
from DamSafety_PMP.core import hydro as H  # noqa: E402
from DamSafety_PMP.noaa14 import atlas14 as A  # noqa: E402
from nd_breach_runner.core import adequacy as AD  # noqa: E402

FIX = os.path.join(HERE, "fixtures_atlas14")
NAD83_PRJ = ('GEOGCS["GCS_North_American_1983",DATUM["D_North_American_1983",SPHEROID["GRS_1980",6378137.0,'
             '298.257222101]],PRIMEM["Greenwich",0.0],UNIT["Degree",0.0174532925199433]]')


def pf_of(name):
    with open(os.path.join(FIX, name), "rb") as fh:
        return A.parse(fh.read())


def modes():
    return (gdal.GetUseExceptions(), ogr.GetUseExceptions(), osr.GetUseExceptions())


def area_zip(path):
    """The HDSC temporal-area shapefile, cut down: Fargo (-96.79, 46.88) in area 2."""
    d = tempfile.mkdtemp()
    with GS.gdal_exceptions():
        ds = ogr.GetDriverByName("ESRI Shapefile").CreateDataSource(d)
        lyr = ds.CreateLayer("Vol_8_Temporal_Regions", None, ogr.wkbPolygon)
        lyr.CreateField(ogr.FieldDefn("Region", ogr.OFTInteger))
        for region, (x0, x1) in ((1, (-104, -98)), (2, (-98, -95)), (3, (-95, -90))):
            f = ogr.Feature(lyr.GetLayerDefn())
            f.SetField("Region", region)
            f.SetGeometry(ogr.CreateGeometryFromWkt("POLYGON(({0} 40,{1} 40,{1} 50,{0} 50,{0} 40))".format(x0, x1)))
            lyr.CreateFeature(f)
        ds = None
    with open(os.path.join(d, "Vol_8_Temporal_Regions.prj"), "w") as fh:
        fh.write(NAD83_PRJ)
    with zipfile.ZipFile(path, "w") as z:
        for fn in os.listdir(d):
            z.write(os.path.join(d, fn), fn)
    shutil.rmtree(d)


class FakeDownload:
    def __init__(self, shp_zip):
        self.urls, self.shp_zip = [], shp_zip

    def __call__(self, url, dest):
        self.urls.append(url)
        if url.endswith("mw_2_24h_temporal.csv"):
            shutil.copyfile(os.path.join(FIX, "mw_2_24h_temporal.csv"), dest)
        elif url.endswith("Vol8_Temporals_Shapefiles.zip"):
            shutil.copyfile(self.shp_zip, dest)
        else:
            raise IOError("unexpected URL " + url)


def raw_lines(path):
    with open(path, encoding="ascii") as fh:
        return fh.read().splitlines()


class Chain(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="atlas14_chain_")
        cls.zip = os.path.join(cls.tmp, "areas.zip")
        area_zip(cls.zip)
        cls.dl = FakeDownload(cls.zip)
        cls.pf = pf_of("pfds_pds_english_fargo_nd.txt")
        cls.out = os.path.join(cls.tmp, "storms")
        cls.warnings = []
        before = modes()
        cls.rec = O.make_storms(cls.pf, [100, 500, 1000], ["24-hr", "3-hr"], ["alternating", "atlas14", "nrcs2"], 60,
                                cls.out, area_mi2=7.68, cache_dir=os.path.join(cls.tmp, "cache"), download=cls.dl,
                                warn=cls.warnings.append)
        cls.modes = (before, modes())

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def files(self):
        return sorted(s["file"] for s in self.rec["storms"])

    # ---------------------------------------------------------------- what atlas14_hyetograph writes
    def test_one_storm_per_frequency_duration_pattern_and_skips_said(self):
        ids = [s["storm_id"] for s in self.rec["storms"]]
        self.assertEqual(len(ids), 12)                         # 24-hr x 3 patterns + 3-hr alternating, x 3 frequencies
        self.assertEqual(len(set(ids)), len(ids))
        for sid in ids:
            self.assertRegex(sid, r"^atlas14_pds_\d{4}yr_[0-9a-z]+_[A-Za-z0-9_]+$")
        self.assertIn("atlas14_pds_0100yr_24hr_alternating", ids)
        self.assertIn("atlas14_pds_1000yr_24hr_atlas14temporal_q2_50pct", ids)
        self.assertIn("atlas14_pds_0500yr_24hr_nrcs_type2", ids)
        self.assertIn("atlas14_pds_0100yr_3hr_alternating", ids)
        self.assertEqual(sorted((s["duration"], s["pattern"]) for s in self.rec["skipped"]),
                         [("3-hr", "atlas14"), ("3-hr", "nrcs2")])
        self.assertEqual(len(self.warnings), 2)
        self.assertTrue(all("Skipped 3-hr" in w for w in self.warnings))
        self.assertEqual(sorted(os.listdir(os.path.join(self.out, "hyetographs"))), sorted(i + ".csv" for i in ids))

    def test_the_area_was_found_and_the_gdal_mode_left_alone(self):
        before, after = self.modes
        self.assertEqual(before, (0, 0, 0))
        self.assertEqual(after, (0, 0, 0))                    # temporal.locate_area switched it only inside the block
        a14 = [s for s in self.rec["storms"] if s["pattern"] == "atlas14"]
        self.assertEqual({s["temporal"]["area"] for s in a14}, {2})
        self.assertEqual(a14[0]["temporal"]["area_from"], "HDSC shapefile point-in-polygon")
        self.assertEqual(sum(u.endswith("_temporal.csv") for u in self.dl.urls), 1)     # one curve per duration

    def test_nd_pmp_hyetograph_format(self):
        for p in self.files():
            lines = raw_lines(p)
            i = lines.index("time_hr,incr_depth_in,cum_depth_in")
            self.assertTrue(all(re.match(r"^# [a-z0-9_]+: ", ln) for ln in lines[:i]), p)
            rows = [[float(x) for x in ln.split(",")] for ln in lines[i + 1:]]
            self.assertEqual(rows[0][0], 1.0)                 # end of the first step, no t = 0 row
            self.assertTrue(all(abs(b[0] - a[0] - 1.0) < 1e-9 for a, b in zip(rows, rows[1:])))
            self.assertAlmostEqual(sum(r[1] for r in rows), rows[-1][2], places=5)
            hdr, dt, precip = H.read_hyetograph(p)
            self.assertEqual(dt, 1.0)
            self.assertEqual(hdr["storm_type"], "atlas14")
            self.assertEqual(hdr["area_mi2"], "7.68")
            self.assertEqual(hdr["storm_id"], os.path.splitext(os.path.basename(p))[0])
            for k in ("duration_hr", "temporal_distribution", "frequency_years", "series", "depth_in", "areal_factor",
                      "citation", "source"):
                self.assertIn(k, hdr, (p, k))
            self.assertIn("gross", hdr["rain"])

    def test_gross_depth_is_the_atlas_14_depth(self):
        want = {(100, "24-hr"): 6.47, (500, "24-hr"): 8.86, (1000, "24-hr"): 10.0}
        for s in self.rec["storms"]:
            hdr, dt, precip = H.read_hyetograph(s["file"])
            d = want.get((s["frequency_years"], s["storm"])) or A.depth_at(self.pf, 180, s["frequency_years"])
            self.assertAlmostEqual(float(precip.sum()), d, places=4, msg=s["storm_id"])
            self.assertAlmostEqual(float(hdr["depth_in"]), d, places=4)
            self.assertEqual(float(hdr["frequency_years"]), s["frequency_years"])
        nrcs = [s for s in self.rec["storms"] if s["pattern"] == "nrcs2" and s["frequency_years"] == 100][0]
        _, _, p = H.read_hyetograph(nrcs["file"])
        self.assertEqual(int(p.argmax()), 11)                 # NRCS Type II: the hour ending at 12 h

    def test_summary_and_record(self):
        with open(self.rec["summary_csv"], newline="") as fh:
            rows = list(csv.DictReader(fh))
        self.assertEqual(len(rows), 12)
        self.assertEqual(list(rows[0]), O.SUMMARY_COLS)
        r = [x for x in rows if x["storm_id"] == "atlas14_pds_0500yr_24hr_alternating"][0]
        self.assertEqual((r["frequency_years"], r["series"], r["duration"], r["pattern"], r["depth_in"], r["areal_factor"]),
                         ("500", "pds", "24-hr", "alternating", "8.86", "1"))
        self.assertTrue(os.path.exists(os.path.join(self.out, r["file"])))
        with open(self.rec["record_json"], encoding="utf-8") as fh:
            doc = json.load(fh)
        self.assertEqual(doc["schema"], "ndpmp.atlas14_hyetographs")
        self.assertEqual({s["schema"] for s in doc["storms"]}, {"damit.pmp.hyetograph"})
        self.assertEqual(len(doc["skipped"]), 2)
        self.assertIn("point values", doc["areal_factor_note"])
        self.assertIn("NOAA Atlas 14 Volume 8 Version 2", doc["citation"])
        self.assertTrue(all(s["losses"] is None for s in doc["storms"]))

    # ---------------------------------------------------------------- inflow_hydrograph -> spillwayadequacy
    def test_chain_to_inflow_hydrograph_and_spillway_adequacy(self):
        flows = os.path.join(self.tmp, "inflow")
        rows, summary = H.run_folder(self.files(), flows, H.HydroConfig(cn=80, tc_hr=3.0), log=None)
        self.assertEqual(len(rows), 12)
        by = {r["storm_id"]: r for r in rows}
        for r in rows:
            self.assertEqual(r["area_mi2"], 7.68)            # from the hyetograph header
            self.assertEqual(r["storm_type"], "atlas14")
            self.assertGreater(r["peak_cfs"], 0)
            self.assertLess(r["excess_in"], r["precip_in"])  # the losses are inflow_hydrograph's
        a = [by["atlas14_pds_{:04d}yr_24hr_alternating".format(y)]["peak_cfs"] for y in (100, 500, 1000)]
        self.assertTrue(a[0] < a[1] < a[2], a)
        inflows = AD.find_inflows(flows)
        self.assertEqual(len(inflows), 12)
        for p in inflows:
            t, q, meta = AD.read_inflow_csv(p)
            self.assertEqual(t[0], 0.0)
            self.assertEqual(meta["warnings"], [])
            self.assertEqual(meta["storm_type"], "atlas14")
            self.assertAlmostEqual(q.max() / 0.028316846592, by[meta["storm_id"]]["peak_cfs"], delta=1.0)
        # a toy dam: 300 ac pool, 60 ft weir spillway at 1000 ft, top of dam 1008 ft, low hazard (IDF = 100-year)
        ss = os.path.join(self.tmp, "ss.csv")
        with open(ss, "w") as fh:
            fh.write("elev_ft,storage_acft\n990,0\n1040,15000\n")
        rating = os.path.join(self.tmp, "rating.csv")
        with open(rating, "w") as fh:
            fh.write("elev_ft,q_cfs\n" + "".join("{},{:.1f}\n".format(1000 + h, 3.0 * 60 * h ** 1.5)
                                                 for h in [x * 0.5 for x in range(0, 41)]))
        pick = [p for p in inflows if "_24hr_alternating" in p]
        out = AD.assess(pick, 1008.0, "low", spillway_csv=rating, stage_storage_csv=ss, crest_length_ft=400.0,
                        bisect=False, dt_s=60.0)
        got = {r["storm_id"]: r for r in out["rows"]}
        self.assertEqual(set(got), {"atlas14_pds_{:04d}yr_24hr_alternating".format(y) for y in (100, 500, 1000)})
        pools = [got["atlas14_pds_{:04d}yr_24hr_alternating".format(y)]["peak_pool_ft"] for y in (100, 500, 1000)]
        self.assertTrue(1000.0 < pools[0] < pools[1] < pools[2], pools)
        passes = [got["atlas14_pds_{:04d}yr_24hr_alternating".format(y)]["passes"] for y in (100, 500, 1000)]
        self.assertEqual(passes, [True, False, False], pools)  # 3 ft residual freeboard (low hazard): 1005 ft allowed
        self.assertFalse(out["verdict"]["passes"])
        self.assertEqual(out["verdict"]["governing_storm"], "atlas14_pds_1000yr_24hr_alternating")
        self.assertIn("1 percent AEP", out["verdict"]["required_idf"])
        for r in out["rows"]:
            self.assertEqual(r["storm_type"], "atlas14")
            self.assertEqual(r["passes"], r["peak_pool_ft"] <= 1008.0 - 3.0 + 1e-9)


class Options(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="atlas14_opt_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_areal_factor_multiplies_every_depth_and_is_recorded(self):
        pf = pf_of("pfds_pds_english_fargo_nd.txt")
        rec = O.make_storms(pf, [100], ["24-hr"], ["alternating"], 30, self.tmp, area_mi2=7.68, areal_factor=0.9)
        s = rec["storms"][0]
        hdr, dt, p = H.read_hyetograph(s["file"])
        self.assertAlmostEqual(float(p.sum()), 6.47 * 0.9, places=5)
        self.assertEqual((hdr["areal_factor"], hdr["atlas14_depth_in"], hdr["depth_in"]), ("0.9", "6.47", "5.823"))
        self.assertEqual((s["areal_factor"], s["atlas14_depth_in"], s["gross_total"]), (0.9, 6.47, 5.823))
        ref = O.make_storms(pf, [100], ["24-hr"], ["alternating"], 30, os.path.join(self.tmp, "ref"))
        _, _, p1 = H.read_hyetograph(ref["storms"][0]["file"])
        for a, b in zip(p, p1):
            self.assertAlmostEqual(a, b * 0.9, places=5)
        with self.assertRaises(A.Atlas14Error):
            O.make_storms(pf, [100], ["24-hr"], ["alternating"], 30, self.tmp, areal_factor=0)

    def test_metric_ams_answer_is_written_in_inches(self):
        pf = pf_of("pfds_ams_metric_lincoln_ne.txt")
        rec = O.make_storms(pf, [100], ["24-hr"], ["alternating"], 60, self.tmp)
        s = rec["storms"][0]
        mm = pf["quantiles"][A.duration_index("24-hr")][A.freq_index(pf, 100)]
        self.assertEqual(s["storm_id"], "atlas14_ams_0100yr_24hr_alternating")
        self.assertAlmostEqual(s["gross_total"], mm / 25.4, places=4)
        hdr, _, p = H.read_hyetograph(s["file"])
        self.assertEqual((hdr["series"], hdr["aep_percent"]), ("ams", "1"))
        self.assertAlmostEqual(float(p.sum()), mm / 25.4, places=4)
        with self.assertRaises(A.Atlas14Error):
            O.make_storms(dict(pf, datatype="intensity"), [100], ["24-hr"], ["alternating"], 60, self.tmp)

    def test_nothing_buildable_says_why(self):
        pf = pf_of("pfds_pds_english_fargo_nd.txt")
        with self.assertRaises(A.Atlas14Error) as cm:
            O.make_storms(pf, [100], ["15-min"], ["alternating", "nrcs2"], 60, self.tmp)
        self.assertIn("no storm could be built", str(cm.exception))
        self.assertIn("shorter than the time step", str(cm.exception))
        with self.assertRaises(A.Atlas14Error):
            O.make_storms(pf, [300], ["24-hr"], ["alternating"], 60, self.tmp)       # not an Atlas 14 frequency

    def test_parse_frequencies(self):
        self.assertEqual(O.parse_frequencies("100,500,1000"), [100, 500, 1000])
        self.assertEqual(O.parse_frequencies(" 100, 500 ; 1000 100 "), [100, 500, 1000])
        self.assertEqual(O.parse_frequencies("1000.0"), [1000])
        for bad in ("", "100,abc", "100.5", "3", "2000"):
            with self.assertRaises(A.Atlas14Error, msg=bad):
                O.parse_frequencies(bad)
        with self.assertRaises(A.Atlas14Error):
            O.parse_frequencies("1", "ams")                  # no 1-year AEP
        self.assertEqual(O.parse_frequencies("2,1000", "ams"), [2, 1000])

    def test_storm_ids(self):
        self.assertEqual(O.storm_id("pds", 100, "24-hr", "alternating"), "atlas14_pds_0100yr_24hr_alternating")
        self.assertEqual(O.storm_id("pds", 5, "2-day", "nrcs1a"), "atlas14_pds_0005yr_2day_nrcs_type1a")
        self.assertEqual(O.storm_id("ams", 1000, "60-min", "atlas14", "all", 90),
                         "atlas14_ams_1000yr_60min_atlas14temporal_qall_90pct")

    def test_table_json_round_trip(self):
        pf = pf_of("pfds_pds_english_fargo_nd.txt")
        c, j = os.path.join(self.tmp, "t.csv"), os.path.join(self.tmp, "t.json")
        doc = O.write_outputs(c, j, pf, [], {"location": {"lat": 46.88, "lon": -96.79}})
        self.assertEqual(doc["schema"], "damit.pmp.atlas14")         # DamIT PMP's pointpf / basinpf file
        pf2, doc2 = O.read_atlas14_json(j)
        self.assertEqual(pf2["quantiles"], pf["quantiles"])
        with open(c, newline="") as fh:
            rows = [r for r in csv.reader(fh) if r and not r[0].startswith("#")]
        self.assertEqual(rows[0][:5], ["duration", "minutes", "frequency_years", "series", "value"])
        self.assertEqual(len(rows), 1 + 19 * 10)
        with self.assertRaises(A.Atlas14Error):
            O.read_atlas14_json(c)

    def test_a_rerun_removes_the_storms_it_does_not_write_again(self):
        pf = pf_of("pfds_pds_english_fargo_nd.txt")
        first = O.make_storms(pf, [100, 500, 1000], ["24-hr", "6-hr"], ["alternating", "nrcs2"], 60, self.tmp)
        folder = first["hyetograph_folder"]
        self.assertEqual(len(O.folder_hyetographs(folder)), 9)          # 6-hr NRCS skipped
        for name in ("pmp_general_24hr.csv", "atlas14_notes.csv"):      # CSVs this tool did not write stay
            with open(os.path.join(folder, name), "w") as fh:
                fh.write("time_hr,incr_depth_in,cum_depth_in\n1,1,1\n")
        warnings = []
        again = O.make_storms(pf, [100], ["24-hr"], ["alternating"], 60, self.tmp, warn=warnings.append)
        self.assertEqual(O.folder_hyetographs(folder), ["atlas14_notes.csv", "atlas14_pds_0100yr_24hr_alternating.csv",
                                                        "pmp_general_24hr.csv"])
        self.assertEqual(len(again["removed_earlier_storms"]), 8)
        self.assertIn("atlas14_pds_1000yr_24hr_alternating.csv", again["removed_earlier_storms"])
        self.assertEqual(len([w for w in warnings if w.startswith("Removed 8 Atlas 14 hyetograph(s)")]), 1)
        with open(again["record_json"], encoding="utf-8") as fh:
            self.assertEqual(len(json.load(fh)["removed_earlier_storms"]), 8)
        with self.assertRaises(A.Atlas14Error):                         # a failed run removes nothing
            O.make_storms(pf, [100], ["15-min"], ["alternating"], 60, self.tmp)
        self.assertEqual(len(O.folder_hyetographs(folder)), 3)

    def test_temporal_downloads_run_outside_the_gdal_block(self):
        zp = os.path.join(self.tmp, "areas.zip")
        area_zip(zp)
        fake, seen = FakeDownload(zp), []

        def download(url, dest):
            seen.append((os.path.basename(url), gdal.GetUseExceptions(), ogr.GetUseExceptions()))
            fake(url, dest)
        before = modes()
        rec = O.make_storms(pf_of("pfds_pds_english_fargo_nd.txt"), [100], ["24-hr"], ["atlas14"], 60,
                            os.path.join(self.tmp, "out"), cache_dir=os.path.join(self.tmp, "cache"), download=download)
        self.assertEqual(seen, [("Vol8_Temporals_Shapefiles.zip", 0, 0), ("mw_2_24h_temporal.csv", 0, 0)])
        self.assertEqual(rec["storms"][0]["temporal"]["area"], 2)
        self.assertEqual(rec["storms"][0]["temporal"]["area_from"], "HDSC shapefile point-in-polygon")
        self.assertEqual(modes(), before)

    def test_gridbasin_style_import_keeps_the_gdal_mode(self):
        before = modes()
        with GS.gdal_exceptions():
            from DamSafety_PMP.noaa14 import gridstats  # noqa: F401 - sets exceptions at import
            # gdal and ogr: what gridstats switches on. osr follows the gdal switch on newer GDAL (3.8 here)
            # but keeps its own on GDAL 3.4 (the QGIS 3.34 LTR image), so it is not part of the check.
            self.assertEqual(modes()[:2], (1, 1))
        self.assertEqual(modes(), before)


class StreamStatsCheck(unittest.TestCase):
    """inflow_hydrograph's StreamStats check (core/streamstats.py check_pmf_run) takes no Atlas 14 storm for the PMF."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="atlas14_ss_")
        self.basin = os.path.join(self.tmp, "dam_streamstats_basin.json")
        with open(self.basin, "w", encoding="utf-8") as fh:
            json.dump({"inputs": {"area_mi2": 7.68}, "sanity": {"peaks": [{"code": "PK0_2AEP", "q_cfs": 3000.0}],
                                                                "crippen_bue_envelope_cfs": 20000.0}}, fh)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_atlas14_storms_are_not_a_pmf(self):
        from DamSafety_PMP.core import streamstats as SS
        rec = O.make_storms(pf_of("pfds_pds_english_fargo_nd.txt"), [100, 1000], ["24-hr"], ["alternating"], 60,
                            os.path.join(self.tmp, "storms"), area_mi2=7.68)
        rows, _ = H.run_folder([s["file"] for s in rec["storms"]], os.path.join(self.tmp, "inflow"),
                               H.HydroConfig(cn=80, tc_hr=3.0), log=None)
        msgs, written = SS.check_pmf_run(rows, self.basin, os.path.join(self.tmp, "chk"), 7.68)
        self.assertEqual([lvl for lvl, _ in msgs], ["INFO", "INFO"])           # no PMF verdict, no false alarm
        self.assertIn("2 NOAA Atlas 14 frequency storm(s) left out", msgs[0][1])
        with open(written["check"], encoding="utf-8") as fh:
            rep = json.load(fh)
        self.assertEqual((rep["pmf_peak_cfs"], rep["pmf_storm_id"], rep["atlas14_storms_left_out"]), (None, None, 2))
        pmf = {"storm_id": "General_24hr_test", "storm_type": "General", "peak_cfs": 15000.0}
        msgs, written = SS.check_pmf_run(rows + [pmf], self.basin, os.path.join(self.tmp, "chk2"), 7.68)
        self.assertEqual(msgs[0][0], "INFO")
        self.assertIn(("OK", "The PMF peak (15000 cfs) is 5.0 times the 0.2% AEP regression peak (3000 cfs)."), msgs)
        with open(written["check"], encoding="utf-8") as fh:
            rep = json.load(fh)
        self.assertEqual((rep["pmf_storm_id"], rep["atlas14_storms_left_out"]), ("General_24hr_test", 2))


if __name__ == "__main__":
    unittest.main()
