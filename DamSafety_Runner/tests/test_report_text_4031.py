"""Runner 3.13.2: run report text from the DWR-4031 toe10m run (Runner 3.12.9 / Hazard
2.20.4). Synthetic values only, no DWR data, no model run.

  * peak inflow: the breach hydrograph's peak (1,696 cfs), with the .mass value labelled as
    the largest Qin at the .mass output samples (1,570 cfs, every 300 s);
  * LISFLOOD-FP passes are numbered, and a clock extension (same domain run, by design) is
    marked, so the report no longer shows "run 1" twice;
  * the ND hazard AADT join reads the ROADS_MORE GeoPackages (ND county roads) too, so
    87TH ST NW takes AADT 60 as in the Hazard plugin, not "AADT unknown (assumed Medium)".

    python3 -m nd_breach_runner.tests.run_tests
"""
import os
import shutil
import tempfile

import numpy as np

from nd_breach_runner.tests.registry import test
from nd_breach_runner.core import aadt_join as A
from nd_breach_runner.core import lisflood as LF
from nd_breach_runner.core import report_docx as R
from nd_breach_runner.core.units import CFS

# DWR-4031 toe10m _breach_model/state.json (trimmed): two passes, both "run": 1
RUNS_4031 = [{"run": 1, "sim_time_hr": 22.11}, {"run": 1, "sim_time_hr": 33.16}]


@test
def peak_inflow_text_4031():
    st = {"peak_inflow_cfs": 1570.0, "peak_inflow_input_cfs": 1696.0, "massint_s": 300.0}
    t = R.peak_inflow_text(st)
    assert t == ("peak inflow 1,696 cfs (breach hydrograph); 1,570 cfs at the .mass output "
                 "samples (every 300 s)"), t
    # the same value: no second figure
    t = R.peak_inflow_text({"peak_inflow_cfs": 1696.0, "peak_inflow_input_cfs": 1696.0})
    assert t == "peak inflow 1,696 cfs (breach hydrograph)", t
    # a run report written before 3.13.2: the .mass value, labelled
    t = R.peak_inflow_text({"peak_inflow_cfs": 1570.0})
    assert t == "peak inflow 1,570 cfs at the .mass output samples", t
    assert R.label_of("peak_inflow_cfs") == "Peak inflow at the .mass output samples (cfs)"
    assert R.label_of("peak_inflow_input_cfs") == "Peak inflow, breach hydrograph (cfs)"


@test
def hydrograph_peak_is_reported():
    class H(object):
        q_cms = np.array([0.0, 48.03, 44.46, 10.0])     # 1,696 / 1,570 cfs

        @property
        def peak_cfs(self):
            return float(self.q_cms.max()) / CFS
    assert LF._hydrograph_peak_cfs(H()) == 1696.0, LF._hydrograph_peak_cfs(H())
    assert LF._hydrograph_peak_cfs(None) is None


@test
def passes_are_numbered_4031():
    # an older run report (no "pass" / "clock_extension"): numbered by position, and the
    # second pass with the same domain run is the clock extension
    got = [(R.pass_number(it, i), R.domain_run_text(RUNS_4031, i))
           for i, it in enumerate(RUNS_4031)]
    assert got == [(1, "1"), (2, "1 (clock extended)")], got
    # 3.13.2 records: pass 1, 2 (clock), 3 (domain moved out)
    runs = [{"run": 1, "pass": 1, "clock_extension": False},
            {"run": 1, "pass": 2, "clock_extension": True},
            {"run": 2, "pass": 3, "clock_extension": False}]
    got = [(R.pass_number(it, i), R.domain_run_text(runs, i)) for i, it in enumerate(runs)]
    assert got == [(1, "1"), (2, "1 (clock extended)"), (3, "2")], got


@test
def aadt_join_reads_roads_more_4031():
    d = tempfile.mkdtemp(prefix="aadt4031_")
    try:
        hpms, cnt, county = (os.path.join(d, n) for n in (
            "pack_HPMS_ND_2024_4031.gpkg", "pack_NDDOT_traffic_counts_4031.gpkg",
            "pack_ND_county_roads_4031.gpkg"))
        for p in (hpms, cnt, county):
            open(p, "wb").close()
        got = A.join_sources(hpms + ";;" + cnt, os.path.join(d, "roads.gpkg"),
                             county + "|layername=ND_county_roads;;" + hpms + ";;"
                             + os.path.join(d, "missing.gpkg"))
        assert got == [hpms, cnt, county], got        # county added once, missing skipped
        # no AADT_LAYERS: siblings by name (none here) + ROADS_MORE
        assert A.join_sources("", os.path.join(d, "roads.gpkg"), county) == [county]
    finally:
        shutil.rmtree(d, ignore_errors=True)
    # an NG911 segment along a county-roads line with AADT 60 takes 60 (Low, 5.2.1.2)
    road = [[(0.0, 0.0), (400.0, 0.0)]]
    traffic = [{"kind": "line", "parts": [[(-50.0, 4.0), (450.0, 4.0)]], "aadt": 60.0,
                "year": None, "name": "87TH ST NW",
                "source": "pack_ND_county_roads_4031.gpkg (AADT)"}]
    m = A.match(road, traffic)
    assert m and m["aadt"] == 60.0, m
