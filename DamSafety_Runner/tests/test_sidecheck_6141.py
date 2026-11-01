"""Runner 3.13.5: two findings from the DWR-6141 Taskers Coulee run (main 1e3e2f7, Runner
3.13.4). Synthetic geometry only (shaped like the case, not its data); no DEM, no model run.

  * CHECK THE DAM POINT was a false alarm: the inflow was downstream of the bent dam line
    but off to one side of mid-crest, and the trace's first 200 m ran east along the toe,
    so check (b) (inflow vs the dam POINT along the trace direction) said "behind". With a
    downstream side or a dam line, (b) is a note; (c), (d), (f) decide.
  * AADT siblings: the per-dam pack clips (pack_HPMS_ND_2024_<dam>.gpkg,
    pack_ND_county_roads_<dam>.gpkg, pack_NDDOT_traffic_counts_<dam>.gpkg) next to the roads
    layer are read with no AADT_LAYERS / ROADS_MORE, as the Hazard plugin reads them.

    python3 -m nd_breach_runner.tests.run_tests
"""
import os
import shutil
import tempfile

from nd_breach_runner.tests.registry import test
from nd_breach_runner.core import aadt_join as A
from nd_breach_runner.core import terrain as T

# local metres, a dam line running ESE -> WNW with a bend at its west end; pool to the SSW
LINE = [(90.0, -37.0), (74.0, -35.0), (46.0, -27.0), (25.0, -18.0), (-82.0, 55.0)]
MID = (0.0, 0.0)                       # the mid-crest dam point
INFLOW = (-22.0, 54.0)                 # 58 m N of mid-crest, ~33 m NE of the west segment
# trace: east along the toe for 210 m, then north and east down the coulee
TRACE = [(-22.0 + 30.0 * i, 44.0) for i in range(8)] + \
        [(188.0 + 30.0 * i, 44.0 + 30.0 * (i + 1)) for i in range(10)]
DOWN = (0.471, 0.882)                  # the line's normal away from the pool


@test
def sidecheck_trace_along_toe_6141():
    fd = T.flow_direction(TRACE)
    assert fd[0] > 0.95, fd                                   # east, like (0.99, 0.14)
    assert T._side(INFLOW, MID, fd) < 0                       # (b)'s old test fires
    chk = T.inflow_side_check(MID, INFLOW, TRACE, DOWN, crest_line=LINE)
    assert chk["ok"], chk
    assert chk["trace_vs_downstream"] > 0 and chk["trace_vs_dam_line_normal"] > 0, chk
    assert any("behind the dam" in n for n in chk.get("notes", [])), chk
    # downstream side alone (no line): (c) and (d) pass, (b) still only a note
    chk = T.inflow_side_check(MID, INFLOW, TRACE, DOWN)
    assert chk["ok"], chk


@test
def sidecheck_b_still_fires_without_side_6141():
    # neither a downstream side nor a dam line: (b) is the only side test and still a problem
    chk = T.inflow_side_check(MID, INFLOW, TRACE)
    assert not chk["ok"] and any("behind the dam" in p for p in chk["problems"]), chk


@test
def sidecheck_upstream_inflow_still_flagged_6141():
    # a real wrong-side inflow (in the pool, SSW) is still caught by (c) / (d) / (f)
    up = (-10.0, -30.0)
    trace = [(-10.0 - 10.0 * i * 0.47, -30.0 - 10.0 * i * 0.88) for i in range(30)]
    chk = T.inflow_side_check(MID, up, trace, DOWN, crest_line=LINE)
    assert not chk["ok"], chk
    assert any("upstream side" in p for p in chk["problems"]), chk


@test
def aadt_siblings_pack_clips_6141():
    d = tempfile.mkdtemp(prefix="aadt6141_")
    try:
        names = ("pack_ND_roads_NG911_6141.gpkg", "pack_HPMS_ND_2024_6141.gpkg",
                 "pack_ND_county_roads_6141.gpkg", "pack_NDDOT_traffic_counts_6141.gpkg",
                 "pack_NSI_structures_6141.gpkg", "HPMS_ND_2024.gpkg", "ND_county_roads.gpkg",
                 "pack_ND_county_roads_pack.geojson")
        for n in names:
            open(os.path.join(d, n), "wb").close()
        roads = os.path.join(d, names[0]) + "|layername=roads"
        got = [os.path.basename(p) for p in A.siblings(roads)]
        assert got == ["HPMS_ND_2024.gpkg", "ND_county_roads.gpkg",
                       "pack_HPMS_ND_2024_6141.gpkg", "pack_NDDOT_traffic_counts_6141.gpkg",
                       "pack_ND_county_roads_6141.gpkg"], got
        assert [os.path.basename(p) for p in A.join_sources("", roads, "")] == got
    finally:
        shutil.rmtree(d, ignore_errors=True)
