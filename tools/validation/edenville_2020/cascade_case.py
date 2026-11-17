#!/usr/bin/env python3
"""The 2020 Edenville -> Sanford cascade dam-break, routed to the Midland gauge.

    <QGIS python> cascade_case.py build --out FOLDER
    <QGIS python> cascade_case.py run FOLDER [--lisflood EXE] [--threads N] [--rule overtop|assume]
    <QGIS python> cascade_case.py score ROOT

On 19 May 2020 the Edenville Dam (Wixom Lake, Tittabawassee River) failed by static liquefaction
of its downstream slope at about its normal pool; the surge overtopped and breached the Sanford
Dam (Sanford Lake) ~18 km downstream, and the combined flood ran down the Tittabawassee through
Midland. It was gauged at USGS 04156000 (Tittabawassee at Midland), which peaked at 35.14 ft gage
height = 614.61 ft NAVD88, a rating-computed 51,800 cfs. There are no surveyed high-water marks
downstream of the two dams, so that one gauge peak is the field observation to test against.

This drives the Runner's OWN cascade engine (core/cascade.py: the two-dam tool behind the
"Cascade failure" QGIS algorithm) headless, at 30 m on USGS 3DEP terrain, through LISFLOOD-FP:

  * Edenville: a sunny-day piping breach at its normal pool (its real failure mode -- the
    Independent Forensic Team found static liquefaction at ~normal pool, not overtopping);
  * the surge is routed ~18 km to Sanford, added to Sanford's pool, and Sanford fails when the
    wave overtops it (fail_rule="overtop");
  * the combined flood is routed on down to the Midland gauge, and the modelled peak water
    surface there is compared with the observed 614.61 ft NAVD88.

It is a dam-break reconstruction, NOT a storm reconstruction: it carries the two reservoir
releases but not the concurrent rainfall-runoff that was also in the river, so the Midland peak
is a lower bound on the real flow. Three numbers are reported against outside references: the
Edenville breach peak and the attenuated wave at Sanford against the peer-reviewed ASCE 2-D model
(Journal of Hydraulic Engineering, 2024: 4,323 m3/s = ~152,600 cfs at the breach, ~35 % attenuated
over the 18 km to Sanford), and the peak stage at Midland against the USGS gauge.

It is the companion to midland_reach.py: the reach isolates roughness at the gauge from the
observed discharge, this isolates the cascade dam-break physics upstream of it."""
import argparse
import concurrent.futures as cf
import glob
import json
import math
import os
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
ND = os.path.join(os.path.dirname(HERE), "nd_gauges")
sys.path.insert(0, REPO)
sys.path.insert(0, ND)
sys.path.insert(0, os.path.join(REPO, "DamSafety_Runner", "scripts"))
import reach  # noqa: E402  (tools/validation/nd_gauges/reach.py: fetch(), to_utm(), 3DEP URL)

FT = 0.3048
CFS = FT ** 3
EPSG = 26916                       # UTM 16N, NAD83: Midland, MI is ~84.2 W (ND's reach.py is 14N)
SRC_CELL = 10.0                    # terrain cell asked of 3DEP, m (averaged to CELL)
CELL = 30.0                        # model cell, m (a full two-dam cascade over ~30 km)
TILE_M = 6000.0                    # one 3DEP request: 600 x 600 cells of 10 m -> 200 x 200 of 30 m
BUFFER_M = 3500.0                  # valley room each side of the dam/gauge bounding box
READ_RADIUS_M = 120.0              # read the model stage within this of the gauge

# USGS 04156000, Tittabawassee River at Midland MI. Gage datum 579.47 ft NAVD88; 2020 annual peak
# 51,800 cfs at 35.14 ft gage height -> 614.61 ft NAVD88 (same as midland_reach.py).
MIDLAND = {
    "id": "USGS-04156000",
    "name": "Tittabawassee River at Midland",
    "lat": 43.5953047686325,
    "lon": -84.2355439496881,
    "datum_ft": 579.47,
    "gage_height_ft": 35.14,
    "measured_stage_ft": 614.61,          # NAVD88
    "measured_q_cfs": 51800.0,
    "source": "USGS 04156000, water-year 2020 annual peak",
}

# Mid-Michigan NGVD29 -> NAVD88 is about -0.6 ft (NGVD29 ~0 to 0.5 m above NAVD88 in eastern CONUS;
# VERTCON regional). The breach OUTFLOW is a head difference (pool - breach bottom) within one
# datum, so it is insensitive to this offset; the Midland stage is computed on the NAVD88 3DEP
# terrain regardless. The offset only lines the stated pools up with the terrain and the gauge.
NGVD29_TO_NAVD88_FT = -0.6


def _navd(ngvd29_ft):
    return round(ngvd29_ft + NGVD29_TO_NAVD88_FT, 2)


# Geometry from the 2021 Independent Forensic Team report (normal pools, datum), the Four Lakes
# lake-level study and the EGLE/FEMA post-failure reports, and the National Inventory of Dams
# (Edenville MI00549). Elevations converted to NAVD88 to match the 3DEP terrain and the gauge.
# Storage-at-top and area-at-top are set so the storage curve gives the published normal-pool
# storage (Edenville ~40,000 ac-ft, Sanford ~15,000 ac-ft); breach widths come from the method's
# regressions on storage and height, so these carry the usual breach-parameter uncertainty.
EDENVILLE = {
    "dam_id": "Edenville",
    "name": "Edenville Dam (Wixom Lake), Tittabawassee River",
    "lat": 43.8162, "lon": -84.3783,
    "dam_height_ft": 54.0,
    "top_of_dam_ft": _navd(682.1),            # min crest, NGVD29 682.1 -> NAVD88
    "normal_pool_ft": _navd(675.8),           # IFT normal pool, NGVD29 675.8 -> NAVD88
    "storage_top_acft": 57000.0,              # curve gives ~40,000 ac-ft at normal pool
    "surface_area_top_ac": 2900.0,
    "storage_normal_acft": 40000.0,           # IFT/EGLE normal-pool storage (the curve target)
    "crest_width_ft": 20.0, "us_slope_h": 3.0, "ds_slope_h": 2.5,
    "crest_length_ft": 1500.0,                # the Tittabawassee (east) embankment that failed
    "scenario": "sunny",                      # static-liquefaction breach at normal pool
}
SANFORD = {
    "dam_id": "Sanford",
    "name": "Sanford Dam (Sanford Lake), Tittabawassee River",
    "lat": 43.6764, "lon": -84.3772,
    "dam_height_ft": 36.0,
    "top_of_dam_ft": _navd(634.0),            # ~3 ft of freeboard above normal pool
    "normal_pool_ft": _navd(630.8),           # IFT normal pool
    "storage_top_acft": 20000.0,              # curve gives ~15,000 ac-ft at normal pool
    "surface_area_top_ac": 1600.0,
    "storage_normal_acft": 15000.0,           # IFT/Four Lakes normal-pool storage (the curve target)
    "crest_width_ft": 20.0, "us_slope_h": 3.0, "ds_slope_h": 2.5,
    "crest_length_ft": 1580.0,
    "spillway_length_ft": 200.0,              # a weir at the normal pool for the overtop routing
    "scenario": "flood",                      # fails when the Edenville wave overtops it
}

# Peer-reviewed cross-checks for the breach outflow (not a gate; the gauge is the field anchor).
ASCE_EDENVILLE_BREACH_CFS = 4323.0 / CFS      # 4,323 m3/s -> cfs (ASCE JHE 2-D model)
ASCE_WAVE_AT_SANFORD_CFS = ASCE_EDENVILLE_BREACH_CFS * 0.65   # ~35 % attenuated over 18 km


# ------------------------------------------------------------------------------ terrain
def _tile(t, folder):
    """One 3DEP tile at SRC_CELL m, averaged to CELL m; cached as .npy. In EPSG (UTM 16N)."""
    from osgeo import gdal
    gdal.UseExceptions()
    x0, y0 = t
    path = os.path.join(folder, "t_{:.0f}_{:.0f}.npy".format(x0, y0))
    if os.path.isfile(path):
        return t, np.load(path)
    n = int(TILE_M / SRC_CELL)
    q = {"bbox": "{},{},{},{}".format(x0, y0, x0 + TILE_M, y0 + TILE_M), "bboxSR": EPSG,
         "imageSR": EPSG, "size": "{},{}".format(n, n), "format": "tiff", "pixelType": "F32",
         "interpolation": "RSP_BilinearInterpolation", "noData": -9999, "f": "image"}
    for k in range(4):
        raw = reach.fetch(reach.DEP + "?" + reach.urllib.parse.urlencode(q), timeout=300)
        if raw[:4] in (b"II*\x00", b"MM\x00*"):
            break
        time.sleep(5 * (k + 1))
    else:
        raise SystemExit("3DEP did not send a picture for tile {}".format(t))
    mem = "/vsimem/c_{:.0f}_{:.0f}.tif".format(x0, y0)
    gdal.FileFromMemBuffer(mem, raw)
    a = gdal.Open(mem).ReadAsArray().astype(np.float64)
    gdal.Unlink(mem)
    a[(a < -1000) | (a > 9000)] = np.nan
    f = int(CELL / SRC_CELL)
    b = a.reshape(n // f, f, n // f, f)
    with np.errstate(all="ignore"):
        m = np.nanmean(b, axis=(1, 3))
        m[np.isnan(b).sum(axis=(1, 3)) > f * f // 2] = np.nan
    np.save(path, m.astype(np.float32))
    return t, m.astype(np.float32)


def terrain(box, folder, workers=4, say=print):
    """The CELL-m mean of SRC_CELL-m 3DEP over box (snapped to whole tiles). Returns (z, origin)."""
    x0 = math.floor(box[0] / TILE_M) * TILE_M
    y0 = math.floor(box[1] / TILE_M) * TILE_M
    x1 = math.ceil(box[2] / TILE_M) * TILE_M
    y1 = math.ceil(box[3] / TILE_M) * TILE_M
    nt_x, nt_y = int((x1 - x0) / TILE_M), int((y1 - y0) / TILE_M)
    m = int(TILE_M / CELL)
    out = np.full((nt_y * m, nt_x * m), np.nan, np.float32)
    tiles = [(x0 + i * TILE_M, y0 + j * TILE_M) for j in range(nt_y) for i in range(nt_x)]
    os.makedirs(folder, exist_ok=True)
    done = 0
    with cf.ThreadPoolExecutor(workers) as ex:
        for (tx, ty), a in ex.map(lambda t: _tile(t, folder), tiles):
            i, j = int((tx - x0) / TILE_M), int((ty - y0) / TILE_M)
            out[(nt_y - 1 - j) * m:(nt_y - j) * m, i * m:(i + 1) * m] = a
            done += 1
            if done % 5 == 0 or done == len(tiles):
                say("terrain: {} of {} tiles".format(done, len(tiles)))
    return out, (x0, y1)


# -------------------------------------------------------------------------------- build
def build(out, say=print):
    """Fetch the Edenville -> Sanford -> Midland corridor terrain at 30 m and write dem.asc."""
    reach.EPSG = EPSG
    from DamSafety_Runner.core.grid import Grid
    os.makedirs(out, exist_ok=True)
    pts = {k: reach.to_utm(v["lon"], v["lat"]) for k, v in
           (("Edenville", EDENVILLE), ("Sanford", SANFORD), ("Midland", MIDLAND))}
    xs = [p[0] for p in pts.values()]
    ys = [p[1] for p in pts.values()]
    box = (min(xs) - BUFFER_M, min(ys) - BUFFER_M, max(xs) + BUFFER_M, max(ys) + BUFFER_M)
    say("corridor {:.1f} km N-S x {:.1f} km E-W (+{:.0f} m buffer)".format(
        (box[3] - box[1]) / 1000, (box[2] - box[0]) / 1000, BUFFER_M))
    z, origin = terrain(box, os.path.join(out, "tiles"), say=say)
    dem = Grid(np.where(np.isfinite(z), z, -9999.0), origin[0], origin[1], CELL)
    dem.write_asc(os.path.join(out, "dem.asc"), fmt="%.3f")
    meta = {"crs": "EPSG:{}".format(EPSG), "cell_m": CELL, "box": list(box),
            "origin": list(origin), "shape": list(dem.data.shape),
            "terrain": "USGS 3DEP {:.0f} m (exportImage, bilinear), mean of each {:.0f} m cell".format(
                SRC_CELL, CELL),
            "dams": {"Edenville": EDENVILLE, "Sanford": SANFORD}, "gauge": MIDLAND,
            "dam_xy": {k: [float(p[0]), float(p[1])] for k, p in pts.items()},
            "ngvd29_to_navd88_ft": NGVD29_TO_NAVD88_FT,
            "references": {"asce_edenville_breach_cfs": round(ASCE_EDENVILLE_BREACH_CFS, 0),
                           "asce_wave_at_sanford_cfs": round(ASCE_WAVE_AT_SANFORD_CFS, 0)}}
    with open(os.path.join(out, "cascade.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=1)
    gz = dem.data[dem.data > -9000]
    say("{} x {} cells at {:.0f} m; ground {:.1f}-{:.1f} ft NAVD88; dams Edenville {},{} "
        "Sanford {},{}; gauge {},{}".format(
            dem.nx, dem.ny, CELL, float(gz.min()) / FT, float(gz.max()) / FT,
            *[round(c) for c in pts["Edenville"]], *[round(c) for c in pts["Sanford"]],
            *[round(c) for c in pts["Midland"]]))
    return meta


# ---------------------------------------------------------------------------------- run
def _dam_data(d):
    from DamSafety_Runner.core import breach as brmod
    dd = brmod.DamData(
        dam_id=d["dam_id"], name=d["name"], dam_height_ft=d["dam_height_ft"],
        storage_top_acft=d["storage_top_acft"], top_of_dam_ft=d["top_of_dam_ft"],
        surface_area_top_ac=d["surface_area_top_ac"], crest_width_ft=d["crest_width_ft"],
        us_slope_h=d["us_slope_h"], ds_slope_h=d["ds_slope_h"],
        crest_length_ft=d["crest_length_ft"])
    # Edenville failed at ~normal pool (static liquefaction, not overtopping), so start its
    # sunny-day breach there, not at the top of dam (the Runner's default sunny-day start). This
    # also makes Vw = storage at that pool (~40,000 ac-ft), which is the real release.
    if d.get("scenario") == "sunny" and d.get("normal_pool_ft") is not None:
        dd.start_below_top_ft = round(d["top_of_dam_ft"] - d["normal_pool_ft"], 2)
    return dd


def run(folder, exe=None, threads=1, rule="overtop", sim_hr=None, say=print):
    reach.EPSG = EPSG
    import ndbreach_cli as cli
    from DamSafety_Runner.core import workflow, cascade as cascmod
    from DamSafety_Runner.core import breach as brmod, flood as floodmod, impacts as impmod
    from DamSafety_Runner.core.grid import Grid
    with open(os.path.join(folder, "cascade.json"), encoding="utf-8") as f:
        meta = json.load(f)
    exe = exe or os.environ.get("LISFLOOD_EXE") or os.environ.get("NDBREACH_LISFLOOD")
    if not exe or not os.path.isfile(exe):
        raise SystemExit("no LISFLOOD-FP executable: pass --lisflood or set LISFLOOD_EXE")
    dem_path = os.path.join(folder, "dem.asc")
    terrain_src = cli.AscTerrain(dem_path, "meters", None, 0.06)
    writer = cli.AscWriter("EPSG:{}".format(EPSG))
    midland_xy = tuple(meta["dam_xy"]["Midland"])

    st = workflow.RunSettings()
    st.cell_size_m = CELL
    st.planning_cell_m = CELL
    st.threads = threads
    st.lisflood_exe = exe
    st.manning_n = 0.06                       # the suite default; this case is the dam-break, not n
    st.max_extent_mi = 25.0                   # must reach ~18 km (11 mi) from Sanford to Midland
    st.grid_refine = False                    # one 30 m pass (keep the cascade within a CI job)
    if sim_hr:                                # cap the clock (the peak reaches Midland well inside
        st.sim_time_hr = float(sim_hr)        # a day; the long drain-down tail only slows the run)
    st.overrides = brmod.BreachOverrides()
    st.scenario = workflow.SCENARIO_SUNNY
    st.gauges = [{"name": "midland", "xy": midland_xy}]
    st.receptors = impmod.Receptors()
    st.receptors.add_points([midland_xy], ids=["Midland gauge"])

    # Edenville: sunny-day piping breach (no inflow). Sanford: zero own-inflow + pool + spillway,
    # so the cascade adds the Edenville wave and Sanford fails when the wave overtops it.
    eden = cascmod.CascadeDam(_dam_data(EDENVILLE), midpoint(meta, "Edenville"), None, None)
    san_fi = floodmod.FloodInputs.triangle(
        0.0, 1.0, 2.0, normal_pool_ft=SANFORD["normal_pool_ft"],
        crest_length_ft=SANFORD["crest_length_ft"],
        spillway=floodmod.Spillway.from_weir(SANFORD["normal_pool_ft"],
                                             SANFORD["spillway_length_ft"]))
    san = cascmod.CascadeDam(_dam_data(SANFORD), midpoint(meta, "Sanford"), None, san_fi)

    t0 = time.time()
    logs = []
    out = cascmod.run_cascade([eden, san], st, terrain_src, writer, folder, rule,
                              log=logs.append)
    with open(os.path.join(folder, "cascade_run.log"), "w", encoding="utf-8") as f:
        f.write("\n".join(logs) + "\n")

    res = {"rule": rule, "wall_s": round(time.time() - t0, 1), "decisions": out.decisions,
           "warnings": out.warnings, "dams": {}}
    for cd, r in out.runs:
        if r is None:
            res["dams"][cd.dam.dam_id] = {"modelled": False}
            continue
        s = r.summary
        d = {"modelled": True, "peak_breach_cfs": round(s.get("peak_breach_cfs") or 0.0, 0),
             "flooded_acres": round(s.get("flooded_acres") or 0.0, 0),
             "max_depth_ft": s.get("max_depth_ft")}
        g = r.gauges.get("to_Sanford") or r.gauges.get("midland")
        for gname in ("to_Sanford", "midland"):
            gg = r.gauges.get(gname)
            if gg is not None and len(gg[1]) and gg[1].max() > 0:
                d["gauge_" + gname + "_peak_cfs"] = round(float(gg[1].max()) / CFS, 0)
        res["dams"][cd.dam.dam_id] = d

    # peak stage at Midland from the combined WSE raster (feet, NAVD88)
    stage = None
    wname = "Cascade WSE (Max).tif"
    if out.combined.get(wname):
        wpath = cli.AscWriter.actual(out.combined[wname])
        if os.path.isfile(wpath):
            stage = _read_stage(Grid.read_asc(wpath), midland_xy)
    res["midland_stage_ft"] = stage
    res["midland_observed_ft"] = MIDLAND["measured_stage_ft"]
    res["midland_diff_ft"] = round(stage - MIDLAND["measured_stage_ft"], 2) if stage is not None \
        else None
    # peak discharge reaching Midland (whichever run's domain covered it)
    qm = [d["gauge_midland_peak_cfs"] for d in res["dams"].values()
          if isinstance(d, dict) and "gauge_midland_peak_cfs" in d]
    res["midland_peak_cfs"] = max(qm) if qm else None
    res["midland_observed_cfs"] = MIDLAND["measured_q_cfs"]
    res["references"] = {"asce_edenville_breach_cfs": round(ASCE_EDENVILLE_BREACH_CFS, 0),
                         "asce_wave_at_sanford_cfs": round(ASCE_WAVE_AT_SANFORD_CFS, 0)}
    with open(os.path.join(folder, "result.json"), "w", encoding="utf-8") as f:
        json.dump(res, f, indent=1)
    _say_run(res, say)
    return res


def midpoint(meta, name):
    return tuple(meta["dam_xy"][name])


def _read_stage(wse_grid, xy):
    """Median model water surface (ft) within READ_RADIUS_M of xy, or None if dry there."""
    gx, gy = xy
    ys = wse_grid.y0 - (np.arange(wse_grid.ny) + 0.5) * wse_grid.dx
    xs = wse_grid.x0 + (np.arange(wse_grid.nx) + 0.5) * wse_grid.dx
    r = np.where(np.abs(ys - gy) <= READ_RADIUS_M)[0]
    c = np.where(np.abs(xs - gx) <= READ_RADIUS_M)[0]
    if not r.size or not c.size:
        return None
    sub = wse_grid.data[np.ix_(r, c)]
    xx, yy = np.meshgrid(xs[c], ys[r])
    near = sub[(np.hypot(xx - gx, yy - gy) <= READ_RADIUS_M) & np.isfinite(sub)
               & (sub > wse_grid.nodata + 1)]
    return round(float(np.median(near)), 2) if near.size else None


def _say_run(res, say):
    for name in ("Edenville", "Sanford"):
        d = res["dams"].get(name, {})
        if d.get("modelled"):
            say("{}: peak breach {:,.0f} cfs, {:,.0f} ac flooded".format(
                name, d.get("peak_breach_cfs", 0), d.get("flooded_acres", 0)))
    if res.get("dams", {}).get("Edenville", {}).get("gauge_to_Sanford_peak_cfs"):
        say("wave reaching Sanford: {:,.0f} cfs (ASCE 2-D model ~{:,.0f})".format(
            res["dams"]["Edenville"]["gauge_to_Sanford_peak_cfs"], ASCE_WAVE_AT_SANFORD_CFS))
    s = res.get("midland_stage_ft")
    if s is not None:
        say("Midland: model {:.2f} ft, observed {:.2f} ft, difference {:+.2f} ft; "
            "peak flow {} cfs (observed 51,800)".format(
                s, res["midland_observed_ft"], res["midland_diff_ft"],
                "{:,.0f}".format(res["midland_peak_cfs"]) if res.get("midland_peak_cfs") else "?"))
    else:
        say("Midland: the modelled flood did not reach the gauge cell "
            "(no water surface there); see the warnings in result.json")


# -------------------------------------------------------------------------------- score
def score(root, say=print):
    rows = []
    for rj in sorted(glob.glob(os.path.join(root, "*", "cascade.json"))):
        d = os.path.dirname(rj)
        rp = os.path.join(d, "result.json")
        if not os.path.isfile(rp):
            continue
        with open(rp, encoding="utf-8") as f:
            rows.append(json.load(f))
    lines = ["# The 2020 Edenville -> Sanford cascade at the Tittabawassee gauge at Midland", "",
             "The Runner's own cascade engine at 30 m on USGS 3DEP terrain (LISFLOOD-FP). Edenville "
             "fails sunny-day at normal pool; the surge overtops and breaches Sanford; the combined "
             "flood is routed to the USGS Midland gauge. Dam-break only (no concurrent storm "
             "runoff), so the Midland peak is a lower bound.", "",
             "| Quantity | Model | Reference | Source |",
             "|---|---|---|---|"]
    for r in rows:
        eden = r["dams"].get("Edenville", {})
        san = r["dams"].get("Sanford", {})
        asce = r.get("references", {})
        def q(v):
            return "{:,.0f} cfs".format(v) if v else "--"
        lines.append("| Edenville breach peak | {} | {} | ASCE JHE 2-D model |".format(
            q(eden.get("peak_breach_cfs")), q(asce.get("asce_edenville_breach_cfs"))))
        lines.append("| Wave reaching Sanford | {} | {} | ASCE (~35 % attenuated) |".format(
            q(eden.get("gauge_to_Sanford_peak_cfs")), q(asce.get("asce_wave_at_sanford_cfs"))))
        lines.append("| Sanford breach peak | {} | -- | (overtopped by the surge) |".format(
            q(san.get("peak_breach_cfs"))))
        lines.append("| Peak flow at Midland | {} | {} | USGS 04156000 (rating) |".format(
            q(r.get("midland_peak_cfs")), q(r.get("midland_observed_cfs"))))
        ms, mo, md = r.get("midland_stage_ft"), r.get("midland_observed_ft"), r.get("midland_diff_ft")
        lines.append("| **Peak stage at Midland** | {} | {:.2f} ft NAVD88 | USGS gage height |".format(
            "**{:.2f} ft** ({:+.2f})".format(ms, md) if ms is not None else "did not reach gauge",
            mo))
        for d in r.get("decisions", []):
            lines.append("| _{}_ | {} | | |".format(
                d.get("dam"), ("fails" if d.get("fails") else "holds") + ": " + d.get("reason", "")))
    text = "\n".join(lines) + "\n"
    with open(os.path.join(root, "Edenville_Sanford_2020_Score.md"), "w", encoding="utf-8") as f:
        f.write(text)
    say(text)
    return rows


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--out", required=True)
    r = sub.add_parser("run")
    r.add_argument("folder")
    r.add_argument("--lisflood", default=None)
    r.add_argument("--threads", type=int, default=1)
    r.add_argument("--rule", choices=("overtop", "assume"), default="overtop")
    r.add_argument("--hours", type=float, default=None, help="cap the LISFLOOD sim clock (hr)")
    s = sub.add_parser("score")
    s.add_argument("root")
    a = ap.parse_args(argv)
    if a.cmd == "build":
        build(a.out)
    elif a.cmd == "run":
        run(a.folder, a.lisflood, a.threads, a.rule, a.hours)
    else:
        score(a.root)
    return 0


if __name__ == "__main__":
    sys.exit(main())
