"""Boann's command line: run a LISFLOOD-FP-style deck with any Boann solver, no QGIS needed.

    boann run DECK.par [--solver acc|sgc|fv|fv2|dg2|subfv|subli] [--backend auto|metal|cpu]
                       [--no-limiter] [--progress] [--quiet] [--stats-json PATH] [--profile]
    boann check          which device a run would use here (Apple GPU, shipped CPU build, compiler)
    boann solvers        the solvers and the Runner engine words that pick each
    boann version        Boann's version and each solver's

`boann` is the launcher beside this file (python boann.py ...), or
`python -m <package>.metalflood ...` from inside a plugin. It needs Python 3 and numpy only; the
CPU build also needs a C++ compiler unless the plugin shipped one prebuilt for this computer.

The deck is the one the Runner writes for LISFLOOD-FP (.par, .dem.asc, .bci, .bdy, optional
.n.asc, .start, .gauge, SGC channel files); the results are LISFLOOD-FP's files, written where the
.par's dirroot / resroot say: .max (maximum depth), .maxVc (maximum velocity), .maxDV (maximum
depth x velocity at one step), .inittm (arrival
time), .mxe (maximum water-surface elevation), .maxtm, .mass, .discharge and -NNNN.wd snapshots.
The Runner turns those into Depth / Velocity / Arrival Time / WSE GeoTIFFs exactly as it does
LISFLOOD-FP's.

--profile (Boann 5.2.0) reports where the run's time went: the GPU's own time per step and per
kernel, the time Python spent encoding the GPU's commands, and how much of the window the
kernels ran was wet (what a dry-cell or dry-tile skip would save). It changes no result; the
numbers go to the screen and into --stats-json under "profile".

The older form `python -m ...metalflood run.par [-backend cpu] [-v] [-version]` (the ACC solver)
still works unchanged.

This module imports nothing outside this folder (no QGIS, no Runner); see tests
test_boann_cli.py."""
import argparse
import importlib
import json
import os
import sys

from .version import BOANN_VERSION

# solver key -> (module in this folder, short description, Runner engine words that pick it)
SOLVERS = (
    ("acc", "engine", "local inertial (LISFLOOD-FP 8.3's ACC scheme); the default",
     ("gpu", "metal", "metalflood")),
    ("sgc", "engine_sgc", "local inertial + LISFLOOD-FP sub-grid channels",
     ("gpu-sgc", "sgc", "boann-sgc")),
    ("fv", "engine_fv", "shock-capturing HLLC finite volume, 1st order",
     ("gpu-fv", "shock", "fv1")),
    ("fv2", "engine_fv2", "shock-capturing HLLC finite volume, 2nd order (MUSCL + SSP-RK2)",
     ("gpu-fv2", "fv2")),
    ("dg2", "engine_dg2", "discontinuous Galerkin, 2nd order (RKDG2; slope limiter on by default)",
     ("gpu-dg2", "dg2")),
    ("subfv", "engine_subfv", "sub-grid finite volume (coarse HLLC with fine sub-cells; "
     "'subgrid <r>' in the .par, default 4)", ("gpu-subfv", "subfv")),
    ("subli", "engine_subli", "sub-grid local inertial (coarse ACC with fine sub-cells, after "
     "Devi & Kuiry 2024; 'subgrid <r>' in the .par, default 4; 'upwind' for the paper's flux "
     "weighting)", ("gpu-subli", "subli")),
)
_KEYS = {k: k for k, _m, _d, _w in SOLVERS}
for _k, _m, _d, _words in SOLVERS:
    for _w in _words:
        _KEYS[_w] = _k
COMMANDS = ("run", "check", "solvers", "version")
# what each result file is, in the words the Runner's rasters use
OUTPUTS = ((".max", "maximum depth (Depth)"), (".maxVc", "maximum velocity (Velocity)"),
           (".maxDV", "maximum depth x velocity, m2/s, at the same step"),
           (".inittm", "arrival time (Arrival Time)"), (".mxe", "maximum water surface (WSE)"),
           (".maxtm", "time of maximum depth"), (".mass", "mass balance"),
           (".discharge", "gauge discharge"),
           (".bed", "final bed after an in-terrain breach, m (only when a breach file is set)"),
           (".pool", "reservoir volume and outflow through an in-terrain breach "
            "(only when a breach file gives a pool mask)"))


class CliError(RuntimeError):
    pass


def solver_key(word):
    """The solver key ('acc', 'fv2', ...) for a key or a Runner engine word; CliError if unknown."""
    k = _KEYS.get(str(word or "").strip().lower())
    if not k:
        raise CliError("unknown solver {!r}; one of: {}".format(
            word, ", ".join(k for k, _m, _d, _w in SOLVERS)))
    return k


def solver_module(key):
    """The solver's module. CliError when this copy of Boann does not carry it (Screening's copy
    has no sub-grid FV or sub-grid local inertial)."""
    mod = dict((k, m) for k, m, _d, _w in SOLVERS)[solver_key(key)]
    try:
        return importlib.import_module("." + mod, __package__)
    except ImportError as e:
        raise CliError("solver {!r} is not in this copy of Boann ({}); the Runner's copy carries "
                       "every solver".format(key, e))


def solver_versions():
    """{key: (NAME, VERSION)} of every solver this copy carries."""
    out = {}
    for k, _m, _d, _w in SOLVERS:
        try:
            m = solver_module(k)
        except CliError:
            continue
        out[k] = (m.NAME, m.VERSION)
    return out


def result_root(par_path):
    """The path prefix the results are written to (dirroot / resroot, as the engines read them)."""
    from . import deckio
    par = deckio.read_par(par_path)
    wd = os.path.dirname(os.path.abspath(par_path))
    return os.path.join(wd, str(par.get("dirroot", "results")), str(par.get("resroot", "run")))


def _sgc_check(par_path):
    """The SGC solver builds its channels from the DEM with the host plugin's core/sgc when the
    deck carries no SGC channel files. Standalone there is no host, and the engine would quietly
    run as plain ACC, so refuse instead."""
    from . import deckio
    par = deckio.read_par(par_path)
    if "sgcwidth" in par and "sgcbed" in par:
        return
    parent = (__package__ or "").rpartition(".")[0]
    if parent:
        try:
            importlib.import_module(parent + ".sgc")
            return
        except ImportError:
            pass
    raise CliError("the sgc solver needs channel files in the deck (SGCwidth / SGCbed / SGCbank, "
                   "as the Runner writes them); standalone Boann cannot build channels from the "
                   "DEM. Add them, or run the deck from the Runner.")


def profile_lines(stats):
    """The profile in a run's stats as lines of text (empty when the run kept none)."""
    pr = (stats or {}).get("profile") or {}
    if not pr:
        return []
    wall = float(stats.get("wall_s") or 0.0)
    steps = int(stats.get("steps") or 0)
    cells = int(stats.get("cells") or stats.get("coarse_cells") or pr.get("grid_cells") or 0)
    lines = ["profile ({}): {} steps in {} batches; {:,} window cell updates dispatched".format(
        pr.get("mode"), steps, pr.get("batches"), int(pr.get("window_cell_steps") or 0))]
    if wall > 0 and steps and cells:
        lines.append("  whole grid: {:.1f} million cell updates a second of wall time".format(
            cells * steps / wall / 1e6))
    host = pr.get("host") or {}
    if "encode_s" in host:
        lines.append("  Python encoding the GPU's commands: {:.2f} s ({:.1f} us a step); waiting "
                     "for the GPU: {:.2f} s; encoding is {:.1f}% of the two".format(
                         host["encode_s"], host.get("encode_us_per_step", 0.0), host["wait_s"],
                         100.0 * host.get("encode_share", 0.0)))
        if wall > 0:
            lines.append("  the rest of the host's time (reading the deck, looking in between "
                         "batches, writing results): {:.2f} s".format(
                             max(wall - host["encode_s"] - host["wait_s"]
                                 - (pr.get("split") or {}).get("host_s", 0.0)
                                 - (pr.get("wet") or {}).get("host_s", 0.0), 0.0)))
        if host.get("run_share_of_window", 1.0) < 1.0:
            lines.append("  the GPU's dry-tile skip ran about {:.1f}% of the window's cell "
                         "updates".format(100.0 * host["run_share_of_window"]))
    elif "run_s" in host:
        lines.append("  in the CPU kernels: {:.2f} s{}".format(
            host["run_s"], " ({:.1f} million window cell updates a second)".format(
                host["mcups_window"]) if "mcups_window" in host else ""))
        if host.get("run_share_of_window", 1.0) < 1.0:
            lines.append("  the dry-cell skip ran {:.1f}% of the window's cell updates".format(
                100.0 * host["run_share_of_window"]))
    gpu = pr.get("gpu") or {}
    if gpu.get("steps"):
        lines.append("  GPU's own time: {:.2f} s ({:.1f} us a step{})".format(
            gpu["gpu_s"], gpu.get("gpu_us_per_step", 0.0),
            ", {:.1f} million window cell updates a second".format(gpu["mcups_window"])
            if "mcups_window" in gpu else ""))
    kern = pr.get("kernels") or []
    if kern:
        lines.append("  GPU time by kernel (from the first {} steps of each batch, one kernel "
                     "per command buffer):".format((pr.get("split") or {}).get("steps_per_batch", 0)))
        lines.append("    {:<18s} {:>10s} {:>8s}   {:>10s} {:>6s} {:>12s}".format(
            "", "us a call", "share", "net us", "share", "threads"))
        for k in kern:
            lines.append("    {:<18s} {:10.1f} {:7.1f}%   {:10.1f} {:5.1f}% {:>12,}".format(
                k["name"], 1e6 * k["gpu_s"] / max(k["calls"], 1), 100.0 * k["share"],
                k.get("net_us_per_call", 0.0), 100.0 * k.get("net_share", 0.0),
                k["threads_per_call"]))
        lines.append("    net: above the {:.1f} us a command buffer costs the GPU whatever it "
                     "holds (the cheapest one-thread kernel); its share is among the grid "
                     "kernels, the nearer guide to their shares of a real step".format(
                         pr.get("kernel_floor_us", 0.0)))
    wet = pr.get("wet") or {}
    if "wet" in wet:
        lines.append("  the window is {:.1f}% of the grid; of the window's cells, on average:"
                     .format(100.0 * wet.get("window_share_of_grid", 0.0)))
        lines.append("    wet {:.1f}%   in wet 16/32/64-cell tiles and their neighbours "
                     "{:.1f}% / {:.1f}% / {:.1f}%   in the CPU dry skip's row ranges {:.1f}%"
                     .format(100.0 * wet["wet"], 100.0 * wet.get("tiles_16", 0.0),
                             100.0 * wet.get("tiles_32", 0.0), 100.0 * wet.get("tiles_64", 0.0),
                             100.0 * wet.get("row_ranges", 0.0)))
    return lines


def run(par_path, solver="acc", backend="auto", limiter=None, progress=False, quiet=False,
        stats_json=None, out=print, profile=False):
    """Run one deck. Returns the engine's stats dict (with 'boann_version' and 'solver' added).
    profile: keep the full profile (backends.Profile) for this run; no result changes."""
    key = solver_key(solver)
    mod = solver_module(key)
    if not os.path.isfile(par_path):
        raise CliError("no such .par file: {}".format(par_path))
    if key == "sgc":
        _sgc_check(par_path)
    kw = {"backend": backend, "log": None if quiet else out}
    if progress:
        kw["progress"] = lambda t, s: out("t = {:.2f} h of {:.2f} h".format(t / 3600.0,
                                                                         s / 3600.0))
    if key == "dg2" and limiter is not None:
        kw["limiter"] = limiter
    if not quiet:
        out("Boann {} - {} {}".format(BOANN_VERSION, mod.NAME, mod.VERSION))
    from . import backends
    was = os.environ.get(backends.PROFILE_ENV)
    if profile:
        os.environ[backends.PROFILE_ENV] = "1"
    try:
        stats = mod.run_par(par_path, **kw)
    finally:
        if profile:
            if was is None:
                os.environ.pop(backends.PROFILE_ENV, None)
            else:
                os.environ[backends.PROFILE_ENV] = was
    stats = dict(stats or {})
    stats["boann_version"] = BOANN_VERSION
    stats["solver"] = key
    if stats_json:
        with open(stats_json, "w", encoding="utf-8") as f:
            json.dump(stats, f, indent=2, default=str)
    if not quiet:
        root = result_root(par_path)
        for ext, what in OUTPUTS:
            if os.path.isfile(root + ext):
                out("  {:<40s} {}".format(os.path.relpath(root + ext), what))
        if "mass_error_m3" in stats:
            out("wall {:.1f} s; volume error {:.3g} m3".format(float(stats.get("wall_s") or 0.0),
                                                              float(stats["mass_error_m3"])))
        if profile:
            for line in profile_lines(stats):
                out(line)
    return stats


def _parser():
    p = argparse.ArgumentParser(
        prog="boann", description="Boann flood engine {} (BoannHydro): runs a LISFLOOD-FP-style "
        "deck on the Apple GPU (Metal) or the CPU build of the same kernels. Not LISFLOOD-FP."
        .format(BOANN_VERSION))
    p.add_argument("--version", action="version", version="boann " + BOANN_VERSION)
    sub = p.add_subparsers(dest="cmd")
    r = sub.add_parser("run", help="run a .par deck")
    r.add_argument("par", help="the LISFLOOD-FP .par file (paths in it are relative to its folder)")
    r.add_argument("--solver", "-s", default="acc",
                   help="acc (default), sgc, fv, fv2, dg2, subfv or subli; the Runner's words "
                        "(gpu, gpu-fv, gpu-fv2, gpu-dg2, gpu-sgc, gpu-subfv, gpu-subli) work too")
    r.add_argument("--backend", "-b", default=os.environ.get("DAMSAFETY_MF_BACKEND") or "auto",
                   choices=("auto", "metal", "cpu"),
                   help="auto (Apple GPU when there is one, else the CPU build), metal or cpu")
    r.add_argument("--no-limiter", action="store_true",
                   help="dg2 only: the raw, unlimited scheme (same as 'nolimitslopes' in the .par)")
    r.add_argument("--progress", "-v", action="store_true", help="print the simulated time")
    r.add_argument("--quiet", "-q", action="store_true", help="print nothing but errors")
    r.add_argument("--stats-json", metavar="PATH", help="write the run's statistics as JSON")
    r.add_argument("--profile", action="store_true",
                   help="report where the time went: GPU time per step and per kernel, Python's "
                        "encoding time, and the wet share of the window (changes no result)")
    sub.add_parser("check", help="which device a run would use here")
    sub.add_parser("solvers", help="list the solvers")
    sub.add_parser("version", help="Boann's version and each solver's")
    return p


def _legacy(argv):
    """argv[1] is not a command: the older `-m metalflood run.par [-backend ..] [-v]` form (ACC)."""
    from . import engine
    return engine.main(argv)


def main(argv=None):
    argv = list(sys.argv if argv is None else argv)
    rest = argv[1:]
    if rest and rest[0] not in COMMANDS and rest[0] not in ("-h", "--help", "--version"):
        return _legacy(argv)
    args = _parser().parse_args(rest)
    if args.cmd in (None, "version"):
        print("boann " + BOANN_VERSION)
        for k, (name, ver) in solver_versions().items():
            print("  {:<6s} {} {}".format(k, name, ver))
        return 0
    if args.cmd == "solvers":
        have = solver_versions()
        for k, _m, desc, words in SOLVERS:
            print("{:<6s} {}{}\n       Runner words: {}".format(
                k, desc, "" if k in have else " [not in this copy]", ", ".join(words)))
        return 0
    if args.cmd == "check":
        from . import available
        ok, why = available()
        print(("ready: " if ok else "cannot run here: ") + why)
        return 0 if ok else 1
    from .deckio import EngineError, Cancelled
    from .backends import BackendError
    try:
        run(args.par, solver=args.solver, backend=args.backend,
            limiter=False if args.no_limiter else None, progress=args.progress,
            quiet=args.quiet, stats_json=args.stats_json, profile=args.profile)
    except (CliError, EngineError, BackendError, OSError, ValueError) as e:
        print("ERROR: {}".format(e), file=sys.stderr)
        return 1
    except (Cancelled, KeyboardInterrupt):
        print("cancelled", file=sys.stderr)
        return 130
    return 0
