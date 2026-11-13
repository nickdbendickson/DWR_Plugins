#!/usr/bin/env python3
"""Pack what a finished batch left in the dams folder into small zip files to hand on.

A dam folder holds gigabytes (the terrain, the LISFLOOD-FP run, snapshots). Scoring another
tool against the Runner needs very little of it, per dam:
  * the four result rasters: Depth (Max).tif, Velocity (Max).tif, Arrival Time.tif, WSE (Max).tif
  * dam.json (what the run used: height, storage, crest, breach)
  * the Runner's report and manifest, and the small text files beside them (_breach_model/)
  * the Screening's small text files (_screening_model/) and the chain's state (_chain_nd/)
and for the batch: its summary CSVs and the watcher's findings.

It writes <out>/batch44_results_part1.zip, _part2.zip, ... each about --max-mb at most, with a
MANIFEST.csv in every part (one line per dam: status, cell, what was packed, what was missing).
Nothing in the dams folder is changed. Any Python 3, no extra packages.

Usage:
  python3 collect_results_44.py                                  # ~/ND_Dams -> ~/Desktop/batch44_results
  python3 collect_results_44.py --dams-folder ~/ND_Dams_Batch44
  python3 collect_results_44.py --only DWR-3034,DWR-4031 --out ~/Desktop/two_dams
  python3 collect_results_44.py --list                           # say what would be packed, write nothing
"""
import argparse
import csv
import io
import json
import os
import sys
import time
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
RASTERS = ("Depth (Max).tif", "Velocity (Max).tif", "Arrival Time.tif", "WSE (Max).tif")
# Small text files worth keeping from the model folders: reports, manifests, hydrographs, the
# LISFLOOD-FP control files. Rasters, snapshots and logs there are what makes a dam folder big.
TEXT_EXT = (".json", ".md", ".csv", ".txt", ".par", ".bci", ".bdy", ".geojson")
TEXT_FOLDERS = ("_breach_model", "_screening_model", "_chain_nd")
TEXT_MAX = 2 * 1024 * 1024
BATCH_FILES = ("batch44_lisflood_summary.csv", "Chain_Summary_ND.csv", "Chain_Batch_Summary.csv",
               "BATCH_FINDINGS.md")
MANIFEST_COLS = ["dam_id", "name", "status", "nd_class", "cell_m", "rasters", "missing", "files", "mb", "part"]


def read_ids(path, only):
    with open(path, newline="", encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))
    if only:
        want = {s.strip() for s in only.split(",") if s.strip()}
        missing = want - {r["dam_id"] for r in rows}
        if missing:
            sys.exit("Not in %s: %s" % (os.path.basename(path), ", ".join(sorted(missing))))
        rows = [r for r in rows if r["dam_id"] in want]
    return rows


def load_json(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def find_rasters(folder):
    """{name: path} for the result rasters: the dam folder's own, else the newest one a run
    subfolder holds (one level down; a model folder's working rasters are never taken)."""
    found = {}
    for name in RASTERS:
        p = os.path.join(folder, name)
        if os.path.isfile(p) and os.path.getsize(p) > 0:
            found[name] = p
            continue
        cands = []
        for sub in sorted(os.listdir(folder)) if os.path.isdir(folder) else []:
            q = os.path.join(folder, sub, name)
            if not sub.startswith("_") and os.path.isfile(q) and os.path.getsize(q) > 0:
                cands.append(q)
        if cands:
            found[name] = max(cands, key=os.path.getmtime)
    return found


def text_files(folder):
    """Small text files under the model and chain folders, as paths relative to the dam folder."""
    out = []
    for top in TEXT_FOLDERS:
        base = os.path.join(folder, top)
        for root, _dirs, files in os.walk(base):
            for f in sorted(files):
                p = os.path.join(root, f)
                if f.lower().endswith(TEXT_EXT) and 0 < os.path.getsize(p) <= TEXT_MAX:
                    out.append(os.path.relpath(p, folder))
    return sorted(out)


def find_report(folder):
    """The Runner's run_report.json: <dam>/_breach_model/, else the newest under the folder."""
    p = os.path.join(folder, "_breach_model", "run_report.json")
    if os.path.isfile(p):
        return p
    cands = [os.path.join(r, "run_report.json") for r, _d, fs in os.walk(folder) if "run_report.json" in fs]
    return max(cands, key=os.path.getmtime) if cands else None


def cell_of(report):
    """The model cell (m) a run_report.json names, or "" (the key has had more than one name)."""
    def walk(o, depth=0):
        if isinstance(o, dict) and depth < 4:
            for k in ("cell_m", "cell_size_m", "final_cell_m", "model_cell_m", "grid_cell_m", "cell_size", "cell", "dx"):
                v = o.get(k)
                if isinstance(v, (int, float)) and 0 < v < 1000:
                    return v
            for v in o.values():
                got = walk(v, depth + 1)
                if got:
                    return got
        return None
    got = walk(report) if report else None
    return ("%g" % got) if got else ""


def plan_dam(dams_folder, row):
    """What one dam contributes: (manifest row, [(path on disk, name in the zip)])."""
    did = row["dam_id"]
    folder = os.path.join(dams_folder, did)
    info = dict.fromkeys(MANIFEST_COLS, "")
    info.update(dam_id=did, name=row.get("name", ""))
    if not os.path.isdir(folder):
        info["status"] = "no folder (not run)"
        return info, []
    state = load_json(os.path.join(folder, "_chain_nd", "state.json")) or {}
    rasters = find_rasters(folder)
    files = [(p, "%s/%s" % (did, name)) for name, p in rasters.items()]
    if os.path.isfile(os.path.join(folder, "dam.json")):
        files.append((os.path.join(folder, "dam.json"), "%s/dam.json" % did))
    files += [(os.path.join(folder, rel), "%s/%s" % (did, rel.replace(os.sep, "/"))) for rel in text_files(folder)]
    report = find_report(folder)
    if report and not any(p == report for p, _n in files) and os.path.getsize(report) <= TEXT_MAX:
        files.append((report, "%s/%s" % (did, os.path.relpath(report, folder).replace(os.sep, "/"))))
    if state.get("status") == "done":
        info["status"] = "done"
    elif state:
        info["status"] = "failed: " + (state.get("error") or "see log")[:200].replace("\n", " ")
    else:
        info["status"] = "started, not finished" if files else "nothing to pack"
    info["nd_class"] = state.get("nd_class") or ""
    info["cell_m"] = cell_of(load_json(report)) if report else ""
    info["rasters"] = str(len(rasters))
    info["missing"] = "; ".join(n for n in RASTERS if n not in rasters)
    info["files"] = str(len(files))
    info["mb"] = "%.1f" % (sum(os.path.getsize(p) for p, _n in files) / 1e6)
    return info, files


class Parts:
    """Zip files of about max_bytes: a dam is never split, and a new part starts once the
    current one has passed the size."""

    def __init__(self, out, max_bytes, prefix="batch44_results"):
        self.out, self.max, self.prefix = out, max_bytes, prefix
        self.n, self.zf, self.path, self.rows, self.paths = 0, None, None, [], []

    def _open(self):
        self.n += 1
        self.path = os.path.join(self.out, "%s_part%d.zip" % (self.prefix, self.n))
        self.zf = zipfile.ZipFile(self.path, "w", zipfile.ZIP_DEFLATED, compresslevel=6)
        self.rows = []

    def _close(self):
        if not self.zf:
            return
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=MANIFEST_COLS)
        w.writeheader()
        w.writerows(self.rows)
        self.zf.writestr("MANIFEST.csv", buf.getvalue())
        self.zf.close()
        self.paths.append(self.path)
        self.zf = None

    def add(self, info, files):
        if self.zf and os.path.getsize(self.path) >= self.max:
            self._close()
        if not self.zf:
            self._open()
        for p, name in files:
            self.zf.write(p, name)
        self.zf.fp.flush()
        self.rows.append(dict(info, part=str(self.n)))

    def add_batch_file(self, p):
        if not self.zf:
            self._open()
        self.zf.write(p, os.path.basename(p))

    def close(self):
        self._close()
        return self.paths


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--csv", default=os.path.join(HERE, "dams_44.csv"), help="the dam list (default dams_44.csv)")
    ap.add_argument("--dams-folder", default="~/ND_Dams", help="where the batch ran (default ~/ND_Dams)")
    ap.add_argument("--out", default="~/Desktop/batch44_results", help="folder for the zip files")
    ap.add_argument("--only", default="", help="comma separated dam ids from the CSV")
    ap.add_argument("--max-mb", type=float, default=150.0, help="start a new zip past this size (default 150)")
    ap.add_argument("--list", action="store_true", help="print what would be packed and write nothing")
    args = ap.parse_args(argv)
    dams_folder = os.path.abspath(os.path.expanduser(args.dams_folder))
    out = os.path.abspath(os.path.expanduser(args.out))
    if not os.path.isdir(dams_folder):
        sys.exit("%s is not a folder. Name the folder the batch ran in with --dams-folder." % dams_folder)
    rows = read_ids(args.csv, args.only)
    plans = [plan_dam(dams_folder, r) for r in rows]
    print("%-10s %-26s %-8s %-5s %-8s %s" % ("dam", "status", "rasters", "cell", "MB", "missing"))
    for info, _files in plans:
        print("%-10s %-26s %-8s %-5s %-8s %s" % (info["dam_id"], info["status"][:26], info["rasters"] or "0",
                                                info["cell_m"], info["mb"] or "0", info["missing"]))
    done = sum(1 for i, _f in plans if i["status"] == "done")
    complete = sum(1 for i, _f in plans if i["status"] == "done" and not i["missing"])
    print("\n%d of %d dams finished; %d of them have all four rasters." % (done, len(plans), complete))
    if args.list:
        return 0
    if not any(files for _i, files in plans):
        sys.exit("Nothing to pack in %s." % dams_folder)
    os.makedirs(out, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M")
    parts = Parts(out, args.max_mb * 1e6, "batch44_results_%s" % stamp)
    for name in BATCH_FILES:
        p = os.path.join(dams_folder, name)
        if os.path.isfile(p):
            parts.add_batch_file(p)
    for info, files in plans:
        parts.add(info, files)
    paths = parts.close()
    print("\nWrote %d zip file(s) to %s:" % (len(paths), out))
    for p in paths:
        print("  %-44s %6.1f MB" % (os.path.basename(p), os.path.getsize(p) / 1e6))
    print("Send all of them. Each has a MANIFEST.csv listing its dams.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
