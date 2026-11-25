"""Boann, discontinuous Galerkin 2nd order (DG2): the finite-volume engine's machinery run with the
second-order Runge-Kutta discontinuous Galerkin scheme (the "slope-decoupled" RKDG2 of Kesserwani et
al. 2018, the scheme in LISFLOOD-FP 8.0). A thin wrapper over engine_fv so the Runner's engine
selection can pick the discontinuous-Galerkin solver by its own path (core/metalflood/engine_dg2.py)
the same way the finite-volume solvers are picked by engine_fv.py / engine_fv2.py. Everything else -
the deck reader, the .mass/.max/.discharge writers, the batching and windowing, the Metal/CPU backend
- is shared with engine_fv; DG2 carries a per-cell planar expansion (an element average and an x- and
a y-slope coefficient, evolved through the Galerkin weak form) rather than reconstructing the slopes
from the neighbours (MUSCL), run with order=3 and kernels_dg2.metal.

The local slope limiter (LISFLOOD-FP's "limitslopes") is on by default: Boann's float32 DG2 needs it
to hold a lake at rest (the unlimited scheme slowly amplifies slope round-off at standing water).
Put 'nolimitslopes' in the .par, or pass limiter=False, for the raw unlimited scheme (sharper on
dynamic flows; not for long near-rest ponding).

Run it alone:  python -m metalflood.engine_dg2 run.par [-backend metal|cpu] [-nolimit] [-v]"""
from .engine_fv import Deck, Engine, EngineError, Cancelled  # noqa: F401
from .engine_fv import run_par as _run_par_fv

NAME = "Boann, discontinuous Galerkin 2nd order (RKDG2, on the GPU via Metal)"
VERSION = "0.3.1"     # 0.3.1: DG2's GPU dry-tile skip (no result moves). 0.3.0 (Runner 3.93.0): a face against NoData is a reflecting wall; 0.2.0 (Runner 3.67.0): edge/Gauss velocities reconstructed as primitives


def run_par(par_path, progress=None, cancel=None, log=None, backend="auto", limiter=None):
    """Run a .par file with the 2nd-order discontinuous-Galerkin scheme (RKDG2). backend: 'auto',
    'metal' or 'cpu'. limiter: the local slope limiter; None reads the deck (on unless the .par has
    'nolimitslopes'), True/False overrides it. Same inputs and outputs as engine_fv.run_par; order 3."""
    return _run_par_fv(par_path, progress=progress, cancel=cancel, log=log,
                       backend=backend, order=3, limiter=limiter)


def main(argv):
    backend = "auto"
    for i, x in enumerate(argv):
        if x in ("-backend", "--backend") and i + 1 < len(argv):
            backend = argv[i + 1]
    limiter = False if "-nolimit" in argv else None
    args = [x for x in argv[1:] if not x.startswith("-") and x != backend]
    if "-version" in argv or not args:
        print(NAME + " " + VERSION)
        return 0
    try:
        stats = run_par(args[-1], log=print, backend=backend, limiter=limiter)
    except (EngineError, OSError, ValueError) as e:
        print("ERROR: {}".format(e))
        return 1
    print("wall {:.1f} s".format(stats["wall_s"]))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main(sys.argv))
