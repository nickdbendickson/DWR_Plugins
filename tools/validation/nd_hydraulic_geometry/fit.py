#!/usr/bin/env python3
"""North Dakota bankfull hydraulic geometry from USGS field measurements: the regional curve
BoannGrid's channel carve (core/metalterrain/prism.py, carve_channels) needs before it can leave
the research gate, in place of the national Bieger et al. (2015) coefficients.

    python3 tools/validation/nd_hydraulic_geometry/fit.py [--refresh]

What it does, for every USGS stream gauge in North Dakota with a drainage area:

1. Bankfull discharge = the 1.5-year annual peak flow (the usual stand-in for bankfull where no
   field bankfull survey exists; Dunne & Leopold 1978). Annual peaks from the USGS Water Data
   API `peaks` collection; peaks the USGS marks REGULATED, ESTIMATED, DAM FAILURE or ice-jam
   affected are left out; at least MIN_PEAKS peaks remain. Q1.5 is read from the log-Pearson III
   fit of those peaks (method of moments on log10 Q, the Bulletin 17C distribution without its
   regional skew or low-outlier test).
2. Width and depth at that flow = the gauge's own at-a-station hydraulic geometry (Leopold &
   Maddock 1953): W = a Q^b and A = c Q^f fitted to the open-water (May to October) discharge
   measurements of the `channel-measurements` collection that carry a width and an area, taken
   between Q1.5 / 10 and 2 x Q1.5 (an out-of-bank measurement measures the floodplain, not the
   channel). Bankfull width = W(Q1.5), bankfull mean depth = A(Q1.5) / W(Q1.5). A gauge needs
   MIN_MEAS such measurements and must bracket Q1.5 within a factor of 2.
3. Regional curve: W = a DA^b and D = c DA^f, ordinary least squares in log10, DA in km2 (the
   form of prism.bieger_geometry), on the drainage area that contributes (ND's prairie potholes
   make the total area of many basins several times the contributing area). Gauges on the
   Missouri River main stem and any gauge with more than MAX_REGULATED_SHARE of its peaks marked
   REGULATED are left out (reservoir-controlled flows set no channel of their own).

Writes results.json (every gauge, its numbers and why one was left out) and RESULTS.md beside
this file. The downloads are cached in cache/ (ignored by git); --refresh fetches again.
Standard library only. Nothing here changes a run.
"""
import argparse
import json
import math
import os
import statistics
import sys
import time
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "cache")
API = "https://api.waterdata.usgs.gov/ogcapi/v0/collections/"
FT = 0.3048
SQMI_KM2 = 2.589988110336
MIN_PEAKS = 10
MIN_MEAS = 8
MAX_REGULATED_SHARE = 0.25
# the USGS peak qualifiers (as the API spells them) of a peak left out of the frequency fit:
# regulated, estimated, a dam failure, ice or backwater affected, a historic peak outside the
# systematic record, a daily mean rather than the instantaneous peak, or only a bound
BAD_PEAK_QUALIFIERS = {"REGULATED", "ESTIMATED", "DAMFAILURE", "ICE", "BACKWATER", "HISTORIC",
                       "MAXDAILYMEAN", "LESSTHAN", "GREATERTHAN"}
BRIDGE_TYPES = {"BridgeCrane", "BridgeDownstreamSide", "BridgeUpstreamSide"}
OPEN_WATER_MONTHS = range(5, 11)
BIEGER_2015_US = {"a_w": 2.70, "b_w": 0.352, "a_d": 0.30, "b_d": 0.213}


# ------------------------------------------------------------------------------ download -----
def _get(collection, params, cache_name, refresh=False):
    """Every page of a collection query, cached as one JSON list of feature properties."""
    path = os.path.join(CACHE, cache_name)
    if os.path.isfile(path) and not refresh:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    out = []
    q = dict(params, f="json", skipGeometry="true", limit=10000)
    url = API + collection + "/items?" + urllib.parse.urlencode(q, safe=",")
    while url:
        for attempt in range(5):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "nd-hydraulic-geometry"})
                with urllib.request.urlopen(req, timeout=180) as r:
                    page = json.load(r)
                break
            except Exception:                                    # noqa - retried, then raised
                if attempt == 4:
                    raise
                time.sleep(3 * (attempt + 1))
        out.extend(feat["properties"] for feat in page.get("features", []))
        url = next((ln["href"] for ln in page.get("links", []) if ln.get("rel") == "next"), None)
        time.sleep(0.3)
    os.makedirs(CACHE, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f)
    return out


def _num(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


# ------------------------------------------------------------------------------ statistics ---
def lp3_quantile(peaks, return_period):
    """Log-Pearson III flow of a return period (method of moments on log10 Q; Wilson-Hilferty
    frequency factor)."""
    logs = [math.log10(q) for q in peaks if q > 0]
    n = len(logs)
    m = statistics.fmean(logs)
    s = statistics.stdev(logs)
    g = (n * sum((x - m) ** 3 for x in logs)) / ((n - 1) * (n - 2) * s ** 3) if s > 0 else 0.0
    p = 1.0 - 1.0 / return_period                             # non-exceedance
    z = statistics.NormalDist().inv_cdf(p)
    if abs(g) < 1e-6:
        k = z
    else:
        k = (2.0 / g) * ((1.0 + g * z / 6.0 - g * g / 36.0) ** 3 - 1.0)
    return 10 ** (m + k * s)


def loglog_fit(xs, ys):
    """y = a x^b by least squares in log10; returns a, b, r2, n, standard error (log10 units)."""
    lx = [math.log10(x) for x in xs]
    ly = [math.log10(y) for y in ys]
    n = len(lx)
    mx, my = statistics.fmean(lx), statistics.fmean(ly)
    sxx = sum((x - mx) ** 2 for x in lx)
    sxy = sum((x - mx) * (y - my) for x, y in zip(lx, ly))
    b = sxy / sxx
    a = my - b * mx
    res = [y - (a + b * x) for x, y in zip(lx, ly)]
    sst = sum((y - my) ** 2 for y in ly)
    r2 = 1.0 - sum(r * r for r in res) / sst if sst > 0 else float("nan")
    se = math.sqrt(sum(r * r for r in res) / (n - 2)) if n > 2 else float("nan")
    return 10 ** a, b, r2, n, se


# ------------------------------------------------------------------------------ per gauge ----
def gauge_geometry(site, peaks, meas, bridges=True):
    """Bankfull Q, width (m) and mean depth (m) of one gauge, or a reason it has none.
    bridges=False leaves out measurements made from a bridge (a bridge opening is often
    narrower than the channel up- and downstream)."""
    out = {"id": site["id"], "name": site["monitoring_location_name"]}
    da_tot = _num(site.get("drainage_area"))
    da_con = _num(site.get("contributing_drainage_area"))
    da = da_con if da_con else da_tot
    out.update(da_total_sqmi=da_tot, da_contributing_sqmi=da_con,
               da_km2=da * SQMI_KM2 if da else None)
    if not da:
        return dict(out, excluded="no drainage area")
    if "MISSOURI R" in site["monitoring_location_name"].upper():
        return dict(out, excluded="Missouri River main stem (reservoir-regulated)")
    flagged = [p for p in peaks if set(p.get("qualifier") or []) & BAD_PEAK_QUALIFIERS]
    regulated = [p for p in peaks if "REGULATED" in (p.get("qualifier") or [])]
    out["n_peaks_all"] = len(peaks)
    if peaks and len(regulated) / len(peaks) > MAX_REGULATED_SHARE:
        return dict(out, excluded="{} of {} peaks REGULATED".format(len(regulated), len(peaks)))
    good = [q for q in (_num(p.get("value")) for p in peaks if p not in flagged) if q and q > 0]
    out["n_peaks"] = len(good)
    if len(good) < MIN_PEAKS:
        return dict(out, excluded="{} usable annual peaks (< {})".format(len(good), MIN_PEAKS))
    qbf = lp3_quantile(good, 1.5)
    out["q15_cfs"] = qbf
    pts = []
    for m in meas:
        month = int(m["time"][5:7]) if m.get("time") else 0
        q, w, a = _num(m.get("channel_flow")), _num(m.get("channel_width")), _num(m.get("channel_area"))
        mtype = m.get("measurement_type") or ""
        if mtype == "Ice" or (not bridges and mtype in BRIDGE_TYPES):
            continue
        if month in OPEN_WATER_MONTHS and q and w and a and q > 0 and w > 0 and a > 0 \
                and qbf / 10.0 <= q <= 2.0 * qbf:
            pts.append((q, w, a))
    out["n_meas"] = len(pts)
    if len(pts) < MIN_MEAS:
        return dict(out, excluded="{} open-water measurements near Q1.5 (< {})".format(
            len(pts), MIN_MEAS))
    qmax = max(p[0] for p in pts)
    if qmax < qbf / 2.0:
        return dict(out, excluded="largest measurement {:.0f} cfs is under half of Q1.5 {:.0f}".format(
            qmax, qbf))
    aw, bw, r2w, _, _ = loglog_fit([p[0] for p in pts], [p[1] for p in pts])
    aa, ba, r2a, _, _ = loglog_fit([p[0] for p in pts], [p[2] for p in pts])
    w_ft = aw * qbf ** bw
    a_ft2 = aa * qbf ** ba
    out.update(width_m=w_ft * FT, depth_m=(a_ft2 / w_ft) * FT, ahg_b_width=bw, ahg_r2_width=r2w,
               ahg_f_area=ba, ahg_r2_area=r2a, q15_cms=qbf * FT ** 3, q_meas_max_cfs=qmax)
    return out


# ------------------------------------------------------------------------------ main ---------
def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--refresh", action="store_true", help="download again instead of the cache")
    args = ap.parse_args(argv)
    sites = _get("monitoring-locations", {"state_code": "38", "site_type_code": "ST"},
                 "sites.json", args.refresh)
    sites = [s for s in sites if _num(s.get("drainage_area")) and s.get("agency_code") == "USGS"]
    ids = [s["id"] for s in sites]
    peaks, meas = {}, {}
    for i in range(0, len(ids), 40):
        chunk = ids[i:i + 40]
        for p in _get("peaks", {"monitoring_location_id": ",".join(chunk), "parameter_code": "00060"},
                      "peaks_{:03d}.json".format(i // 40), args.refresh):
            peaks.setdefault(p["monitoring_location_id"], []).append(p)
    with_peaks = [s for s in sites if len(peaks.get(s["id"], [])) >= MIN_PEAKS]
    for i in range(0, len(with_peaks), 20):
        chunk = [s["id"] for s in with_peaks[i:i + 20]]
        for m in _get("channel-measurements", {"monitoring_location_id": ",".join(chunk)},
                      "meas_{:03d}.json".format(i // 20), args.refresh):
            meas.setdefault(m["monitoring_location_id"], []).append(m)
    rows = [gauge_geometry(s, peaks.get(s["id"], []), meas.get(s["id"], [])) for s in sites]
    curve, fit, ratio = regional(rows)
    rows_nb = [gauge_geometry(s, peaks.get(s["id"], []), meas.get(s["id"], []), bridges=False)
               for s in sites]
    curve_nb, fit_nb, ratio_nb = regional(rows_nb)
    result = {
        "what_this_is": "ND bankfull hydraulic geometry (W = a_w DA^b_w, D = a_d DA^b_d; m, DA km2 "
                        "contributing) from USGS annual peaks (Q1.5, LP3) and open-water channel "
                        "measurements (at-a-station W(Q), A(Q) at Q1.5). Built by fit.py.",
        "read": time.strftime("%Y-%m-%d"),
        "curve_nd": curve,
        "fit": fit,
        "median_ratio_nd_over_bieger": ratio,
        "without_bridge_measurements": {"curve_nd": curve_nb, "fit": fit_nb,
                                        "median_ratio_nd_over_bieger": ratio_nb},
        "curve_bieger_2015_us": BIEGER_2015_US,
        "gauges": rows,
    }
    with open(os.path.join(HERE, "results.json"), "w", encoding="utf-8") as f:
        json.dump(result, f, indent=1)
    write_md(result)
    print(json.dumps({k: result[k] for k in ("curve_nd", "fit", "median_ratio_nd_over_bieger",
                                             "without_bridge_measurements")}, indent=1))
    return 0


def regional(rows):
    """The regional curve of the gauges a row set keeps, its fit and the median ND / Bieger."""
    used = [r for r in rows if "excluded" not in r]
    da = [r["da_km2"] for r in used]
    a_w, b_w, r2_w, n, se_w = loglog_fit(da, [r["width_m"] for r in used])
    a_d, b_d, r2_d, _, se_d = loglog_fit(da, [r["depth_m"] for r in used])
    curve = {"a_w": round(a_w, 3), "b_w": round(b_w, 3), "a_d": round(a_d, 3), "b_d": round(b_d, 3)}
    for r in used:
        r["bieger_width_m"] = BIEGER_2015_US["a_w"] * r["da_km2"] ** BIEGER_2015_US["b_w"]
        r["bieger_depth_m"] = BIEGER_2015_US["a_d"] * r["da_km2"] ** BIEGER_2015_US["b_d"]
    fit = {"n_gauges": n, "r2_width": r2_w, "r2_depth": r2_d, "se_log10_width": se_w,
           "se_log10_depth": se_d, "da_km2_min": min(da), "da_km2_max": max(da)}
    ratio = {"width": statistics.median(r["width_m"] / r["bieger_width_m"] for r in used),
             "depth": statistics.median(r["depth_m"] / r["bieger_depth_m"] for r in used)}
    return curve, fit, ratio


def write_md(res):
    used = [r for r in res["gauges"] if "excluded" not in r]
    left = [r for r in res["gauges"] if "excluded" in r]
    c, fit = res["curve_nd"], res["fit"]
    lines = [
        "# North Dakota bankfull hydraulic geometry",
        "",
        "Built by `fit.py` from the USGS Water Data API, read {}. See `fit.py` for the method.".format(
            res["read"]),
        "",
        "| | width W (m) | mean depth D (m) |",
        "|---|---|---|",
        "| North Dakota (this fit) | {a_w} DA^{b_w} | {a_d} DA^{b_d} |".format(**c),
        "| ND, bridge measurements left out ({} gauges) | {a_w} DA^{b_w} | {a_d} DA^{b_d} |".format(
            res["without_bridge_measurements"]["fit"]["n_gauges"],
            **res["without_bridge_measurements"]["curve_nd"]),
        "| Bieger et al. 2015, US | 2.70 DA^0.352 | 0.30 DA^0.213 |",
        "| r2 / standard error (log10) | {:.2f} / {:.3f} | {:.2f} / {:.3f} |".format(
            fit["r2_width"], fit["se_log10_width"], fit["r2_depth"], fit["se_log10_depth"]),
        "",
        "{} gauges, contributing drainage area {:.0f} to {:.0f} km2. Median ND / Bieger: width "
        "{:.2f}, depth {:.2f}.".format(fit["n_gauges"], fit["da_km2_min"], fit["da_km2_max"],
                                        res["median_ratio_nd_over_bieger"]["width"],
                                        res["median_ratio_nd_over_bieger"]["depth"]),
        "",
        "## Gauges used",
        "",
        "| Gauge | Name | DA km2 | Q1.5 cfs | W m | D m | Bieger W | Bieger D | meas |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in sorted(used, key=lambda r: r["da_km2"]):
        lines.append("| {} | {} | {:.0f} | {:.0f} | {:.1f} | {:.2f} | {:.1f} | {:.2f} | {} |".format(
            r["id"].replace("USGS-", ""), r["name"], r["da_km2"], r["q15_cfs"], r["width_m"],
            r["depth_m"], r["bieger_width_m"], r["bieger_depth_m"], r["n_meas"]))
    reasons = {}
    for r in left:
        key = r["excluded"]
        if "REGULATED" in key:
            key = "more than {:.0%} of peaks REGULATED".format(MAX_REGULATED_SHARE)
        elif "usable annual peaks" in key:
            key = "fewer than {} usable annual peaks".format(MIN_PEAKS)
        elif "measurements near" in key:
            key = "fewer than {} open-water measurements near Q1.5".format(MIN_MEAS)
        elif key.startswith("largest measurement"):
            key = "measurements stop below half of Q1.5"
        reasons[key] = reasons.get(key, 0) + 1
    lines += ["", "## Left out ({} gauges)".format(len(left)), ""]
    lines += ["- {}: {}".format(k, v) for k, v in sorted(reasons.items(), key=lambda kv: -kv[1])]
    with open(os.path.join(HERE, "RESULTS.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    sys.exit(main())
