"""Boann, shock-capturing 2nd order (FV2): the finite-volume engine run with the MUSCL + SSP-RK2
scheme (order 2). A thin wrapper over engine_fv so the Runner's engine selection can pick the
2nd-order solver by its own path (core/metalflood/engine_fv2.py) the same way the 1st-order one is
picked by engine_fv.py. Everything else - the deck reader, the .mass/.max/.discharge writers, the
batching and windowing, the Metal/CPU backend - is shared with engine_fv; only the reconstruction
order differs (kernels_fv2.metal instead of kernels_fv.metal, run with order=2).

Run it alone:  python -m metalflood.engine_fv2 run.par [-backend metal|cpu] [-v]"""
from .engine_fv import Deck, Engine, EngineError, Cancelled  # noqa: F401
from .engine_fv import run_par as _run_par_fv

NAME = "Boann, shock-capturing 2nd order (MUSCL + SSP-RK2, on the GPU via Metal)"
VERSION = "0.3.0"     # 0.3.0 (Runner 3.93.0): a face against NoData is a reflecting wall; 0.2.0 (Runner 3.66.0): face velocities reconstructed, not discharges


def run_par(par_path, progress=None, cancel=None, log=None, backend="auto"):
    """Run a .par file with the 2nd-order finite-volume scheme (MUSCL + SSP-RK2). backend:
    'auto', 'metal' or 'cpu'. Same inputs and outputs as engine_fv.run_par; order 2."""
    return _run_par_fv(par_path, progress=progress, cancel=cancel, log=log,
                       backend=backend, order=2)


def main(argv):
    backend = "auto"
    for i, x in enumerate(argv):
        if x in ("-backend", "--backend") and i + 1 < len(argv):
            backend = argv[i + 1]
    args = [x for x in argv[1:] if not x.startswith("-") and x != backend]
    if "-version" in argv or not args:
        print(NAME + " " + VERSION)
        return 0
    try:
        stats = run_par(args[-1], log=print, backend=backend)
    except (EngineError, OSError, ValueError) as e:
        print("ERROR: {}".format(e))
        return 1
    print("wall {:.1f} s".format(stats["wall_s"]))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main(sys.argv))
