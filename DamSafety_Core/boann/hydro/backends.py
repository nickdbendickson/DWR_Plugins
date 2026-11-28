"""Where the kernels of kernels.metal run.

MetalBackend   the Apple GPU, through the Metal framework called with ctypes (no PyObjC, no
               Xcode, nothing to install: the shader source is compiled by Metal at run time).
               Apple silicon (no float atomics needed).
CpuBackend     the same kernel source built as C++ (mf_shim.h + cpu_kernels.cpp): a library
               shipped with the plugin (prebuilt/<platform>/, made by the release workflow with
               tools/build_boann_cpu.py) when one matches the source's hash, else compiled with
               the computer's C++ compiler at first use and cached by that hash. Built with
               OpenMP when the compiler has it (every core, bit-identical to one core; set
               DAMSAFETY_MF_THREADS to cap it, DAMSAFETY_MF_OPENMP=0 for the one-core build;
               on a Mac, its performance cores only unless DAMSAFETY_MF_THREADS says otherwise),
               else one core. It is how Boann runs on Windows and Linux, and how the engine is
               tested where there is no Apple GPU (Linux CI, the cloud).

Both hold every grid as a numpy array the host reads and writes directly (Metal: shared
storage, the GPU's own memory on Apple silicon), and run n steps per call (run_batch).

Both keep a Profile of where the time went (profile(); the engines put it in their stats). The
host times are always kept; DAMSAFETY_MF_PROFILE=1 (the command line's --profile) adds the GPU's
own time for the batch and for each kernel, and how much of the window is wet. Timing only: no
result changes with it on or off.

DAMSAFETY_MF_TG=WxH (Metal only, for speed tests) sets the threadgroup size of every kernel in
place of the default execution width x up to 8 rows. The grids do not change with it; the
outflow and clipped totals are summed per SIMD group before rounding, so they can move by a
rounding unit."""
import ctypes
import hashlib
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
KERNEL_SOURCE = os.path.join(HERE, "kernels.metal")
SHIM = os.path.join(HERE, "mf_shim.h")
CPU_SOURCE = os.path.join(HERE, "cpu_kernels.cpp")
FV_KERNEL_SOURCE = os.path.join(HERE, "kernels_fv.metal")
FV_CPU_SOURCE = os.path.join(HERE, "cpu_kernels_fv.cpp")
FV2_KERNEL_SOURCE = os.path.join(HERE, "kernels_fv2.metal")
FV2_CPU_SOURCE = os.path.join(HERE, "cpu_kernels_fv2.cpp")
DG2_KERNEL_SOURCE = os.path.join(HERE, "kernels_dg2.metal")
DG2_CPU_SOURCE = os.path.join(HERE, "cpu_kernels_dg2.cpp")
SGC_KERNEL_SOURCE = os.path.join(HERE, "kernels_sgc.metal")
SGC_CPU_SOURCE = os.path.join(HERE, "cpu_kernels_sgc.cpp")

S_WORDS = 32
KERNELS = ("k_begin", "k_faces", "k_scale", "k_limit", "k_cont", "k_speed", "k_end",
           "k_tclear", "k_tseed", "k_tgrow")
# The ACC kernels' dry-tile skip on the GPU (Runner 3.90.0, Boann 5.5.0; kernels.metal MF_TILE):
# tiles of TILE x TILE cells, the state word that turns it on, and the switch. DAMSAFETY_MF_TILES=0
# runs the whole window, which changes the time and nothing else.
TILE = 16
S_TILES = 18
TILES_ENV = "DAMSAFETY_MF_TILES"


def tiles_wanted():
    return os.environ.get(TILES_ENV, "1").strip().lower() not in ("0", "no", "false", "off")


def tile_count(ny, nx):
    """Tiles of the largest window (the whole grid): the length of the tseed / ton buffers."""
    return (-(-int(ny) // TILE)) * (-(-int(nx) // TILE))
FV_KERNELS = ("k_begin_fv", "k_faces_fv", "k_cont_fv", "k_end_fv",
              "k_tclear_fv", "k_tseed_fv", "k_tgrow_fv")
FV2_KERNELS = ("k_stage_p_fv2", "k_begin_fv2", "k_save_fv2", "k_faces_fv2", "k_cont_fv2",
               "k_stage_c_fv2", "k_end_fv2", "k_tclear_fv2", "k_tseed_fv2", "k_tgrow_fv2")
DG2_KERNELS = ("k_stage_p_dg2", "k_begin_dg2", "k_save_dg2", "k_limit_dg2", "k_faces_dg2",
               "k_cont_dg2", "k_stage_c_dg2", "k_end_dg2",
               "k_tclear_dg2", "k_tseed_dg2", "k_tgrow_dg2")
SGC_KERNELS = ("k_begin_sgc", "k_faces_sgc", "k_scale_sgc", "k_limit_sgc", "k_cont_sgc",
               "k_speed_sgc", "k_end_sgc")


class BackendError(RuntimeError):
    pass


class Params(ctypes.Structure):
    """struct Params of kernels.metal, field for field."""
    _fields_ = [(n, ctypes.c_uint32) for n in (
        "nx", "ny", "r0", "c0", "wr", "wc", "free_mask", "n_src", "n_series",
        "fric2d", "clip_end", "inflow_lf_order")] + [(n, ctypes.c_float) for n in (
            "dx", "dt0", "alpha", "g", "dry", "maxhflow", "velmin", "arr_depth", "t0", "t_end",
            "theta")] + [("rain", ctypes.c_uint32), ("n_wl", ctypes.c_uint32)]
    # "rain" is kernels.metal's rainfall switch (ACC and FV1; FV2 / DG2 / SGC declare that word
    # as an unused float pad4, left 0 = 0.0f). "n_wl" (Boann 6.8.0) counts the water-level (HFIX /
    # HVAR) boundary faces, listed in s_cell / s_series after the n_src point sources; 0 in every
    # deck without one (SGC declares the word and never reads it).


def _wl_counts(ny, nx, n_src, n_wl):
    """(src length, s_cell / s_series length): a deck with water-level faces lists them after the
    point sources and keeps each edge position's level in src past the cells (engine.wl_setup);
    without them both are the cells and n_src, as before."""
    cells = ny * nx
    if not n_wl:
        return cells, n_src
    return cells + 2 * (nx + ny), n_src + n_wl


def buffer_layout(ny, nx, n_src, n_series, n_values, n_wl=0):
    """[(name, dtype, count)] in the order of the kernels' buffer indices 0..24."""
    cells = ny * nx
    nsrcb, n_src = _wl_counts(ny, nx, n_src, n_wl)
    f, u = np.float32, np.uint32
    return [("z", f, cells), ("n2", f, cells), ("h", f, cells), ("qx", f, ny * (nx + 1)),
            ("qy", f, (ny + 1) * nx), ("src", f, nsrcb), ("scl", f, cells), ("hmax", f, cells),
            ("tmax", f, cells), ("arr", f, cells), ("vmax", f, 2 * cells), ("ok", np.uint8, cells),  # vmax: [0:cells) peak speed; [cells:) peak depth x speed
            ("s_cell", u, n_src), ("s_series", u, n_src), ("ser_off", u, n_series + 1),
            ("ser_t", f, n_values), ("ser_q", f, n_values), ("cursor", u, n_series),
            ("st", u, S_WORDS), ("qxo", f, ny * (nx + 1)), ("qyo", f, (ny + 1) * nx),
            ("vxf", f, ny * (nx + 1)), ("vyf", f, (ny + 1) * nx),
            ("tseed", u, tile_count(ny, nx)), ("ton", u, tile_count(ny, nx))]


def fv_buffer_layout(ny, nx, n_src, n_series, n_values, n_wl=0):
    """[(name, dtype, count)] in the order of kernels_fv.metal's buffer indices 0..25.
    tseed and ton are the dry-tile skip (one uint per tile of the whole grid). Params is bound
    at 26."""
    cells = ny * nx
    nsrcb, n_src = _wl_counts(ny, nx, n_src, n_wl)
    xf, yf = ny * (nx + 1), (ny + 1) * nx
    f, u = np.float32, np.uint32
    return [("z", f, cells), ("n2", f, cells), ("h", f, cells), ("hu", f, cells),
            ("hv", f, cells), ("src", f, nsrcb), ("hmax", f, cells), ("tmax", f, cells),
            ("arr", f, cells), ("vmax", f, 2 * cells), ("ok", np.uint8, cells),  # vmax: [0:cells) peak speed; [cells:) peak depth x speed
            ("s_cell", u, n_src), ("s_series", u, n_src), ("ser_off", u, n_series + 1),
            ("ser_t", f, n_values), ("ser_q", f, n_values), ("cursor", u, n_series),
            ("st", u, S_WORDS), ("fxh", f, xf), ("fxhu", f, xf), ("fxhv", f, xf),
            ("fyh", f, yf), ("fyhu", f, yf), ("fyhv", f, yf),
            ("tseed", u, tile_count(ny, nx)), ("ton", u, tile_count(ny, nx))]


def fv2_buffer_layout(ny, nx, n_src, n_series, n_values, n_wl=0):
    """[(name, dtype, count)] in the order of kernels_fv2.metal's buffer indices 0..29: the FV1
    layout without its tile buffers (0..23), the previous-step state h0/hu0/hv0 and the
    stage-start depth hs, then tseed/ton. Params is bound at 30 (30 buffers + Params = 31 slots,
    the Metal cap). The minmod slopes are not stored; they are recomputed inline."""
    cells = ny * nx
    f, u = np.float32, np.uint32
    base = [b for b in fv_buffer_layout(ny, nx, n_src, n_series, n_values, n_wl)
            if b[0] not in ("tseed", "ton")]
    return base + [("h0", f, cells), ("hu0", f, cells), ("hv0", f, cells), ("hs", f, cells),
                   ("tseed", u, tile_count(ny, nx)), ("ton", u, tile_count(ny, nx))]


def dg2_buffer_layout(ny, nx, n_src, n_series, n_values, n_wl=0):
    """[(name, dtype, count)] in the order of kernels_dg2.metal's buffer indices 0..28. The three
    conserved variables (Uh, Uhu, Uhv) and their previous-step RK2 copy (Uh0, Uhu0, Uhv0) are stored
    packed, three modal coefficients per cell (3*cells floats): [3 i + 0] element average, [3 i + 1]
    x-slope, [3 i + 2] y-slope. The bed z is piecewise constant (one float per cell). tseed and ton
    are the dry-tile skip. 29 buffers plus Params at 29 stay inside Metal's 31-slot cap."""
    cells = ny * nx
    nsrcb, n_src = _wl_counts(ny, nx, n_src, n_wl)
    xf, yf = ny * (nx + 1), (ny + 1) * nx
    f, u = np.float32, np.uint32
    return [("z", f, cells), ("n2", f, cells), ("Uh", f, 3 * cells), ("Uhu", f, 3 * cells),
            ("Uhv", f, 3 * cells), ("src", f, nsrcb), ("hmax", f, cells), ("tmax", f, cells),
            ("arr", f, cells), ("vmax", f, 2 * cells), ("ok", np.uint8, cells),  # vmax: [0:cells) peak speed; [cells:) peak depth x speed
            ("s_cell", u, n_src), ("s_series", u, n_src), ("ser_off", u, n_series + 1),
            ("ser_t", f, n_values), ("ser_q", f, n_values), ("cursor", u, n_series),
            ("st", u, S_WORDS), ("fxh", f, xf), ("fxhu", f, xf), ("fxhv", f, xf),
            ("fyh", f, yf), ("fyhu", f, yf), ("fyhv", f, yf),
            ("Uh0", f, 3 * cells), ("Uhu0", f, 3 * cells), ("Uhv0", f, 3 * cells),
            ("tseed", u, tile_count(ny, nx)), ("ton", u, tile_count(ny, nx))]


def sgc_buffer_layout(ny, nx, n_src, n_series, n_values):
    """[(name, dtype, count)] in the order of kernels_sgc.metal's buffer indices 0..29. The ACC
    buffers plus the channel/floodplain discharge split (qxc/qxf and their old qxco/qxfo, likewise
    y) and the per-cell SGC geometry (width w, bankfull depth Dd so bed = z - Dd, channel n^2 nc2).
    30 buffers + Params at index 30 = 31 slots, the Metal argument-table cap."""
    cells = ny * nx
    xf, yf = ny * (nx + 1), (ny + 1) * nx
    f, u = np.float32, np.uint32
    return [("z", f, cells), ("n2", f, cells), ("h", f, cells),
            ("qxc", f, xf), ("qxf", f, xf), ("qyc", f, yf), ("qyf", f, yf),
            ("src", f, cells), ("scl", f, cells), ("hmax", f, cells), ("tmax", f, cells),
            ("arr", f, cells), ("vmax", f, 2 * cells), ("ok", np.uint8, cells),  # vmax: [0:cells) peak speed; [cells:) peak depth x speed
            ("s_cell", u, n_src), ("s_series", u, n_src), ("ser_off", u, n_series + 1),
            ("ser_t", f, n_values), ("ser_q", f, n_values), ("cursor", u, n_series),
            ("st", u, S_WORDS), ("qxco", f, xf), ("qxfo", f, xf), ("qyco", f, yf), ("qyfo", f, yf),
            ("vxf", f, xf), ("vyf", f, yf), ("w", f, cells), ("Dd", f, cells), ("nc2", f, cells)]


class Spec:
    """What tells the generic Metal/CPU backend which kernels to run: one engine's kernel
    source, its C++-build source and shim, the kernel names, the ctypes Params for that
    source, and a plan(params) -> [(kernel_name, (w, h, d))] giving the dispatch order and
    grid size of one step. core/metalterrain (BoannGrid) reuses this backend with its own
    Spec; the default is this flood engine, so existing callers are unchanged."""

    def __init__(self, name, kernel_source, shim, cpu_source, kernels, params_cls, plan,
                 tile_plan=None):
        self.name = name
        # tile_plan(params) -> the dispatches that redraw the dry-tile skip's tiles, run before
        # the first step of every chunk (the ACC engine only; None = no skip on the GPU)
        self.tile_plan = tile_plan
        self.kernel_source = kernel_source
        self.shim = shim
        self.cpu_source = cpu_source
        self.kernels = tuple(kernels)
        self.params_cls = params_cls
        self.plan = plan


def _flood_plan(p):
    one = (1, 1, 1)
    faces = (p.wc + 1, p.wr + 1, 1)
    cells = (p.wc, p.wr, 1)
    return [("k_begin", one), ("k_faces", faces), ("k_scale", cells), ("k_limit", faces),
            ("k_cont", cells), ("k_speed", cells), ("k_end", one)]


def _flood_tile_plan(p):
    tiles = (-(-int(p.wc) // TILE), -(-int(p.wr) // TILE), 1)
    return [("k_tclear", tiles), ("k_tseed", (p.wc, p.wr, 1)), ("k_tgrow", tiles)]


FLOOD_SPEC = Spec("mf", KERNEL_SOURCE, SHIM, CPU_SOURCE, KERNELS, Params, _flood_plan,
                  tile_plan=_flood_tile_plan)


def _tiles_on(spec, params, chunk):
    """Whether a batch runs with the GPU's dry-tile skip: the engine asked for chunks, the spec
    has the tile kernels, it does not rain, the window is not empty, and DAMSAFETY_MF_TILES is
    not 0."""
    return (int(chunk) > 0 and spec.tile_plan is not None and tiles_wanted()
            and int(getattr(params, "rain", 0)) == 0 and int(getattr(params, "wr", 0)) > 0
            and int(getattr(params, "wc", 0)) > 0)


def _fv_plan(p):
    one = (1, 1, 1)
    faces = (p.wc + 1, p.wr + 1, 1)
    cells = (p.wc, p.wr, 1)
    return [("k_begin_fv", one), ("k_faces_fv", faces), ("k_cont_fv", cells), ("k_end_fv", one)]


def _fv_tile_plan(p):
    tiles = (-(-int(p.wc) // TILE), -(-int(p.wr) // TILE), 1)
    return [("k_tclear_fv", tiles), ("k_tseed_fv", (p.wc, p.wr, 1)), ("k_tgrow_fv", tiles)]


FV_SPEC = Spec("fv", FV_KERNEL_SOURCE, SHIM, FV_CPU_SOURCE, FV_KERNELS, Params, _fv_plan,
               tile_plan=_fv_tile_plan)


def _fv2_plan(p):
    one = (1, 1, 1)
    faces = (p.wc + 1, p.wr + 1, 1)
    cells = (p.wc, p.wr, 1)
    # one SSP-RK2 step: predictor sweep (stage 0), then corrector sweep (stage 1), then close.
    # k_save_fv2 saves the stage-start depth hs at both stages and U^n once at stage 0; the slopes are
    # recomputed inline in faces/cont (cont reads its neighbours from hs, not the h it overwrites).
    return [("k_stage_p_fv2", one), ("k_begin_fv2", one), ("k_save_fv2", cells),
            ("k_faces_fv2", faces), ("k_cont_fv2", cells),
            ("k_stage_c_fv2", one), ("k_begin_fv2", one), ("k_save_fv2", cells),
            ("k_faces_fv2", faces), ("k_cont_fv2", cells), ("k_end_fv2", one)]


def _fv2_tile_plan(p):
    tiles = (-(-int(p.wc) // TILE), -(-int(p.wr) // TILE), 1)
    return [("k_tclear_fv2", tiles), ("k_tseed_fv2", (p.wc, p.wr, 1)), ("k_tgrow_fv2", tiles)]


FV2_SPEC = Spec("fv2", FV2_KERNEL_SOURCE, SHIM, FV2_CPU_SOURCE, FV2_KERNELS, Params, _fv2_plan,
                tile_plan=_fv2_tile_plan)


def _dg2_plan(p):
    one = (1, 1, 1)
    faces = (p.wc + 1, p.wr + 1, 1)
    cells = (p.wc, p.wr, 1)
    # one SSP-RK2 step: predictor sweep (stage 0), then corrector sweep (stage 1), then close.
    # k_save_dg2 copies U^n once at stage 0; k_limit_dg2 is a no-op when Params.limiter is 0.
    return [("k_stage_p_dg2", one), ("k_begin_dg2", one), ("k_save_dg2", cells),
            ("k_limit_dg2", cells), ("k_faces_dg2", faces), ("k_cont_dg2", cells),
            ("k_stage_c_dg2", one), ("k_begin_dg2", one), ("k_save_dg2", cells),
            ("k_limit_dg2", cells), ("k_faces_dg2", faces), ("k_cont_dg2", cells),
            ("k_end_dg2", one)]


def _dg2_tile_plan(p):
    tiles = (-(-int(p.wc) // TILE), -(-int(p.wr) // TILE), 1)
    return [("k_tclear_dg2", tiles), ("k_tseed_dg2", (p.wc, p.wr, 1)), ("k_tgrow_dg2", tiles)]


DG2_SPEC = Spec("dg2", DG2_KERNEL_SOURCE, SHIM, DG2_CPU_SOURCE, DG2_KERNELS, Params, _dg2_plan,
                tile_plan=_dg2_tile_plan)


def _sgc_plan(p):
    one = (1, 1, 1)
    faces = (p.wc + 1, p.wr + 1, 1)
    cells = (p.wc, p.wr, 1)
    return [("k_begin_sgc", one), ("k_faces_sgc", faces), ("k_scale_sgc", cells),
            ("k_limit_sgc", faces), ("k_cont_sgc", cells), ("k_speed_sgc", cells),
            ("k_end_sgc", one)]


SGC_SPEC = Spec("sgc", SGC_KERNEL_SOURCE, SHIM, SGC_CPU_SOURCE, SGC_KERNELS, Params, _sgc_plan)


# The C++ flags of every CPU build. -ffp-contract=off: never fuse a * b + c into one FMA. A compiler
# that targets a CPU with FMA (Ubuntu 26.04's g++, every Apple Silicon clang) otherwise fuses some
# sums and not others depending on how it vectorises a loop, and the one-core and multi-core builds
# vectorise differently, so their answers drifted apart in the last bit (DG2 on CI, 2026-11-22).
# The flags are part of the library's name (_source_hash), so a build made with other flags is
# never reused.
CPU_FLAGS = ("-O3", "-std=c++14", "-fno-strict-aliasing", "-ffp-contract=off")


def _source_hash(spec):
    """16 hex characters naming these exact kernel sources and CPU_FLAGS. Line ends do not count
    (Boann 6.8.1): Git for Windows checks the sources out with CRLF, and the prebuilt libraries,
    built from LF files on Linux, must still match there. An LF file hashes as before."""
    h = hashlib.sha256()
    for p in (spec.kernel_source, spec.shim, spec.cpu_source):
        with open(p, "rb") as f:
            h.update(f.read().replace(b"\r\n", b"\n"))
    h.update(" ".join(CPU_FLAGS).encode())
    return h.hexdigest()[:16]


def _cache_dir():
    d = os.environ.get("DAMSAFETY_MF_CACHE")
    if not d:
        if sys.platform == "darwin":
            d = os.path.expanduser("~/Library/Caches/DamSafety/metalflood")
        else:
            d = os.path.join(os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"),
                             "DamSafety", "metalflood")
    os.makedirs(d, exist_ok=True)
    return d


# -- profile ------------------------------------------------------------------------------
# Where a run's time goes (Runner 3.80.0, Boann 5.2.0). Measurement only: it reads the grids and
# the clocks and writes nothing the kernels read.

PROFILE_ENV = "DAMSAFETY_MF_PROFILE"
# full profile, Metal: the first steps of every batch go one kernel per command buffer, so each
# kernel's GPU time can be read (Apple GPUs sample counters at pass boundaries only); the rest of
# the batch is one command buffer, as without the profile. Same dispatches in the same order.
PROFILE_SPLIT_STEPS = 2
# full profile: the wet-share look after a batch is skipped while it has cost more than this share
# of the time spent running batches
PROFILE_WET_BUDGET = 0.02
PROFILE_TILES = (16, 32, 64)
# the CPU dry-cell skip's rule (#488): columns within this many cells of water, redrawn every 8 steps
PROFILE_ROW_MARGIN = 9
PROFILE_SERIES_MAX = 200
# the grid that says where the water is, per solver: (buffer, values per cell)
_WET_BUFFERS = (("h", 1), ("Uh", 3), ("V", 1))


def profile_wanted():
    return os.environ.get(PROFILE_ENV, "").strip().lower() in ("1", "yes", "true", "on", "full")


def _dilate(a):
    """a 2-D bool grid with every True spread to its eight neighbours."""
    ny, nx = a.shape
    b = np.zeros((ny + 2, nx + 2), dtype=bool)
    b[1:-1, 1:-1] = a
    out = np.zeros_like(a)
    for dy in (0, 1, 2):
        for dx in (0, 1, 2):
            out |= b[dy:dy + ny, dx:dx + nx]
    return out


def _coarsen(a, k):
    """a 2-D bool grid in k x k blocks: True where any cell of the block is (the last block of a
    row or column may be short)."""
    ny, nx = a.shape
    my, mx = -(-ny // k), -(-nx // k)
    b = np.zeros((my * k, mx * k), dtype=bool)
    b[:ny, :nx] = a
    return b.reshape(my, k, mx, k).any(axis=(1, 3))


def _block_cells(n, k):
    """how many of n cells fall in each k-cell block."""
    m = -(-n // k)
    c = np.full(m, k, dtype=np.int64)
    c[-1] = n - (m - 1) * k
    return c


def wet_shares(wet, tiles=PROFILE_TILES, row_margin=PROFILE_ROW_MARGIN):
    """How much of a window a dry skip would still run. wet: the window as a 2-D bool grid.
    Returns shares of the window's cells:
      wet           cells holding water
      tiles_<T>     cells in the T x T tiles that hold water or touch a tile that does (what a
                    dry-tile skip keeps: water moves at most one cell a step, so a tile's ring of
                    neighbours covers T steps)
      row_ranges    cells in each row's column range under the CPU dry-cell skip's rule (the
                    span of the water within row_margin rows, widened by row_margin columns)
    The rule the real skip uses also keeps faces still carrying flow and the sources, and
    redraws every 8 steps; this counts water only, after each batch, so it is an estimate (the
    small diagonal valley of the speed test: 14.6% here, 13.5% run by the real skip)."""
    wet = np.asarray(wet, dtype=bool)
    ny, nx = wet.shape
    n = ny * nx
    out = {"wet": 0.0, "row_ranges": 0.0}
    for t in tiles:
        out["tiles_{}".format(t)] = 0.0
    if n == 0:
        return out
    out["wet"] = float(np.count_nonzero(wet)) / n
    for t in sorted(tiles):
        act = _dilate(_coarsen(wet, t))
        cells = np.outer(_block_cells(ny, t), _block_cells(nx, t))
        out["tiles_{}".format(t)] = float(cells[act].sum()) / n
    rows = wet.any(axis=1)
    lo = np.where(rows, wet.argmax(axis=1), nx + row_margin).astype(np.int64)
    hi = np.where(rows, nx - 1 - wet[:, ::-1].argmax(axis=1), -1 - row_margin).astype(np.int64)
    lo_p = np.full(ny + 2 * row_margin, nx + row_margin, dtype=np.int64)
    hi_p = np.full(ny + 2 * row_margin, -1 - row_margin, dtype=np.int64)
    lo_p[row_margin:row_margin + ny] = lo
    hi_p[row_margin:row_margin + ny] = hi
    a, z = lo.copy(), hi.copy()
    for k in range(2 * row_margin + 1):
        np.minimum(a, lo_p[k:k + ny], out=a)
        np.maximum(z, hi_p[k:k + ny], out=z)
    span = np.minimum(nx, z + row_margin + 1) - np.maximum(0, a - row_margin)
    out["row_ranges"] = float(np.maximum(span, 0).sum()) / n
    return out


class Profile:
    """One backend's running totals. Seconds are host wall time unless named gpu / driver."""

    def __init__(self, backend):
        self.b = backend
        self.full = profile_wanted()
        self.batches = 0
        self.steps = 0                 # steps dispatched (a batch's last steps can be no-ops)
        self.cell_steps = 0            # window cells x steps dispatched
        self.grid_cells = 0
        self.encode_s = 0.0            # Metal: encoding and committing the batch's command buffer
        self.encode_steps = 0          # ... and the steps that buffer held
        self.wait_s = 0.0              # Metal: waiting for the GPU after the commit
        self.split_s = 0.0             # Metal, full: encoding the one-kernel command buffers
        self.split_steps = 0
        self.run_s = 0.0               # CPU: inside the kernels' library
        self.gpu_s = 0.0               # Metal, full: the GPU's own time on the batch buffer
        self.driver_s = 0.0            # ... and the driver's time scheduling it
        self.gpu_steps = 0
        self.kern = {}                 # Metal, full: kernel -> [gpu seconds, calls, threads]
        self.kern_order = []
        self.wet_s = 0.0
        self.wet_n = 0
        self.wet_w = 0                 # steps the wet look stands for
        self.wet_sum = {}
        self.win_sum = 0.0
        self.series = []               # (model time s, window cells, wet share, tiles_32 share)

    def add_batch(self, params, nsteps):
        wr, wc = int(getattr(params, "wr", 0)), int(getattr(params, "wc", 0))
        self.batches += 1
        self.steps += int(nsteps)
        self.cell_steps += wr * wc * int(nsteps)
        self.grid_cells = int(getattr(params, "ny", 0)) * int(getattr(params, "nx", 0))

    def add_kernel(self, name, gpu_s, threads):
        k = self.kern.get(name)
        if k is None:
            k = self.kern[name] = [0.0, 0, 0]
            self.kern_order.append(name)
        k[0] += gpu_s
        k[1] += 1
        k[2] += int(threads)

    def look(self, params, nsteps):
        """Full profile: the wet shares of the window the batch just ran, kept inside a small
        share of the run's time (a look is skipped when the looks so far cost too much)."""
        if not self.full:
            return
        spent = self.encode_s + self.wait_s + self.split_s + self.run_s
        if self.wet_n and self.wet_s > PROFILE_WET_BUDGET * spent:
            return
        t0 = time.perf_counter()
        try:
            ny, nx = int(params.ny), int(params.nx)
            r0, c0, wr, wc = int(params.r0), int(params.c0), int(params.wr), int(params.wc)
        except AttributeError:
            return
        arr = None
        for name, stride in _WET_BUFFERS:
            a = self.b.arrays.get(name)
            if a is not None and a.size == ny * nx * stride:
                arr = a[0::stride] if stride > 1 else a
                break
        if arr is None or wr <= 0 or wc <= 0:
            return
        wet = arr.reshape(ny, nx)[r0:r0 + wr, c0:c0 + wc] > 0
        sh = wet_shares(wet)
        w = max(int(nsteps), 1)
        for k, v in sh.items():
            self.wet_sum[k] = self.wet_sum.get(k, 0.0) + v * w
        self.win_sum += (wr * wc) / float(max(ny * nx, 1)) * w
        self.wet_w += w
        self.wet_n += 1
        self.series.append((float(getattr(params, "t0", 0.0)), wr * wc, sh["wet"],
                            sh.get("tiles_32", 0.0)))
        self.wet_s += time.perf_counter() - t0

    def report(self):
        """A plain dict (JSON-ready) of the totals and what follows from them."""
        r6 = lambda v: round(float(v), 6)   # noqa: E731
        out = {"mode": "full" if self.full else "basic", "backend": self.b.kind,
               "batches": self.batches, "steps_dispatched": self.steps,
               "window_cell_steps": self.cell_steps, "grid_cells": self.grid_cells}
        if self.b.kind == "metal":
            host = {"encode_s": r6(self.encode_s), "wait_s": r6(self.wait_s),
                    "encode_steps": self.encode_steps}
            if self.encode_steps:
                host["encode_us_per_step"] = round(1e6 * self.encode_s / self.encode_steps, 3)
            if self.encode_s + self.wait_s > 0:
                host["encode_share"] = round(self.encode_s / (self.encode_s + self.wait_s), 4)
            out["host"] = host
            run = getattr(self.b, "cells_run", None)
            if run is not None and self.cell_steps:
                # the GPU's dry-tile skip (ACC): the cells of the tiles it ran, from the tiles of
                # each batch's last chunk (an estimate; the whole window without the skip)
                out["host"]["cell_updates_run"] = int(run)
                out["host"]["run_share_of_window"] = round(run / float(self.cell_steps), 4)
            tg = getattr(self.b, "tg", {})
            out["threadgroup"] = {k: [int(v.width), int(v.height)] for k, v in tg.items()}
        else:
            out["host"] = {"run_s": r6(self.run_s)}
            if self.cell_steps and self.run_s > 0:
                out["host"]["mcups_window"] = round(self.cell_steps / self.run_s / 1e6, 3)
            run = getattr(self.b, "cells_run", None)
            if run is not None and self.cell_steps:
                # the cell updates the kernels really ran: fewer than the window's where the
                # dry-cell skip is on (the ACC solver)
                out["host"]["cell_updates_run"] = int(run)
                out["host"]["run_share_of_window"] = round(run / float(self.cell_steps), 4)
        if self.full and self.b.kind == "metal":
            gpu = {"gpu_s": r6(self.gpu_s), "driver_s": r6(self.driver_s),
                   "steps": self.gpu_steps}
            if self.gpu_steps:
                gpu["gpu_us_per_step"] = round(1e6 * self.gpu_s / self.gpu_steps, 3)
            if self.gpu_s > 0 and self.gpu_steps and self.steps:
                # window cell updates per second of GPU time, in millions
                cells = self.cell_steps * (self.gpu_steps / float(self.steps))
                gpu["mcups_window"] = round(cells / self.gpu_s / 1e6, 3)
            out["gpu"] = gpu
            tot = sum(v[0] for v in self.kern.values())
            # A command buffer costs the GPU a fixed time whatever it holds, and on a small
            # window that is most of a one-kernel buffer's time. The floor is the cheapest
            # one-thread kernel's time a call (it does next to nothing); a kernel's net time
            # is what it takes above that, the nearer estimate of its share of a real step.
            # The net share is among the grid kernels only: the first buffer after a pause
            # also pays the GPU's start-up, and that one is a one-thread kernel.
            per = {k: v[0] / max(v[1], 1) for k, v in self.kern.items()}
            one = {k: bool(v[1]) and v[2] == v[1] for k, v in self.kern.items()}
            floor = min([per[k] for k in per if one[k]] or [0.0])
            net = {k: max(per[k] - floor, 0.0) for k in per}
            net_tot = sum(net[k] for k in net if not one[k])
            out["kernels"] = [{"name": k, "gpu_s": r6(self.kern[k][0]),
                               "share": round(self.kern[k][0] / tot, 4) if tot > 0 else 0.0,
                               "net_us_per_call": round(1e6 * net[k], 3),
                               "net_share": round(net[k] / net_tot, 4)
                               if net_tot > 0 and not one[k] else 0.0,
                               "calls": self.kern[k][1],
                               "threads_per_call": int(self.kern[k][2] / max(self.kern[k][1], 1))}
                              for k in self.kern_order]
            out["kernel_floor_us"] = round(1e6 * floor, 3)
            out["split"] = {"steps": self.split_steps, "steps_per_batch": PROFILE_SPLIT_STEPS,
                            "host_s": r6(self.split_s)}
        if self.full:
            wet = {"looks": self.wet_n, "host_s": r6(self.wet_s)}
            if self.wet_w:
                wet["window_share_of_grid"] = round(self.win_sum / self.wet_w, 4)
                for k, v in sorted(self.wet_sum.items()):
                    wet[k] = round(v / self.wet_w, 4)
                step = max(1, -(-len(self.series) // PROFILE_SERIES_MAX))
                wet["series"] = [[round(t, 1), int(c), round(w, 4), round(tl, 4)]
                                 for t, c, w, tl in self.series[::step]]
                wet["series_columns"] = ["model_time_s", "window_cells", "wet", "tiles_32"]
            out["wet"] = wet
        return out


def threadgroup_env():
    """(width, height) from DAMSAFETY_MF_TG=WxH, or None. BackendError when it is not that."""
    v = os.environ.get("DAMSAFETY_MF_TG", "").strip().lower()
    if not v:
        return None
    try:
        w, h = (int(x) for x in v.split("x"))
    except ValueError:
        w = h = 0
    if w < 1 or h < 1:
        raise BackendError("DAMSAFETY_MF_TG must be WIDTHxHEIGHT, such as 32x8 (got {!r})".format(v))
    return w, h


# -- CPU ----------------------------------------------------------------------------------

def find_compiler():
    for c in (os.environ.get("CXX"), "c++", "clang++", "g++"):
        if c and shutil.which(c):
            return shutil.which(c)
    return None


PREBUILT_DIR = os.path.join(HERE, "prebuilt")


def platform_tag():
    """linux-x64, windows-x64, macos-arm64, ... : the prebuilt/ folder for this computer."""
    m = platform.machine().lower()
    arch = "arm64" if m in ("arm64", "aarch64") else ("x64" if m in ("x86_64", "amd64") else m)
    osname = "macos" if sys.platform == "darwin" else ("windows" if os.name == "nt" else "linux")
    return "{}-{}".format(osname, arch)


def library_ext(osname=None):
    osname = osname or platform_tag().split("-")[0]
    return {"macos": ".dylib", "windows": ".dll"}.get(osname, ".so")


def cpu_library_name(spec, ext, openmp):
    """<spec>_cpu_<source hash>[_omp]<ext>, the name of a CPU build of these exact sources."""
    return "{}_cpu_{}{}{}".format(spec.name, _source_hash(spec), "_omp" if openmp else "", ext)


def mac_libomp_prefixes():
    """Folders holding an LLVM OpenMP runtime (lib/libomp.dylib) for Apple clang,
    which has no OpenMP of its own: $DAMSAFETY_MF_LIBOMP, this Python's own prefix (a conda
    environment's llvm-openmp: the same runtime numpy may already have loaded, and two OpenMP
    runtimes in one process abort it), then Homebrew's libomp."""
    found = []
    for d in (os.environ.get("DAMSAFETY_MF_LIBOMP"), sys.prefix, "/opt/homebrew/opt/libomp",
              "/usr/local/opt/libomp"):
        if d and os.path.isfile(os.path.join(d, "lib", "libomp.dylib")) and d not in found:
            found.append(d)
    return found


def openmp_flag_sets():
    """The ways to ask the compiler for OpenMP, in the order they are tried: (compile flags, link
    flags). Plain -fopenmp (g++, LLVM clang); on a Mac also Apple clang's -Xpreprocessor -fopenmp
    against each libomp found (Runner 3.61.0: before, a Mac built the one-core library)."""
    sets = [(["-fopenmp"], [])]
    if sys.platform == "darwin":
        for d in mac_libomp_prefixes():
            lib = os.path.join(d, "lib")
            # no -I: mf_shim.h declares the two runtime calls itself (omp.h may not parse)
            sets.append((["-Xpreprocessor", "-fopenmp"], ["-L", lib, "-lomp", "-Wl,-rpath," + lib]))
    return sets


def cpu_build_command(spec, cxx, out, openmp=True, extra=(), omp_flags=None):
    """The compiler command for the CPU build of spec (tools/build_boann_cpu.py uses it too).
    omp_flags: one (compile, link) pair of openmp_flag_sets(); default plain -fopenmp."""
    inc = []
    for d in dict.fromkeys(os.path.dirname(p) for p in
                           (spec.cpu_source, spec.shim, spec.kernel_source)):
        inc += ["-I", d]
    cmd = [cxx] + list(CPU_FLAGS) + ["-shared", "-fPIC"]
    link = []
    if openmp:
        cflags, link = omp_flags or (["-fopenmp"], [])
        cmd += list(cflags)
    return cmd + inc + ["-x", "c++", spec.cpu_source] + list(extra) + list(link) + ["-o", out]


def cpu_available(spec=FLOOD_SPEC):
    """True when the CPU build of spec can be had here without a Metal GPU: a prebuilt library
    shipped for this computer, or a C++ compiler to build one (Boann 6.8.1)."""
    return bool(prebuilt_available(spec) or find_compiler())


def prebuilt_available(spec=FLOOD_SPEC):
    """True when this plugin ships a CPU build of spec's current sources for this computer."""
    d = os.path.join(PREBUILT_DIR, platform_tag())
    return any(os.path.isfile(os.path.join(d, cpu_library_name(spec, library_ext(), omp)))
               for omp in (True, False))


def _loads(path, log=None):
    """True when the library opens here (a prebuilt OpenMP build needs the OpenMP runtime, which a
    computer may lack: the one-core build is the fallback)."""
    try:
        ctypes.CDLL(path)
        return True
    except OSError as e:
        if log:
            log("Not using {} ({}).".format(os.path.basename(path), e))
        return False


# Boann 6.9.1: on Windows the compiler found on PATH is MinGW-w64's g++ (Git for Windows,
# Strawberry Perl, MSYS2, conda m2w64). Its -fopenmp build needs libgomp-1.dll, libstdc++-6.dll
# and libwinpthread-1.dll from the compiler's own folder, and Python 3.8+ does not look for a
# library's dependencies on PATH, so ctypes could not open it ("Could not find module ... or one of
# its dependencies"). -static links them in, as the release's prebuilt Windows libraries are built
# (tools/build_boann_cpu.py --extra=-static). The kernels and their results are the same.
_WINDOWS_LINK = ("-static",) if os.name == "nt" else ()


def _openmp_wanted():
    return os.environ.get("DAMSAFETY_MF_OPENMP", "1").strip().lower() not in ("0", "no", "false", "off")


def build_cpu_library(spec=FLOOD_SPEC, log=None):
    """The CPU build of the kernels, its path: a prebuilt library shipped with the plugin for
    these exact sources, else one compiled here once per source version (OpenMP first, the
    one-core build if the compiler has no OpenMP)."""
    ext = library_ext()
    kinds = [True, False] if _openmp_wanted() else [False]
    for omp in kinds:
        p = os.path.join(PREBUILT_DIR, platform_tag(), cpu_library_name(spec, ext, omp))
        if os.path.isfile(p) and _loads(p, log):
            return p
    cxx = find_compiler()
    err = ""
    for omp in kinds:
        out = os.path.join(_cache_dir(), cpu_library_name(spec, ext, omp))
        if os.path.isfile(out):
            if _loads(out, log):
                return out
            # Boann 6.9.1: a build that does not open (an earlier Windows build that needs the
            # compiler's runtime DLLs) is built again, not handed to ctypes to fail on every run
            try:
                os.remove(out)
            except OSError:
                continue
        # this compiler has no OpenMP: do not retry each run, unless the ways to ask for it have
        # changed since (a libomp installed later)
        failed = out + ".failed"
        variants = openmp_flag_sets() if omp else [None]
        tried = "\n".join(" ".join(v[0] + v[1]) for v in variants) if omp else ""
        if not cxx:
            continue
        if omp and os.path.isfile(failed):
            try:
                with open(failed) as f:
                    same = f.read().split("\n--\n", 1)[0] == tried
            except OSError:
                same = True
            if same:
                continue
        for v in variants:
            fd, tmp = tempfile.mkstemp(suffix=ext, dir=os.path.dirname(out))
            os.close(fd)
            cmd = cpu_build_command(spec, cxx, tmp, openmp=omp, omp_flags=v, extra=_WINDOWS_LINK)
            if log:
                log("Building the engine's CPU kernels: " + " ".join(cmd))
            r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               universal_newlines=True)
            if r.returncode == 0 and _loads(tmp, log):
                os.replace(tmp, out)
                return out
            os.remove(tmp)
            err = r.stdout[-3000:] if r.returncode else (
                "the library built but does not open here: " + " ".join(cmd))
        if omp:
            try:
                with open(failed, "w") as f:
                    f.write(tried + "\n--\n" + err)
            except OSError:
                pass
            if log:
                log("This compiler has no OpenMP{}; building the one-core CPU kernels instead.".format(
                    " (on a Mac: brew install libomp, or conda install llvm-openmp)"
                    if sys.platform == "darwin" else ""))
    if not cxx:
        raise BackendError("no C++ compiler (c++, clang++ or g++) for the engine's CPU build, and no "
                           "prebuilt library for {} in this plugin (a release zip ships them)".format(
                               platform_tag()))
    raise BackendError("the CPU kernels did not compile:\n" + err)


def _sysctl_int(name):
    """An integer sysctl on macOS (sysctlbyname), or None when there is no such key here."""
    if sys.platform != "darwin":
        return None
    try:
        libc = ctypes.CDLL("/usr/lib/libc.dylib")
        v = ctypes.c_int(0)
        n = ctypes.c_size_t(ctypes.sizeof(v))
        if libc.sysctlbyname(name.encode(), ctypes.byref(v), ctypes.byref(n), None,
                             ctypes.c_size_t(0)) != 0:
            return None
        return int(v.value)
    except (OSError, AttributeError):
        return None


def default_threads(perf_cores=None, all_cores=None):
    """How many cores the CPU build uses when DAMSAFETY_MF_THREADS does not say (Runner 3.89.0).

    0 = the OpenMP runtime's own choice (every core): Linux, Windows, an Intel Mac. On Apple
    silicon, the performance cores only (hw.perflevel0.physicalcpu: 6 or 8 on an M2 Pro of 10 or
    12). The kernels hand each core an equal share of rows, so with the efficiency cores in the
    team every step waits for them (an efficiency core runs at about a third the speed). The
    results do not depend on it: the CPU build is bit-identical on any number of cores (test).
    perf_cores / all_cores stand in for the sysctl values in tests."""
    if perf_cores is None and all_cores is None:
        perf_cores = _sysctl_int("hw.perflevel0.physicalcpu")
        all_cores = _sysctl_int("hw.physicalcpu")
    if not perf_cores or perf_cores < 1:
        return 0                       # not Apple silicon, or a macOS without the key
    if all_cores and perf_cores >= all_cores:
        return 0                       # every core is a performance core (a VM, an Mx Ultra..)
    return int(perf_cores)


def threads_wanted():
    """DAMSAFETY_MF_THREADS when set to a number (0 = every core), else default_threads().
    On a Mac, "every core" is passed as hw.ncpu rather than 0: the OpenMP runtime keeps the
    last count it was given for the life of the process, so 0 (leave it alone) after an earlier
    performance-core run in the same QGIS would stay on the performance cores."""
    v = os.environ.get("DAMSAFETY_MF_THREADS", "").strip()
    want = None
    if v:
        try:
            want = max(0, int(v))
        except ValueError:
            want = None
    if want is None:
        want = default_threads()
    if want == 0 and sys.platform == "darwin":
        want = _sysctl_int("hw.ncpu") or 0
    return want


class CpuBackend:
    kind = "cpu"

    def __init__(self, spec=FLOOD_SPEC, log=None):
        self.spec = spec
        self.path = build_cpu_library(spec, log)
        self.lib = ctypes.CDLL(self.path)
        self.lib.mf_run_batch.argtypes = [ctypes.c_void_p, ctypes.POINTER(spec.params_cls),
                                          ctypes.c_int]
        self.lib.mf_run_batch.restype = ctypes.c_int
        # the dry-cell skip (Runner 3.61.0; FLOOD_SPEC only, absent from an older prebuilt
        # library): DAMSAFETY_MF_SKIP=0 turns it off, which changes the time and nothing else
        self.skip = (hasattr(self.lib, "mf_run_batch_skip")
                     and os.environ.get("DAMSAFETY_MF_SKIP", "1").strip() != "0")
        if self.skip:
            self.lib.mf_run_batch_skip.argtypes = [ctypes.c_void_p,
                                                   ctypes.POINTER(spec.params_cls),
                                                   ctypes.c_int, ctypes.c_int]
            self.lib.mf_run_batch_skip.restype = ctypes.c_longlong
        # the GPU's dry-tile skip run on the CPU build (DAMSAFETY_MF_CPU_TILES=1, for the cloud
        # tests of the GPU's skip): in place of the CPU's own skip
        self.tiles = (spec.tile_plan is not None and hasattr(self.lib, "mf_run_batch_tiles")
                      and os.environ.get("DAMSAFETY_MF_CPU_TILES", "").strip() == "1")
        if self.tiles:
            self.lib.mf_run_batch_tiles.argtypes = [ctypes.c_void_p,
                                                    ctypes.POINTER(spec.params_cls),
                                                    ctypes.c_int, ctypes.c_int]
            self.lib.mf_run_batch_tiles.restype = ctypes.c_longlong
        self.cells_run = 0
        if self.lib.mf_params_size() != ctypes.sizeof(spec.params_cls):
            raise BackendError("the CPU kernels' Params is {} bytes, Python's {}".format(
                self.lib.mf_params_size(), ctypes.sizeof(spec.params_cls)))
        self.threads = 1
        if hasattr(self.lib, "mf_threads"):
            self.lib.mf_threads.argtypes = [ctypes.c_int]
            self.lib.mf_threads.restype = ctypes.c_int
            self.threads = max(1, int(self.lib.mf_threads(threads_wanted())))
        self.device = ("CPU ({} cores, C++ build of the Metal kernels)".format(self.threads)
                       if self.threads > 1 else "CPU (one core, C++ build of the Metal kernels)")
        self.arrays = {}
        self.prof = Profile(self)

    def profile(self):
        return self.prof.report()

    def allocate(self, layout):
        self.arrays = {}
        ptrs = (ctypes.c_void_p * len(layout))()
        for k, (name, dtype, count) in enumerate(layout):
            a = np.zeros(max(int(count), 1), dtype=dtype)
            self.arrays[name] = a
            ptrs[k] = a.ctypes.data
        self._ptrs = ptrs
        return self.arrays

    def run_batch(self, params, nsteps, chunk=0):
        """nsteps steps. chunk > 0 (the ACC engine's SKIP_CHUNK) lets the CPU build leave out the
        cells that cannot get wet, redrawn every chunk steps; the results are the same bit for bit."""
        t0 = time.perf_counter()
        st = self.arrays.get("st")
        tiles = self.tiles and _tiles_on(self.spec, params, chunk)
        if st is not None and self.spec.tile_plan is not None:
            st[S_TILES] = 1 if tiles else 0
        if tiles:
            self.cells_run += int(self.lib.mf_run_batch_tiles(
                self._ptrs, ctypes.byref(params), int(nsteps), int(chunk)))
        elif chunk > 0 and self.skip:
            self.cells_run += int(self.lib.mf_run_batch_skip(
                self._ptrs, ctypes.byref(params), int(nsteps), int(chunk)))
        else:
            self.lib.mf_run_batch(self._ptrs, ctypes.byref(params), int(nsteps))
            self.cells_run += (int(getattr(params, "wr", 0)) * int(getattr(params, "wc", 0))
                               * int(nsteps))
        self.prof.run_s += time.perf_counter() - t0
        self.prof.add_batch(params, nsteps)
        self.prof.look(params, nsteps)

    def close(self):
        pass


# -- Metal --------------------------------------------------------------------------------

class _MTLSize(ctypes.Structure):
    _fields_ = [("width", ctypes.c_ulong), ("height", ctypes.c_ulong),
                ("depth", ctypes.c_ulong)]


_MTL_STORAGE_SHARED = 0
_MTL_CB_COMPLETED = 4
_MTL_CB_ERROR = 5


class _ObjC:
    """objc_msgSend with a ctypes prototype per signature (arm64 needs the exact one)."""

    def __init__(self):
        self.lib = ctypes.CDLL("/usr/lib/libobjc.A.dylib")
        ctypes.CDLL("/System/Library/Frameworks/Foundation.framework/Foundation")
        self.lib.objc_getClass.restype = ctypes.c_void_p
        self.lib.objc_getClass.argtypes = [ctypes.c_char_p]
        self.lib.sel_registerName.restype = ctypes.c_void_p
        self.lib.sel_registerName.argtypes = [ctypes.c_char_p]
        self.lib.objc_autoreleasePoolPush.restype = ctypes.c_void_p
        self.lib.objc_autoreleasePoolPop.argtypes = [ctypes.c_void_p]
        self._send = ctypes.cast(self.lib.objc_msgSend, ctypes.c_void_p).value
        self._protos = {}
        self._sels = {}

    def cls(self, name):
        c = self.lib.objc_getClass(name.encode())
        if not c:
            raise BackendError("no Objective-C class " + name)
        return c

    def sel(self, name):
        s = self._sels.get(name)
        if s is None:
            s = self._sels[name] = self.lib.sel_registerName(name.encode())
        return s

    def __call__(self, obj, sel, *args, restype=ctypes.c_void_p, argtypes=()):
        key = (restype, tuple(argtypes))
        f = self._protos.get(key)
        if f is None:
            f = self._protos[key] = ctypes.CFUNCTYPE(
                restype, ctypes.c_void_p, ctypes.c_void_p, *argtypes)(self._send)
        return f(obj, self.sel(sel), *args)

    def nsstring(self, text):
        return self(self.cls("NSString"), "stringWithUTF8String:", text.encode("utf-8"),
                    argtypes=[ctypes.c_char_p])

    def text(self, nsstr):
        if not nsstr:
            return ""
        b = self(nsstr, "UTF8String", restype=ctypes.c_char_p)
        return b.decode("utf-8", "replace") if b else ""

    def error_text(self, err):
        if not err:
            return "(no detail)"
        return self.text(self(err, "localizedDescription"))

    def release(self, obj):
        if obj:
            self(obj, "release", restype=None)


def metal_available():
    """(True, device name) when this Mac has a Metal GPU the engine can use, else
    (False, why not)."""
    if sys.platform != "darwin":
        return False, "not macOS"
    try:
        m = ctypes.CDLL("/System/Library/Frameworks/Metal.framework/Metal")
        m.MTLCreateSystemDefaultDevice.restype = ctypes.c_void_p
        objc = _ObjC()
        pool = objc.lib.objc_autoreleasePoolPush()
        try:
            dev = m.MTLCreateSystemDefaultDevice()
            if not dev:
                return False, "no Metal device"
            name = objc.text(objc(dev, "name"))
            objc.release(dev)
            return True, name
        finally:
            objc.lib.objc_autoreleasePoolPop(pool)
    except (OSError, AttributeError) as e:
        return False, "Metal could not be loaded ({})".format(e)


class MetalBackend:
    kind = "metal"

    def __init__(self, spec=FLOOD_SPEC, log=None):
        self.spec = spec
        if sys.platform != "darwin":
            raise BackendError("Metal needs macOS")
        try:
            self._mtl = ctypes.CDLL("/System/Library/Frameworks/Metal.framework/Metal")
        except OSError as e:
            raise BackendError("Metal could not be loaded ({})".format(e))
        self._mtl.MTLCreateSystemDefaultDevice.restype = ctypes.c_void_p
        o = self.o = _ObjC()
        pool = o.lib.objc_autoreleasePoolPush()
        try:
            self.dev = self._mtl.MTLCreateSystemDefaultDevice()
            if not self.dev:
                raise BackendError("this Mac reports no Metal GPU")
            self.device = o.text(o(self.dev, "name"))
            self.queue = o(self.dev, "newCommandQueue")
            with open(spec.kernel_source, encoding="utf-8") as f:
                src = f.read()
            opts = o(o(o.cls("MTLCompileOptions"), "alloc"), "init")
            # IEEE math (no fast-math), so the GPU follows the CPU build closely
            if o(opts, "respondsToSelector:", o.sel("setMathMode:"), restype=ctypes.c_bool,
                 argtypes=[ctypes.c_void_p]):
                o(opts, "setMathMode:", 0, restype=None, argtypes=[ctypes.c_long])
            else:
                o(opts, "setFastMathEnabled:", False, restype=None, argtypes=[ctypes.c_bool])
            err = ctypes.c_void_p()
            self.library = o(self.dev, "newLibraryWithSource:options:error:", o.nsstring(src),
                             opts, ctypes.byref(err),
                             argtypes=[ctypes.c_void_p, ctypes.c_void_p,
                                       ctypes.POINTER(ctypes.c_void_p)])
            o.release(opts)
            if not self.library:
                raise BackendError("Metal did not compile the kernels: " + o.error_text(err.value))
            self.pso = {}
            self.tg = {}
            tg_env = threadgroup_env()
            for name in spec.kernels:
                fn = o(self.library, "newFunctionWithName:", o.nsstring(name),
                       argtypes=[ctypes.c_void_p])
                if not fn:
                    raise BackendError("no kernel {} in the Metal library".format(name))
                err = ctypes.c_void_p()
                pso = o(self.dev, "newComputePipelineStateWithFunction:error:", fn,
                        ctypes.byref(err),
                        argtypes=[ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)])
                o.release(fn)
                if not pso:
                    raise BackendError("Metal pipeline {}: {}".format(name,
                                                                       o.error_text(err.value)))
                w = o(pso, "threadExecutionWidth", restype=ctypes.c_ulong)
                mx = o(pso, "maxTotalThreadsPerThreadgroup", restype=ctypes.c_ulong)
                self.pso[name] = pso
                self.tg[name] = _MTLSize(w, max(1, min(8, mx // max(w, 1))), 1)
                if tg_env:                      # a speed-test size, kept inside the kernel's limit
                    tw = max(1, min(tg_env[0], mx))
                    self.tg[name] = _MTLSize(tw, max(1, min(tg_env[1], mx // tw)), 1)
        finally:
            o.lib.objc_autoreleasePoolPop(pool)
        self.buffers = []
        self.arrays = {}
        self.cells_run = 0
        self.prof = Profile(self)
        self._gpu_clock = None      # whether command buffers report GPUStartTime (asked once)
        if log:
            log("Metal GPU: {}".format(self.device))
            if tg_env:
                log("Threadgroup size from DAMSAFETY_MF_TG: {}".format(
                    ", ".join(sorted({"{}x{}".format(t.width, t.height)
                                      for t in self.tg.values()}))))

    def allocate(self, layout):
        o = self.o
        for b in self.buffers:
            o.release(b)
        self.buffers = []
        self.arrays = {}
        for name, dtype, count in layout:
            nbytes = max(int(count), 1) * np.dtype(dtype).itemsize
            buf = o(self.dev, "newBufferWithLength:options:", nbytes, _MTL_STORAGE_SHARED,
                    argtypes=[ctypes.c_ulong, ctypes.c_ulong])
            if not buf:
                raise BackendError("the GPU could not allocate {} ({:,} bytes)".format(
                    name, nbytes))
            ptr = o(buf, "contents")
            raw = (ctypes.c_char * nbytes).from_address(ptr)
            a = np.frombuffer(raw, dtype=dtype)
            a[:] = 0
            self.buffers.append(buf)
            self.arrays[name] = a
        return self.arrays

    def profile(self):
        return self.prof.report()

    def _commit(self, params, plan, nsteps):
        """One command buffer holding nsteps steps of plan [(kernel, MTLSize)], committed.
        Returns the command buffer (autoreleased: use it inside the caller's pool)."""
        return self._commit_list(params, list(plan) * int(nsteps))

    def _commit_list(self, params, entries):
        """One command buffer holding the dispatches [(kernel, MTLSize)] in order, committed."""
        o = self.o
        cb = o(self.queue, "commandBuffer")
        enc = o(cb, "computeCommandEncoder")
        for k, buf in enumerate(self.buffers):
            o(enc, "setBuffer:offset:atIndex:", buf, 0, k, restype=None,
              argtypes=[ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong])
        o(enc, "setBytes:length:atIndex:", ctypes.addressof(params), ctypes.sizeof(params),
          len(self.buffers), restype=None,
          argtypes=[ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong])
        one = _MTLSize(1, 1, 1)
        set_pso = ("setComputePipelineState:", [ctypes.c_void_p])
        disp_args = [_MTLSize, _MTLSize]
        for name, grid in entries:
            o(enc, set_pso[0], self.pso[name], restype=None, argtypes=set_pso[1])
            tg = self.tg[name] if (grid.width * grid.height * grid.depth) > 1 else one
            o(enc, "dispatchThreads:threadsPerThreadgroup:", grid, tg, restype=None,
              argtypes=disp_args)
        o(enc, "endEncoding", restype=None)
        o(cb, "commit", restype=None)
        return cb

    def _check(self, cb):
        o = self.o
        status = o(cb, "status", restype=ctypes.c_ulong)
        if status != _MTL_CB_COMPLETED:
            raise BackendError("the GPU stopped with an error: " + o.error_text(o(cb, "error")))

    def _gpu_seconds(self, cb):
        """(GPU seconds, driver seconds) of a completed command buffer, or None on a macOS
        without the clocks (before 10.15)."""
        o = self.o
        if self._gpu_clock is None:
            self._gpu_clock = bool(o(cb, "respondsToSelector:", o.sel("GPUStartTime"),
                                     restype=ctypes.c_bool, argtypes=[ctypes.c_void_p]))
        if not self._gpu_clock:
            return None
        d = ctypes.c_double
        gpu = o(cb, "GPUEndTime", restype=d) - o(cb, "GPUStartTime", restype=d)
        drv = o(cb, "kernelEndTime", restype=d) - o(cb, "kernelStartTime", restype=d)
        return max(gpu, 0.0), max(drv, 0.0)

    def run_batch(self, params, nsteps, chunk=0):
        o = self.o
        prof = self.prof
        nsteps = int(nsteps)
        tiles = _tiles_on(self.spec, params, chunk)
        st = self.arrays.get("st")
        if st is not None and self.spec.tile_plan is not None:
            st[S_TILES] = 1 if tiles else 0     # read by the kernels (shared memory)
        pool = o.lib.objc_autoreleasePoolPush()
        try:
            mk = lambda pl: [(name, _MTLSize(w, h, d)) for name, (w, h, d) in pl]  # noqa: E731
            plan = mk(self.spec.plan(params))
            tplan = mk(self.spec.tile_plan(params)) if tiles else []

            def step(s):
                # the dispatches of step s of the batch: the tiles redrawn before every chunk
                return (tplan if tiles and s % int(chunk) == 0 else []) + plan

            # full profile: the first steps one kernel per command buffer (the same dispatches
            # in the same order; command buffers of one queue run in the order committed)
            nsplit = min(nsteps, PROFILE_SPLIT_STEPS) if prof.full else 0
            t0 = time.perf_counter()
            split = []
            for s in range(nsplit):
                for name, grid in step(s):
                    split.append((name, grid, self._commit(params, [(name, grid)], 1)))
            t1 = time.perf_counter()
            main = None
            if nsteps > nsplit or not split:
                main = self._commit_list(params, [e for s in range(nsplit, nsteps)
                                                  for e in step(s)])
            t2 = time.perf_counter()
            o(main if main else split[-1][2], "waitUntilCompleted", restype=None)
            t3 = time.perf_counter()
            for _name, _grid, cb in split:
                self._check(cb)
            if main:
                self._check(main)
                prof.encode_s += t2 - t1
                prof.encode_steps += nsteps - nsplit
            prof.split_s += t1 - t0
            prof.split_steps += nsplit
            prof.wait_s += t3 - t2
            if prof.full:
                for name, grid, cb in split:
                    g = self._gpu_seconds(cb)
                    if g:
                        prof.add_kernel(name, g[0], grid.width * grid.height * grid.depth)
                g = self._gpu_seconds(main) if main else None
                if g:
                    prof.gpu_s += g[0]
                    prof.driver_s += g[1]
                    prof.gpu_steps += nsteps - nsplit
        finally:
            o.lib.objc_autoreleasePoolPop(pool)
        self.cells_run += self._cells_marked(params, tiles) * nsteps
        prof.add_batch(params, nsteps)
        prof.look(params, nsteps)

    def _cells_marked(self, params, tiles):
        """The window cells the skip ran in the batch's last chunk (all of them without it): the
        estimate of the cell updates run that the profile reports."""
        wr, wc = int(getattr(params, "wr", 0)), int(getattr(params, "wc", 0))
        ton = self.arrays.get("ton")
        if not tiles or ton is None:
            return wr * wc
        ntx, nty = -(-wc // TILE), -(-wr // TILE)
        on = ton[:ntx * nty].reshape(nty, ntx) != 0
        w = np.minimum(TILE, wc - TILE * np.arange(ntx))
        h = np.minimum(TILE, wr - TILE * np.arange(nty))
        return int((on * np.outer(h, w)).sum())

    def close(self):
        o = self.o
        for b in self.buffers:
            o.release(b)
        self.buffers = []
        for p in self.pso.values():
            o.release(p)
        self.pso = {}
        for obj in ("library", "queue", "dev"):
            o.release(getattr(self, obj, None))
            setattr(self, obj, None)


def make_backend(which="auto", log=None, spec=FLOOD_SPEC):
    """'metal', 'cpu', 'opencl' or 'auto' (Metal when this Mac has it, else the CPU build; auto
    never picks OpenCL, which is asked for by name). The DAMSAFETY_MF_BACKEND environment
    variable overrides 'auto'."""
    which = (which or "auto").lower()
    if which == "auto":
        which = os.environ.get("DAMSAFETY_MF_BACKEND", "auto").lower()
    if which == "metal":
        return MetalBackend(spec, log)
    if which == "cpu":
        return CpuBackend(spec, log)
    if which == "opencl":
        from .opencl_backend import OpenCLBackend      # a GPU that is not Apple's (Arc, AMD, NVIDIA)
        return OpenCLBackend(spec, log)
    if sys.platform == "darwin":
        try:
            return MetalBackend(spec, log)
        except BackendError as e:
            if log:
                log("Metal not used ({}); the CPU build runs instead.".format(e))
    return CpuBackend(spec, log)
