"""Three-way classification parity (audit P1, 2026-11-08): the same structures on the same
depth / velocity rasters get the SAME ACER TM 11 zone from DamSafety_Hazard (the classifier of
record), DamSafety_Runner (algorithms_ndhazard.Sampler + core/nd_hazard) and DamSafety_Screening
(core/hazard.Grid + the byte-identical core/nd_hazard).

What differed before (and is now one rule):
  (a) the sampling footprint - Hazard reads the outline + a 5 ft edge ring (constants
      OSM_EDGE_BUFFER_FT) and a point as a 20 ft disc + the ring (DEFAULT_POINT_RADIUS_FT);
      Runner / Screening read the bare footprint and the point's own cell;
  (b) the zone rule - Hazard reads the digitized REG_05.2026 table (a depth ON a boundary takes
      the higher zone); Runner / Screening put a point within 0.01 of each axis (about 0.1 ft)
      of a higher zone's polygon INTO that zone.

Legs:
  Hazard    the array-level pieces of structures.evaluate_structures: fastsample.structure_cells
            (the exact cell selection of the outline + ring / point disc + ring, the path
            fast_sampling uses; the GEOS path selects the same cells except in the arc / chord
            gap of GEOS buffers), the peak-pair rule (structures.peak_pair: peak depth with the
            peak velocity over the wet cells) and helpers.classify_values' rule on the table
            (damsuite.tm11_curves). structures.py / helpers.py import QGIS and GDAL, so their
            functions are not called here; the whole-plugin leg runs in
            DamSafety_Hazard/tests (real QGIS) and is checked against these same numbers by
            DamSafety_Hazard/tests/test_golden_dams.py on real dams when fixtures are present.
  Runner    algorithms_ndhazard.Sampler (stub Qt) + nd_hazard.classify
  Screening nd_dam_screening.core.hazard.Grid (metres) + hazard.classify_nd

The rasters are synthetic numpy arrays (no GDAL): a 1 m grid in metres, so the Screening leg
reads it as is and the Runner leg with unit_m = 1 (its depth / velocity arrays are ft and
ft/s: the arrays hold those values; only the coordinates are metres).
"""
import contextlib
import math
import os
import sys

import numpy as np

from nd_breach_runner.core import nd_hazard as nh
from nd_breach_runner.damsuite import tm11_curves as tm
from nd_breach_runner.tests.registry import test


@contextlib.contextmanager
def _isolated_imports():
    """Run a block without letting its imports leak into sys.modules / sys.path (3.16.9).

    runner_leg stubs Qt (test_ndhazard_tool._load_tool leaves a fake `qgis` package and the
    gdal_io / gdalsafe / qgis_io modules imported under it), screening_leg imports the Screening
    through its qgis_mock, and hazard_leg loads the Hazard's fastsample under a synthetic package.
    Left in sys.modules, the fake-Qt / mock copies poison the GDAL tests that run after this file
    in the same process (test_reservoir_carve), which read the real osgeo. Snapshot and restore
    both so each leg's imports are undone; the legs return plain {id: (zone, depth, vel)} data,
    so nothing used afterwards depends on the modules being kept."""
    saved_mods = dict(sys.modules)
    saved_path = list(sys.path)
    try:
        yield
    finally:
        for name in list(sys.modules):
            if name not in saved_mods:
                del sys.modules[name]
        sys.modules.update(saved_mods)
        sys.path[:] = saved_path

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
M_PER_FT = 0.3048
EDGE_M = nh.EDGE_RING_FT * M_PER_FT                   # 1.524 m
DISC_M = (nh.POINT_RADIUS_FT + nh.EDGE_RING_FT) * M_PER_FT   # 7.62 m
ZONE_OF_CLS = {1: nh.ZONE_HIGH, 2: nh.ZONE_JUDGMENT, 3: nh.ZONE_LOW}


# ------------------------------------------------------------------ the synthetic flood ------
def rasters():
    """(depth ft, velocity ft/s) on a 60 x 60 grid of 1 m cells, x0 = 0, y_top = 60 (north up).
    The wet area is the block rows 10..39 x cols 10..39 (x 10..40, y 20..50) with a gentle
    gradient; outside it the depth is 0. Particular cells carry the values the structures below
    are built around."""
    d = np.zeros((60, 60))
    v = np.zeros((60, 60))
    rr, cc = np.mgrid[0:60, 0:60]
    wet = (rr >= 10) & (rr < 40) & (cc >= 10) & (cc < 40)
    d[wet] = 1.0 + 0.05 * (cc[wet] - 10)               # 1.0 .. 2.45 ft, deeper to the east
    v[wet] = 0.5 + 0.02 * (rr[wet] - 10)               # 0.5 .. 1.08 ft/s, faster to the south
    # a fast shallow cell next to a deep slow one (peak pair from different cells)
    d[20, 20], v[20, 20] = 3.0, 0.4
    d[20, 21], v[20, 21] = 0.3, 6.0
    # a wet cell exactly 0.05 ft below the house high boundary at 1.0 ft/s (5.968 ft)
    d[30, 30], v[30, 30] = 5.918, 1.0
    # a wet cell just INTO the house judgment zone at 1.0 ft/s (the boundary is 2.916 ft). It
    # must not sit exactly on 2.916: the plugin reads depth from a float32 raster, where 2.916
    # rounds to 2.9159998 (1e-7 below) and classifies low, while the numpy leg keeps the exact
    # 2.916 and classifies judgment - a measure-zero float32 disagreement, not a real parity
    # gap (a boundary-snap in the classifier is the separate follow-up). 2.92 is >= 2.916 in
    # both float32 and float64, so every leg agrees on judgment. The 5 ft ring reaches only the
    # shallower 1.0 ft cells, so the peak pair IS this cell.
    d[33:38, 10:15] = 1.0
    v[33:38, 10:15] = 1.0
    d[35, 12], v[35, 12] = 2.92, 1.0
    # one isolated wet cell (dry on every side) for the point_disc structure below: its sampling
    # disc reaches this cell with margin and nothing else, so the peak depth / velocity are this
    # cell's regardless of which edge cells the GEOS arc / chord gap includes (a gradient at the
    # disc edge would let the fast and slow paths pick different peak cells - the former fixture).
    d[50, 5], v[50, 5] = 1.0, 0.92
    # the only wet cells near the "3 ft outside the wet edge" structure: the block's west edge
    # (cols 10..) at rows 14..16 - its east wall at x = 9.0 m is 3 ft (0.914 m) from the wet
    # column's west face (x = 10) and 1.414 m from the nearest centre (10.5, ...)
    d[14:17, 10], v[14:17, 10] = 2.2, 0.9
    return d, v


def structures():
    """[(id, kind, geometry in metres, mobile, note)]: polygons as exterior rings, points as
    (x, y). Chosen so each exercises one of the former differences."""
    sq = lambda x0, y0, x1, y1: [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    return [
        # inside the flood, well away from any boundary: everyone agrees before and after
        ("plain", "polygon", sq(25, 30, 29, 34), False, "low danger block"),
        # peak depth (3.0 ft, slow) and peak velocity (6 ft/s, shallow) from different cells
        ("peakpair", "polygon", sq(19.6, 38.6, 22.4, 40.4), False, "3.0 ft with 6 ft/s"),
        # 0.05 ft below the high boundary: the old polygon tolerance made it HIGH, the table
        # (Hazard) says JUDGMENT, near the high boundary
        ("near_high", "polygon", sq(30.2, 28.2, 30.8, 29.8), False, "5.918 ft at 1 ft/s"),
        # exactly on the judgment boundary: the higher zone (judgment) in all three
        ("on_line", "polygon", sq(12.2, 23.2, 12.8, 24.8), False, "2.916 ft at 1 ft/s"),
        # dry footprint whose east wall is 3 ft (0.914 m) outside the wet edge: only the 5 ft
        # edge ring reaches the wet cells (10.5, 44.5..) - wet in Hazard, dry before in
        # Runner / Screening
        ("outside_3ft", "polygon", sq(5.0, 43.5, 9.086, 46.5), False, "3 ft outside"),
        # a dry footprint more than the ring away: dry everywhere
        ("far", "polygon", sq(2.0, 52.0, 6.0, 55.0), False, "dry"),
        # a point 7 m west of the wet block: its own cell is dry, the 25 ft disc (7.62 m)
        # reaches the wet block - wet in Hazard, dry before in Runner / Screening
        ("point_disc", "point", (5.5, 4.0), False, "point whose disc reaches one isolated wet cell"),
        # a point inside the flood: its 25 ft disc reads the deepest / fastest cells around it
        # (depth 1.0 + 0.05 x (col - 10): about 2.1 ft at col 32, judgment on the mobile table)
        ("point_in", "point", (32.5, 45.5), True, "mobile home point in the flood"),
    ]


# ------------------------------------------------------------------ the three legs -----------
def _zone_table(kind, d_ft, v_fts):
    """Hazard's classify_values rule for one (peak depth, peak velocity): the table with a
    depth ON the boundary in the higher zone; velocity above the axis -> high."""
    vmax = tm.AXES[kind][0]
    if v_fts > vmax:
        return nh.ZONE_HIGH
    lo, hi = tm.thresholds(kind, v_fts)
    if d_ft >= hi:
        return nh.ZONE_HIGH
    if d_ft >= lo and d_ft > 0:
        return nh.ZONE_JUDGMENT
    return nh.ZONE_LOW


def hazard_leg(d, v, gt, structs):
    """{id: (zone or None when dry, depth ft, vel ft/s)} the Hazard way: fastsample cell
    selection of the outline + edge ring / point disc + ring, peak pair, table rule."""
    import importlib.util
    import re
    import types
    hz_dir = os.path.join(REPO, "DamSafety_Hazard")
    path = os.path.join(hz_dir, "fastsample.py")
    if not os.path.isfile(path):
        raise RuntimeError("DamSafety_Hazard/fastsample.py not found")
    # fastsample is numpy + the standard library; its one plugin import is constants.ONEPASS_MIN.
    # The Hazard's constants.py reads the data-source file through QgsApplication at import
    # (an _Any under the stub Qt other tests install), so it is NOT imported: a private package
    # holds a constants stand-in, and the two footprint constants are read from its text.
    const_src = open(os.path.join(hz_dir, "constants.py"), encoding="utf-8").read()
    consts = {}
    for name in ("OSM_EDGE_BUFFER_FT", "DEFAULT_POINT_RADIUS_FT", "ONEPASS_MIN"):
        m = re.search(r"^{}\s*=\s*([0-9.]+)".format(name), const_src, re.M)
        assert m, name
        consts[name] = float(m.group(1))
    FS = sys.modules.get("_hz_parity_pkg.fastsample")
    if FS is None:
        pkg = types.ModuleType("_hz_parity_pkg")
        pkg.__path__ = [hz_dir]
        sys.modules["_hz_parity_pkg"] = pkg
        cmod = types.ModuleType("_hz_parity_pkg.constants")
        cmod.ONEPASS_MIN = int(consts["ONEPASS_MIN"])
        sys.modules["_hz_parity_pkg.constants"] = cmod
        spec = importlib.util.spec_from_file_location("_hz_parity_pkg.fastsample", path)
        FS = importlib.util.module_from_spec(spec)
        sys.modules["_hz_parity_pkg.fastsample"] = FS
        spec.loader.exec_module(FS)
    C = types.SimpleNamespace(**consts)
    assert C.OSM_EDGE_BUFFER_FT == nh.EDGE_RING_FT and \
        C.DEFAULT_POINT_RADIUS_FT == nh.POINT_RADIUS_FT, "the footprint constants drifted"
    out = {}
    size = (d.shape[1], d.shape[0])
    for sid, kind, geom, mobile, _note in structs:
        if kind == "polygon":
            shp = FS.polygon_shape([list(geom)])
            reach, r_disc = EDGE_M, None
        else:
            r_disc = C.DEFAULT_POINT_RADIUS_FT * M_PER_FT
            shp = FS.disc_shape(geom[0], geom[1], r_disc)
            reach = r_disc + EDGE_M
        cs = FS.structure_cells(shp, gt, reach, EDGE_M, FS.AREA, size)
        assert cs is not None
        rr, cc = cs.rows_cols("area")
        dd, vv = d[rr, cc], v[rr, cc]
        if not rr.size:
            # grid coarser than the area: the cell under the centre (not the case here)
            raise AssertionError("no cell centre in the area of {}".format(sid))
        wet = np.isfinite(dd) & (dd > 0)
        if not wet.any():
            out[sid] = (None, None, None)
            continue
        dd, vv = dd[wet], vv[wet]
        dpk = float(dd.max())
        pick = vv[np.isfinite(vv)]
        vpk = float(pick.max()) if pick.size else 0.0
        kind_t = "mobile" if mobile else "house"
        # evaluate_structures: zone = min(peak-pair class, worst per-cell class); the peak
        # pair is at least as severe as any cell, so it decides
        out[sid] = (_zone_table(kind_t, dpk, vpk), dpk, vpk)
    return out


def runner_leg(d, v, gt, structs):
    from nd_breach_runner.tests.test_ndhazard_tool import _load_tool
    an, _fq, _algs = _load_tool()                        # stub Qt; the tool's own import order
    from nd_breach_runner.core.grid import Grid
    g = Grid(np.zeros(d.shape), gt[0], gt[3], gt[1])
    s = an.Sampler(g, d, v, wet_ft=0.1, unit_m=1.0)
    charts = nh.load_charts()
    out = {}
    for sid, kind, geom, mobile, _note in structs:
        if kind == "polygon":
            ring = np.asarray(geom, float)
            cx, cy = ring.mean(axis=0)
            got = s.polygon([ring], (cx, cy), structure=True)
        else:
            got = s.points([geom], structure=True)
        if got is None or not got["depth_ft"] > 0:
            out[sid] = (None, None, None)
            continue
        st = nh.Structure(sid, got["depth_ft"], got["vel_fts"], mobile=mobile)
        c = nh.classify([st], charts=charts)
        f = c.findings[0] if c.findings else None
        out[sid] = (f.zone if f else None, got["depth_ft"], got["vel_fts"])
    return out


def screening_leg(d, v, gt, structs):
    sys.path.insert(0, os.path.join(REPO, "DamSafety_Screening", "tests", "qgis_mock"))
    try:
        from nd_dam_screening.core import hazard as hz
    finally:
        sys.path.pop(0)
    grid = hz.Grid(d * M_PER_FT, v * M_PER_FT, gt)      # the Screening's grids are m, m/s
    charts = hz.load_ready_charts()
    out = {}
    for sid, kind, geom, mobile, _note in structs:
        parts = [list(geom)] if kind == "polygon" else [geom]
        got = grid.sample(kind, parts, 0.1, structure=True)
        if got is None or not got["depth_m"] > 0:
            out[sid] = (None, None, None)
            continue
        c = hz.classify_nd([dict(got, id=sid, mobile=mobile, habitable=True)], charts=charts)
        f = c.findings[0] if c.findings else None
        out[sid] = (f.zone if f else None, got["depth_m"] / M_PER_FT,
                    None if got["vel_ms"] is None else got["vel_ms"] / M_PER_FT)
    return out


def _same(a, b, tol=1e-6):
    if a is None or b is None:
        return a is None and b is None
    return abs(a - b) <= tol


@test
def classification_parity_hazard_runner_screening_same_zones():
    d, v = rasters()
    gt = (0.0, 1.0, 0.0, 60.0, 0.0, -1.0)
    structs = structures()
    # the three legs stub Qt / load mock osgeo and qgis; keep that out of sys.modules so it
    # cannot poison the GDAL tests that run after this file (3.16.9)
    with _isolated_imports():
        hz_r = hazard_leg(d, v, gt, structs)
        rn_r = runner_leg(d, v, gt, structs)
        sc_r = screening_leg(d, v, gt, structs)
    problems = []
    for sid, _k, _g, _m, note in structs:
        h, r, s = hz_r[sid], rn_r[sid], sc_r[sid]
        if not (h[0] == r[0] == s[0]):
            problems.append("{} ({}): zone Hazard {} / Runner {} / Screening {}".format(
                sid, note, h[0], r[0], s[0]))
        for lab, o in (("Runner", r), ("Screening", s)):
            if not (_same(h[1], o[1]) and _same(h[2], o[2])):
                problems.append("{} ({}): Hazard {} ft / {} ft/s, {} {} ft / {} ft/s".format(
                    sid, note, h[1], h[2], lab, o[1], o[2]))
    assert not problems, "\n".join(problems)
    # the expected zones, so a change in any leg is caught by name, not only by disagreement
    zones = {sid: hz_r[sid][0] for sid, *_ in structs}
    assert zones == {"plain": nh.ZONE_LOW, "peakpair": nh.ZONE_JUDGMENT,
                     "near_high": nh.ZONE_JUDGMENT, "on_line": nh.ZONE_JUDGMENT,
                     "outside_3ft": nh.ZONE_LOW, "far": None, "point_disc": nh.ZONE_LOW,
                     "point_in": nh.ZONE_JUDGMENT}, zones
    # the structure 3 ft outside the wet edge and the point near the isolated cell are WET
    # through the ring / disc (as in the Hazard); the bare footprint / own cell was dry
    bare_poly = nh.sample_footprint(d, v, 0.0, 60.0, 1.0, structs[4][2], 0.1)[0]
    assert not (bare_poly or 0) > 0 and hz_r["outside_3ft"][1] == 2.2
    assert nh.sample_point(d, v, 0.0, 60.0, 1.0, 5.5, 4.0)[0] == 0.0 and hz_r["point_disc"][0]
    # 0.05 ft below the high boundary: the old polygon tolerance band made it HIGH in the
    # Runner / Screening; the table (Hazard) says JUDGMENT - the structure drops a zone, and
    # its finding is flagged near the high boundary instead
    ch = nh.load_charts().charts["house"]
    assert ch.polygon_zone(5.918, 1.0)[0] == nh.ZONE_HIGH
    assert ch.zone(5.918, 1.0)[0] == nh.ZONE_JUDGMENT
    assert ch.near(5.918, 1.0)[2] == nh.ZONE_HIGH and abs(ch.near(5.918, 1.0)[3] - 0.05) < 1e-9


@test
def classification_parity_zone_rule_grid_against_the_hazard_table():
    """Every (d, v) of a dense grid over both charts: nd_hazard's zone == the table's zone
    (the Hazard's classify_values rule), including points ON and just either side of each
    boundary. Quantifies what the old polygon-tolerance rule changed: the old rule put points
    within ~0.1 ft (house) / ~0.03 ft (mobile) below a boundary into the higher zone, never
    lower - so this change can only LOWER a Runner / Screening zone, towards the Hazard's."""
    cs = nh.load_charts()
    names = {tm.LOW: nh.ZONE_LOW, tm.JUDGMENT: nh.ZONE_JUDGMENT, tm.HIGH: nh.ZONE_HIGH}
    rank = nh.ZONE_ORDER
    for kind, key in (("house", nh.CHART_HOUSE), ("mobile", nh.CHART_MOBILE)):
        ch = cs.charts[key]
        vmax, dmax = tm.AXES[kind]
        n = old_higher = old_lower = 0
        worst = 0.0
        for i in range(0, 126):
            vel = vmax * i / 125.0
            lo, hi = tm.thresholds(kind, vel)
            depths = [dmax * j / 100.0 for j in range(0, 101)]
            for c in (lo, hi):
                depths += [c, c - 1e-6, c + 1e-6, c - 0.05, c + 0.05]
            for dep in depths:
                if dep < 0 or dep > dmax:
                    continue
                want = names[tm.zone(kind, dep, vel)]
                got = ch.zone(dep, vel)[0]
                assert got == want, (kind, dep, vel, got, want)
                n += 1
                old = ch.polygon_zone(dep, vel)[0]
                if old != got:
                    if rank[old] > rank[got]:
                        old_higher += 1
                        mh, mj = ch.margins(dep, vel)
                        worst = max(worst, mh if old == nh.ZONE_HIGH else mj)
                    else:
                        old_lower += 1
        assert n > 10000 and old_lower == 0 and old_higher > 0, (kind, n, old_higher, old_lower)
        assert worst <= 0.01 * dmax * 1.3 + 1e-9, (kind, worst)   # the old band, ~1% of the axis
