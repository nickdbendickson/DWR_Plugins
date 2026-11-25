#!/usr/bin/env python3
"""Mac GPU check: FV1, FV2 and DG2 dry-tile skip, on against off.

Nothing is downloaded. Pass the .par decks already on this Mac, or let it look
in the usual places (it does not walk the whole home folder). Results are
written under <deck>/.fvtiles/, not into the deck's own results, and removed
after a match unless --keep is given (a mismatch is kept).

    python3 tools/mac/check_fv_tiles.py --list
    python3 tools/mac/check_fv_tiles.py --delhi ~/path/lake_delhi_30m.par --carlisle ~/path/Carlisle.par
    python3 tools/mac/check_fv_tiles.py --solver dg2 --deck carlisle=~/path/Carlisle.par
    python3 tools/mac/check_fv_tiles.py --self-test

Each solver runs twice on the Apple GPU, with --profile both times (the same
overhead on each side): tiles on, then DAMSAFETY_MF_TILES=0. Every result file
is compared byte for byte. It prints wall time, GPU microseconds per step, and
how many cell updates the skip ran.

Exit 0 when every file matches and no skip-on GPU step is slower. Exit 1 when
a file differs, a run fails, or the skip-on GPU step is more than 2% slower.
Exit 2 when a deck you named is missing, or none was found. --self-test runs
no model.

DG2 on Carlisle was about 44 minutes a run, so the pair is about an hour and a
half before the other solvers and Lake Delhi.
"""
import argparse
import filecmp
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
BOANN = os.path.join(ROOT, "DamSafety_Runner", "core", "metalflood", "boann.py")
SOLVERS = ("fv", "fv2", "dg2")
# a skip-on GPU step this much over skip-off counts as slower (long-run noise)
SLOWER = 1.02
SEARCH = (
    "~/DamSafety/Validation",
    "~/DamSafety",
    "~/dwr-dam-suite",
    "~/lisflood-benchmarks-2026-11-24",
    "/mnt/project-files/lisflood-benchmarks-2026-11-24",
)
SKIP_DIRS = {".git", "node_modules", ".fvtiles", "__pycache__"}


def _roots(extra):
    out = []
    if extra:
        out.append(os.path.expanduser(extra))
    env = os.environ.get("DAMSAFETY_FV_TILES_ROOT")
    if env:
        out.append(os.path.expanduser(env))
    for p in SEARCH:
        out.append(os.path.expanduser(p))
    seen = []
    for p in out:
        if p not in seen:
            seen.append(p)
    return seen


def find_pars(roots):
    """Every .par under roots, not deeper than 6 folders, skipping hidden trees."""
    found = []
    for root in roots:
        if not os.path.isdir(root):
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
            if dirpath[len(root):].count(os.sep) > 6:
                dirnames[:] = []
                continue
            for fn in filenames:
                if fn.endswith(".par") and not fn.startswith("."):
                    found.append(os.path.join(dirpath, fn))
    return found


def classify(path):
    p = path.lower()
    if "carlisle" in p:
        return "carlisle"
    if "delhi" in p or "lake_delhi" in p:
        return "delhi"
    return None


def write_par(src, dest, dirroot):
    """A copy of src in the same folder, with dirroot replaced, so relative inputs still resolve."""
    lines, seen = [], False
    with open(src, encoding="utf-8", errors="replace") as f:
        raw = f.read().splitlines()
    for line in raw:
        head = line.split("#", 1)[0].strip().split()
        if head and head[0].lower() == "dirroot":
            lines.append("dirroot {}".format(dirroot))
            seen = True
        else:
            lines.append(line)
    if not seen:
        lines.append("dirroot {}".format(dirroot))
    with open(dest, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def result_files(folder):
    out = []
    if not os.path.isdir(folder):
        return out
    for dirpath, dirnames, filenames in os.walk(folder):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for fn in filenames:
            out.append(os.path.relpath(os.path.join(dirpath, fn), folder))
    return sorted(out)


def compare_trees(on_dir, off_dir):
    """[] when the trees match byte for byte, else lines naming what differs."""
    a, b = result_files(on_dir), result_files(off_dir)
    bad = []
    if not a and not b:
        return ["no result files were written"]
    for name in a:
        if name not in b:
            bad.append("only with the skip on: {}".format(name))
    for name in b:
        if name not in a:
            bad.append("only with the skip off: {}".format(name))
    for name in a:
        if name not in b:
            continue
        pa, pb = os.path.join(on_dir, name), os.path.join(off_dir, name)
        if not filecmp.cmp(pa, pb, shallow=False):
            bad.append("differs: {}".format(name))
    return bad


def _gpu_us(stats):
    gpu = (stats.get("profile") or {}).get("gpu") or {}
    v = gpu.get("gpu_us_per_step")
    return float(v) if isinstance(v, (int, float)) else None


def _num(stats, key):
    v = stats.get(key)
    return float(v) if isinstance(v, (int, float)) else None


def run_one(par, solver, mode, stats_path):
    """Run Boann. mode is 'on' or 'off'. Returns (returncode, stats or None)."""
    env = dict(os.environ)
    env.pop("DAMSAFETY_MF_CPU_TILES", None)
    env.pop("DAMSAFETY_MF_SKIP", None)
    if mode == "off":
        env["DAMSAFETY_MF_TILES"] = "0"
    else:
        env.pop("DAMSAFETY_MF_TILES", None)
    cmd = [sys.executable, BOANN, "run", par, "--solver", solver, "--backend", "metal",
           "--profile", "--progress", "--stats-json", stats_path]
    print("+ {}".format(" ".join(cmd)), flush=True)
    rc = subprocess.call(cmd, env=env)
    stats = None
    if os.path.isfile(stats_path):
        try:
            with open(stats_path, encoding="utf-8") as f:
                stats = json.load(f)
        except ValueError:
            stats = None
    return rc, stats


def check_pair(deck_par, solver, keep):
    """One solver, skip on then off. Returns a row dict with ok True/False."""
    wd = os.path.dirname(os.path.abspath(deck_par))
    row = {"deck": deck_par, "solver": solver, "ok": False, "note": ""}
    made = []
    try:
        for mode in ("on", "off"):
            dirroot = os.path.join(".fvtiles", "{}_{}".format(solver, mode))
            par = os.path.join(wd, ".fvtiles_{}_{}.par".format(solver, mode))
            out_dir = os.path.join(wd, dirroot)
            if os.path.isdir(out_dir):
                shutil.rmtree(out_dir)
            write_par(deck_par, par, dirroot)
            made.append(par)
            stats_path = os.path.join(wd, ".fvtiles", "{}_{}.json".format(solver, mode))
            os.makedirs(os.path.dirname(stats_path), exist_ok=True)
            rc, stats = run_one(par, solver, mode, stats_path)
            row[mode] = {"returncode": rc, "stats": stats, "dir": out_dir}
            if rc != 0:
                row["note"] = "the {} run exited {}".format(mode, rc)
                return row
        bad = compare_trees(row["on"]["dir"], row["off"]["dir"])
        if bad:
            row["note"] = "; ".join(bad[:8])
            return row
        on_us, off_us = _gpu_us(row["on"]["stats"] or {}), _gpu_us(row["off"]["stats"] or {})
        row["gpu_us_on"], row["gpu_us_off"] = on_us, off_us
        row["wall_on"] = _num(row["on"]["stats"] or {}, "wall_s")
        row["wall_off"] = _num(row["off"]["stats"] or {}, "wall_s")
        row["steps"] = (row["on"]["stats"] or {}).get("steps")
        row["cells_on"] = (row["on"]["stats"] or {}).get("cell_steps")
        row["cells_off"] = (row["off"]["stats"] or {}).get("cell_steps")
        nfiles = len(result_files(row["on"]["dir"]))
        row["files"] = nfiles
        if on_us is None or off_us is None:
            row["note"] = "identical ({} files); no GPU clock in the profile".format(nfiles)
            row["ok"] = True
            return row
        if on_us > off_us * SLOWER:
            row["note"] = "SLOWER: GPU {:.1f} us/step with the skip, {:.1f} without".format(
                on_us, off_us)
            return row
        share = ""
        if row["cells_on"] is not None and row["cells_off"]:
            share = "; cell updates {:.3f} of the window".format(
                float(row["cells_on"]) / float(row["cells_off"]))
        row["note"] = "identical ({} files){}".format(nfiles, share)
        row["ok"] = True
        return row
    finally:
        for p in made:
            if os.path.isfile(p):
                os.remove(p)
        if row.get("ok") and not keep:
            for mode in ("on", "off"):
                d = os.path.join(wd, ".fvtiles", "{}_{}".format(solver, mode))
                if os.path.isdir(d):
                    shutil.rmtree(d)


def _fmt(v, spec):
    return spec.format(v) if isinstance(v, (int, float)) else "-"


def print_table(rows):
    print("")
    print("{:<10s} {:<6s} {:>8s} {:>10s} {:>10s} {:>10s} {:>10s}  {}".format(
        "deck", "solver", "files", "wall on", "wall off", "us on", "us off", "note"))
    for r in rows:
        deck = os.path.basename(os.path.dirname(r["deck"])) or os.path.basename(r["deck"])
        print("{:<10s} {:<6s} {:>8s} {:>10s} {:>10s} {:>10s} {:>10s}  {}".format(
            deck[:10], r["solver"],
            str(r.get("files") if r.get("files") is not None else "-"),
            _fmt(r.get("wall_on"), "{:.0f}"), _fmt(r.get("wall_off"), "{:.0f}"),
            _fmt(r.get("gpu_us_on"), "{:.1f}"), _fmt(r.get("gpu_us_off"), "{:.1f}"),
            r.get("note") or ""))


def resolve_decks(args):
    """[(label, path)] or a message and exit code when the choice is not usable."""
    explicit = []
    if args.delhi:
        explicit.append(("delhi", os.path.expanduser(args.delhi)))
    if args.carlisle:
        explicit.append(("carlisle", os.path.expanduser(args.carlisle)))
    for item in args.deck or []:
        if "=" not in item:
            return None, " --deck needs label=path.par, not {!r}".format(item), 2
        label, path = item.split("=", 1)
        explicit.append((label.strip() or "deck", os.path.expanduser(path.strip())))
    if explicit:
        missing = [p for _l, p in explicit if not os.path.isfile(p)]
        if missing:
            return None, "no such deck:\n  " + "\n  ".join(missing), 2
        return explicit, None, 0
    found = {}
    for path in find_pars(_roots(args.root)):
        kind = classify(path)
        if kind:
            found.setdefault(kind, []).append(path)
    if args.list or not found:
        lines = ["looked under:"]
        for r in _roots(args.root):
            lines.append("  {}{}".format(r, "" if os.path.isdir(r) else " (not there)"))
        if not found:
            lines.append("no Lake Delhi or Carlisle .par found. Pass --delhi and --carlisle.")
            return None, "\n".join(lines), 2
        lines.append("found:")
        for kind in ("delhi", "carlisle"):
            for p in found.get(kind, []):
                lines.append("  {:<10s} {}".format(kind, p))
        if args.list:
            return None, "\n".join(lines), 0
    chosen = []
    ambiguous = []
    for kind in ("delhi", "carlisle"):
        hits = found.get(kind, [])
        if len(hits) == 1:
            chosen.append((kind, hits[0]))
        elif len(hits) > 1:
            ambiguous.append((kind, hits))
    if ambiguous:
        lines = ["more than one deck; pass the path."]
        for kind, hits in ambiguous:
            for p in hits:
                lines.append("  --{} {}".format(kind, p))
        return None, "\n".join(lines), 2
    if not chosen:
        return None, "no Lake Delhi or Carlisle .par found. Pass --delhi and --carlisle.", 2
    return chosen, None, 0


def self_test():
    d = tempfile.mkdtemp(prefix="fvtiles_")
    try:
        src = os.path.join(d, "run.par")
        with open(src, "w", encoding="utf-8") as f:
            f.write("DEMfile dem.asc\ndirroot results\nresroot run\nsim_time 10\n")
        dest = os.path.join(d, "out.par")
        write_par(src, dest, ".fvtiles/fv_on")
        with open(dest, encoding="utf-8") as f:
            text = f.read()
        assert "dirroot .fvtiles/fv_on" in text and "dirroot results" not in text, text
        assert "DEMfile dem.asc" in text
        bare = os.path.join(d, "bare.par")
        with open(bare, "w", encoding="utf-8") as f:
            f.write("DEMfile dem.asc\n")
        write_par(bare, dest, ".fvtiles/fv_off")
        with open(dest, encoding="utf-8") as f:
            assert f.read().strip().endswith("dirroot .fvtiles/fv_off")
        on, off = os.path.join(d, "on"), os.path.join(d, "off")
        os.makedirs(on)
        os.makedirs(off)
        for folder in (on, off):
            with open(os.path.join(folder, "run.max"), "w", encoding="utf-8") as f:
                f.write("same\n")
        assert compare_trees(on, off) == [], compare_trees(on, off)
        with open(os.path.join(off, "run.max"), "w", encoding="utf-8") as f:
            f.write("other\n")
        bad = compare_trees(on, off)
        assert bad == ["differs: run.max"], bad
        with open(os.path.join(on, "run.mass"), "w", encoding="utf-8") as f:
            f.write("x\n")
        bad = compare_trees(on, off)
        assert any(x.startswith("only with the skip on") for x in bad), bad
        print("self-test ok")
        return 0
    finally:
        shutil.rmtree(d, True)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--delhi", help="Lake Delhi 30 m .par (already on this Mac)")
    p.add_argument("--carlisle", help="Carlisle .par (already on this Mac)")
    p.add_argument("--deck", action="append", help="another deck as label=path.par (repeatable)")
    p.add_argument("--root", help="also search this folder for a Lake Delhi or Carlisle .par")
    p.add_argument("--solver", action="append", choices=SOLVERS, help="one solver (repeatable; default fv fv2 dg2)")
    p.add_argument("--list", action="store_true", help="print the decks found and exit")
    p.add_argument("--keep", action="store_true", help="keep .fvtiles even when the files match")
    p.add_argument("--out", help="write the table as JSON (default: fv_tiles_mac.json in the working directory)")
    p.add_argument("--self-test", action="store_true", help="check the compare logic; runs no model")
    args = p.parse_args(argv)
    if args.self_test:
        return self_test()
    if not os.path.isfile(BOANN):
        print("Boann launcher not found at {}".format(BOANN), file=sys.stderr)
        return 1
    decks, msg, code = resolve_decks(args)
    if msg:
        print(msg)
    if decks is None:
        return code
    solvers = tuple(args.solver) if args.solver else SOLVERS
    print("decks:")
    for label, path in decks:
        print("  {:<10s} {}".format(label, path))
    print("solvers: {}".format(", ".join(solvers)))
    print("DG2 on Carlisle is about 44 minutes a run, twice (on and off), before the others.")
    rows = []
    for label, path in decks:
        for solver in solvers:
            print("\n== {} {} ==".format(label, solver), flush=True)
            row = check_pair(path, solver, args.keep)
            row["label"] = label
            rows.append(row)
            print(row["note"], flush=True)
    print_table(rows)
    out = args.out or os.path.join(os.getcwd(), "fv_tiles_mac.json")
    slim = []
    for r in rows:
        slim.append({k: v for k, v in r.items() if k not in ("on", "off")})
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"ok": all(r["ok"] for r in rows), "rows": slim}, f, indent=2)
    print("wrote {}".format(out))
    return 0 if all(r["ok"] for r in rows) else 1


if __name__ == "__main__":
    sys.exit(main())
