"""DamSafety GPU flood engine (phase 1 prototype): LISFLOOD-FP's local-inertial scheme as Metal
compute kernels for the Apple GPU, with a CPU build of the same kernels. See README.md.

    from nd_breach_runner.core.metalflood import run_par
    stats = run_par("run.par", backend="auto")      # same call as builtin_lisflood.run_par
"""
from .engine import NAME, VERSION, EngineError, Cancelled, run_par  # noqa: F401
from .backends import BackendError, metal_available  # noqa: F401
