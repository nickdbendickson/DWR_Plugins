#!/usr/bin/env python3
"""Make the Runner's inputs for the Lake Delhi Dam failure of 24 July 2010 (Iowa).

    <QGIS python> tools/validation/lake_delhi_2010/build_case.py [--out FOLDER]

Writes into FOLDER (default ~/DamSafety/Validation/Lake_Delhi_2010):

    terrain/lake_delhi_10m_5070.tif   USGS 3DEP 10 m terrain, dam to Monticello (a 21 MB download)
    inputs/inflow.csv                 hours, cfs: the Manchester gauge record, scaled to the dam
    inputs/spillway_rating.csv        pool (ft), cfs: the three gates as they stood in the flood
    inputs/gauges.geojson             the four surveyed bridges: the run records the flow past each
    blind.json                        run 1: the Runner works the breach out itself
    as_observed.json                  run 2: the breach width and time are the observed ones
    case.json                         what was written, with every assumption

and prints the two run commands. score.py then compares the finished runs with the surveyed
high-water marks. Every published number comes from observed.json beside this file. One number
is fitted, and to the lake, not to the flood below the dam: INFLOW_FACTOR (see there). Needs GDAL for the terrain (QGIS's Python has it) and the
internet for the download; --no-terrain writes everything else.

Elevations in the written files are NAVD 88 feet (the terrain's datum); observed.json says
how the published NGVD 29 values are moved."""
import argparse
import csv
import datetime
import json
import math
import os
import sys
import time
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
OBSERVED = os.path.join(HERE, "observed.json")
GAUGE_CSV = os.path.join(HERE, "manchester_05416900_iv.csv")
DEFAULT_OUT = os.path.join("~", "DamSafety", "Validation", "Lake_Delhi_2010")
DEM_NAME = "lake_delhi_10m_5070.tif"
SERVICE = "https://elevation.nationalmap.gov/arcgis/rest/services/3DEPElevation/ImageServer"
TILE_PX = 2000
G_FT = 32.174
ORIFICE_CD = 0.70                  # a vertical lift gate with water against it (an assumption)
HOLD_BAND_FT = 0.25                # the gates hold the normal pool: no release this far below it
START_CDT = "2010-07-23 12:00"     # the first inflow row (the pool is still at normal pool)
END_CDT = "2010-07-25 12:00"       # the last one: 23 hours after the breach
# The flow into the lake was not gauged: the gauge is 11.7 river miles up, with 275 of the dam's
# 347 square miles above it. Flow at the dam = flow at the gauge x INFLOW_FACTOR. The plain
# drainage-area ratio (1.26) puts water over the dam at 3:20 a.m. and the pool 2.3 ft over the
# top by the time of the breach; it was seen over the road at 9:40 a.m. and the surveyed peak
# pool is 0.75 ft over the top. 1.10 gives 9:24 a.m. and 0.9 ft (reservoir routing only, no
# flood model). So the heavy rain fell mostly above the gauge, and 1.10 is used.
INFLOW_FACTOR = 1.10
RUNS = ("blind", "as_observed")
FMT = "%Y-%m-%d %H:%M"


def observed():
    with open(OBSERVED, encoding="utf-8") as f:
        return json.load(f)


def when(text):
    return datetime.datetime.strptime(text, FMT)


def hours_between(a, b):
    return (when(b) - when(a)).total_seconds() / 3600.0


def navd(ob, ft_ngvd29):
    return ft_ngvd29 + float(ob["datum"]["ngvd29_to_navd88_ft"])


def gauge_rows(path=GAUGE_CSV):
    """[(datetime, cfs)] of the gauge record."""
    out = []
    with open(path, encoding="utf-8") as f:
        for row in csv.reader(l for l in f if not l.startswith("#")):
            if not row or row[0] == "datetime_cdt":
                continue
            out.append((when(row[0]), float(row[1])))
    if len(out) < 2:
        raise SystemExit("build_case: no gauge rows in " + path)
    return out


def area_factor(ob):
    """The dam's drainage area over the gauge's (the same runoff per square mile from the land
    in between): the upper bound of the inflow factor. See INFLOW_FACTOR."""
    return float(ob["inflow"]["dam_drainage_mi2"]) / float(ob["inflow"]["gauge_drainage_mi2"])


def inflow_rows(ob, factor=None, lag_hr=0.0, start=START_CDT, end=END_CDT, rows=None):
    """[(hours from `start`, cfs)] at the dam: the gauge record times `factor` (None:
    INFLOW_FACTOR), `lag_hr` later."""
    factor = INFLOW_FACTOR if factor is None else float(factor)
    t0, t1 = when(start), when(end)
    lag = datetime.timedelta(hours=float(lag_hr))
    out = [((t + lag - t0).total_seconds() / 3600.0, q * factor)
           for t, q in (rows or gauge_rows()) if t0 <= t + lag <= t1]
    if len(out) < 2 or out[0][0] > 1e-6:
        raise SystemExit("build_case: the gauge record does not cover {} to {}".format(start, end))
    return out


def gate_cfs(pool_ft, crest_ft, width_ft, opening_ft, full_at_top_cfs, top_ft):
    """One gate. Clear of the water it passes the published capacity at the top of dam, scaled
    with the head to the 1.5 (a weir). With its lip in the water it is an orifice, and never
    passes more than the open gate would."""
    h = pool_ft - crest_ft
    if h <= 0:
        return 0.0
    weir = full_at_top_cfs * (h / (top_ft - crest_ft)) ** 1.5
    if opening_ft is None or h <= opening_ft:
        return weir
    orifice = ORIFICE_CD * width_ft * opening_ft * math.sqrt(2.0 * G_FT * (h - opening_ft / 2.0))
    return min(weir, orifice)


def gates_cfs(ob, pool_ft_ngvd29):
    """The three gates as they stood in the flood: two fully open, Gate 3 at 4.25 ft."""
    sp, dam = ob["spillway"], ob["dam"]
    one = float(sp["capacity_all_open_at_top_cfs"]) / int(sp["gates"])
    a = (pool_ft_ngvd29, float(sp["crest_ft_ngvd29"]), float(sp["gate_width_ft"]))
    top = float(dam["top_of_dam_ft_ngvd29"])
    return 2.0 * gate_cfs(*a, None, one, top) + gate_cfs(*a, float(sp["gate3_opening_ft"]), one, top)


def rating_rows(ob, step_ft=0.25, above_top_ft=6.0):
    """[(pool ft NAVD 88, cfs)]: nothing until HOLD_BAND_FT below the normal pool (the gates
    were worked to hold it), all the gates could pass from the normal pool up."""
    dam = ob["dam"]
    pool, top = float(dam["normal_pool_ft_ngvd29"]), float(dam["top_of_dam_ft_ngvd29"])
    out = [(navd(ob, pool - 5.0), 0.0), (navd(ob, pool - HOLD_BAND_FT), 0.0)]
    z = pool
    while z <= top + above_top_ft + 1e-9:
        out.append((navd(ob, z), gates_cfs(ob, z)))
        z += step_ft
    return out


def surface_area_top_ac(ob):
    """The Runner's storage curve is V = Vtop (y / H)^m with m = area_top x H / Vtop. This is
    the area at the top of dam that makes the curve pass through BOTH published storages
    (normal pool and top of dam); the lake's 440 acres at normal pool is not used."""
    dam = ob["dam"]
    h = float(dam["embankment_height_ft"])
    y = h - (float(dam["top_of_dam_ft_ngvd29"]) - float(dam["normal_pool_ft_ngvd29"]))
    vt, vn = float(dam["storage_at_top_acft"]), float(dam["storage_at_normal_pool_acft"])
    m = math.log(vn / vt) / math.log(y / h)
    return m * vt / h


def to_5070(lon, lat):
    from osgeo import osr
    osr.UseExceptions()
    a, b = osr.SpatialReference(), osr.SpatialReference()
    a.ImportFromEPSG(4326)
    a.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    b.ImportFromEPSG(5070)
    x, y = osr.CoordinateTransformation(a, b).TransformPoint(float(lon), float(lat))[:2]
    return x, y


def breach_hours(ob, start=START_CDT):
    """(hours from `start` to the moment the breach speeds up, hours it took to form). Formed
    is taken as the 200-ft report: the last width with a time."""
    b = ob["breach"]
    return (hours_between(start, b["breach_speeds_up_cdt"]),
            hours_between(b["breach_speeds_up_cdt"], b["width_200ft_cdt"]))


def configs(ob, out, dam_xy, cell_m=30.0, threads=0, sim_hr=None, inflow_xy=None, manning_n=None):
    """{run name: configuration} for ndbreach_cli.py."""
    dam = ob["dam"]
    t_breach, t_form = breach_hours(ob)
    sim_hr = sim_hr if sim_hr else hours_between(START_CDT, END_CDT)
    base = {
        "_case": ob["case"],
        "dem": {"path": os.path.join("terrain", DEM_NAME), "z_units": "meters"},
        "dam": {"dam_id": "IA01297", "name": dam["name"], "point": [round(dam_xy[0], 1), round(dam_xy[1], 1)],
                "inflow_point": [round(v, 1) for v in inflow_xy] if inflow_xy else None,
                "dam_height_ft": dam["embankment_height_ft"],
                "storage_top_acft": dam["storage_at_top_acft"],
                "top_of_dam_ft": navd(ob, dam["top_of_dam_ft_ngvd29"]),
                "surface_area_top_ac": round(surface_area_top_ac(ob), 1),
                "crest_width_ft": dam["crest_width_ft"], "us_slope_h": dam["upstream_slope_h"],
                "ds_slope_h": dam["downstream_slope_h"],
                "crest_length_ft": dam["embankment_length_ft"]},
        "scenario": {"type": "flood", "inflow_csv": os.path.join("inputs", "inflow.csv"),
                     "spillway_csv": os.path.join("inputs", "spillway_rating.csv"),
                     "normal_pool_ft": navd(ob, dam["normal_pool_ft_ngvd29"]),
                     "trigger": "time", "trigger_value": round(t_breach, 3), "baseline": False},
        "model": {"cell_size_m": cell_m, "sim_time_hr": sim_hr, "grid_refine": False,
                  "initial_length_mi": 22.0, "half_width_mi": 1.5, "max_extent_mi": 22.0,
                  "max_iterations": 1, "threads": threads, "elev_datum": "NAVD88"},
        "layers": {"gauges": os.path.join("inputs", "gauges.geojson"), "gauge_name_field": "name"},
    }
    if manning_n:                    # left out: the suite's shared default (0.06) is used
        base["manning"] = {"n": float(manning_n)}
    runs = {}
    for name in RUNS:
        cfg = json.loads(json.dumps(base))
        cfg["breach"] = {"method": "overtopping", "overrides": {}}
        if name == "as_observed":
            cfg["breach"]["overrides"] = {"avg_width_ft": ob["breach"]["largest_width_ft"],
                                          "formation_hr": round(t_form, 3)}
        cfg["output"] = {"folder": os.path.join("runs", name)}
        cfg["_run"] = ("The Runner works out the breach from the dam's height and storage."
                       if name == "blind" else
                       "The breach is {:g} ft wide and forms in {:.2f} h, as observed.".format(
                           ob["breach"]["largest_width_ft"], t_form))
        runs[name] = cfg
    return runs


# ---------------------------------------------------------------------------- terrain
def tiles(box, cell, tile_px=TILE_PX):
    x0, y0, x1, y1 = box
    nx, ny = int(round((x1 - x0) / cell)), int(round((y1 - y0) / cell))
    out = []
    for j in range(0, ny, tile_px):
        for i in range(0, nx, tile_px):
            w, h = min(tile_px, nx - i), min(tile_px, ny - j)
            out.append((x0 + i * cell, y1 - (j + h) * cell, x0 + (i + w) * cell, y1 - j * cell, w, h))
    return out


def tile_url(t):
    q = {"bbox": "{},{},{},{}".format(*t[:4]), "bboxSR": 5070, "imageSR": 5070,
         "size": "{},{}".format(t[4], t[5]), "format": "tiff", "pixelType": "F32",
         "interpolation": "RSP_BilinearInterpolation", "f": "image"}
    return SERVICE + "/exportImage?" + urllib.parse.urlencode(q)


def fetch(url, timeout=180):
    req = urllib.request.Request(url, headers={"User-Agent": "DamSafety-suite"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def get_terrain(ob, folder, say=print, fetch=fetch):
    """Download the terrain of observed.json's box in parts, join them, check them."""
    from osgeo import gdal
    gdal.UseExceptions()
    box, cell = ob["terrain"]["box"], float(ob["terrain"]["cell_m"])
    dem = os.path.join(folder, DEM_NAME)
    if os.path.isfile(dem):
        say("terrain: already there ({})".format(dem))
        return dem
    os.makedirs(folder, exist_ok=True)
    parts = []
    todo = tiles(box, cell)
    for k, t in enumerate(todo):
        part = os.path.join(folder, "part_{:02d}.tif".format(k))
        if not os.path.isfile(part):
            for attempt in (1, 2, 3):
                try:
                    raw = fetch(tile_url(t))
                    if raw[:4] not in (b"II*\x00", b"MM\x00*"):
                        raise OSError("the elevation service did not send a picture")
                    with open(part + ".tmp", "wb") as f:
                        f.write(raw)
                    os.replace(part + ".tmp", part)
                    break
                except OSError as e:
                    if attempt == 3:
                        raise SystemExit("build_case: the terrain download failed ({}). Run it "
                                         "again: finished parts are kept.".format(e))
                    time.sleep(5 * attempt)
        parts.append(part)
        say("terrain: part {} of {}".format(k + 1, len(todo)))
    vrt = gdal.BuildVRT(os.path.join(folder, "parts.vrt"), parts)
    out = gdal.Translate(dem + ".part.tif", vrt, creationOptions=[
        "COMPRESS=DEFLATE", "PREDICTOR=3", "TILED=YES"])
    vrt = None
    band = out.GetRasterBand(1)
    band.SetUnitType("metre")
    nx, ny = out.RasterXSize, out.RasterYSize
    lo, hi = band.ComputeRasterMinMax(False)
    out.FlushCache()
    out = None
    want = (int(round((box[2] - box[0]) / cell)), int(round((box[3] - box[1]) / cell)))
    if (nx, ny) != want or not (100.0 < lo < hi < 500.0):
        os.remove(dem + ".part.tif")
        raise SystemExit("build_case: the terrain is not usable ({} x {} cells, {:.1f} to {:.1f} m)"
                         .format(nx, ny, lo, hi))
    os.replace(dem + ".part.tif", dem)
    for p in parts + [os.path.join(folder, "parts.vrt")]:
        os.remove(p)
    say("terrain: {} ({} x {} cells, {:.0f} to {:.0f} m)".format(dem, nx, ny, lo, hi))
    return dem


# ---------------------------------------------------------------------------- main
def write_inputs(ob, out, factor=None, lag_hr=0.0):
    os.makedirs(os.path.join(out, "inputs"), exist_ok=True)
    rows = inflow_rows(ob, factor, lag_hr)
    with open(os.path.join(out, "inputs", "inflow.csv"), "w", encoding="utf-8", newline="") as f:
        f.write("hours,inflow_cfs\n")
        for t, q in rows:
            f.write("{:.2f},{:.0f}\n".format(t, q))
    rate = rating_rows(ob)
    with open(os.path.join(out, "inputs", "spillway_rating.csv"), "w", encoding="utf-8", newline="") as f:
        f.write("pool_ft_navd88,discharge_cfs\n")
        for z, q in rate:
            f.write("{:.2f},{:.0f}\n".format(z, q))
    feats = [{"type": "Feature", "properties": {"name": m["id"]},
              "geometry": {"type": "Point", "coordinates": [round(v, 1) for v in to_5070(*m["bridge"])]}}
             for m in ob["observed"]["high_water_marks"] if m.get("bridge")]
    with open(os.path.join(out, "inputs", "gauges.geojson"), "w", encoding="utf-8") as f:
        json.dump({"type": "FeatureCollection", "name": "lake_delhi_2010_bridges",
                   "crs": {"type": "name", "properties": {"name": "urn:ogc:def:crs:EPSG::5070"}},
                   "features": feats}, f, indent=1)
    return rows, rate


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--cell", type=float, default=30.0, help="model cell, m (default 30)")
    ap.add_argument("--threads", type=int, default=0)
    ap.add_argument("--hours", type=float, default=None,
                    help="hours of flood to model, from noon on 23 July (default {:g}: to noon on "
                         "25 July; the breach is at 24.5)".format(hours_between(START_CDT, END_CDT)))
    ap.add_argument("--inflow-factor", type=float, default=None,
                    help="flow at the dam over flow at the gauge (default {:g}: see "
                         "INFLOW_FACTOR in this file; the drainage-area ratio is 1.26)".format(
                             INFLOW_FACTOR))
    ap.add_argument("--inflow-lag-hr", type=float, default=0.0,
                    help="hours the gauge's flow takes to reach the dam (default 0)")
    ap.add_argument("--manning-n", type=float, default=None,
                    help="one Manning's n for the whole valley (default: the suite's shared "
                         "default, 0.06)")
    ap.add_argument("--no-terrain", action="store_true", help="do not download the terrain")
    args = ap.parse_args(argv)
    ob = observed()
    out = os.path.abspath(os.path.expanduser(args.out))
    os.makedirs(out, exist_ok=True)
    rows, rate = write_inputs(ob, out, args.inflow_factor, args.inflow_lag_hr)
    if not args.no_terrain:
        get_terrain(ob, os.path.join(out, "terrain"))
    xy = to_5070(ob["dam"]["lon"], ob["dam"]["lat"])
    runs = configs(ob, out, xy, args.cell, args.threads, args.hours,
                   to_5070(*ob["dam"]["outflow_point"]), args.manning_n)
    for name, cfg in runs.items():
        with open(os.path.join(out, name + ".json"), "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=1)
    factor = INFLOW_FACTOR if args.inflow_factor is None else args.inflow_factor
    t_breach, t_form = breach_hours(ob)
    case = {"case": ob["case"], "time_zero_cdt": START_CDT, "inflow_factor": round(factor, 4),
            "inflow_lag_hr": args.inflow_lag_hr, "manning_n": args.manning_n, "inflow_peak_cfs": round(max(q for _t, q in rows)),
            "gates_at_top_of_dam_cfs": round(gates_cfs(ob, ob["dam"]["top_of_dam_ft_ngvd29"])),
            "breach_starts_hr": round(t_breach, 3), "breach_formation_hr_as_observed": round(t_form, 3),
            "surface_area_top_ac": round(surface_area_top_ac(ob), 1), "cell_m": args.cell,
            "ngvd29_to_navd88_ft": ob["datum"]["ngvd29_to_navd88_ft"], "runs": list(runs)}
    with open(os.path.join(out, "case.json"), "w", encoding="utf-8") as f:
        json.dump(case, f, indent=1)
    cli = os.path.join(REPO, "DamSafety_Runner", "scripts", "ndbreach_cli.py")
    print("Lake Delhi 2010 case written to " + out)
    print("  inflow at the dam: the Manchester gauge x {:.2f}, peak {:,.0f} cfs; the gates pass "
          "{:,.0f} cfs at the top of dam".format(factor, case["inflow_peak_cfs"],
                                                case["gates_at_top_of_dam_cfs"]))
    print("Run both, then score them:")
    for name in runs:
        print('  "{}" "{}" run "{}"'.format(sys.executable, cli, os.path.join(out, name + ".json")))
    print('  "{}" "{}" "{}"'.format(sys.executable, os.path.join(HERE, "score.py"), out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
