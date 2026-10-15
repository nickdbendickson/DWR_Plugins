"""Culvert matching: a road-crossing guess takes the real size of an NDDOT / NBI inventory
culvert within the match distance (core/culvert_guess.py)."""
import ast
import os
import tempfile

import numpy as np

from nd_breach_runner.tests.registry import test as register

HERE = os.path.dirname(os.path.abspath(__file__))
SOURCES = "/workspace/culvert_sources"
FT = 0.3048


def _guess(x, y, name="guess: road 1 crosses channel 1"):
    return {"name": name, "kind": "culvert", "shape": "box", "barrels": 1, "width_m": 1.2,
            "height_m": 0.9, "source": "guess", "surveyed": False,
            "line": [(x - 6.0, y), (x + 6.0, y)], "note": "not surveyed"}


def _nddot(bid, x, y, text, design="Culvert", spans=2):
    from nd_breach_runner.core import culvert_guess as cg
    return cg.inventory_record({"BRIDGE_ID": bid, "DESIGNMAINDESC": design, "CULVERT": text,
                                "TOTAL_SPANS": spans}, (x, y))


@register
def culvert_match_copies_nddot_size_within_distance():
    from nd_breach_runner.core import culvert_guess as cg
    inv = [_nddot("09-118-21.1", 40.0, 0.0, "DOUBLE 16X8X52' PRECAST RCB")]
    rows, notes = cg.match_inventory([_guess(0, 0), _guess(1000, 0)], inv, 75.0)
    m, u = rows
    assert m["source"] == "NDDOT 09-118-21.1" and m["surveyed"] is True
    assert abs(m["match_m"] - 40.0) < 1e-6
    assert abs(m["width_m"] - 16 * FT) < 1e-3 and abs(m["height_m"] - 8 * FT) < 1e-3
    assert m["barrels"] == 2 and m["shape"] == "box"
    assert m["name"].startswith("NDDOT 09-118-21.1")
    assert u["source"] == "guess" and u["surveyed"] is False and "match_m" not in u
    # distance is configurable
    rows, _ = cg.match_inventory([_guess(0, 0)], inv, 30.0)
    assert rows[0]["source"] == "guess"
    # matching twice does not re-match or rename
    again, _ = cg.match_inventory(rows and cg.match_inventory([_guess(0, 0)], inv)[0], inv)
    assert again[0]["name"].count("matched guess") == 1


@register
def culvert_match_bridges_never_size_and_nddot_wins():
    from nd_breach_runner.core import culvert_guess as cg
    br = _nddot("B1", 5.0, 0.0, None, design="Slab", spans=3)
    assert br["kind"] == "bridge"
    nbi = cg.inventory_record({"STRUCTURE_NUMBER_008": "000000009116150",
                               "STRUCTURE_TYPE_043B": "19", "MAIN_UNIT_SPANS_045": 2,
                               "MAX_SPAN_LEN_MT_048": 3.0}, (10.0, 0.0))
    assert nbi["kind"] == "culvert" and nbi["source"] == "NBI" and nbi["barrels"] == 2
    nd = _nddot("09-116-15.0", 60.0, 0.0, "DOUBLE, 10X4X34' RCB")
    rows, _ = cg.match_inventory([_guess(0, 0)], [br, nbi, nd], 75.0)
    assert rows[0]["source"] == "NDDOT 09-116-15.0", rows[0]["source"]
    rows, _ = cg.match_inventory([_guess(0, 0)], [br, nbi], 75.0)
    assert rows[0]["source"] == "NBI 9116150"
    assert abs(rows[0]["width_m"] - 3.0) < 1e-3 and rows[0]["height_m"] == 0.9   # no NBI rise
    rows, _ = cg.match_inventory([_guess(0, 0)], [br], 75.0)
    assert rows[0]["source"] == "guess"
    # 43B: 19 culvert, anything else (02, 4) bridge, leading zeros ignored
    assert cg.nbi_is_culvert("19") and cg.nbi_is_culvert("019") and cg.nbi_is_culvert(19)
    assert not cg.nbi_is_culvert("02") and not cg.nbi_is_culvert("") and not cg.nbi_is_culvert(None)


@register
def culvert_match_one_record_per_guess_and_pipe_arch_shapes():
    from nd_breach_runner.core import culvert_guess as cg
    inv = [_nddot("P", 10.0, 0.0, "SINGLE 48\" X 60' CMP"),
           _nddot("A", 500.0, 0.0, "(2) 117\" X 79\" X 168' CSPA")]
    rows, _ = cg.match_inventory([_guess(0, 0), _guess(20, 0), _guess(510, 0)], inv, 75.0)
    assert rows[0]["source"] == "NDDOT P" and rows[1]["source"] == "guess"   # nearest only
    assert rows[0]["shape"] == "pipe" and rows[0]["width_m"] == rows[0]["height_m"]
    assert rows[2]["shape"] == "box" and "arch" in rows[2]["note"]          # arch -> box
    assert abs(rows[2]["width_m"] - 9.75 * FT) < 1e-3


@register
def culvert_guesses_near_structures_are_dropped():
    from nd_breach_runner.core import culvert_guess as cg
    from nd_breach_runner.core import structures as S
    s = S.Structure("culvert", np.array([(0.0, -5.0), (0.0, 5.0)]), 1.0, 1.0)
    kept, dropped = cg.drop_near_structures([_guess(30, 0), _guess(300, 0)], [s], 75.0)
    assert len(kept) == 1 and len(dropped) == 1 and kept[0]["line"][0][0] == 294.0
    kept, dropped = cg.drop_near_structures([_guess(30, 0)], [s], 10.0)
    assert len(kept) == 1


@register
def culvert_matched_rows_become_structures_and_csv():
    from nd_breach_runner.core import culvert_guess as cg
    inv = [_nddot("09-116-20.0", 0.0, 0.0, "TRIPLE, 10X4X30' RCB")]
    rows, _ = cg.match_inventory([_guess(0, 0), _guess(900, 0)], inv)
    ss = cg.matched_structures(rows)
    assert len(ss) == 1 and ss[0].barrels == 3 and ss[0].surveyed and \
        ss[0].source == "NDDOT 09-116-20.0"
    assert ss[0].corridor_width(1.0) > 3 * 10 * FT
    p = os.path.join(tempfile.mkdtemp(), "c.csv")
    cg.write_candidates_csv(p, rows)
    lines = open(p).read().splitlines()
    assert lines[0].split(",")[5] == "barrels" and lines[0].split(",")[8] == "match_m"
    assert lines[1].split(",")[6] == "NDDOT 09-116-20.0" and lines[1].split(",")[8] == "0.0"
    assert lines[2].split(",")[7] == "no" and len(lines[2].split(",")) == 14


@register
def culvert_parser_is_the_hazard_parser():
    """nddot_text.py is a verbatim copy of ND Dam Hazard's parser functions."""
    hz = os.path.join(HERE, "..", "..", "DamSafety_Hazard", "runnerculverts.py")
    if not os.path.isfile(hz):
        return
    own = os.path.join(HERE, "..", "core", "nddot_text.py")

    def defs(path):
        t = ast.parse(open(path, encoding="utf-8").read())
        out = {}
        for n in t.body:
            if isinstance(n, ast.FunctionDef):
                out[n.name] = ast.dump(n)
            elif isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name):
                out[n.targets[0].id] = ast.dump(n)
        return out
    a, b = defs(own), defs(hz)
    for k, v in a.items():
        assert b.get(k) == v, "nddot_text.{} differs from DamSafety_Hazard/runnerculverts.py".format(k)


@register
def culvert_inventory_real_gpkgs():
    """The Absaraka-area NDDOT and NTAD NBI extracts (skipped when absent)."""
    from nd_breach_runner.core import culvert_guess as cg
    nd = os.path.join(SOURCES, "nddot_bridge_inventory.gpkg")
    nb = os.path.join(SOURCES, "ntad_nbi.gpkg")
    if not (os.path.isfile(nd) and os.path.isfile(nb)):
        return
    a = cg.read_inventory_gpkg(nd)
    assert len(a) == 16 and sum(r["kind"] == "culvert" for r in a) == 8
    swan = [r for r in a if r["id"] == "09-118-21.1"][0]
    assert (swan["span_ft"], swan["rise_ft"], swan["barrels"], swan["shape"]) == (16.0, 8.0, 2, "box")
    assert all(r["span_ft"] for r in a if r["kind"] == "culvert")
    b = cg.read_inventory_gpkg(nb)
    assert len(b) == 16 and sum(r["kind"] == "culvert" for r in b) == 7   # 43B = 19
    seen = []
    cg.read_inventory_gpkg(nd, lambda srs: seen.append(srs) or (lambda x, y: (x + 1.0, y)))
    assert seen == [5070]
    # a guess 50 m from NDDOT 09-116-15.0 takes its 2 x 10 x 4 ft RCB
    r = [x for x in a if x["id"] == "09-116-15.0"][0]
    rows, _ = cg.match_inventory([_guess(r["x"] + 50.0, r["y"])], a + b, 75.0)
    assert rows[0]["source"] == "NDDOT 09-116-15.0" and rows[0]["barrels"] == 2
    assert abs(rows[0]["height_m"] - 4 * FT) < 1e-3


class _AveragedRoad(object):
    """A 1 m terrain (prairie falling east 2 m/km, a channel along y = 0 running east, a
    10 m wide 3 m high road embankment along x = 0) read at any cell by AVERAGING the
    1 m cells - what an 'average' DEM resample hands the engine."""

    def read(self, bounds, cell):
        from nd_breach_runner.core.grid import Grid
        xmin, ymin, xmax, ymax = bounds
        k = int(round(cell))
        nx, ny = int(round((xmax - xmin) / cell)), int(round((ymax - ymin) / cell))
        xs = xmin + np.arange(nx * k) + 0.5
        ys = ymax - np.arange(ny * k) - 0.5
        X, Y = np.meshgrid(xs, ys)
        z = 300.0 - 0.002 * X + 0.01 * np.abs(Y) - np.where(np.abs(Y) < 6, 1.0, 0.0)
        z = z + np.where(np.abs(X) <= 5.0, 3.0, 0.0)
        z = z.reshape(ny, k, nx, k).mean(axis=(1, 3))
        return Grid(z, xmin, ymax, float(cell))


def _matched_burn_case():
    from nd_breach_runner.core import culvert_guess as cg
    road = [(0.0, -500.0), (0.0, 500.0)]
    chan = [(-600.0, 0.0), (600.0, 0.0)]                 # flow path, upstream first
    guesses = cg.guess_crossings([road], [chan])
    assert len(guesses) == 1
    inv = [_nddot("09-118-21.1", 5.7, 0.0, "DOUBLE 16X8X52' PRECAST RCB")]
    rows, _ = cg.match_inventory(guesses, inv, 75.0)
    assert rows[0]["match_m"] is not None
    return rows


@register
def culvert_burn_changes_the_engine_dem_at_40m():
    """Regression (PR #30 run 2): the burn must change the DEM LISFLOOD gets at its cell size.
    Before the fix the cut line was the guess's 12 m line across the road chord; at 40 m
    both default inverts were the embankment cell itself and nothing was cut."""
    from nd_breach_runner.core import culvert_guess as cg
    from nd_breach_runner.core import structures as S
    from nd_breach_runner.core import workflow as W
    rows = _matched_burn_case()
    terr = _AveragedRoad()
    bounds = (-400.0, -400.0, 400.0, 400.0)
    base = W.model_dem(terr, [], "lisflood", bounds, 40.0)
    ss = cg.matched_structures(rows)
    assert len(ss) == 1
    notes = cg.lidar_inverts(ss, terr.read, 1.0)           # as the workflow does, before resampling
    assert ss[0].invert_up_m is not None and ss[0].invert_up_m > ss[0].invert_dn_m, notes
    assert ss[0].invert_up_m < 300.0 + 0.002 * 30             # the channel bed, not the road
    line = np.asarray(ss[0].line)
    assert abs(line[0][1]) < 1e-6 and abs(line[-1][1]) < 1e-6 and line[0][0] < line[-1][0], \
        "the cut must follow the channel, upstream end first"
    for cell in (40.0, 10.0):
        b0 = base if cell == 40.0 else W.model_dem(terr, [], "lisflood", bounds, cell)
        got = W.model_dem(terr, ss, "lisflood", bounds, cell)
        assert got.data.shape == b0.data.shape
        diff = b0.data - got.data
        assert (diff >= -1e-9).all(), "a cut never raises ground"
        r, c = b0.cell_of(0.0, 0.0) if hasattr(b0, "cell_of") else (
            int((b0.y0 - 0.0) // cell), int((0.0 - b0.x0) // cell))
        assert diff[r, c] > 0.3, (cell, diff[r, c])        # the road cell on the channel is cut
        assert got.data[r, c] <= ss[0].invert_up_m + 1e-6
        # only cells near the crossing move
        moved = np.argwhere(diff > 1e-9)
        assert (np.abs(moved[:, 0] - r) <= 1).all() and (np.abs(moved[:, 1] - c) <= 3).all()
    # the old line (12 m across the road chord, no lengthening) cuts nothing at 40 m
    old = S.Structure("culvert", np.asarray(rows[0]["line"]), 4.877, 2.438, 2, "box")
    assert np.array_equal(W.model_dem(terr, [old], "lisflood", bounds, 40.0).data, base.data)
    # ANUGA: culverts go to the culvert operator (not the DEM); its ends lie beyond the road
    specs, notes = S.anuga_culverts(ss, base)
    assert len(specs) == 1 and not notes, notes
    (x0, _y0), (x1, _y1) = specs[0]["end_points"]
    assert x0 < -5.0 and x1 > 5.0 and specs[0]["invert_elevations"][0] > \
        specs[0]["invert_elevations"][1] - 1.0


_MIN_REPORT = {
    "hydrograph": [], "policy": [], "run_finished": [], "tool": [],
    "inflow_point": {"xy": [0.0, 0.0], "how": "given"},
    "lisflood_runs": [{"run": 1, "bounds": [0.0, 0.0, 4000.0, 4000.0], "size_mi": [2.49, 2.49],
                       "cells": 10000, "dem_coverage": 1.0, "sim_time_hr": 10.0,
                       "runtime_min": 0.2, "edge_max_depth_ft": {"N": 0.0, "S": 0.0, "W": 0.0,
                                                                 "E": 0.0},
                       "edges_over_tolerance": [], "latest_arrival_hr": 9.0,
                       "stats": {"cells": 10000, "cell_size_m": 40.0, "sim_time_hr": 10.0,
                                 "max_depth_ft": 9.0, "max_velocity_fps": 11.0,
                                 "flooded_acres": 1000.0, "peak_inflow_cfs": 25000.0,
                                 "max_volume_error_pct": 0.0, "worst_interval_error_pct": 0.0,
                                 "injected_acft": 1600.0, "in_domain_acft": 1400.0,
                                 "velocity_written": True}}],
    "outputs": {"final": {"depth": "d.tif", "velocity": "v.tif", "arrival": "a.tif",
                          "wse": "w.tif"}},
    "settings": {"edge_tolerance_ft": 1.0, "expand_step_mi": 3.0},
    "summary": {"dam_id": "T1", "final_model": "LISFLOOD-FP", "peak_breach_cfs": 25000.0,
                "breach_method": "piping", "storage_at_failure_acft": 1600.0,
                "flooded_acres": 1000.0, "flooded_acres_over_arrival_depth": 900.0,
                "max_depth_ft": 9.0, "max_velocity_fps": 11.0, "latest_arrival_hr": 9.0,
                "lisflood_runs": 1, "domain_mi": [2.49, 2.49], "units": "ft"}}


@register
def culvert_matches_section_in_run_report():
    import copy
    from nd_breach_runner.core import workflow as W
    r = copy.deepcopy(_MIN_REPORT)
    assert "## Culvert matches" not in W.render_report(r)
    r["culvert_matches"] = [{"name": "NDDOT 09-118-21.1 (matched guess: road 1)",
                             "source": "NDDOT 09-118-21.1", "match_m": 5.7,
                             "width_m": 16 * FT, "height_m": 8 * FT, "barrels": 2,
                             "shape": "box", "burned": True}]
    md = W.render_report(r)
    assert "## Culvert matches" in md
    assert ("| NDDOT 09-118-21.1 (matched guess: road 1) | NDDOT 09-118-21.1 | 5.7 | 16.0 | "
            "8.0 | 2 | box | yes |  |") in md, md
    r["culvert_matches"][0].update(invert_up_m=300.0 * FT, invert_dn_m=299.5 * FT)
    assert "| box | yes | 300.0 / 299.5 |" in W.render_report(r)


CLP = os.path.join(SOURCES, "research", "nddot_centerline_pipes.gpkg")
CLP_ABS = os.path.join(SOURCES, "research", "nddot_centerline_pipes_absaraka.csv")


def _clp(desc, d, a=0, length=56, le=938.0, re_=937.5, loc="NO ERROR", rid=276, meas=0.2466,
         xy=(0.0, 0.0)):
    from nd_breach_runner.core import culvert_guess as cg
    return cg.inventory_record({"ROUTE_ID": rid, "ROUTE_MEASURE": meas, "PIPE_DESC": desc,
                                "DIAMETER_E": d, "ARCH_E": a, "LENGTH_E": length,
                                "LEFT_ELEV_E": le, "RIGHT_ELEV_E": re_, "LOC_ERROR": loc}, xy)


@register
def clp_records_map_shape_barrels_sizes_and_plan_inverts():
    r = _clp("REINFORCED CONCRETE PIPE                ", 42)
    assert r["source"] == "NDDOT CLP" and r["id"] == "276:0.2466" and r["kind"] == "culvert"
    assert (r["shape"], r["barrels"], r["span_ft"], r["rise_ft"]) == ("pipe", 1, 3.5, 3.5)
    assert r["barrel_len_ft"] == 56.0 and r["clp_inverts_ft"] == (938.0, 937.5)
    assert r["invert_datum"] == "plan" and r["loc_error"] is False
    assert _clp("DOUBLE ROW OF CONCRETE PIPE", 36)["barrels"] == 2
    assert _clp("TRIPLE ROW OF STEEL PIPE", 36)["barrels"] == 3
    assert _clp("FOUR ROW CONCRETE PIPE", 36)["barrels"] == 4
    a = _clp("REINFORCED CONCRETE PIPE ARCH", 36, 58)          # rise-first: span = larger
    assert a["shape"] == "arch" and abs(a["span_ft"] - 58 / 12.0) < 1e-3 and \
        abs(a["rise_ft"] - 3.0) < 1e-3
    d = _clp("DBL REINFORCED CONCRETE PIPE ARCH", 73, 45)
    assert d["barrels"] == 2 and d["shape"] == "arch"
    assert _clp("ELLIPTICAL R. C. P.", 30, 19)["shape"] == "arch"
    assert _clp("DOUBLE BOX CONCRETE PIPE", 48, 36)["shape"] == "box"
    c = _clp("CATTLE PASS", 48, 72)                            # inches
    assert (c["shape"], c["span_ft"], c["rise_ft"]) == ("box", 4.0, 6.0)
    c = _clp("CATTLE PASS", 5, 0)                              # feet (NDDOT's way)
    assert (c["shape"], c["span_ft"], c["rise_ft"]) == ("box", 5.0, 5.0)
    assert _clp("CATTLE PASS", 0, 0) is None                   # no size: skipped
    assert _clp("REINFORCED CONCRETE PIPE", 42, loc="ROUTE WITHOUT MEASURE")["loc_error"]
    assert "clp_inverts_ft" not in _clp("REINFORCED CONCRETE PIPE", 42, le=0, re_=99999.9)


@register
def clp_priority_bridge_inventory_then_clp_then_nbi_then_loc_error():
    from nd_breach_runner.core import culvert_guess as cg
    nbi = cg.inventory_record({"STRUCTURE_NUMBER_008": "9", "STRUCTURE_TYPE_043B": "19",
                               "MAIN_UNIT_SPANS_045": 2, "MAX_SPAN_LEN_MT_048": 3.0}, (1.0, 0.0))
    bad = _clp("REINFORCED CONCRETE PIPE", 60, loc="ROUTE WITHOUT MEASURE", rid=1, meas=1.0,
               xy=(0.5, 0.0))
    good = _clp("DOUBLE ROW OF CONCRETE PIPE", 42, rid=276, meas=0.2466, xy=(50.0, 0.0))
    nd = _nddot("09-118-21.1", 70.0, 0.0, "DOUBLE 16X8X52' PRECAST RCB")
    pick = lambda inv: cg.match_inventory([_guess(0, 0)], inv, 75.0)[0][0]
    assert pick([bad, nbi, good, nd])["source"] == "NDDOT 09-118-21.1"
    m = pick([bad, nbi, good])
    assert m["source"] == "NDDOT CLP 276:0.2466" and m["barrels"] == 2 and m["shape"] == "pipe"
    assert abs(m["width_m"] - 3.5 * FT) < 1e-3 and m["width_m"] == m["height_m"]
    assert m["plan_inverts_ft"] == (938.0, 937.5) and "plan-datum" in m["note"]
    assert m["barrel_len_m"] == round(56 * FT, 2)
    assert pick([bad, nbi])["source"].startswith("NBI")
    assert pick([bad])["source"] == "NDDOT CLP 1:1"
    # burn: plan-datum inverts are NOT used (structure inverts stay for the LiDAR)
    s = cg.matched_structures([m])[0]
    assert s.invert_up_m is None and s.invert_dn_m is None


@register
def clp_absaraka_sample_csv():
    """The 188 CLP points around Absaraka (research/ csv, lon/lat): all read, and a guess at one
    of them takes its size."""
    import csv
    from nd_breach_runner.core import culvert_guess as cg
    if not os.path.isfile(CLP_ABS):
        return
    from nd_breach_runner.damsuite import suitegpkg as G
    recs = []
    with open(CLP_ABS, encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            xy = G.to_5070(float(row["X"]), float(row["Y"]))
            r = cg.inventory_record(row, xy)
            if r is not None:
                recs.append(r)
    assert len(recs) >= 180 and all(r["source"] == "NDDOT CLP" for r in recs)
    first = [r for r in recs if r["id"] == "276:0.2466"][0]
    assert first["shape"] == "pipe" and first["span_ft"] == 3.5 and first["barrel_len_ft"] == 56.0
    rows, _ = cg.match_inventory([_guess(first["x"] + 10.0, first["y"])], recs, 75.0)
    assert rows[0]["source"].startswith("NDDOT CLP 276:0.24"), rows[0]["source"]


@register
def clp_statewide_gpkg_bbox_filter_is_fast():
    """The statewide layer (22,555 points, EPSG:4269): a bbox filters before parsing."""
    import time
    from nd_breach_runner.core import culvert_guess as cg
    from nd_breach_runner.damsuite import suitegpkg as G
    if not os.path.isfile(CLP):
        return
    bb = (-97.6, 46.85, -97.2, 47.10)                       # the research/ Absaraka window
    t0 = time.time()
    seen = []
    recs = cg.read_inventory_gpkg(CLP, lambda srs: seen.append(srs) or
                                  (lambda x, y: G.to_5070(x, y)), lambda srs: bb)
    dt = time.time() - t0
    assert seen == [4269]
    assert 150 <= len(recs) <= 200, len(recs)               # report.md: 188 points
    assert dt < 5.0, dt
    assert all(r["source"] == "NDDOT CLP" for r in recs)


@register
def clp_plan_datum_inverts_reach_run_report_json_and_md():
    import copy
    import json
    from nd_breach_runner.core import culvert_guess as cg
    from nd_breach_runner.core import workflow as W
    clp = _clp("REINFORCED CONCRETE PIPE", 42, le=938.0, re_=937.5, xy=(10.0, 0.0))
    nd = _nddot("09-118-21.1", 900.0, 0.0, "DOUBLE 16X8X52' PRECAST RCB")
    rows, _ = cg.match_inventory([_guess(0, 0), _guess(900, 0)], [clp, nd], 75.0)
    ents = cg.report_entries(rows, True)
    assert len(ents) == 2
    e = [x for x in ents if x["source"].startswith("NDDOT CLP")][0]
    assert e["plan_inverts_ft"] == [938.0, 937.5] and e["invert_datum"] == "plan"
    assert "plan-datum" in e["plan_datum_note"] and e["burned"] is True
    other = [x for x in ents if x["source"] == "NDDOT 09-118-21.1"][0]
    assert "plan_inverts_ft" not in other
    json.dumps(ents)                                          # run_report.json-safe
    r = copy.deepcopy(_MIN_REPORT)
    r["culvert_matches"] = ents
    md = W.render_report(r)
    assert "Plan-datum inverts" in md
    assert "- NDDOT CLP 276:0.2466: left 938.0 ft, right 937.5 ft (datum: plan)." in md, md
    r["culvert_matches"] = [other]
    assert "Plan-datum inverts" not in W.render_report(r)
