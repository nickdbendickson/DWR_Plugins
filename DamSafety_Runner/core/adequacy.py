"""Spillway adequacy: can the dam pass its Inflow Design Flood (ND REG_05.2026)?

Per inflow hydrograph (e.g. the nd_pmp storms, <out>/hydrographs/<storm_id>.csv) the flood is
routed through the reservoir WITHOUT a breach, from the starting pool (default: the crest of
the spillway rating, REG_05.2026 Section 9), through the spillway rating (and outlets / gates),
with flow over the embankment crest once the pool is above the top of dam. The dam passes a
storm when the highest pool stays at or below the post-settlement top of dam minus the required
residual freeboard (Section 8.1.1). The largest multiple of each inflow the dam passes is found by
bisection.

Pass/fail comes from the routed peak pool only. flood.route_flood's FloodResult.failed is NOT
used: with its default trigger (the peak pool) it means "a breach was modelled at the peak
pool", not that the dam overtopped - it is True even with feet of freeboard left.

Same numerics as flood.route_flood's no-breach routing (its step 1): the midpoint step
flood._step every dt (30 s), the spillway rating flood.Spillway, outlets.q_total, the gate
schedule, and the embankment crest as a broad-crested weir (flood.CREST_C_US). The reservoir is
breach.Reservoir (storage and surface area at the top of dam and the dam height, as the flood
scenario uses) or a stage-storage table (elevation ft, storage ac-ft).

Not a design: results need an engineer's review. Pure Python + numpy, no QGIS.
"""
import csv
import os

import numpy as np

from . import breach as br
from . import flood as fl
from . import outlets as outmod
from .units import FT, ACFT, CFS

# REG_05.2026 Table 6.1 (minimum Inflow Design Flood and principal spillway capacity)
HAZARD_IDF = {
    "high": ("Probable Maximum Flood (PMF)", "1 percent AEP (100-year flood)"),
    "medium": ("0.1 percent AEP flood (1,000-year flood)", "2 percent AEP (50-year flood)"),
    "low": ("1 percent AEP flood (100-year flood)", "4 percent AEP (25-year flood)"),
}
HAZARDS = ("high", "medium", "low")
MIN_RESIDUAL_FT = {"high": 1.0, "medium": 1.0, "low": 3.0}   # Section 8.1.1 a / b
MAX_MULT = 2.0                  # largest inflow multiple searched
BISECT_TOL = 0.005


class AdequacyError(ValueError):
    pass


# ------------------------------------------------------------------ inputs --
def read_inflow_csv(path):
    """(t_s, q_cms, meta) from an inflow hydrograph CSV.

    nd_pmp: '#' header lines ('# key: value' kept in meta), a header row with time_hr and
    flow_cfs read BY NAME (v0.3.0 time_hr,flow_cfs,precip_in,excess_in; v0.2.0
    time_hr,precip_in,excess_in,flow_cfs), time_hr at the END of each step and no t=0 row - a
    zero-flow t=0 point is added. Plain files: time_hr,flow_cfs (header optional; without one
    the first two columns are time hr and flow cfs)."""
    meta, rows, header = {}, [], None
    with open(path, newline="", encoding="utf-8-sig") as f:
        for line in f:
            s = line.strip()
            if not s:
                continue
            if s.startswith("#"):
                body = s.lstrip("#").strip()
                if ":" in body:
                    k, v = body.split(":", 1)
                    meta[k.strip()] = v.strip()
                continue
            cells = next(csv.reader([s]))
            if header is None:
                try:
                    [float(c) for c in cells[:2]]
                    header = []                          # no header row
                except ValueError:
                    header = [c.strip().lower() for c in cells]
                    continue
            rows.append(cells)
    if not rows:
        raise AdequacyError("{}: no data rows".format(path))
    if header:
        def col(*names):
            for n in names:
                if n in header:
                    return header.index(n)
            return None
        it = col("time_hr", "time", "hr", "hours", "t_hr")
        iq = col("flow_cfs", "discharge_cfs", "q_cfs", "inflow_cfs", "flow", "discharge", "cfs")
        if it is None or iq is None:
            if len(header) == 2:
                it, iq = 0, 1
            else:
                raise AdequacyError("{}: needs columns time_hr and flow_cfs (found {})".format(
                    path, ", ".join(header)))
    else:
        it, iq = 0, 1
    try:
        t = np.array([float(r[it]) for r in rows])
        q = np.array([float(r[iq]) for r in rows])
    except (ValueError, IndexError) as e:
        raise AdequacyError("{}: unreadable number ({})".format(path, e))
    if np.any(np.diff(t) <= 0):
        raise AdequacyError("{}: times must increase".format(path))
    if t[0] > 0:                       # end-of-step times: the record starts at 0 with no flow
        t, q = np.r_[0.0, t], np.r_[0.0, q]
    meta.setdefault("storm_id", os.path.splitext(os.path.basename(path))[0])
    return (t - t[0]) * 3600.0, np.clip(q, 0.0, None) * CFS, meta


def find_inflows(folder):
    """The inflow CSVs: <folder>/hydrographs/*.csv when that exists (nd_pmp output folder),
    else <folder>/*.csv, without nd_pmp's summary tables."""
    sub = os.path.join(folder, "hydrographs")
    d = sub if os.path.isdir(sub) else folder
    out = [os.path.join(d, n) for n in sorted(os.listdir(d)) if n.lower().endswith(".csv")
           and not n.lower().startswith(("inflow_hydrograph_summary", "adequacy"))]
    if not out:
        raise AdequacyError("no inflow hydrograph CSVs in {}".format(d))
    return out


class TableReservoir:
    """Stage-storage table: elevation (m) and storage (m3), rising. Above the table the storage
    grows with the last surface area (constant area); below, it is clipped to the table."""

    def __init__(self, elev_m, vol_m3):
        z, v = np.asarray(elev_m, float), np.asarray(vol_m3, float)
        o = np.argsort(z)
        z, v = z[o], np.maximum.accumulate(v[o])
        if len(z) < 2 or np.any(np.diff(z) <= 0):
            raise AdequacyError("the stage-storage table needs at least two different elevations")
        if v[-1] <= v[0]:
            raise AdequacyError("the stage-storage table's storage does not increase")
        self.z, self.v = z, v
        self.area_top = (v[-1] - v[-2]) / (z[-1] - z[-2])
        self.notes = []

    @classmethod
    def from_csv(cls, path):
        z, v = br.read_csv_series(path, "stage-storage", ACFT, FT)    # x elevation ft, y ac-ft
        return cls(z, v)

    def volume(self, wse):
        w = np.asarray(wse, float)
        out = np.interp(w, self.z, self.v)
        return np.where(w > self.z[-1], self.v[-1] + (w - self.z[-1]) * self.area_top, out)

    def wse(self, volume):
        v = np.asarray(volume, float)
        out = np.interp(v, self.v, self.z)
        return np.where(v > self.v[-1], self.z[-1] + (v - self.v[-1]) / self.area_top, out)


# ----------------------------------------------------------------- routing --
def route_no_breach(res, fi, top_m, start_m, max_hours=240.0, dt_s=30.0, mult=1.0):
    """Route mult x the inflow of `fi` (flood.FloodInputs) through `res` with no breach.
    Same scheme as flood.route_flood's no-breach run. Returns dict of arrays (t_s, wse_m,
    qout_cms, qin_cms) and peaks."""
    spill = fi.spillway
    crest_len_m = (fi.crest_length_ft or 0.0) * FT
    c_ot = fi.crest_c_us * FT ** 0.5

    def qout(t, v):
        wse = float(res.wse(v))
        q = spill.q(wse) if spill is not None else 0.0
        if spill is not None and fi.spillway_schedule:
            q *= outmod.schedule_factor(fi.spillway_schedule, t)
        if fi.outlets:
            q += outmod.q_total(fi.outlets, wse, t)
        h = wse - top_m
        if h > 0 and crest_len_m:
            q += c_ot * crest_len_m * h ** 1.5
        return q

    def qin(t):
        return mult * fi.qin(t)

    t_in = max([fi.inflow_end_s] + [p[0] for p in (fi.spillway_schedule or [])]
               + [p[0] for cd in (fi.outlets or []) for p in (cd.schedule or [])])
    v = float(res.volume(start_m))
    ts, vs, qs = [0.0], [v], [qout(0.0, v)]
    t, qpk = 0.0, qs[0]
    while t < max_hours * 3600.0:
        v = fl._step(qin, qout, t, v, dt_s)
        t += dt_s
        q = qout(t, v)
        ts.append(t)
        vs.append(v)
        qs.append(q)
        qpk = max(qpk, q)
        if t > t_in and (q <= 1e-9 or q < 0.002 * qpk):
            break
    ts, vs, qs = np.asarray(ts), np.asarray(vs), np.asarray(qs)
    wse = np.asarray(res.wse(vs), float)
    qi = np.array([qin(x) for x in ts])
    i = int(np.argmax(wse))
    return {"t_s": ts, "wse_m": wse, "qout_cms": qs, "qin_cms": qi,
            "peak_wse_m": float(wse[i]), "t_peak_wse_s": float(ts[i]),
            "peak_qout_cms": float(qs.max()), "peak_qin_cms": float(qi.max()),
            "complete": bool(t <= max_hours * 3600.0 and t > t_in)}


def required_freeboard_ft(hazard, wind_ft=None, override_ft=None):
    """Residual freeboard required at the IDF peak pool: high / medium the greater of 1 ft and
    the wind setup + runup (Section 8.1.1 a); low 3 ft (8.1.1 b). override_ft replaces it."""
    if override_ft is not None:
        return float(override_ft)
    base = MIN_RESIDUAL_FT[hazard]
    if hazard in ("high", "medium") and wind_ft:
        return max(base, float(wind_ft))
    return base


def max_multiple(res, fi, top_m, start_m, allowed_m, dt_s=30.0, max_hours=240.0):
    """Largest multiple of the inflow (0..MAX_MULT) whose peak pool stays <= allowed_m."""
    def ok(m):
        return route_no_breach(res, fi, top_m, start_m, max_hours, dt_s, m)["peak_wse_m"] \
            <= allowed_m + 1e-9
    if ok(MAX_MULT):
        return MAX_MULT, True
    if not ok(0.0):
        return 0.0, False
    lo, hi = 0.0, MAX_MULT
    while hi - lo > BISECT_TOL:
        mid = 0.5 * (lo + hi)
        if ok(mid):
            lo = mid
        else:
            hi = mid
    return lo, False


def assess(inflows, top_of_dam_ft, hazard="high", spillway_csv=None, crest_length_ft=None,
           start_pool_ft=None, freeboard_ft=None, wind_setup_runup_ft=None,
           stage_storage_csv=None, dam=None, outlets=None, spillway_schedule=None,
           dt_s=30.0, max_hours=240.0, bisect=True):
    """Assess each inflow CSV. `dam` (breach.DamData) gives the power-law reservoir when no
    stage-storage table is given. Returns {"rows": [...], "verdict": {...}, "series": {...},
    "notes": [...]}."""
    hazard = (hazard or "high").lower()
    if hazard not in HAZARDS:
        raise AdequacyError("hazard class must be high, medium or low")
    if top_of_dam_ft is None:
        raise AdequacyError("the post-settlement top of dam elevation is required")
    top_m = float(top_of_dam_ft) * FT
    notes = []
    if stage_storage_csv:
        res = TableReservoir.from_csv(stage_storage_csv)
        notes.append("Reservoir: stage-storage table {} (constant surface area above its top)."
                     .format(stage_storage_csv))
    elif dam is not None:
        c = br._common(dam, top_m, "top of dam")
        res = c.res
        notes += ["Reservoir: power law from storage {:g} ac-ft and surface area {} at the top "
                  "of dam, height {:g} ft (exponent {:.2f}).".format(
                      dam.storage_top_acft, "{:g} ac".format(dam.surface_area_top_ac)
                      if dam.surface_area_top_ac else "(not given)", dam.dam_height_ft, res.m)]
        notes += list(res.notes)
    else:
        raise AdequacyError("give a stage-storage table or the dam's storage, area and height")
    spill = fl.Spillway.from_csv(spillway_csv) if spillway_csv else None
    if start_pool_ft is None:
        if spill is None:
            raise AdequacyError("give the starting pool or a spillway rating (its crest is the "
                                "default starting pool, REG_05.2026 Section 9)")
        start_m = spill.crest_m
        rule = "spillway crest from the rating (lowest ungated spillway, REG_05.2026 Section 9)"
    else:
        start_m = float(start_pool_ft) * FT
        rule = "given"
    if start_m >= top_m:
        raise AdequacyError("the starting pool ({:.2f} ft) is not below the top of dam".format(
            start_m / FT))
    fb = required_freeboard_ft(hazard, wind_setup_runup_ft, freeboard_ft)
    allowed_m = top_m - fb * FT
    if spill is None and not outlets:
        notes.append("No spillway rating and no outlets: the only release is over the crest.")
    if not crest_length_ft:
        notes.append("No crest length: no flow over the embankment above the top of dam (the "
                     "pool keeps rising instead). This only matters once the dam has failed the "
                     "check.")
    rows, series = [], {}
    for path in inflows:
        t, q, meta = read_inflow_csv(path)
        fi = fl.FloodInputs(t, q, spill, crest_length_ft=crest_length_ft)
        fi.outlets = list(outlets or [])
        fi.spillway_schedule = spillway_schedule
        r = route_no_breach(res, fi, top_m, start_m, max_hours, dt_s)
        peak_ft = r["peak_wse_m"] / FT
        resid = float(top_of_dam_ft) - peak_ft
        passed = r["peak_wse_m"] <= allowed_m + 1e-9
        mult, capped = (max_multiple(res, fi, top_m, start_m, allowed_m, dt_s, max_hours)
                        if bisect else (None, False))
        sid = meta.get("storm_id")
        rows.append({
            "storm_id": sid, "file": path, "storm_type": meta.get("storm_type", ""),
            "duration_hr": meta.get("duration_hr", ""),
            "peak_inflow_cfs": round(r["peak_qin_cms"] / CFS, 1),
            "inflow_volume_acft": round(float(fi.inflow_volume_m3) / ACFT, 1),
            "peak_outflow_cfs": round(r["peak_qout_cms"] / CFS, 1),
            "start_pool_ft": round(start_m / FT, 2),
            "peak_pool_ft": round(peak_ft, 2), "time_of_peak_pool_hr": round(r["t_peak_wse_s"] / 3600.0, 2),
            "top_of_dam_ft": round(float(top_of_dam_ft), 2),
            "residual_freeboard_ft": round(resid, 2), "required_freeboard_ft": round(fb, 2),
            "overtops": bool(peak_ft > float(top_of_dam_ft)),
            "passes": bool(passed),
            "max_multiple_passed": None if mult is None else round(mult, 3),
            "max_multiple_capped": bool(capped),
            "routing_complete": r["complete"],
        })
        series[sid] = r
    if not rows:
        raise AdequacyError("no inflow hydrographs")
    gov = max(rows, key=lambda x: x["peak_pool_ft"])
    ok = all(x["passes"] for x in rows)
    idf, psc = HAZARD_IDF[hazard]
    verdict = {
        "hazard": hazard, "required_idf": idf, "min_principal_spillway_capacity": psc,
        "required_residual_freeboard_ft": fb, "allowed_peak_pool_ft": round(allowed_m / FT, 2),
        "start_pool_ft": round(start_m / FT, 2), "start_pool_rule": rule,
        "governing_storm": gov["storm_id"], "governing_peak_pool_ft": gov["peak_pool_ft"],
        "governing_residual_freeboard_ft": gov["residual_freeboard_ft"],
        "passes": ok,
        "text": ("PASSES" if ok else "DOES NOT PASS") + (
            ": governing storm {} peaks at {:.2f} ft, {:.2f} ft below the top of dam {:.2f} ft "
            "(required residual freeboard {:.2f} ft, allowed peak pool {:.2f} ft).".format(
                gov["storm_id"], gov["peak_pool_ft"], gov["residual_freeboard_ft"],
                float(top_of_dam_ft), fb, allowed_m / FT)),
    }
    notes += [
        "Required IDF for a {} hazard dam (REG_05.2026 Table 6.1): {}; minimum principal "
        "spillway capacity {} (not checked here). The inflow hydrographs given are taken to be "
        "that IDF (all PMP storm types and durations >= the time of concentration, with "
        "snowmelt as appropriate, Section 6.2) - check that.".format(hazard, idf, psc),
        "Freeboard: residual freeboard = top of dam (post-settlement, roads excluded) minus the "
        "peak pool of the routed IDF (Section 8.1.1, 16).",
        "If an incremental damage analysis (Section 6.4) sets a smaller IDF, the dam must still "
        "pass the 24-hour, 500-year rainstorm without overtopping: run that storm here too.",
        "Pass / fail is the routed peak pool against the allowed pool, with NO breach. The flood "
        "scenario's 'failed' flag (breach at the peak pool) is not a pass / fail and is not used.",
        "Screening aid only: an engineer must review the inputs and results. The official "
        "determination is made by the ND Department of Water Resources.",
    ]
    return {"rows": rows, "verdict": verdict, "series": series, "notes": notes}


COLUMNS = ("storm_id", "storm_type", "duration_hr", "peak_inflow_cfs", "inflow_volume_acft",
           "peak_outflow_cfs", "start_pool_ft", "peak_pool_ft", "time_of_peak_pool_hr",
           "top_of_dam_ft", "residual_freeboard_ft", "required_freeboard_ft", "overtops",
           "passes", "max_multiple_passed", "max_multiple_capped", "routing_complete", "file")


def write_csv(result, path):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(COLUMNS)
        for r in result["rows"]:
            w.writerow([r.get(c) for c in COLUMNS])
    return path


def write_report(result, path):
    v = result["verdict"]
    lines = ["# Spillway adequacy (REG_05.2026)", "", "**{}**".format(v["text"]), "",
             "| storm | peak inflow cfs | peak outflow cfs | peak pool ft | residual fb ft | "
             "passes | max multiple |", "|---|---|---|---|---|---|---|"]
    for r in result["rows"]:
        mm = r["max_multiple_passed"]
        lines.append("| {} | {} | {} | {} | {} | {} | {} |".format(
            r["storm_id"], r["peak_inflow_cfs"], r["peak_outflow_cfs"], r["peak_pool_ft"],
            r["residual_freeboard_ft"], "yes" if r["passes"] else "NO",
            "" if mm is None else ("{:g}{}".format(mm, "+" if r["max_multiple_capped"] else ""))))
    lines += ["", "Starting pool {} ft ({}). Required residual freeboard {} ft; allowed peak "
              "pool {} ft.".format(v["start_pool_ft"], v["start_pool_rule"],
                                   v["required_residual_freeboard_ft"], v["allowed_peak_pool_ft"]),
              "", "## Notes", ""] + ["- " + n for n in result["notes"]]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return path


def plot_governing(result, path, top_of_dam_ft):
    """Pool and flows against time for the governing storm (PNG). Returns path or None."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception:
        return None
    v = result["verdict"]
    s = result["series"][v["governing_storm"]]
    th = s["t_s"] / 3600.0
    fig, ax = plt.subplots(figsize=(9, 5))
    ax.plot(th, s["wse_m"] / FT, color="tab:blue", label="pool (ft)")
    ax.axhline(top_of_dam_ft, color="k", lw=1, label="top of dam")
    ax.axhline(v["allowed_peak_pool_ft"], color="tab:red", ls="--", lw=1,
               label="allowed peak pool (freeboard {:g} ft)".format(
                   v["required_residual_freeboard_ft"]))
    ax.set_xlabel("time (hr)")
    ax.set_ylabel("elevation (ft)")
    ax2 = ax.twinx()
    ax2.plot(th, s["qin_cms"] / CFS, color="tab:gray", lw=0.8, label="inflow (cfs)")
    ax2.plot(th, s["qout_cms"] / CFS, color="tab:green", lw=0.8, label="outflow (cfs)")
    ax2.set_ylabel("flow (cfs)")
    h1, l1 = ax.get_legend_handles_labels()
    h2, l2 = ax2.get_legend_handles_labels()
    ax.legend(h1 + h2, l1 + l2, loc="upper right", fontsize=8)
    ax.set_title("Governing storm {}: {}".format(v["governing_storm"],
                                                 "passes" if v["passes"] else "does not pass"))
    fig.tight_layout()
    fig.savefig(path, dpi=120)
    plt.close(fig)
    return path
