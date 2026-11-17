#!/usr/bin/env python3
"""Verify audit M24's real-inventory impact: diff the tool-7b surface area the Runner reads
(dam.json "surface_area_ac", with its source note) BEFORE vs AFTER re-running the Screening
tool-7b "record" stage.

M24 (Screening 0.19.3) changed the shallow-curve guard in core/handoff to measure basin depth
(freeboard + below-pool) instead of freeboard. For a wet dam with no DWR-recorded max-pool
area it now SENDS the stage-storage curve area (and a lower routed peak) where it used to drop
the area and let the Runner fall back to storage ~ depth^2 (m = 2, a higher peak). This script
shows which dams' surface_area_ac value or source actually moves, so you can tell whether any
published number (and hazard class) changes before trusting the live code - without re-running
the expensive LISFLOOD/ANUGA/hazclass stages for every dam.

dam.json shape (damsuite/dam_record.py):
  {"fields":  {"surface_area_ac": <acres or null>, "dam_id": ..., ...},
   "sources": {"surface_area_ac": {"tool","version","when","note"}, ...}, ...}

Pure Python standard library; runs anywhere python3 does.

  # 1. snapshot the current dam.json set (your "before")
  python3 m24_area_diff.py snapshot /path/to/NickReview/dams --out /tmp/dam_before

  # 2. re-run ONLY the tool-7b "record" stage (it overwrites each dam.json). The record stage
  #    is cheap; rundam / hazclass are NOT needed to see the area decision. e.g.:
  #        <qgis python> nickreview_mac.py ... --redo --stages record

  # 3. diff before vs after
  python3 m24_area_diff.py diff /tmp/dam_before /path/to/NickReview/dams
"""
import argparse
import glob
import json
import os
import shutil
import sys

FIELD = "surface_area_ac"


def _load(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError) as e:
        print("  ! could not read {}: {}".format(path, e), file=sys.stderr)
        return None


def _find_records(root):
    """{dam_id: dam.json path} for every dam.json under root (one level or nested)."""
    out = {}
    for path in sorted(glob.glob(os.path.join(root, "**", "dam.json"), recursive=True)):
        rec = _load(path)
        if rec is None:
            continue
        dam_id = (rec.get("fields") or {}).get("dam_id") or os.path.basename(os.path.dirname(path))
        out[str(dam_id)] = path
    return out


def _area_and_source(rec):
    """(surface_area_ac value or the sentinel '(absent)', source note or '')."""
    fields = rec.get("fields") or {}
    sources = rec.get("sources") or {}
    val = fields.get(FIELD, "(absent)")
    note = ((sources.get(FIELD) or {}).get("note") or "").strip()
    return val, note


def _fmt(v):
    if v == "(absent)":
        return "(field absent)"
    if v is None:
        return "null  (no area sent -> Runner uses its m=2 fallback)"
    return "{} ac".format(v)


def cmd_snapshot(args):
    recs = _find_records(args.dams)
    if not recs:
        sys.exit("no dam.json found under {}".format(args.dams))
    out = args.out or (args.dams.rstrip("/\\") + "_m24_before")
    os.makedirs(out, exist_ok=True)
    for dam_id, path in recs.items():
        d = os.path.join(out, dam_id)
        os.makedirs(d, exist_ok=True)
        shutil.copy2(path, os.path.join(d, "dam.json"))
    print("snapshot: copied {} dam.json files to {}".format(len(recs), out))
    print("next: re-run the tool-7b 'record' stage, then:")
    print("  python3 {} diff {} {}".format(os.path.basename(sys.argv[0]), out, args.dams))


def cmd_diff(args):
    before, after = _find_records(args.before), _find_records(args.after)
    ids = sorted(set(before) | set(after))
    moved, only = [], []
    for dam_id in ids:
        if dam_id not in before or dam_id not in after:
            only.append((dam_id, "after" if dam_id not in before else "before"))
            continue
        b, a = _load(before[dam_id]), _load(after[dam_id])
        if b is None or a is None:
            continue
        bv, bn = _area_and_source(b)
        av, an = _area_and_source(a)
        if bv != av or bn != an:
            moved.append((dam_id, bv, av, bn, an))

    bar = "=" * 78
    print(bar)
    print("M24 surface_area_ac diff: {} dams compared, {} changed".format(len(ids), len(moved)))
    print(bar)
    if only:
        print("\nNOTE: {} dam(s) present in only one set (listed, not compared):".format(len(only)))
        for dam_id, where in only:
            print("  {}  (only in {})".format(dam_id, where))
    if not moved:
        print("\nNo dam's surface_area_ac value or source changed.")
        print("=> M24 is INERT on this inventory: no published area, routed peak or hazard")
        print("   class moves. Nothing further to run.")
        return
    for dam_id, bv, av, bn, an in moved:
        print("\n{}".format(dam_id))
        print("  area:   {}".format(_fmt(bv)))
        print("       -> {}".format(_fmt(av)))
        if bn != an:
            print("  source: {!r}".format(bn or "(none)"))
            print("       -> {!r}".format(an or "(none)"))
    print("\n" + bar)
    print("{} dam(s) changed. For each: if surface_area_ac now carries a value where it was null".format(len(moved)))
    print("or absent (or the source moved to 'stage_storage'), M24 LOWERED that dam's routed peak")
    print("(less conservative). Run rundam + ndhazardclass for just those dams to see whether the")
    print("hazard class actually moves. If every class holds, M24 is safe on your inventory.")


def main():
    ap = argparse.ArgumentParser(
        description="Diff tool-7b surface_area_ac (audit M24) before/after a 'record' re-run.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("snapshot", help="copy the current dam.json set as a 'before'")
    s.add_argument("dams", help="the NickReview dams dir (holds <ID>/dam.json)")
    s.add_argument("--out", help="snapshot dir (default: <dams>_m24_before)")
    s.set_defaults(func=cmd_snapshot)
    d = sub.add_parser("diff", help="diff a before snapshot vs the current (re-run) dams dir")
    d.add_argument("before", help="snapshot dir from the `snapshot` command")
    d.add_argument("after", help="the current (re-run) dams dir")
    d.set_defaults(func=cmd_diff)
    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
