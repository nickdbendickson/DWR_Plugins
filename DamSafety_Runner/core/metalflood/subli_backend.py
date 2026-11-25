"""Sub-grid local-inertial backend definitions (Boann's gpu-subli solver, Runner 3.79.0).

Like subfv_backend.py these live here, NOT in backends.py, so backends.py (and __init__.py) stay
byte-for-byte identical with the copy DamSafety_Screening carries: the sub-grid solvers are
Runner-only. This module adds the one engine's kernel sources, its ctypes Params, its buffer layout
and its dispatch plan, as its own Spec for the shared Metal/CPU backend.

    from . import backends as be
    from . import subli_backend as lb
    bk = be.make_backend("auto", spec=lb.SUBLI_SPEC, log=say)   # same backend, subli kernels
"""
import ctypes
import os

import numpy as np

from . import backends as be

HERE = os.path.dirname(os.path.abspath(__file__))
SUBLI_KERNEL_SOURCE = os.path.join(HERE, "kernels_subli.metal")
SUBLI_CPU_SOURCE = os.path.join(HERE, "cpu_kernels_subli.cpp")
SUBLI_KERNELS = ("k_begin_subli", "k_faces_subli", "k_scale_subli", "k_limit_subli",
                 "k_cont_subli", "k_speed_subli", "k_end_subli",
                 "k_tclear_subli", "k_tseed_subli", "k_tgrow_subli")

# Params.weighting (kernels_subli.metal MF_W_*)
W_NONE, W_UPWIND = 0, 1
WEIGHTING = {"none": W_NONE, "upwind": W_UPWIND}
# Params.friction
F_COMPOUND, F_MEAN = 0, 1
FRICTION = {"compound": F_COMPOUND, "mean": F_MEAN}


class SubLIParams(ctypes.Structure):
    """struct Params of kernels_subli.metal, field for field: the ACC engine's fields plus the
    refinement factor r, the cell curve stride m = r*r, the flux weighting and friction form, and
    the sub-segment length wsub = dx/r."""
    _fields_ = [(n, ctypes.c_uint32) for n in (
        "nx", "ny", "r0", "c0", "wr", "wc", "free_mask", "n_src", "n_series",
        "fric2d", "clip_end", "inflow_lf_order", "r", "m", "weighting", "friction")] + [
            (n, ctypes.c_float) for n in (
                "dx", "dt0", "alpha", "g", "dry", "maxhflow", "arr_depth", "t0", "t_end",
                "wsub")]


def subli_buffer_layout(ny, nx, n_src, n_series, n_values, r):
    """[(name, dtype, count)] in the order of kernels_subli.metal's buffer indices 0..29. Each cell
    keeps its r*r sorted sub-heights above its own lowest sub-cell (s, stride m = r*r) and that
    lowest sub-cell's elevation above the run's datum (zb); the conserved state is the volume V, and
    d is the depth above the lowest sub-cell (the exact inverse of V, refreshed every step). The
    faces carry whole-face discharges (qx/qy, last step's qxo/qyo), the face velocities (vxf/vyf)
    and their common-edge sub-bed profiles and Manning n^2 (zfx/nf2x, zfy/nf2y, r per face; the
    boundary slots hold the FREE-edge profile). Two more words mark the dry-tile skip
    (tseed, ton: one uint per tile of the coarse grid). 30 buffers + Params at 30 = 31 slots,
    Metal's cap: nothing else may be added to this engine without removing a buffer."""
    cells = ny * nx
    m = r * r
    xf, yf = ny * (nx + 1), (ny + 1) * nx
    f, u = np.float32, np.uint32
    return [("s", f, cells * m), ("zb", f, cells), ("V", f, cells), ("d", f, cells),
            ("qx", f, xf), ("qy", f, yf), ("src", f, cells), ("scl", f, cells),
            ("hmax", f, cells), ("tmax", f, cells), ("arr", f, cells), ("vmax", f, cells),
            ("ok", np.uint8, cells), ("s_cell", u, n_src), ("s_series", u, n_src),
            ("ser_off", u, n_series + 1), ("ser_t", f, n_values), ("ser_q", f, n_values),
            ("cursor", u, n_series), ("st", u, be.S_WORDS), ("qxo", f, xf), ("qyo", f, yf),
            ("vxf", f, xf), ("vyf", f, yf), ("zfx", f, xf * r), ("nf2x", f, xf * r),
            ("zfy", f, yf * r), ("nf2y", f, yf * r),
            ("tseed", u, be.tile_count(ny, nx)), ("ton", u, be.tile_count(ny, nx))]


def _subli_plan(p):
    one = (1, 1, 1)
    faces = (p.wc + 1, p.wr + 1, 1)
    cells = (p.wc, p.wr, 1)
    return [("k_begin_subli", one), ("k_faces_subli", faces), ("k_scale_subli", cells),
            ("k_limit_subli", faces), ("k_cont_subli", cells), ("k_speed_subli", cells),
            ("k_end_subli", one)]


def _subli_tile_plan(p):
    tiles = (-(-int(p.wc) // be.TILE), -(-int(p.wr) // be.TILE), 1)
    return [("k_tclear_subli", tiles), ("k_tseed_subli", (p.wc, p.wr, 1)),
            ("k_tgrow_subli", tiles)]


SUBLI_SPEC = be.Spec("subli", SUBLI_KERNEL_SOURCE, be.SHIM, SUBLI_CPU_SOURCE, SUBLI_KERNELS,
                     SubLIParams, _subli_plan, tile_plan=_subli_tile_plan)
