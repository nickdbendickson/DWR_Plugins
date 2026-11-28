"""DamSafety GPU flood engine: runs the LISFLOOD-FP input files the Runner writes (.par,
.dem.asc, .bci, .bdy, optional .n.asc / .start / .gauge) with the local-inertial scheme of
LISFLOOD-FP's "acceleration" solver on the Apple GPU (Metal), and writes the result files the
Runner reads (.max, .maxVc, .maxDV, .inittm, .maxtm, .mxe, .mass, .discharge, depth snapshots).

It is core/builtin_lisflood.py's scheme moved onto the GPU: the same faces, FREE edges,
donor-cell limiter, time step and outputs, but every step runs as GPU kernels
(kernels.metal) over the wet part of the grid, and the host only looks in every few dozen
steps (a "batch") for output times, the wet window and progress. Where there is no Apple GPU
the same kernel source runs as a C++ build on one CPU core (backends.CpuBackend).

Arithmetic is float32 (Apple GPUs have no float64). Bed elevations are stored relative to the
lowest cell so depths keep about 1e-5 m resolution, and volumes are summed in float64 on the
host. Expect results to match the float64 built-in engine to within a few mm of depth.

Run it alone:  python -m metalflood run.par [-backend metal|cpu] [-v]"""
import json
import math
import os
import time

import numpy as np

from . import deckio as bl
from . import backends as be

NAME = "DamSafety GPU flood engine (local inertial on Metal, LISFLOOD-FP compatible)"
VERSION = "0.3.0"     # 0.3.0 (Boann 6.8.0): water-level (HFIX / HVAR) edge faces, LISFLOOD-FP 8.3's
                      # acceleration boundary; a deck without one runs as before
G = bl.G
ALPHA = bl.ALPHA
DRY_M = bl.DRY_M
MAX_HFLOW = bl.MAX_HFLOW
VEL_MIN_DEPTH_M = bl.VEL_MIN_DEPTH_M
NODATA = bl.NODATA
# Steps per batch, and the window margin: the front moves at most one cell per step, so a
# margin larger than the batch keeps every wet cell inside the window the batch runs on.
BATCH_STEPS = 64
WINDOW_MARGIN = BATCH_STEPS + 4
# CPU build: the cells it runs are redrawn every SKIP_CHUNK steps, as those within SKIP_CHUNK + 1
# cells of water (cpu_kernels.cpp mf_rows); 0 runs the whole window every step
SKIP_CHUNK = 8

EngineError = bl.EngineError
Cancelled = bl.Cancelled

_FREE_BITS = {"N": 1, "S": 2, "E": 4, "W": 8}
S_T, S_DT, S_HMAX, S_HNEXT, S_VIN, S_VOUT, S_CLIP, S_STEPS, S_MINDT = range(9)
S_RMIN, S_RMAX, S_CMIN, S_CMAX, S_QIN, S_QOUT, S_LASTDT, S_RAIN, S_RAIND = range(9, 18)
MF_FIX = 1.0e-4       # kernels.metal: fixed-point unit of the summed outflow and clip


def _f(st, k):
    return float(st[k:k + 1].view(np.float32)[0])


def _setf(st, k, v):
    st[k:k + 1].view(np.float32)[0] = v


class Deck:
    """The inputs of one .par file, read with deckio's readers (builtin_lisflood's)."""

    def __init__(self, par_path):
        wd = os.path.dirname(os.path.abspath(par_path))
        par = bl.read_par(par_path)
        self.par = par

        def path(key):
            return os.path.join(wd, str(par[key]))

        for key in ("demfile", "sim_time"):
            if key not in par:
                raise EngineError("the .par file has no '{}'".format(key))
        z, xll, yll, dx, nod = bl._load("DEM file", bl._read_asc, path("demfile"))
        z = np.where(z == nod, np.nan, z)
        self.ny, self.nx = z.shape
        self.xll, self.yll, self.dx = xll, yll, dx

        def num(key, default):
            v = par.get(key, default)
            try:
                return float(v)
            except (TypeError, ValueError):
                raise EngineError("the .par file's '{}' is not a number ({!r})".format(key, v))
        self.sim = num("sim_time", None)
        self.massint = num("massint", 300.0)
        self.saveint = num("saveint", self.sim + 10.0)
        self.arr_depth = num("inittm_depth", 0.0)
        self.dt0 = float(par.get("initial_tstep", 10.0))
        n_def = num("fpfric", 0.05)
        n = np.full(z.shape, n_def)
        if "manningfile" in par:
            ng, _, _, _, nn = bl._load("Manning's n file", bl._read_asc, path("manningfile"),
                                       z.shape)
            n = np.where((ng == nn) | ~np.isfinite(ng) | (ng <= 0), n_def, ng)
        h0 = np.zeros(z.shape)
        if "startfile" in par:
            s, _, _, _, sn = bl._load("start-depth file", bl._read_asc, path("startfile"),
                                      z.shape)
            h0 = np.where((s == sn) | ~np.isfinite(s), 0.0, s)
        pts, self.free = (bl._load("boundary (.bci) file", bl.read_bci, path("bcifile"))
                          if "bcifile" in par else ([], set()))
        bdy = bl._load("inflow (.bdy) file", bl.read_bdy, path("bdyfile")) \
            if "bdyfile" in par else {}
        ytop = yll + self.ny * dx
        by_name = {}
        for x, y, name in pts:
            c = int(np.floor((x - xll) / dx))
            r = int(np.floor((ytop - y) / dx))
            if not (0 <= r < self.ny and 0 <= c < self.nx):
                raise EngineError("point source {} ({:.1f}, {:.1f}) is outside the DEM".format(
                    name, x, y))
            by_name.setdefault(name, []).append((r, c))
        self.series = []                  # [(name, t_s, q m3/s per cell)]
        self.src_cells = []               # [(row, col, series index)]
        for name, cells in by_name.items():
            if name not in bdy:
                raise EngineError("the .bdy file has no series '{}'".format(name))
            ts, vals = bdy[name]
            k = len(self.series)
            self.series.append((name, np.asarray(ts, float), np.asarray(vals, float) * dx))
            for r, c in cells:
                self.src_cells.append((r, c, k))
        # water-level (stage) boundaries, HFIX / HVAR edge lines (Boann 6.8.0). None in a deck
        # without them: wl_faces stays empty and nothing below changes.
        levels = (bl._load("boundary (.bci) file", bl.read_bci_levels, path("bcifile"))
                  if "bcifile" in par else [])
        self.wl_series, self.wl_faces = _level_faces(self, levels, bdy)
        self.gauges = bl._load("gauge file", bl.read_gauges, path("gaugefile")) \
            if "gaugefile" in par else []
        # LISFLOOD-FP's "rainfall" keyword: one rate (m/s) over every valid cell. Only the ACC
        # engine adds it (Runner 3.60.0); the other Boann engines refuse a deck that has it.
        self.rain = bl._load("rainfall (.rain) file", bl.read_rain, path("rainfall")) \
            if "rainfall" in par else None
        self.solver = "fv1" if "fv1" in par else "acceleration"
        # LISFLOOD-FP 8.3's ACC options: cfl (0.7), theta (1.0: Bates et al. 2010; the Runner's
        # damped rerun writes 0.7) and the 2-D friction term (on unless "1Dfriction")
        self.cfl = num("cfl", ALPHA)
        self.theta = num("theta", 1.0)
        if not (0.0 < self.cfl <= 1.0 and 0.0 < self.theta <= 1.0):
            raise EngineError("cfl and theta must be in (0, 1] (cfl {}, theta {})".format(
                self.cfl, self.theta))
        self.fric2d = "1dfriction" not in par
        # The point source enters in LISFLOOD-FP's order by default (the donor-cell limiter /
        # DryCheck runs before the source is added), so the breach-inflow cell and its near field
        # match LISFLOOD-FP to the mm. "physicalinflow" in the .par keeps the mass-conserving order
        # instead - the source is available to flow out the same step, a lower, CFL-independent
        # breach-cell depth. Only the inflow cell and its near field (within ~300 m) differ between
        # the two; the rest of the domain agrees to ~0.2 mm.
        self.inflow_lf_order = "physicalinflow" not in par
        # Boann 6.13.0: "sourcecfl" holds the step also to the speed at which each point source
        # pushes water out of its cell (kernels.metal mf_src_dt). Off by default: the step is
        # LISFLOOD-FP 8.3's. The ACC and sub-grid local-inertial engines read it; the others ignore it.
        self.source_cfl = "sourcecfl" in par
        # DG2 only: the local slope limiter (LISFLOOD-FP's "limitslopes" family). On by default -
        # Boann's float32 DG2 needs it to hold a lake at rest (the unlimited scheme slowly amplifies
        # slope round-off at a standing water body). "nolimitslopes" in the .par runs the raw,
        # unlimited scheme (sharper on dynamic flows; not for long near-rest ponding).
        self.dg2_limiter = "nolimitslopes" not in par
        self.valid = np.isfinite(z) & (z != NODATA)
        self.z = np.where(self.valid, z, 0.0)
        self.n = np.where(self.valid, n, 0.0)
        self.h0 = np.where(self.valid, np.maximum(h0, 0.0), 0.0)
        res = os.path.join(wd, str(par.get("dirroot", "results")))
        os.makedirs(res, exist_ok=True)
        self.root = os.path.join(res, str(par.get("resroot", "run")))
        self.breach = TerrainBreach.read(self, path("breachfile")) if "breachfile" in par else None


#   Water-level (stage) boundary, LISFLOOD-FP's HFIX / HVAR edge lines (Boann 6.8.0)
#
# A .bci line "W <y1> <y2> HFIX <level>" or "W <y1> <y2> HVAR <series>" (likewise N / S / E, with
# x for N / S) holds the edge cells it covers at a water-surface elevation: a ghost cell outside
# the edge face stands at that level over the edge cell's own bed, and the scheme's own face flux
# lets water in or out. HFIX is one level, HVAR a .bdy series of levels (value time, as an inflow
# hydrograph, but elevations in the DEM's datum and not multiplied by the cell size).
#
# The cells a line covers are LISFLOOD-FP 8.3's (input.cpp LoadBCs: its boundary index from the
# coordinate range, truncated, the end exclusive), and the last line that covers a cell wins. A
# FREE line keeps today's meaning (its whole edge is free, Params.free_mask) and also takes back
# the cells it covers from an earlier water-level line. A water-level cell on a FREE edge is a
# water-level cell.
#
# The kernels find a face's level by its edge position (W rows 0..ny-1, E ny.., N 2 ny.., S
# 2 ny + nx..): the start of every step writes the level of each water-level face into the
# src buffer past its cells (src[cells + pos]), and a face whose slot holds MF_WL_NONE is not a
# water-level face. A deck without water-level lines allocates none of that and runs as before.

WL_NONE = -1.0e30          # kernels' MF_WL_NONE: an edge position with no water level


def wl_pos(side, r, c, ny, nx):
    """Edge position of the face of edge cell (r, c) on side N / S / E / W."""
    return {"W": r, "E": ny + r, "N": 2 * ny + c, "S": 2 * ny + nx + c}[side]


def _lf_bc_cells(side, a, b, d):
    """The edge cells (row, col) that LISFLOOD-FP 8.3 attaches a .bci edge line to: its boundary
    index range from the clamped coordinates (input.cpp LoadBCs), mapped to cells as
    boundary.cpp BCs maps them."""
    nx, ny, dx = d.nx, d.ny, d.dx
    blx, bly = d.xll, d.yll
    tly = bly + ny * dx
    brx = blx + nx * dx

    def clamp(v, lo, hi):
        return min(max(v, lo), hi)
    if side in ("N", "S"):
        s, f = clamp(a, blx, brx), clamp(b, blx, brx)
        if side == "N":
            i1, i2 = int((s - blx) / dx), int((f - blx) / dx)
        else:
            i1 = int(2 * nx + ny - (s - blx) / dx)
            i2 = int(2 * nx + ny - (f - blx) / dx)
    else:
        s, f = clamp(a, bly, tly), clamp(b, bly, tly)
        if side == "E":
            i1, i2 = int(nx + (tly - s) / dx), int(nx + (tly - f) / dx)
        else:
            i1 = int(2 * nx + 2 * ny - (tly - s) / dx)
            i2 = int(2 * nx + 2 * ny - (tly - f) / dx)
    if i1 > i2:
        i1, i2 = i2, i1
    nbc = 2 * nx + 2 * ny
    out = []
    for i in range(i1, i2):                  # LISFLOOD-FP: BCi2-- , so the end is exclusive
        if side == "N" and 0 <= i < nx:
            out.append((0, i))
        elif side == "E" and nx <= i < nx + ny:
            out.append((i - nx, nx - 1))
        elif side == "S" and nx + ny <= i < 2 * nx + ny:
            out.append((ny - 1, 2 * nx + ny - 1 - i))
        elif side == "W" and 2 * nx + ny <= i < nbc:
            out.append((nbc - 1 - i, 0))
    return out


def _level_faces(d, lines, bdy):
    """(series [(name, t_s, level m datum)], faces [(pos, row, col, series index)]) from the
    .bci lines read_bci_levels returns. Both empty when the deck has no HFIX / HVAR line."""
    if not any(kind in ("HFIX", "HVAR") for _, _, _, kind, _ in lines):
        return [], []
    cell = {}                                 # (side, r, c) -> (kind, payload), last line wins
    for side, a, b, kind, payload in lines:
        if side == "P":
            if kind in ("HFIX", "HVAR"):
                raise EngineError(
                    "the .bci file holds a water level at a point (P {:g} {:g} {}), which Boann "
                    "does not support; put the water level on an edge line (N, S, E or W) "
                    "instead".format(a, b, kind))
            continue
        for r, c in _lf_bc_cells(side, a, b, d):
            cell[(side, r, c)] = (kind, payload)
    series, faces, index = [], [], {}
    for (side, r, c), (kind, payload) in sorted(
            cell.items(), key=lambda kv: wl_pos(kv[0][0], kv[0][1], kv[0][2], d.ny, d.nx)):
        if kind == "FREE":
            continue
        if kind == "HFIX":
            key = ("HFIX", float(payload))
            if key not in index:
                index[key] = len(series)
                series.append(("HFIX {:g}".format(payload), np.array([0.0]),
                               np.array([float(payload)])))
        else:
            key = ("HVAR", payload)
            if key not in index:
                if payload not in bdy:
                    raise EngineError("the .bdy file has no series '{}' (an HVAR water level in "
                                      "the .bci file)".format(payload))
                ts, vals = bdy[payload]
                index[key] = len(series)
                series.append((payload, np.asarray(ts, float), np.asarray(vals, float)))
        faces.append((wl_pos(side, r, c, d.ny, d.nx), r, c, index[key]))
    return series, faces


def wl_faces(d):
    """The deck's water-level faces ([] for a deck without them, or a deck object of an older
    shape that has no wl_faces)."""
    return getattr(d, "wl_faces", None) or []


def wl_setup(eng, d, a, base):
    """Fill the water-level buffers of an engine whose series buffers already hold `base`
    series: each level series goes in after them shifted to the engine's bed reference (zref),
    each face into s_cell / s_series after the point sources, and every edge slot of src starts
    at WL_NONE. Returns the faces' (rows, cols) for the window. A no-op without water levels."""
    if not wl_faces(d):
        return (np.zeros(0, int), np.zeros(0, int))
    cells = d.ny * d.nx
    a["src"][cells:] = WL_NONE
    n_src = len(d.src_cells)
    for j, (pos, r, c, k) in enumerate(d.wl_faces):
        a["s_cell"][n_src + j] = pos
        a["s_series"][n_src + j] = base + k
    return (np.array([f[1] for f in d.wl_faces], int), np.array([f[2] for f in d.wl_faces], int))


def wl_series(d, zref):
    """The water-level series as the series buffers hold them: levels relative to zref."""
    if not wl_faces(d):
        return []
    return [(name, ts, np.asarray(v, float) - zref) for name, ts, v in d.wl_series]


def refuse_levels(deck, engine):
    """The sub-grid engines have no water-level boundary yet: stop rather than run the edge as a
    wall and silently drop the boundary."""
    if wl_faces(deck):
        raise EngineError(
            "the .bci file holds a water-level boundary (HFIX or HVAR), which Boann's {} engine "
            "does not support yet. Use Boann ACC, FV1, FV2 or DG2, or LISFLOOD-FP.".format(engine))


def refuse_rain(deck, engine):
    """The engines other than ACC do not add rainfall yet: stop rather than silently drop it."""
    if getattr(deck, "rain", None) is not None:
        raise EngineError("the .par file asks for rainfall ('rainfall'), which Boann's {} engine "
                          "does not add yet; run it with Boann ACC (acceleration) or LISFLOOD-FP"
                          .format(engine))


def refuse_breach(deck, engine):
    """SGC and the sub-grid solvers build their tables from the bed at the start, so a bed that
    keeps falling would not be the bed they solved. Stop rather than ignore the file."""
    if getattr(deck, "breach", None) is not None:
        raise EngineError(
            "the .par file has a breachfile, which Boann's {} engine does not lower into the bed. "
            "Use the ACC engine or a shock-capturing engine (FV1, FV2 or DG2).".format(engine))


def _need(spec, key, kind):
    if key not in spec:
        raise EngineError("the breach file has no '{}'".format(key))
    v = spec[key]
    if kind is float:
        try:
            f = float(v)
        except (TypeError, ValueError):
            raise EngineError("the breach file's '{}' is not a number ({!r})".format(key, v))
        if not math.isfinite(f):
            raise EngineError("the breach file's '{}' is not a finite number ({!r})".format(key, v))
        return f
    return v


class TerrainBreach:
    """A trapezoid notch lowered into the bed between batches. The host writes the bed; the
    kernels read it every step and keep no copy. With no breach file this class is never built,
    and the run is unchanged.

    The notch follows the same linear weir opening the Runner routes: the invert falls from
    invert_start_m to invert_final_m and the bottom width grows from 0, both linear in time.
    frac = clamp((t - start_s) / formation_s, 0, 1), and frac = 1 once t >= start_s when
    formation_s is 0. At along-crest distance a, zt = invert + max(0, |a| - half) / side_slope_h
    with half = 0.5 * bottom_width_final_m * frac. A non-positive side slope is a vertical cut:
    zt = invert where |a| <= half, and the cell is not cut otherwise. The new bed is
    min(current, max(invert_final_m, zt)): it only falls, and never below the final invert.
    """

    def __init__(self, deck, spec, cells, pool):
        self.start_s = _need(spec, "start_s", float)
        self.formation_s = _need(spec, "formation_s", float)
        self.invert_start = _need(spec, "invert_start_m", float)
        self.invert_final = _need(spec, "invert_final_m", float)
        self.bottom_w = _need(spec, "bottom_width_final_m", float)
        self.slope = _need(spec, "side_slope_h", float)
        self.update_s = _need(spec, "update_s", float)
        if self.formation_s < 0:
            raise EngineError("the breach file's formation_s is negative")
        if self.update_s <= 0:
            raise EngineError("the breach file's update_s must be positive")
        if self.invert_final > self.invert_start:
            raise EngineError("the breach file's invert_final_m is above invert_start_m")
        if self.bottom_w < 0:
            raise EngineError("the breach file's bottom_width_final_m is negative")
        ny, nx = deck.ny, deck.nx
        rows, cols, along = [], [], []
        seen = set()
        for i, item in enumerate(cells):
            if not isinstance(item, (list, tuple)) or len(item) != 3:
                raise EngineError("breach cell {} is not [row, col, along_m]".format(i))
            try:
                r, c, a = int(item[0]), int(item[1]), float(item[2])
            except (TypeError, ValueError, OverflowError):
                raise EngineError("breach cell {} is not [row, col, along_m]".format(i))
            if not math.isfinite(a):
                raise EngineError("breach cell {} has an along_m that is not finite".format(i))
            if not (0 <= r < ny and 0 <= c < nx) or not deck.valid[r, c]:
                raise EngineError("breach cell {} ({}, {}) is outside the grid or NoData".format(
                    i, r, c))
            if (r, c) in seen:
                raise EngineError("breach cell ({}, {}) is listed twice".format(r, c))
            seen.add((r, c))
            rows.append(r)
            cols.append(c)
            along.append(abs(a))
        if not rows:
            raise EngineError("the breach file lists no cells")
        self.rows = np.asarray(rows, np.int64)
        self.cols = np.asarray(cols, np.int64)
        self.idx = self.rows * nx + self.cols
        self.along = np.asarray(along, np.float64)
        self.z0 = np.asarray(deck.z, np.float64).ravel()[self.idx].copy()
        self.zcur = self.z0.copy()
        self.grid_z = np.asarray(deck.z, np.float64)
        self.dx = float(deck.dx)
        self.pool_idx = pool
        self.times = self._times()
        self._k = 0
        self.next_u = self.times[0]
        self.updates = 0
        self.last_t = None
        # Peak water level (datum) on the breach cells over the bed periods already closed.
        # The bed is constant between updates and only falls; hmax only rises. For a period k
        # with bed z_k whose peak depth so far is H_k at its end, the highest water level in
        # it is H_k + z_k when hmax rose in that period, and at most that otherwise, which is
        # then no higher than an earlier period's value (same H, higher bed). So the max over
        # wet periods of (H_k + z_k) is the exact peak water level; one array, O(cells).
        self.wse_peak = np.full(self.idx.size, -np.inf)
        self._pool_f = None
        self._pool_t = None
        self._pool_v = None
        self._dvin = 0.0

    @classmethod
    def read(cls, deck, path):
        try:
            with open(path, encoding="utf-8") as f:
                spec = json.load(f)
        except (OSError, ValueError) as e:
            raise EngineError("cannot read the breach file {}: {}".format(path, e))
        if not isinstance(spec, dict):
            raise EngineError("the breach file must be a JSON object")
        if int(spec.get("version", 0)) != 1:
            raise EngineError("the breach file's version must be 1")
        cells = spec.get("cells")
        if not isinstance(cells, list):
            raise EngineError("the breach file's cells must be a list")
        pool = None
        name = spec.get("pool_mask")
        if name:
            mask_path = name if os.path.isabs(name) else os.path.join(os.path.dirname(path), name)
            data, _, _, _, nod = bl._load("pool mask", bl._read_asc, mask_path,
                                          (deck.ny, deck.nx))
            pool = np.flatnonzero((data != nod) & (data != 0) & np.isfinite(data))
            if pool.size == 0:
                raise EngineError("the pool mask has no pool cells")
            if getattr(deck, "rain", None) is not None:
                # the pool file's outflow is the pool's volume change plus what flowed in; rain
                # on the pool is not counted in that, so it would show as less outflow
                raise EngineError("the breach file's pool mask cannot be used with rainfall: the "
                                  "pool outflow would not count the rain on the pool")
        return cls(deck, spec, cells, pool)

    def _times(self):
        start, form, upd = self.start_s, self.formation_s, self.update_s
        end = start + max(form, 0.0)
        if form <= 0.0:
            return [start]
        times = []
        k = 0
        while True:
            u = start + k * upd
            if u >= end - 1e-9:
                times.append(end)
                break
            times.append(u)
            k += 1
            if k > 10000000:
                raise EngineError("the breach update grid is too long")
        return times

    def _frac(self, t):
        if t < self.start_s:
            return 0.0
        if self.formation_s <= 0.0:
            return 1.0
        return min(1.0, (t - self.start_s) / self.formation_s)

    def candidate(self, t):
        """Notch bed (m, datum) at time t, float64. +inf where a vertical cut does not reach."""
        frac = self._frac(t)
        invert = self.invert_start - frac * (self.invert_start - self.invert_final)
        half = 0.5 * self.bottom_w * frac
        if self.slope <= 0.0:
            zt = np.where(self.along <= half, invert, np.inf)
        else:
            zt = invert + np.maximum(0.0, self.along - half) / self.slope
        return np.maximum(self.invert_final, zt)

    def lowest_final(self):
        """Lowest bed on the footprint at the end of formation, for the reference level."""
        end = self.start_s + max(self.formation_s, 0.0)
        return float(np.minimum(self.z0, self.candidate(end)).min())

    def apply(self, eng):
        """Lower the bed to the notch at next_u. Depth is left alone, so volume is unchanged."""
        # close the period on the current bed: its peak water level is hmax-so-far + its bed
        h = np.asarray(eng.a["hmax"][self.idx], dtype=np.float64)
        self.wse_peak = np.where(h > 0, np.maximum(self.wse_peak, h + self.zcur), self.wse_peak)
        self.zcur = np.minimum(self.zcur, self.candidate(self.next_u))
        eng.a["z"][self.idx] = (self.zcur - eng.zref).astype(np.float32)
        self.updates += 1
        self.last_t = self.next_u
        self._k += 1
        self.next_u = self.times[self._k] if self._k < len(self.times) else None

    def catch_up(self, eng, t):
        applied = False
        while self.next_u is not None and t >= self.next_u - 1e-6:
            self.apply(eng)
            applied = True
        # A row at the model time of each bed update, and then at least every update_s while
        # the pool drains (the time-to-empty is read from these rows; the mass-interval rows
        # alone, 300 s by default, would put it up to a whole interval late). The run loop
        # adds the mass-interval rows and the last one.
        if applied or (self._pool_t is not None and t >= self._pool_t + self.update_s - 1e-6):
            self.pool_row(eng, t)

    def note_in(self, vin):
        self._dvin += float(vin)

    def pool_row(self, eng, t):
        if self.pool_idx is None or self._pool_f is None:
            return
        # A time that is not later than the last row is dropped. Writing it would put the
        # time column backwards, or repeat t = 0 after the file is opened. The cursor stays.
        if self._pool_t is not None and t <= self._pool_t + 1e-6:
            return
        vol = float(eng.h2().flat[self.pool_idx].sum(dtype=np.float64)) * self.dx * self.dx
        if self._pool_t is None:
            q = 0.0
        else:
            q = (self._pool_v - vol + self._dvin) / (t - self._pool_t)
        self._pool_f.write("{:.6f},{:.6f},{:.6f}\n".format(t, vol, q))
        self._pool_f.flush()
        self._pool_t = t
        self._pool_v = vol
        self._dvin = 0.0

    def open_pool(self, root, eng):
        if self.pool_idx is None:
            return
        self._pool_f = open(root + ".pool", "w", encoding="utf-8")
        self._pool_f.write("time_s,pool_volume_m3,outflow_m3s\n")
        self.pool_row(eng, 0.0)

    def close_pool(self):
        if self._pool_f is not None:
            self._pool_f.close()
            self._pool_f = None

    def final_bed(self):
        bed = self.grid_z.copy()
        flat = bed.ravel()
        flat[self.idx] = self.zcur
        return bed

    def mxe(self, hmax_buf):
        """Peak water level (datum) for the .mxe grid. Off the notch: bed + peak depth, as with
        no breach file. On a breach cell: the highest of (peak depth so far + bed) over the bed
        periods, the last one closed with the final peak depth, i.e. max over time of h + z(t).
        The peak depth alone with any single bed can be too low: the water may stand highest
        over the intact crest before the cut and be deepest over the lowered bed after it."""
        hmax = np.asarray(hmax_buf, dtype=np.float64)
        out = self.grid_z + hmax.reshape(self.grid_z.shape)
        hf = hmax[self.idx]
        wse = np.where(hf > 0, np.maximum(self.wse_peak, hf + self.zcur), self.wse_peak)
        out.ravel()[self.idx] = wse
        return out

    def stats(self):
        frac = self._frac(self.last_t) if self.last_t is not None else 0.0
        invert = self.invert_start - frac * (self.invert_start - self.invert_final)
        return {"updates": self.updates,
                "final_invert_m": float(invert),
                "final_bottom_width_m": float(self.bottom_w * frac)}


class Engine:
    def __init__(self, deck, backend):
        self.d = d = deck
        self.b = backend
        ny, nx = d.ny, d.nx
        # bed relative to the lowest valid cell: float32 then resolves depths to ~1e-5 m.
        # A breach notch can go below that lowest cell; the reference includes the final notch
        # so the lowered bed stays at or above it. No breach file: today's reference.
        self.zref = float(d.z[d.valid].min()) if d.valid.any() else 0.0
        if d.breach is not None:
            self.zref = min(self.zref, d.breach.lowest_final())
        # rainfall rides as one more series after the inflow series (index n_series), and the
        # water-level series (relative to zref) after that
        series = list(d.series) + ([("rain",) + tuple(d.rain)] if d.rain is not None else [])
        wl_base = len(series)
        series += wl_series(d, self.zref)
        n_values = sum(len(s[1]) for s in series)
        layout = be.buffer_layout(ny, nx, len(d.src_cells), len(series), n_values,
                                  n_wl=len(wl_faces(d)))
        a = self.a = backend.allocate(layout)
        a["z"][:] = np.where(d.valid, d.z - self.zref, 0.0).ravel()
        a["n2"][:] = (d.n ** 2).ravel()
        a["h"][:] = d.h0.ravel()
        a["ok"][:] = d.valid.ravel()
        a["scl"][:] = 1.0
        a["hmax"][:] = 0.0                  # LISFLOOD-FP: maxima from the first step on
        a["tmax"][:] = NODATA
        a["arr"][:] = NODATA
        off = 0
        for k, (_, ts, qs) in enumerate(series):
            a["ser_off"][k] = off
            a["ser_t"][off:off + len(ts)] = ts
            a["ser_q"][off:off + len(ts)] = qs
            off += len(ts)
        a["ser_off"][len(series)] = off
        for i, (r, c, k) in enumerate(d.src_cells):
            a["s_cell"][i] = r * nx + c
            a["s_series"][i] = k
        self.wl_rc = wl_setup(self, d, a, wl_base)
        st = a["st"]
        st[S_HMAX] = np.float32(a["h"].max()).view(np.uint32)
        self.src_rc = (np.array([s[0] for s in d.src_cells], int),
                       np.array([s[1] for s in d.src_cells], int))
        self.breach_rc = ((d.breach.rows, d.breach.cols) if d.breach is not None else None)
        p = self.p = be.Params()
        p.nx, p.ny = nx, ny
        p.free_mask = sum(_FREE_BITS[e] for e in d.free)
        p.n_src, p.n_series = len(d.src_cells), len(d.series)
        p.n_wl = len(wl_faces(d))
        p.dx, p.dt0, p.alpha, p.g = d.dx, d.dt0, d.cfl, G
        p.theta, p.fric2d = d.theta, int(d.fric2d)
        p.src_cfl = int(d.source_cfl)
        p.inflow_lf_order = int(d.inflow_lf_order)
        p.rain = int(d.rain is not None)
        self.rain_area = float(d.valid.sum()) * d.dx * d.dx
        p.dry, p.maxhflow, p.velmin, p.arr_depth = DRY_M, MAX_HFLOW, VEL_MIN_DEPTH_M, d.arr_depth
        self.win = self._window_from(self.h2() > 0)

    def h2(self):
        return self.a["h"].reshape(self.d.ny, self.d.nx)

    def _bbox(self, rmin, rmax, cmin, cmax):
        d = self.d
        if d.rain is not None:
            return (0, d.ny, 0, d.nx)        # rain falls on every cell: the window is the grid
        if len(self.src_rc[0]):
            rmin, rmax = min(rmin, self.src_rc[0].min()), max(rmax, self.src_rc[0].max())
            cmin, cmax = min(cmin, self.src_rc[1].min()), max(cmax, self.src_rc[1].max())
        brc = getattr(self, "breach_rc", None)
        if brc is not None and len(brc[0]):
            rmin, rmax = min(rmin, int(brc[0].min())), max(rmax, int(brc[0].max()))
            cmin, cmax = min(cmin, int(brc[1].min())), max(cmax, int(brc[1].max()))
        wrc = getattr(self, "wl_rc", None)
        if wrc is not None and len(wrc[0]):
            rmin, rmax = min(rmin, int(wrc[0].min())), max(rmax, int(wrc[0].max()))
            cmin, cmax = min(cmin, int(wrc[1].min())), max(cmax, int(wrc[1].max()))
        if rmin > rmax:
            return (0, 1, 0, 1)
        m = WINDOW_MARGIN
        return (max(0, rmin - m), min(d.ny, rmax + m + 1),
                max(0, cmin - m), min(d.nx, cmax + m + 1))

    def _window_from(self, wet):
        rows = np.flatnonzero(wet.any(axis=1))
        cols = np.flatnonzero(wet.any(axis=0))
        if not len(rows):
            return self._bbox(1, 0, 1, 0)
        return self._bbox(rows[0], rows[-1], cols[0], cols[-1])

    def batch(self, t, t_event, nsteps, clip):
        """Up to nsteps steps from model time t until t_event is reached (with clip, the last
        step is cut to land on it; without, it passes it as LISFLOOD-FP passes its output
        times). Returns the batch's state words as a dict."""
        st = self.a["st"]
        r0, r1, c0, c1 = self.win
        p = self.p
        p.r0, p.c0, p.wr, p.wc = r0, c0, r1 - r0, c1 - c0
        p.t0, p.t_end = t, t_event - t
        p.clip_end = int(bool(clip))
        hm = st[S_HMAX]
        st[:] = 0
        st[S_HMAX] = hm
        _setf(st, S_MINDT, 1e30)
        st[S_RMIN] = st[S_CMIN] = 0xFFFFFFFF
        # the CPU build leaves out the cells of the window that cannot get wet (redrawn every
        # SKIP_CHUNK steps; the same numbers bit for bit; the GPU runs the whole window)
        self.b.run_batch(p, nsteps, SKIP_CHUNK)
        out = {"t_local": _f(st, S_T), "dt": _f(st, S_LASTDT), "steps": int(st[S_STEPS]),
               "min_dt": _f(st, S_MINDT), "vin": _f(st, S_VIN), "vout": _f(st, S_VOUT),
               "clip": int(st[S_CLIP]) * MF_FIX, "qin": _f(st, S_QIN),
               "qout": int(st[S_QOUT]) * MF_FIX,
               # engines built on this class without rain (SGC) leave rain_area unset: no rain
               "vrain": _f(st, S_RAIND) * getattr(self, "rain_area", 0.0)}
        rmin, rmax, cmin, cmax = (int(st[k]) for k in (S_RMIN, S_RMAX, S_CMIN, S_CMAX))
        if out["steps"]:
            # every cell wet during the batch lies in this box; cells still wet from before
            # are inside it too unless they dried, which needs no window
            self.win = self._bbox(rmin, rmax, cmin, cmax) if rmin <= rmax \
                else self._bbox(1, 0, 1, 0)
        return out


def run_par(par_path, progress=None, cancel=None, log=None, backend="auto"):
    """Run a .par file (paths in it are relative to its folder), the same call as
    builtin_lisflood.run_par. backend: 'auto', 'metal' or 'cpu'. Returns a dict."""
    def say(msg):
        if log:
            log(msg)

    deck = Deck(par_path)
    try:
        bk = be.make_backend(backend, log=say)
    except be.BackendError as e:
        raise EngineError(str(e))
    try:
        return _run(deck, bk, progress, cancel, say)
    finally:
        bk.close()


def _run(d, bk, progress, cancel, say):
    if d.solver == "fv1":
        say("The GPU engine runs 'fv1' with its local-inertial scheme.")
    eng = Engine(d, bk)
    a = eng.a
    ny, nx, dx = d.ny, d.nx, d.dx
    xll, yll = d.xll, d.yll
    ytop = yll + ny * dx
    root = d.root
    sim, massint, saveint = d.sim, d.massint, d.saveint

    gauge_cells = []
    for gx, gy, dd, w in d.gauges:
        k = max(1, int(round(w / dx)))
        c = int(np.floor((gx - xll) / dx))
        r = int(np.floor((ytop - gy) / dx))
        if dd in ("E", "W"):
            gauge_cells.append(("x", np.clip(np.arange(r, r + k), 0, ny - 1),
                                np.clip(c, 0, nx), 1.0 if dd == "E" else -1.0))
        else:
            gauge_cells.append(("y", np.clip(r, 0, ny), np.clip(np.arange(c, c + k), 0, nx - 1),
                                -1.0 if dd == "N" else 1.0))

    def gauge_q():
        qx = a["qx"].reshape(ny, nx + 1)
        qy = a["qy"].reshape(ny + 1, nx)
        out = []
        for kind, ga, gb, sign in gauge_cells:
            q = qx[ga, gb] if kind == "x" else qy[ga, gb]
            out.append(sign * float(np.asarray(q, np.float64).sum()) * dx)
        return out

    def volume():
        return float(a["h"].sum(dtype=np.float64)) * dx * dx

    def h_out():
        return eng.h2().astype(np.float64)

    mass_f = open(root + ".mass", "w")
    mass_f.write("Time         Tstep      MinTstep   NumTsteps    Area         Vol         "
                 "Qin         Hds        Qout          Qerror         Verror       "
                 "Rain-Inf+Evap\n")
    dis_f = open(root + ".discharge", "w") if d.gauges else None
    if dis_f:
        dis_f.write("Time" + "".join("  gauge{}".format(i + 1) for i in range(len(d.gauges)))
                    + "\n")

    t = 0.0
    steps, min_dt, last_dt = 0, 1e9, d.dt0
    mean_dt = d.dt0
    next_mass = massint
    snap_k = 0
    next_snap = 0.0 if saveint <= sim else None
    vol0 = volume()
    vin = vout = clip = vrain = 0.0
    qin = qout = 0.0
    batches = 0
    t_start = time.time()
    last_prog = t_start
    br = d.breach
    if br is not None:
        br.open_pool(root, eng)

    def write_mass(tt):
        vol = volume()
        verr = vol - (vol0 + vin - vout)
        area = float((a["h"] > DRY_M).sum()) * dx * dx
        mass_f.write("{:<12.3f} {:<10.4f} {:<10.4f} {:<12d} {:<12.1f} {:<12.3f} {:<12.4f} "
                     "{:<10.4f} {:<13.4f} {:<14.6f} {:<12.4f} {:<12.4f}\n".format(
                         tt, last_dt, min_dt if steps else last_dt, steps, area, vol, qin,
                         0.0, qout, 0.0, verr - write_mass.last_err, vrain))
        write_mass.last_err = verr
        write_mass.last_t = tt
        mass_f.flush()
        if dis_f:
            dis_f.write("{:.3f}".format(tt) + "".join(" {:.4f}".format(v) for v in gauge_q())
                        + "\n")
            dis_f.flush()
    write_mass.last_err = 0.0
    write_mass.last_t = 0.0

    try:
        while t < sim - 1e-6:
            if cancel and cancel():
                raise Cancelled("cancelled")
            if next_snap is not None and t >= next_snap - 1e-6:
                bl._write_asc("{}-{:04d}.wd".format(root, snap_k), h_out(), xll, yll, dx)
                snap_k += 1
                next_snap = snap_k * saveint if snap_k * saveint <= sim + 1e-9 else None
            # an update time that has already passed must not stay in the min below: that
            # would make t_end <= 0, zero steps and a loop that never ends
            if br is not None:
                br.catch_up(eng, t)
            t_event = min(sim, next_mass, next_snap if next_snap is not None else sim)
            if br is not None and br.next_u is not None:
                t_event = min(t_event, br.next_u)
            # never cut a step to land on a time (as LISFLOOD-FP): a cut step is short, and the
            # Runner reads a short step in the .mass file as a collapsing solver. The kernels'
            # clip_end Param is reserved for that cut and is deliberately held False here (not dead
            # plumbing): enabling it would reintroduce the .mass dt-collapse false positive above.
            cut = False      # the clip_end flag; NOT the clipped-volume total `clip`
            want = int(math.ceil((t_event - t) / max(mean_dt, 1e-3))) + 1
            n = max(1, min(BATCH_STEPS, want))
            out = eng.batch(t, t_event, n, cut)
            batches += 1
            if out["steps"]:
                vin += out["vin"] + out["vrain"]
                vrain += out["vrain"]
                vout += out["vout"]
                clip += out["clip"]
                steps += out["steps"]
                min_dt = min(min_dt, out["min_dt"])
                last_dt = out["dt"]
                qin, qout = out["qin"], out["qout"]
                mean_dt = out["t_local"] / out["steps"]
                if br is not None:
                    br.note_in(out["vin"])
            if cut and (t_event - t) - out["t_local"] <= 2e-3:
                t = t_event
            else:
                if not out["steps"] and out["t_local"] < (t_event - t) - 2e-3:
                    raise EngineError("the GPU engine made no progress at t = {:.1f} s".format(t))
                t += out["t_local"]
                if (t_event - t) <= 2e-3:      # float32 rounding just short of the event
                    t = max(t, t_event)
            if t >= next_mass - 1e-6:
                write_mass(t)
                if br is not None:
                    br.pool_row(eng, t)
                next_mass += massint
            now = time.time()
            if progress and now - last_prog > 2.0:
                progress(t, sim)
                last_prog = now
        if write_mass.last_t < t - 1e-6:
            write_mass(t)
            if br is not None:
                br.pool_row(eng, t)
        if next_snap is not None and t >= next_snap - 1e-9:
            bl._write_asc("{}-{:04d}.wd".format(root, snap_k), h_out(), xll, yll, dx)
    finally:
        mass_f.close()
        if dis_f:
            dis_f.close()
        if br is not None:
            br.close_pool()

    valid = d.valid
    shape = (ny, nx)
    cells = ny * nx
    hmax = a["hmax"].reshape(shape).astype(np.float64)
    vmax = a["vmax"][:cells].reshape(shape).astype(np.float64)
    dv = a["vmax"][cells:].reshape(shape).astype(np.float64)
    arrival = a["arr"].reshape(shape).astype(np.float64)
    tmax = a["tmax"].reshape(shape).astype(np.float64)
    bl._write_asc(root + ".max", np.where(valid, hmax, NODATA), xll, yll, dx)
    bl._write_asc(root + ".maxVc", np.where(valid, vmax, NODATA), xll, yll, dx)
    bl._write_asc(root + ".maxDV", np.where(valid, dv, NODATA), xll, yll, dx)
    bl._write_asc(root + ".inittm", np.where(valid, arrival, NODATA), xll, yll, dx, fmt="%.4f")
    bl._write_asc(root + ".maxtm", np.where(valid & (hmax > 0), tmax, NODATA), xll, yll, dx,
                  fmt="%.4f")
    mxe = br.mxe(a["hmax"]) if br is not None else d.z + hmax
    bl._write_asc(root + ".mxe", np.where(valid & (hmax > 0), mxe, NODATA), xll, yll, dx)
    if br is not None:
        bl._write_asc(root + ".bed", np.where(valid, br.final_bed(), NODATA), xll, yll, dx,
                      fmt="%.4f")
    vol = volume()
    wall = time.time() - t_start
    stats = {"engine": NAME, "version": VERSION, "backend": bk.kind, "device": bk.device,
             "steps": steps, "batches": batches, "sim_time_s": t, "wall_s": round(wall, 2),
             "cells": nx * ny, "injected_m3": vin, "outflow_m3": vout, "final_volume_m3": vol,
             "mass_error_m3": vol - (vol0 + vin - vout), "clipped_m3": clip,
             "rain_m3": vrain,
             # cell updates the CPU build ran (the dry-cell skip leaves out the rest of the window)
             "cell_steps": getattr(bk, "cells_run", None)}
    # where the time went (backends.Profile); timing only, no result depends on it
    stats["profile"] = bk.profile() if hasattr(bk, "profile") else None
    if br is not None:
        stats["breach"] = br.stats()
    say("GPU engine ({}, {}): {} steps, {:.2f} h simulated in {:.1f} min; volume error {:.3g} m3 "
        "of {:.4g} m3 injected.".format(bk.kind, bk.device, steps, t / 3600.0, wall / 60.0,
                                       stats["mass_error_m3"], vin))
    return stats


def main(argv):
    args = [x for x in argv[1:] if not x.startswith("-")]
    backend = "auto"
    for i, x in enumerate(argv):
        if x in ("-backend", "--backend") and i + 1 < len(argv):
            backend = argv[i + 1]
            args = [y for y in args if y != backend]
    if "-version" in argv or not args:
        print(NAME + " " + VERSION)
        return 0
    try:
        stats = run_par(args[-1], log=print, backend=backend,
                        progress=(lambda t, s: print("t = {:.2f} h".format(t / 3600.0)))
                        if "-v" in argv else None)
    except (EngineError, OSError, ValueError) as e:
        print("ERROR: {}".format(e))
        return 1
    print("wall {:.1f} s".format(stats["wall_s"]))
    return 0
