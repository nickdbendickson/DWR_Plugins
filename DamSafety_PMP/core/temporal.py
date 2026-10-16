"""
Port of the temporal-distribution functions of ND_PMP_Tool.py (temporalDistLS2, temporalDistLS6,
temporalDist_24hr/_48hr/_72hr, temporalDistControlStorm_06hr/_24hr, temporalCritStacked)
plus the hyetograph / summary CSV export consumed by DamSafety_Runner (nd_breach_runner).

Every function returns a list of Pattern objects with CUMULATIVE depths (inches) per timestep,
exactly as the original writes them into its output gdb tables.
NOT ported: checkTemporal (the PASS/FAIL 'Temporal_Distribution_Check' table).
"""
import os
import csv
from collections import Counter
from dataclasses import dataclass
from heapq import nlargest

import numpy as np
from osgeo import ogr

from .pmp import py2round, _gdb

TYPE_LABEL = {"General": "general", "Local": "local", "CoolSeason": "cool-season"}
TYPE_PREFIX = {"General": "GS", "Local": "LS", "CoolSeason": "CS"}


@dataclass
class Pattern:
    storm_type: str      # General / Local / CoolSeason
    duration: str        # "06", "24", ...
    name: str            # temporal distribution name (original field name / suffix)
    minutes: list        # end-of-timestep minutes
    cum: list            # cumulative depth, inches
    source_table: str


def read_table(home, name):
    ds = ogr.Open(_gdb(home, "Non_Storm_Data.gdb"))
    lyr = ds.GetLayerByName(name)
    d = lyr.GetLayerDefn()
    cols = [d.GetFieldDefn(i).GetName() for i in range(d.GetFieldCount())]
    rows = [{c: f.GetField(c) for c in cols} for f in lyr]
    return cols, rows


def _factor_fields(cols, prefix):
    return [c for c in cols if c.upper().startswith(prefix)]   # arcpy.ListFields(table, "LS*") is case-insensitive


def ls2(home, P):
    cols, rows = read_table(home, "LS_TEMPORAL_DISTRIBUTIONS_02HR")
    one, two = P["01"], P["02"]
    out = []
    for fld in _factor_fields(cols, "LS"):
        acc = 0.0; cum = []; mins = []
        for r in rows:
            t = r["TIMESTEP"]
            if t <= 6:
                acc += (two - one) / 12
            elif t <= 18:
                acc += one * r[fld]
            elif t <= 24:
                acc += (two - one) / 12
            else:
                continue
            cum.append(acc); mins.append(r["MINUTE"])        # NB: original does not round LS 2-hr values
        out.append(Pattern("Local", "02", fld, mins, cum, "LS_TEMPORAL_DISTRIBUTIONS_02HR"))
    return out


def _scaled(home, table, prefix, stype, dur, depth):
    cols, rows = read_table(home, table)
    return [Pattern(stype, dur, fld, [r["MINUTE"] for r in rows],
                    [py2round(r[fld] * depth, 3) for r in rows], table)
            for fld in _factor_fields(cols, prefix)]


def ls6(home, P):
    return _scaled(home, "LS_TEMPORAL_DISTRIBUTIONS_06HR", "LS", "Local", "06", P["06"])


def d24(home, stype, P):
    if stype not in ("General", "CoolSeason"):
        return []
    p = TYPE_PREFIX[stype]
    return _scaled(home, "%s_TEMPORAL_DISTRIBUTIONS_24HR" % p, p, stype, "24", P["24"])


def _multi_day(home, stype, P, hours):
    """48-hr and 72-hr patterns: even ramp of residual depth before/after the largest 24 h (original logic)."""
    if stype not in ("General", "CoolSeason"):
        return []
    p = TYPE_PREFIX[stype]
    table = "%s_TEMPORAL_DISTRIBUTIONS_%dHR" % (p, hours)
    cols, rows = read_table(home, table)
    L24 = P["24"]
    if hours == 48:
        second = (P["48"] - L24) / 2; third = second; a, b, n1, n3 = 48, 144, 48, 48
    else:
        second = P["48"] - L24; third = P["72"] - P["48"]; a, b, n1, n3 = 96, 192, 96, 96
    out = []
    for fld in _factor_fields(cols, p):
        acc = 0.0; vals = [None] * len(rows)
        for i, r in enumerate(rows):                 # pass 1: TIMESTEP <= a
            if r["TIMESTEP"] <= a:
                acc += second / n1; vals[i] = py2round(acc, 3)
        for i, r in enumerate(rows):                 # pass 2: a < TIMESTEP <= b
            if a < r["TIMESTEP"] <= b:
                acc = L24 * r[fld] + second; vals[i] = py2round(acc, 3)
        for i, r in enumerate(rows):                 # pass 3: rows whose value IS NULL
            if vals[i] is None and r[fld] is None:
                acc += third / n3; vals[i] = py2round(acc, 3)
            elif vals[i] is None:
                vals[i] = r[fld]                     # original leaves non-null untouched factors as-is
        out.append(Pattern(stype, "%02d" % hours, fld, [r["MINUTE"] for r in rows], vals, table))
    return out


def controlling(home, stype, dur, P, drivers):
    """temporalDistControlStorm_06hr/_24hr: patterns of the storms that control >=1 grid point."""
    table = "CONTROLLING_STORM_TEMPORAL_DISTRIBUTIONS_%s" % dur
    cols, rows = read_table(home, table)
    dist = sorted(set(drivers))
    have = [d for d in dist if d in cols]
    if not have:
        return [], dist
    missing = [d for d in dist if d not in cols]
    out = [Pattern(stype, dur, "%s (%d-hr)" % (s, int(dur)), [r["MINUTE"] for r in rows],
                   [py2round(r[s] * P[dur], 3) for r in rows], table) for s in have]
    return out, missing


def crit_stacked(stype, dur, P, timestep):
    """temporalCritStacked(). Uses PMP depths by duration NAME (original indexes by field position, which is
    equivalent only when the run used the default duration list starting 01,02,...)."""
    if stype == "Local" and dur == "06":
        fld = "LS_06_HOUR_%dMIN_CRIT_STACKED" % timestep
    elif stype in ("General", "CoolSeason"):
        fld = "%s_%s_HOUR_%dMIN_CRIT_STACKED" % (TYPE_PREFIX[stype], dur, timestep)
    else:
        return []
    keys = {"06": [1, 2, 3, 4, 5, 6], "12": [1, 2, 3, 4, 5, 6, 12], "24": [1, 2, 3, 4, 5, 6, 12, 24]}.get(dur)
    if keys is None or any("%02d" % k not in P for k in keys):
        return []
    n = int(dur) * 60 // timestep
    xp = [0] + [k * n / int(dur) for k in keys]
    fp = [0] + [P["%02d" % k] for k in keys]
    y = np.interp(np.arange(0, n + 1), xp, fp)
    inc = list(np.diff(y))
    inc = [float(y[0])] + inc            # original: inc[0] = y[0]-0 = 0, then diffs (len n+1)
    periods = int(dur); plen = 60 // timestep
    ranks = [1 + i * plen for i in range(periods)]
    order = [ranks.pop(0)]
    for _ in range(n // plen):
        if ranks: order.insert(0, ranks.pop(0))
        if ranks: order.insert(0, ranks.pop(0))
        if ranks: order.append(ranks.pop(0))
    order += [order.pop(0)]
    if order[0] == max(order):
        raise RuntimeError("critically-stacked rank ordering hit a code path that crashes the original (pop(max))")
    oinc = []
    for k in range(periods):
        v = nlargest(order[k], inc)[-1]
        oinc += [v] * plen
    cum, prev = [], 0.0
    for v in oinc:
        cum.append(py2round(v + prev, 3)); prev = v + prev
    return [Pattern(stype, dur, fld, [timestep * (i + 1) for i in range(n)], cum, "computed")]


def build_patterns(home, stype, durs, P, drivers, log=None):
    """Same call order/conditions as the runTemporal block of pmpAnalysis()."""
    pats, notes = [], []
    for d in durs:
        try:
            if stype == "Local" and d == "02":
                pats += ls2(home, P)
            if d == "06":
                if stype == "Local":
                    pats += ls6(home, P)
                cp, miss = controlling(home, stype, "06", P, drivers["06"])
                pats += cp
                if miss: notes.append("06-hr controlling storm(s) without temporal pattern: %s" % ", ".join(miss))
            if stype == "Local" and d == "06":
                pats += crit_stacked(stype, "06", P, 5)
            if d == "24":
                pats += d24(home, stype, P)
                pats += crit_stacked(stype, "24", P, 15)
                cp, miss = controlling(home, stype, "24", P, drivers["24"])
                pats += cp
                if miss: notes.append("24-hr controlling storm(s) without temporal pattern: %s" % ", ".join(miss))
            if d == "48":
                pats += _multi_day(home, stype, P, 48)
            if d == "72":
                pats += _multi_day(home, stype, P, 72)
        except KeyError as e:
            notes.append("%s %s-hr temporal distribution skipped: PMP for duration %s not computed" % (stype, d, e))
        except RuntimeError as e:
            notes.append("%s %s-hr: %s" % (stype, d, e))
    for n in notes:
        if log: log(n)
    return pats, notes


# ----------------------------------------------------------------------------- exports for nd_breach_runner
def storm_id(p: Pattern):
    import re
    nm = re.sub(r"[^A-Za-z0-9]+", "_", p.name).strip("_")
    return "%s_%shr_%s" % (TYPE_LABEL[p.storm_type].replace("-", ""), p.duration, nm)


def write_hyetograph(out_dir, p: Pattern, area, basin_avg, snowmelt=None):
    hd = os.path.join(out_dir, "hyetographs"); os.makedirs(hd, exist_ok=True)
    path = os.path.join(hd, storm_id(p) + ".csv")
    steps = np.diff([0.0] + list(p.minutes))
    ts = float(steps[0]) / 60.0 if len(steps) else None
    with open(path, "w", newline="") as fh:
        fh.write("# storm_type: %s\n" % TYPE_LABEL[p.storm_type])
        fh.write("# duration_hr: %d\n" % int(p.duration))
        fh.write("# timestep_hr: %s\n" % _fmt(ts))
        fh.write("# area_mi2: %s\n" % _fmt(area))
        fh.write("# basin_avg_depth_in: %s\n" % _fmt(basin_avg))
        fh.write("# temporal_distribution: %s\n" % p.name)
        fh.write("# snowmelt_in: %s\n" % ("" if snowmelt is None else _fmt(snowmelt)))
        fh.write("# source: ND PMP QGIS prototype (port of AWA ND_PMP_Tool.py); source_table=%s\n" % p.source_table)
        w = csv.writer(fh)
        w.writerow(["time_hr", "incr_depth_in", "cum_depth_in"])
        prev = 0.0
        for m, c in zip(p.minutes, p.cum):
            w.writerow([_fmt(m / 60.0), _fmt(round(c - prev, 6)), _fmt(c)])
            prev = c
    return path


def controlling_storm(drivers):
    """Storm controlling the most grid points for this duration (ties -> alphabetical); 'STORM' = none."""
    c = Counter(drivers)
    best = max(c.values())
    return sorted(k for k, v in c.items() if v == best)[0]


def write_summary(out_dir, rows):
    path = os.path.join(out_dir, "basin_avg_depth_by_duration.csv")
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["duration_hr", "depth_in", "storm_type", "controlling_storm"])
        for r in rows:
            w.writerow(r)
    return path


def _fmt(v):
    if v is None:
        return ""
    f = float(v)
    return ("%.6f" % f).rstrip("0").rstrip(".") if f != int(f) else str(int(f))
