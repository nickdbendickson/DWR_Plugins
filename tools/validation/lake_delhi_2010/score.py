#!/usr/bin/env python3
"""Compare finished Runner floods of the Lake Delhi 2010 case with what was surveyed.

    <QGIS python> tools/validation/lake_delhi_2010/score.py CASE_FOLDER [--runs blind,as_observed]

CASE_FOLDER is the folder build_case.py wrote and the runs were made in. For every run found
under CASE_FOLDER/runs it reads the Runner's own outputs (`WSE (Max).tif`, `Depth (Max).tif`,
`_breach_model/run_report.json`, the gauge files) and writes, into CASE_FOLDER:

    Lake_Delhi_2010_Score.md     the tables below, for reading
    Lake_Delhi_2010_Score.csv    one row per run and high-water mark
    Lake_Delhi_2010_Score.json   everything, for a program

What is compared (observed.json holds the surveyed side and its sources):
  - the breach: width, time to form and peak outflow against the reported ones;
  - the lake: the peak pool against the surveyed high-water mark at the dam;
  - the flood: the highest modelled water surface at the surveyed high-water marks of the
    USGS, 0 to 21 river miles below the dam.

It judges nothing: there is no pass or fail here, only the differences. Exit status 0 when
every run that was asked for could be read, 1 when one could not, 2 for a bad call."""
import argparse
import csv
import glob
import json
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
OBSERVED = os.path.join(HERE, "observed.json")
DAM_FOLDER = "IA01297"
WET_FT = 0.5                # a cell counts as flooded from this depth
RADIUS_M = 60.0             # cells this near a mark's point are read (never less than 1.5 cells)
NAME = "Lake_Delhi_2010_Score"
FT = 0.3048
CFS = FT ** 3


class ScoreError(Exception):
    pass


def observed():
    with open(OBSERVED, encoding="utf-8") as f:
        return json.load(f)


def marks(ob):
    """[(mark id, side, location, miles below the dam, lon, lat, surveyed ft NAVD 88)]."""
    shift = float(ob["datum"]["ngvd29_to_navd88_ft"])
    out = []
    for m in ob["observed"]["high_water_marks"]:
        for side in ("upstream", "downstream"):
            z = m.get(side + "_ft_ngvd29")
            pt = (m.get("points") or {}).get(side)
            if z is None or not pt:
                continue
            out.append((m["id"], side, m["location"], float(m["miles_below_dam"]),
                        float(pt[0]), float(pt[1]), float(z) + shift))
    return out


# ---------------------------------------------------------------------------- rasters
class Raster:
    """A band as an array with what is needed to read it at a point."""

    def __init__(self, array, geotransform, nodata=None, wkt=None):
        self.a, self.gt, self.nodata, self.wkt = array, geotransform, nodata, wkt

    @classmethod
    def open(cls, path):
        from osgeo import gdal
        gdal.UseExceptions()
        ds = gdal.Open(path)
        band = ds.GetRasterBand(1)
        gt = ds.GetGeoTransform()
        if abs(gt[2]) > 1e-9 or abs(gt[4]) > 1e-9:
            raise ScoreError("a rotated raster is not read: " + path)
        return cls(band.ReadAsArray().astype("float64"), gt, band.GetNoDataValue(),
                   ds.GetProjection())

    @property
    def cell(self):
        return abs(self.gt[1])

    def near(self, x, y, radius):
        """(values, distances) of the cells whose centres are within `radius` of x, y."""
        import numpy as np
        gt, a = self.gt, self.a
        c, r = (x - gt[0]) / gt[1] - 0.5, (y - gt[3]) / gt[5] - 0.5
        k = int(math.ceil(radius / self.cell))
        r0, r1 = max(int(round(r)) - k, 0), min(int(round(r)) + k + 1, a.shape[0])
        c0, c1 = max(int(round(c)) - k, 0), min(int(round(c)) + k + 1, a.shape[1])
        if r0 >= r1 or c0 >= c1:
            return np.array([]), np.array([])
        rr, cc = np.mgrid[r0:r1, c0:c1]
        d = np.hypot((cc - c) * gt[1], (rr - r) * gt[5])
        v = a[r0:r1, c0:c1]
        ok = (d <= radius) & np.isfinite(v)
        if self.nodata is not None:
            ok &= v != self.nodata
        return v[ok], d[ok]


def transformer(wkt):
    """lon, lat (WGS 84) -> x, y of a raster's CRS."""
    from osgeo import osr
    osr.UseExceptions()
    a, b = osr.SpatialReference(), osr.SpatialReference()
    a.ImportFromEPSG(4326)
    a.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    b.ImportFromWkt(wkt)
    b.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    tr = osr.CoordinateTransformation(a, b)
    return lambda lon, lat: tr.TransformPoint(lon, lat)[:2]


def model_wse(wse, depth, x, y, radius=RADIUS_M, wet_ft=WET_FT):
    """The modelled highest water surface (ft) at a point: the median over the flooded cells
    within the radius, or None where none is flooded (the model keeps the point dry)."""
    import numpy as np
    radius = max(radius, 1.5 * wse.cell)
    w, dw = wse.near(x, y, radius)
    h, dh = depth.near(x, y, radius)
    if len(w) != len(h) or not len(w):
        return None, 0
    wet = h >= wet_ft
    if not wet.any():
        return None, 0
    return float(np.median(w[wet])), int(wet.sum())


# ---------------------------------------------------------------------------- one run
def _first(d, *keys):
    for k in keys:
        if isinstance(d, dict) and d.get(k) is not None:
            return d[k]
    return None


def read_report(folder):
    path = os.path.join(folder, "_breach_model", "run_report.json")
    if not os.path.isfile(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def breach_facts(report):
    """What the run says about its breach and lake, under fixed names (None where the report
    has no such value)."""
    hy, sm = report.get("hydrograph") or {}, report.get("summary") or {}
    fl = report.get("flood") or {}
    return {
        "engine": _first(sm, "final_model"),
        "breach_width_ft": _first(hy, "avg_breach_width_ft"),
        "formation_hr": _first(hy, "formation_time_hr"),
        "peak_outflow_cfs": _first(hy, "peak_cfs", "peak_total_cfs") or _first(sm, "peak_breach_cfs"),
        "peak_breach_only_cfs": _first(fl, "peak_breach_component_cfs"),
        "peak_outflow_no_breach_cfs": _first(fl, "peak_outflow_no_breach_cfs"),
        "pool_at_breach_ft": _first(hy, "pool_at_breach_ft", "starting_wse_ft"),
        "storage_at_breach_acft": _first(hy, "storage_at_failure_acft"),
        "time_to_peak_hr": _first(hy, "time_to_peak_hr"),
        "peak_pool_no_breach_ft": _first(hy, "peak_pool_ft_no_breach", "peak_pool_ft"),
        "breach_at_hr": _first(hy, "breach_initiation_hr"),
        "cell_m": _first(report.get("settings") or {}, "cell_m", "cell_size_m"),
        "elapsed_min": report.get("elapsed_min"),
    }


def gauge_peaks(folder):
    """{gauge name: (peak cfs, hours of the peak)} from the run's gauge files, if any."""
    out = {}
    for path in sorted(glob.glob(os.path.join(glob.escape(folder), "_breach_model", "gauge_*.csv"))):
        t, q = [], []
        with open(path, encoding="utf-8") as f:
            for row in csv.reader(f):
                try:
                    t.append(float(row[0]))
                    q.append(float(row[1]))
                except (ValueError, IndexError):
                    continue
        if q:
            k = max(range(len(q)), key=lambda i: abs(q[i]))
            out[os.path.basename(path)[6:-4]] = (abs(q[k]), t[k], t[-1])
    return out


def score_run(case, name, ob):
    folder = os.path.join(case, "runs", name, DAM_FOLDER)
    wse_p, dep_p = os.path.join(folder, "WSE (Max).tif"), os.path.join(folder, "Depth (Max).tif")
    for p in (wse_p, dep_p):
        if not os.path.isfile(p):
            raise ScoreError("run '{}' has no {} (not finished?)".format(name, os.path.basename(p)))
    wse, depth = Raster.open(wse_p), Raster.open(dep_p)
    to_xy = transformer(wse.wkt)
    rows = []
    for mid, side, loc, miles, lon, lat, z_obs in marks(ob):
        x, y = to_xy(lon, lat)
        z, n = model_wse(wse, depth, x, y)
        rows.append({"run": name, "mark": mid, "side": side, "location": loc,
                     "miles_below_dam": miles, "surveyed_ft": round(z_obs, 2),
                     "model_ft": None if z is None else round(z, 2),
                     "model_minus_surveyed_ft": None if z is None else round(z - z_obs, 2),
                     "flooded_cells_read": n})
    facts = breach_facts(read_report(folder))
    return {"run": name, "folder": folder, "facts": facts, "marks": rows,
            "gauges": gauge_peaks(folder), "summary": summarize(rows)}


def summarize(rows):
    """Mean, mean absolute, and largest difference over the marks below the dam (the mark at
    the dam itself, miles 0, is left out: it is the tailwater, listed on its own)."""
    d = [r["model_minus_surveyed_ft"] for r in rows
         if r["model_minus_surveyed_ft"] is not None and r["miles_below_dam"] > 0]
    dry = sum(1 for r in rows if r["model_ft"] is None)
    if not d:
        return {"marks": 0, "dry": dry}
    return {"marks": len(d), "dry": dry, "mean_ft": round(sum(d) / len(d), 2),
            "mean_abs_ft": round(sum(abs(v) for v in d) / len(d), 2),
            "largest_ft": round(max(d, key=abs), 2)}


# ---------------------------------------------------------------------------- report
def _n(v, fmt="{:,.0f}"):
    return "-" if v is None else fmt.format(v)


def markdown(ob, results, case_info=None):
    b, o = ob["breach"], ob["observed"]
    shift = float(ob["datum"]["ngvd29_to_navd88_ft"])
    L = ["# Lake Delhi Dam, 24 July 2010: the Runner against what was surveyed", ""]
    L += ["Elevations are feet NAVD 88 (published NGVD 29 values moved by {:+.2f} ft, good to "
          "about {:g} ft). Differences are model minus surveyed.".format(
              shift, ob["datum"]["uncertainty_ft"]), ""]
    if case_info:
        L += ["Inflow at the dam: the Manchester gauge x {:g}. Model cell {:g} m.".format(
            case_info.get("inflow_factor"), case_info.get("cell_m")), ""]
    L += ["## The breach and the lake", "",
          "| | Reported | " + " | ".join(r["run"] for r in results) + " |",
          "|---|---|" + "---|" * len(results)]

    def line(label, rep, key, fmt="{:,.0f}"):
        L.append("| {} | {} | {} |".format(label, rep, " | ".join(
            _n(r["facts"].get(key), fmt) for r in results)))
    line("Breach width, ft", "{:g} (largest)".format(b["largest_width_ft"]), "breach_width_ft")
    line("Time to form, h", "about 2.2 (12:30 to the 200-ft report at 2:40 p.m.)", "formation_hr", "{:.2f}")
    line("Peak outflow, cfs", "{:,.0f} (the panel's estimate)".format(b["peak_outflow_cfs"]), "peak_outflow_cfs")
    line("Pool when the breach starts, ft", "{:.2f} (surveyed peak pool)".format(
        o["peak_pool_ft_ngvd29"] + shift), "pool_at_breach_ft", "{:.2f}")
    L += ["", "## High-water marks below the dam", ""]
    for r in results:
        s = r["summary"]
        L += ["### Run `{}`".format(r["run"]), "",
              "Engine: {}.".format(r["facts"].get("engine") or "not recorded"), "",
              "| Mark | Miles below the dam | Side of the bridge | Surveyed, ft | Model, ft | Difference, ft |",
              "|---|---|---|---|---|---|"]
        for m in r["marks"]:
            L.append("| {} | {:.2f} | {} | {:.2f} | {} | {} |".format(
                m["location"], m["miles_below_dam"], m["side"], m["surveyed_ft"],
                "dry in the model" if m["model_ft"] is None else "{:.2f}".format(m["model_ft"]),
                "-" if m["model_minus_surveyed_ft"] is None else "{:+.2f}".format(
                    m["model_minus_surveyed_ft"])))
        if s.get("marks"):
            L += ["", "Over the {} marks below the dam (the one at the dam left out): mean "
                  "difference {:+.2f} ft, mean size {:.2f} ft, largest {:+.2f} ft.{}".format(
                      s["marks"], s["mean_ft"], s["mean_abs_ft"], s["largest_ft"],
                      " {} mark(s) dry in the model.".format(s["dry"]) if s["dry"] else "")]
        if r["gauges"]:
            L += ["", "| Flow past | Peak, cfs | Hours after noon on 23 July | Run ends at |", "|---|---|---|---|"]
            for g, (q, t, t_end) in sorted(r["gauges"].items()):
                L.append("| {} | {:,.0f} | {:.1f} | {:.1f} |".format(g, q, t, t_end))
        L.append("")
    L += ["Sources: " + "; ".join(ob["sources"][k] for k in ("eash2012", "mcdaniel", "nwis")), ""]
    return "\n".join(L)


def write(case, ob, results):
    info = None
    try:
        with open(os.path.join(case, "case.json"), encoding="utf-8") as f:
            info = json.load(f)
    except (OSError, ValueError):
        pass
    base = os.path.join(case, NAME)
    with open(base + ".md", "w", encoding="utf-8") as f:
        f.write(markdown(ob, results, info))
    cols = ["run", "mark", "side", "location", "miles_below_dam", "surveyed_ft", "model_ft",
            "model_minus_surveyed_ft", "flooded_cells_read"]
    with open(base + ".csv", "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, cols)
        w.writeheader()
        for r in results:
            w.writerows(r["marks"])
    with open(base + ".json", "w", encoding="utf-8") as f:
        json.dump({"case": ob["case"], "inputs": info, "runs": results}, f, indent=1)
    return base + ".md"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("case")
    ap.add_argument("--runs", default="", help="run names, comma separated (default: every "
                                               "folder under CASE_FOLDER/runs)")
    args = ap.parse_args(argv)
    case = os.path.abspath(os.path.expanduser(args.case))
    root = os.path.join(case, "runs")
    names = [n for n in args.runs.split(",") if n] or (
        sorted(os.listdir(root)) if os.path.isdir(root) else [])
    if not names:
        print("score: no runs under " + root)
        return 2
    ob = observed()
    results, bad = [], 0
    for n in names:
        try:
            results.append(score_run(case, n, ob))
        except ScoreError as e:
            print("score: " + str(e))
            bad += 1
    if results:
        path = write(case, ob, results)
        for r in results:
            s = r["summary"]
            print("{}: {} marks below the dam, mean difference {} ft, mean size {} ft".format(
                r["run"], s.get("marks"), s.get("mean_ft"), s.get("mean_abs_ft")))
        print("written: " + path)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
