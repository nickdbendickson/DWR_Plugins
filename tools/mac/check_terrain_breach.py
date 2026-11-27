"""Sunny-day check: today's routed breach against a notch lowered into the terrain.

DWR-2801 (Absaraka) and DWR-2710 (Raleigh). Nothing is downloaded. If the local
fixtures are not on this machine the script says so and exits 0. The runs themselves
are long, so they stay here for a Mac and are not started unless --run is given.

Looks for a dam folder that already holds dam.json and a DEM, under
$DAMSAFETY_TB_ROOT, ~/dwr-dam-suite, or a --root you pass. A finished pair is
<folder>/routed and <folder>/terrain (each a Runner output with run_report.json).
--run writes two configs from --config (a Runner config json whose dem.path exists)
and starts them with the command-line runner. Pass --town X,Y (the work CRS) to
also print depth and arrival there, which needs GDAL.

    python3 tools/mac/check_terrain_breach.py
    python3 tools/mac/check_terrain_breach.py --root ~/dwr-dam-suite --town -106000,2667000
    python3 tools/mac/check_terrain_breach.py --run --config ~/dwr/DWR-2801.json --root ~/dwr/out
"""
import argparse
import json
import os
import subprocess
import sys

DAMS = (("DWR-2801", "Absaraka"), ("DWR-2710", "Raleigh"))


def _roots(given):
    out = []
    if given:
        out.append(os.path.expanduser(given))
    env = os.environ.get("DAMSAFETY_TB_ROOT")
    if env:
        out.append(os.path.expanduser(env))
    out.append(os.path.expanduser("~/dwr-dam-suite"))
    return out


def _find_dam(root, dam_id):
    """A folder named for the dam that holds dam.json, or None."""
    if not root or not os.path.isdir(root):
        return None
    direct = os.path.join(root, dam_id)
    if os.path.isfile(os.path.join(direct, "dam.json")):
        return direct
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in (".git", "node_modules")]
        if os.path.basename(dirpath) == dam_id and "dam.json" in filenames:
            return dirpath
        if len(dirpath) - len(root) > 80:
            dirnames[:] = []
    return None


def _load(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _peak_routed(report):
    for key in ("peak_inflow_input_cfs", "peak_cfs", "routed_peak_cfs"):
        v = report.get(key)
        if isinstance(v, (int, float)):
            return v
    hydro = report.get("hydrograph") or {}
    v = hydro.get("peak_cfs")
    return v if isinstance(v, (int, float)) else None


def _report_lines(folder):
    rep = _load(os.path.join(folder, "run_report.json"))
    if not rep:
        return None
    tb = rep.get("terrain_breach") or {}
    routed = tb.get("routed_peak_cfs_not_injected")
    if routed is None:
        routed = _peak_routed(rep)
    terrain = tb.get("peak_outflow_cfs")
    return routed, terrain, tb.get("time_to_empty_hr"), tb.get("pool_volume_acft")


def _sample(folder, xy):
    """Depth (ft) and arrival (hr) at one point, or None without GDAL / the rasters."""
    try:
        from osgeo import gdal
    except Exception:
        return None
    out = {}
    for kind, name in (("depth", "Depth (Max).tif"), ("arrival", "Arrival Time.tif")):
        path = os.path.join(folder, name)
        ds = gdal.Open(path) if os.path.isfile(path) else None
        if ds is None:
            out[kind] = None
            continue
        gt = ds.GetGeoTransform()
        col = int((xy[0] - gt[0]) / gt[1])
        row = int((xy[1] - gt[3]) / gt[5])
        band = ds.GetRasterBand(1)
        if not (0 <= col < ds.RasterXSize and 0 <= row < ds.RasterYSize):
            out[kind] = None
            continue
        val = float(band.ReadAsArray(col, row, 1, 1)[0, 0])
        nod = band.GetNoDataValue()
        out[kind] = None if nod is not None and val == nod else val
    return out


def _compare(dam_id, place, folder, town):
    routed_dir = os.path.join(folder, "routed")
    terrain_dir = os.path.join(folder, "terrain")
    if not (os.path.isfile(os.path.join(routed_dir, "run_report.json"))
            and os.path.isfile(os.path.join(terrain_dir, "run_report.json"))):
        print("{} ({}): dam folder {}, no finished routed/ and terrain/ pair yet.".format(
            dam_id, place, folder))
        return
    a = _report_lines(routed_dir)
    b = _report_lines(terrain_dir)
    print("{} ({}): routed peak {} cfs; in-terrain peak {} cfs (routed, not injected, {} cfs); "
          "pool {:,} ac-ft; time to 5% {} hr.".format(
              dam_id, place,
              None if a is None else a[0],
              None if b is None else b[1],
              None if b is None else b[0],
              None if b is None or b[3] is None else b[3],
              None if b is None else b[2]))
    if town is None:
        return
    for label, path in (("routed", routed_dir), ("in-terrain", terrain_dir)):
        got = _sample(path, town)
        if not got:
            print("  {}: depth and arrival at the town need GDAL and the final rasters.".format(
                label))
            continue
        print("  {} at ({:.0f}, {:.0f}): depth {} ft, arrival {} hr.".format(
            label, town[0], town[1], got.get("depth"), got.get("arrival")))


def _dem_ok(cfg):
    dem = ((cfg.get("dem") or {}).get("path"))
    return bool(dem) and os.path.isfile(os.path.expanduser(str(dem)))


def _launch(cfg_path, root, dam_id):
    cfg = _load(cfg_path)
    if not cfg or not _dem_ok(cfg):
        print("{}: --run needs --config whose dem.path is a file on this machine. "
              "Nothing was started.".format(dam_id))
        return 0
    cli = os.path.join(os.path.dirname(__file__), "..", "..", "DamSafety_Runner", "scripts",
                       "ndbreach_cli.py")
    if not os.path.isfile(cli):
        cli = os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "ndbreach_cli.py")
    out = os.path.join(root, dam_id)
    os.makedirs(out, exist_ok=True)
    for label, flag in (("routed", False), ("terrain", True)):
        one = json.loads(json.dumps(cfg))
        model = one.setdefault("model", {})
        model["breach_in_terrain"] = flag
        model["tier"] = "lisflood"
        one.setdefault("scenario", {})["type"] = "sunny"
        one.setdefault("output", {})["folder"] = os.path.join(out, label)
        path = os.path.join(out, label + ".json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(one, f, indent=1)
            f.write("\n")
        print("starting {} ({})".format(dam_id, label))
        run = subprocess.run([sys.executable, cli, "run", path])
        if run.returncode != 0:
            print("{} {} failed ({})".format(dam_id, label, run.returncode))
            return run.returncode
    return 0


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--root", help="fixture / output root (no download if it is missing)")
    p.add_argument("--config", help="a Runner config json to copy for --run")
    p.add_argument("--run", action="store_true",
                   help="start the two sunny-day runs (long; a Mac, not the cloud)")
    p.add_argument("--town", help="x,y in the work CRS: depth and arrival there")
    args = p.parse_args()
    town = None
    if args.town:
        try:
            a, b = args.town.split(",")
            town = (float(a), float(b))
        except ValueError:
            print("--town needs x,y")
            return 2
    roots = _roots(args.root)
    found = False
    code = 0
    for dam_id, place in DAMS:
        folder = None
        for root in roots:
            folder = _find_dam(root, dam_id)
            if folder:
                break
        if folder is None:
            print("{} ({}): no local fixture (looked in {}). Skipped; nothing downloaded.".format(
                dam_id, place, ", ".join(roots)))
            continue
        found = True
        if args.run:
            if not args.config:
                print("{}: --run needs --config. Not started.".format(dam_id))
                continue
            code = _launch(args.config, os.path.dirname(folder), dam_id) or code
        _compare(dam_id, place, folder, town)
    if not found:
        print("No fixtures for DWR-2801 or DWR-2710. Skipped.")
    return code


if __name__ == "__main__":
    sys.exit(main())
