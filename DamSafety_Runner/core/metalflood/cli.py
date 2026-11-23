"""Boann's command line: run a LISFLOOD-FP-style deck with any Boann solver, no QGIS needed.

    boann run DECK.par [--solver acc|sgc|fv|fv2|dg2|subfv] [--backend auto|metal|cpu]
                       [--no-limiter] [--progress] [--quiet] [--stats-json PATH]
    boann check          which device a run would use here (Apple GPU, shipped CPU build, compiler)
    boann solvers        the solvers and the Runner engine words that pick each
    boann version        Boann's version and each solver's

`boann` is the launcher beside this file (python boann.py ...), or
`python -m <package>.metalflood ...` from inside a plugin. It needs Python 3 and numpy only; the
CPU build also needs a C++ compiler unless the plugin shipped one prebuilt for this computer.

The deck is the one the Runner writes for LISFLOOD-FP (.par, .dem.asc, .bci, .bdy, optional
.n.asc, .start, .gauge, SGC channel files); the results are LISFLOOD-FP's files, written where the
.par's dirroot / resroot say: .max (maximum depth), .maxVc (maximum velocity), .inittm (arrival
time), .mxe (maximum water-surface elevation), .maxtm, .mass, .discharge and -NNNN.wd snapshots.
The Runner turns those into Depth / Velocity / Arrival Time / WSE GeoTIFFs exactly as it does
LISFLOOD-FP's.

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
)
_KEYS = {k: k for k, _m, _d, _w in SOLVERS}
for _k, _m, _d, _words in SOLVERS:
    for _w in _words:
        _KEYS[_w] = _k
COMMANDS = ("run", "check", "solvers", "version")
# what each result file is, in the words the Runner's rasters use
OUTPUTS = ((".max", "maximum depth (Depth)"), (".maxVc", "maximum velocity (Velocity)"),
           (".inittm", "arrival time (Arrival Time)"), (".mxe", "maximum water surface (WSE)"),
           (".maxtm", "time of maximum depth"), (".mass", "mass balance"),
           (".discharge", "gauge discharge"))


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
    has no sub-grid FV)."""
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


def run(par_path, solver="acc", backend="auto", limiter=None, progress=False, quiet=False,
        stats_json=None, out=print):
    """Run one deck. Returns the engine's stats dict (with 'boann_version' and 'solver' added)."""
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
    stats = mod.run_par(par_path, **kw)
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
                   help="acc (default), sgc, fv, fv2, dg2 or subfv; the Runner's words (gpu, "
                        "gpu-fv, gpu-fv2, gpu-dg2, gpu-sgc, gpu-subfv) work too")
    r.add_argument("--backend", "-b", default=os.environ.get("DAMSAFETY_MF_BACKEND") or "auto",
                   choices=("auto", "metal", "cpu"),
                   help="auto (Apple GPU when there is one, else the CPU build), metal or cpu")
    r.add_argument("--no-limiter", action="store_true",
                   help="dg2 only: the raw, unlimited scheme (same as 'nolimitslopes' in the .par)")
    r.add_argument("--progress", "-v", action="store_true", help="print the simulated time")
    r.add_argument("--quiet", "-q", action="store_true", help="print nothing but errors")
    r.add_argument("--stats-json", metavar="PATH", help="write the run's statistics as JSON")
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
            quiet=args.quiet, stats_json=args.stats_json)
    except (CliError, EngineError, BackendError, OSError, ValueError) as e:
        print("ERROR: {}".format(e), file=sys.stderr)
        return 1
    except (Cancelled, KeyboardInterrupt):
        print("cancelled", file=sys.stderr)
        return 130
    return 0
