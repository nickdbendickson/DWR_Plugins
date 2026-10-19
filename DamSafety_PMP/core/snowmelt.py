"""Port of Snowmelt_Runoff_Tool.py (AWA ND PMP tool): temperature-index (degree-day) melt of the
100-yr SWE grid over a date window, with the rain-on-snow warming factor 1.15 (50 % on day 2 and
day rainDur+3, 100 % on days 3..rainDur+2), masked to the basin.

melt_d = DD_d*Cm, capped so the cumulative melt never exceeds the starting SWE.
Table values are basin means, rounded to 2 decimals like the original. SWE in the row is
the SWE before that day's melt (as the original).
Original-script defect noted, not reproduced: it advances the date with `datetime.timedelta`
but only imports `timedelta`; here the date is advanced with timedelta.
"""
import os
import csv
import glob
from datetime import date, timedelta
import numpy as np
from . import gridutil as G

TA_FACTOR = 1.15


def run_snowmelt(home, start, end, rain_dur_days, cm, basin_path, out_dir, sample_xy=None,
                 grid_output=False, basin_layer=None, log=print):
    log = log or (lambda *a: None)
    if isinstance(start, str): start = date.fromisoformat(start)
    if isinstance(end, str): end = date.fromisoformat(end)
    swe_gdb = os.path.join(home, "Input", "SWE.gdb"); t_gdb = os.path.join(home, "Input", "Temperature.gdb")
    swe0, sgt = G.read(G.gdb_raster(swe_gdb, "ND_Final_100yr_SWE_" + start.strftime("%m%d")))
    tnames = G.gdb_raster_names(t_gdb)
    geom = G.basin_wgs84(basin_path, basin_layer)
    os.makedirs(out_dir, exist_ok=True)
    tag = "%s_%s" % (os.path.splitext(os.path.basename(basin_path))[0], start.strftime("%m_%d"))
    rows, prow = [], []
    mask = None; melt = 0.0; acc = 0.0; swe = swe0.copy()
    ndays = (end - start).days + 1
    for i in range(ndays):
        day = i + 1; cur = start + timedelta(days=i); md = cur.strftime("%m%d")
        tn = next((x for x in tnames if x.endswith(md)), None)
        if tn is None:
            raise ValueError("no temperature raster for %s" % md)
        ta, tgt = G.read(G.gdb_raster(t_gdb, tn))
        h = min(ta.shape[0], swe0.shape[0]); w = min(ta.shape[1], swe0.shape[1])
        if tgt[:2] != sgt[:2] or abs(tgt[3] - sgt[3]) > 1e-9 or abs(tgt[5] - sgt[5]) > 1e-12:
            raise ValueError("temperature and SWE grids are not aligned")
        ta = ta[:h, :w]
        if mask is None:
            swe0 = swe0[:h, :w]; swe = swe[:h, :w]
            mask, touched = G.mask_on_grid(geom, sgt, (h, w))
            log("basin mask: %d cells%s" % (mask.sum(), " (all-touched fallback)" if touched else ""))
        f = 1.0
        if rain_dur_days:
            if day == 2 or day == rain_dur_days + 3: f = (TA_FACTOR - 1) / 2 + 1
            elif 3 <= day < rain_dur_days + 3: f = TA_FACTOR
        ta = ta * f
        dd = np.where(ta < 32.0, 0.0, ta - 32.0)
        swe = swe - melt
        ddc = dd * cm
        melt = np.where(ddc + acc >= swe0, swe0 - acc, ddc)
        acc = melt + acc
        mean = lambda a: round(float(np.nanmean(np.where(mask, a, np.nan))), 2)
        rows.append({"Day": cur.isoformat(), "Ta": mean(ta), "DegreeDays": mean(dd), "Cm": cm, "SWE": mean(swe),
                     "Melt": mean(melt), "MeltAccum": mean(acc), "TaFactor": f})
        if sample_xy is not None:
            c = int((sample_xy[0] - sgt[0]) / sgt[1]); r = int((sample_xy[1] - sgt[3]) / sgt[5])
            pv = lambda a: round(float(a[r, c]), 2)
            prow.append({"Day": cur.isoformat(), "Ta": pv(ta), "DegreeDays": pv(dd), "Cm": cm, "SWE": pv(swe),
                         "Melt": pv(melt), "MeltAccum": pv(acc), "TaFactor": f})
        if grid_output:
            gd = os.path.join(out_dir, "Snowmelt_" + tag); os.makedirs(gd, exist_ok=True)
            for nm, a in (("degDay", dd), ("SWE", swe), ("melt", melt), ("meltAccum", acc)):
                G.write_tif(os.path.join(gd, "%s_%s.tif" % (nm, md)), np.where(mask, a, np.nan), sgt)
    outs = {}
    for nm, rr in (("Basin_Ave_" + tag, rows), ("Sample_Point_" + tag, prow)):
        if rr:
            p = os.path.join(out_dir, nm + ".csv")
            with open(p, "w", newline="") as fh:
                w = csv.DictWriter(fh, list(rr[0].keys())); w.writeheader(); [w.writerow(x) for x in rr]
            outs[nm] = p
    # melt during the rain window (days 3 .. rainDur+2), and total
    rain_days = [r for k, r in enumerate(rows, 1) if rain_dur_days and 3 <= k < rain_dur_days + 3]
    res = {"basin_cells": int(mask.sum()), "days": ndays, "start_swe_in": rows[0]["SWE"],
           "total_melt_in": rows[-1]["MeltAccum"],
           "rain_window_melt_in": round(sum(r["Melt"] for r in rain_days), 2) if rain_days else None,
           "tables": outs, "rows": rows}
    return res


def fill_snowmelt(hyeto_dir, snowmelt_in, storm_types=("Cool-Season", "CoolSeason", "CS")):
    """Write `# snowmelt_in: <value>` into the headers of cool-season hyetograph CSVs (only that line changes)."""
    done = []
    for p in sorted(glob.glob(os.path.join(hyeto_dir, "*.csv"))):
        txt = open(p).read().split("\n")
        st = next((l.split(":", 1)[1].strip() for l in txt if l.startswith("# storm_type:")), "")
        if not any(s.lower() == st.lower() for s in storm_types) and not os.path.basename(p).lower().startswith("coolseason"):
            continue
        txt = [("# snowmelt_in: %s" % snowmelt_in) if l.startswith("# snowmelt_in:") else l for l in txt]
        open(p, "w").write("\n".join(txt)); done.append(p)
    return done
