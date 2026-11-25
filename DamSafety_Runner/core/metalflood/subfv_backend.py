"""Sub-grid finite-volume backend definitions (Boann's gpu-subfv solver, Runner 3.42.0).

These live here, NOT in backends.py, so backends.py (and __init__.py) stay byte-for-byte identical
with the copy DamSafety_Screening carries: the sub-grid FV solver is Runner-only research and is not
mirrored into Screening. Everything else — the generic Metal/CPU backend, Spec, the build cache, the
ACC/FV/FV2/DG2/SGC specs — stays in backends.py and is shared. This module just adds the one engine's
kernel sources, its ctypes Params, its buffer layout and its dispatch plan, as its own Spec.

    from . import backends as be
    from . import subfv_backend as sb
    bk = be.make_backend("auto", spec=sb.SUBFV_SPEC, log=say)   # same backend, subfv kernels
"""
import ctypes
import os

import numpy as np

from . import backends as be

HERE = os.path.dirname(os.path.abspath(__file__))
SUBFV_KERNEL_SOURCE = os.path.join(HERE, "kernels_subfv.metal")
SUBFV_CPU_SOURCE = os.path.join(HERE, "cpu_kernels_subfv.cpp")
SUBFV_KERNELS = ("k_begin_subfv", "k_eta_subfv", "k_faces_subfv", "k_limit_subfv", "k_cont_subfv",
                 "k_end_subfv")


class SubFVParams(ctypes.Structure):
    """struct Params of kernels_subfv.metal, field for field (adds the refinement factor r, the cell
    curve stride m = r*r, and the sub-cell width wsub = dx/r; drops the ACC-only fields)."""
    _fields_ = [(n, ctypes.c_uint32) for n in (
        "nx", "ny", "r0", "c0", "wr", "wc", "free_mask", "n_src", "n_series",
        "r", "m", "clip_end")] + [(n, ctypes.c_float) for n in (
            "dx", "dt0", "alpha", "g", "dry", "arr_depth", "t0", "t_end", "wsub", "pad")]


def subfv_buffer_layout(ny, nx, n_src, n_series, n_values, r):
    """[(name, dtype, count)] in the order of kernels_subfv.metal's buffer indices 0..29. Each cell
    keeps its r*r sorted sub-elevations (s, stride m = r*r); the conserved state is volume V and the
    two momenta Px/Py (eta is inverted from V into its own buffer each step). The HLLC face fluxes are
    whole-face mass/normal/tangential (fxA/fxN/fxT, fyA/fyN/fyT). The sub-bed face profiles zfx/zfy
    (r sub-cells per face) carry the FREE-edge sub-bed in their boundary slots; nf2x/nf2y are the
    per-face sub-grid Manning n^2 (reserved); lim is the per-cell positivity limit (Runner 3.59.0).
    30 buffers + Params at 30 = 31 slots (Metal's 31)."""
    cells = ny * nx
    m = r * r
    xf, yf = ny * (nx + 1), (ny + 1) * nx
    xfp, yfp = ny * (nx + 1) * r, (ny + 1) * nx * r          # face sub-bed profiles (r per face)
    f, u = np.float32, np.uint32
    return [("s", f, cells * m), ("nc2", f, cells), ("V", f, cells), ("Px", f, cells),
            ("Py", f, cells), ("eta", f, cells), ("src", f, cells), ("hmax", f, cells),
            ("tmax", f, cells), ("arr", f, cells), ("vmax", f, 2 * cells), ("ok", np.uint8, cells),  # vmax: [0:cells) peak speed; [cells:) peak depth x speed
            ("s_cell", u, n_src), ("s_series", u, n_src), ("ser_off", u, n_series + 1),
            ("ser_t", f, n_values), ("ser_q", f, n_values), ("cursor", u, n_series),
            ("st", u, be.S_WORDS), ("fxA", f, xf), ("fxN", f, xf), ("fxT", f, xf),
            ("fyA", f, yf), ("fyN", f, yf), ("fyT", f, yf),
            ("zfx", f, xfp), ("nf2x", f, xfp), ("zfy", f, yfp), ("nf2y", f, yfp),
            ("lim", f, cells)]


def _subfv_plan(p):
    one = (1, 1, 1)
    faces = (p.wc + 1, p.wr + 1, 1)
    cells = (p.wc, p.wr, 1)
    # eta from V (every cell), HLLC face fluxes, the positivity limit, then the FV update +
    # friction + close.
    return [("k_begin_subfv", one), ("k_eta_subfv", cells), ("k_faces_subfv", faces),
            ("k_limit_subfv", cells), ("k_cont_subfv", cells), ("k_end_subfv", one)]


SUBFV_SPEC = be.Spec("subfv", SUBFV_KERNEL_SOURCE, be.SHIM, SUBFV_CPU_SOURCE, SUBFV_KERNELS,
                     SubFVParams, _subfv_plan)
