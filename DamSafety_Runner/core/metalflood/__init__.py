"""DamSafety GPU flood engine: LISFLOOD-FP 8.3's local-inertial (ACC) scheme as Metal compute
kernels for the Apple GPU, with a CPU build of the same kernels. See README.md.

    from .metalflood import run_par
    stats = run_par("run.par", backend="auto")      # same call as builtin_lisflood.run_par

The Runner's core/metalflood is the original; DamSafety_Screening/core/metalflood holds a copy of
its engine files (no bench / cases), and DamIT Runner ports it with the rest of core/.
"""
from .engine import NAME, VERSION, EngineError, Cancelled, run_par  # noqa: F401
from .engine_fv import NAME as FV_NAME, VERSION as FV_VERSION, run_par as run_par_fv  # noqa: F401
from .engine_fv2 import NAME as FV2_NAME, VERSION as FV2_VERSION, run_par as run_par_fv2  # noqa: F401,E501
from .engine_dg2 import NAME as DG2_NAME, VERSION as DG2_VERSION, run_par as run_par_dg2  # noqa: F401,E501
from .engine_sgc import NAME as SGC_NAME, VERSION as SGC_VERSION, run_par as run_par_sgc  # noqa: F401,E501
from .backends import BackendError, metal_available, find_compiler, prebuilt_available  # noqa: F401


def available():
    """(True, device text) when the engine can run here: a Metal GPU, a CPU build shipped with the
    plugin, or a C++ compiler for the CPU build. (False, why) otherwise."""
    ok, why = metal_available()
    if ok:
        return True, "Apple GPU ({})".format(why)
    if prebuilt_available():
        return True, "CPU build of the GPU kernels, shipped with the plugin (no Metal GPU: {})".format(why)
    if find_compiler():
        return True, "CPU build of the GPU kernels, compiled here (no Metal GPU: {})".format(why)
    return False, "no Metal GPU ({}), no CPU build shipped for this computer and no C++ compiler".format(why)
