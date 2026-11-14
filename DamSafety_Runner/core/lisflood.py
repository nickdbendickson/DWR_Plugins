"""LISFLOOD-FP (Bates et al.; University of Bristol) tier: write the inputs,
run the executable, read the envelopes back.

Uses the inertial ("acceleration") floodplain solver on a raster, with the
breach hydrograph as a point source just downstream of the dam and free
(uniform-flow) outflow on all four domain edges.

Arrival time needs the `inittm_depth` keyword added by the patch shipped
with this plugin (scripts/lisflood-nd-breach.patch). Without it LISFLOOD-FP
ignores the keyword and records arrival at its internal wet/dry depth (about
1 mm); `check_lisflood()` tells the two apart."""
import math
import os
import subprocess
import sys
import time

import numpy as np

from .grid import Grid
from .units import ACFT, ACRE, CFS, FT, ARRIVAL_DEPTH_FT

# Windows: no console window for a child program (QGIS is a GUI program, so each console
# program it starts opens its own window, and closing that window kills the run)
NO_WINDOW = {"creationflags": 0x08000000} if os.name == "nt" else {}   # CREATE_NO_WINDOW


ARRIVAL_TOL_M = 0.001    # LISFLOOD-FP writes depth to 3 decimals
# audit P3 (3.16.10): the dtype of the result grids read_results returns (depth, velocity,
# arrival, WSE). The files hold 3 decimals from a single-precision solver; float32 keeps every
# digit of them and halves the four full-domain arrays. Elevation grids stay float64.
RESULT_DTYPE = np.float32

# The line LISFLOOD-FP prints when it does not have the arrival-depth patch (it then ignores
# the inittm_depth keyword and records arrival at the first wetting, about 1 mm).
PATCH_MISSING_MARKER = "Unknown parameter ignored: inittm_depth"
PATCH_MISSING_MSG = (
    "This LISFLOOD-FP build does not have the arrival-depth patch (it ignored "
    "'inittm_depth'), so arrival times would mark the first millimetre of water "
    "instead of the arrival depth. Rebuild it with scripts/build_lisflood_linux.sh on Linux "
    "or scripts/build_lisflood_macos.sh on macOS (both apply scripts/patch_lisflood.py; a "
    "plain CMake build of your own needs that patch first) and set the new executable in "
    "Processing > Options > Providers > ND Dam Breach Runner, or tick 'Accept an unpatched "
    "LISFLOOD-FP build' there to run anyway with unreliable arrival times.")
PATCH_MISSING_WARNING = (
    "LISFLOOD-FP has no arrival-depth patch and 'Accept an unpatched build' is on: "
    "arrival times mark the first wetting (about 1 mm), NOT the arrival depth, so any "
    "hazard warning time from this run is unreliable. Build a patched LISFLOOD-FP for a "
    "defensible run.")


def patch_missing(logtxt):
    """True when a LISFLOOD-FP log shows it ignored the arrival-depth keyword."""
    return PATCH_MISSING_MARKER in (logtxt or "")

# v3.4.0: the floodplain solver keyword written to the .par file. "acceleration" is the
# local-inertial solver used everywhere by default; "fv1" is the first-order finite-volume
# (shallow-water, shock-capturing) solver of LISFLOOD-FP 8, whose velocities near the breach
# are not limited by the inertial approximation (used by the near-dam refine option). Neither
# has been run against a real executable here: nothing in this plugin was run in a real QGIS
# or with a real LISFLOOD-FP.
EDGE_LETTERS = ("N", "S", "E", "W")
SOLVER_ACC = "acceleration"
SOLVER_FV1 = "fv1"
# 3.16.0: the main run's solver is a choice among the solvers that write everything the
# hazard classification needs - maximum velocity (.maxVc) and arrival time honouring the
# arrival-depth patch (inittm_depth) - with ACC the default (Nick, 2026-11-06). FV2 is the
# second-order finite-volume and MWDG2 the adaptive multiwavelet DG2 solver of LISFLOOD-FP 8;
# both run with the slope limiter ("limitslopes") unless limit_slopes is off. FV1 is NOT
# offered for the main run: LISFLOOD-FP 8.1 writes no maximum velocity and no arrival time for
# it (it stays the near-dam refine's solver, which keeps the ACC velocities). Plain DG2 is not
# offered either: it needs slope-coefficient DEMs this plugin does not make.
SOLVER_FV2 = "fv2"
SOLVER_MWDG2 = "mwdg2"
SOLVERS = (SOLVER_ACC, SOLVER_FV1, SOLVER_FV2, SOLVER_MWDG2)      # what the .par may hold
MAIN_SOLVERS = (SOLVER_ACC, SOLVER_FV2, SOLVER_MWDG2)             # what the main run may use
LIMIT_SLOPES_SOLVERS = (SOLVER_FV2, SOLVER_MWDG2)
SOLVER_LABELS = {SOLVER_ACC: "ACC (local inertial, default)",
                 SOLVER_FV1: "FV1 (first-order finite volume)",
                 SOLVER_FV2: "FV2 (second-order finite volume, slope limiter)",
                 SOLVER_MWDG2: "MWDG2 (adaptive multiwavelet DG2, slope limiter)"}
SOLVER_SHORT = {SOLVER_ACC: "ACC", SOLVER_FV1: "FV1", SOLVER_FV2: "FV2", SOLVER_MWDG2: "MWDG2"}
SOLVER_ALIASES = {"acc": SOLVER_ACC, "acceleration": SOLVER_ACC, "inertial": SOLVER_ACC,
                  "lia": SOLVER_ACC, "fv2": SOLVER_FV2, "fv2-ll": SOLVER_FV2,
                  "fv2ll": SOLVER_FV2, "mwdg2": SOLVER_MWDG2, "mwdg2-ll": SOLVER_MWDG2,
                  "mwdg2ll": SOLVER_MWDG2}
SOLVER_REFUSED = {"fv1": "FV1 writes no maximum velocity or arrival time",
                  "dg2": "DG2 needs slope-coefficient DEMs this plugin does not make",
                  "hwfv1": "HWFV1 is not tested with this plugin"}
SOLVER_ENV = "NDBREACH_LISFLOOD_SOLVER"


def solver_from_text(text, default=SOLVER_ACC):
    """The main-run solver keyword for a name ("ACC", "fv2", "mwdg2-ll" ...); empty =
    `default`. Any other name raises ModelError: the run never falls back to another solver."""
    t = str(text or "").strip().lower()
    if not t:
        return default
    if t not in SOLVER_ALIASES:
        why = SOLVER_REFUSED.get(t)
        raise ModelError("LISFLOOD-FP solver '{}' cannot be used for the main run{} - choose "
                         "ACC, FV2 or MWDG2".format(text, " ({})".format(why) if why else ""))
    return SOLVER_ALIASES[t]


def solver_from_env(default=SOLVER_ACC):
    """The solver named by the NDBREACH_LISFLOOD_SOLVER environment variable, else `default`."""
    return solver_from_text(os.environ.get(SOLVER_ENV, ""), default)
# 3.15.1: stable settings for the ACC (local-inertial) solver. With LISFLOOD-FP's defaults (theta
# 1.0, no numerical diffusion, Bates et al. 2010; cfl 0.7, a time step from the wave celerity
# only) the scheme can go unstable on a fine grid and MAKE water: the depth is clipped where a
# cell empties past zero. DWR-2865 at 10 m: the water level checkerboarded +-3 m in a deep
# pond 2 km below the dam, 0.21 M m3 injected and 1.37 M m3 in the model after 8.3 h (+567%),
# flood 838 ac instead of 213 ac. DWR-4031 at 10 m: fast shallow flow at the breach, +36%
# while the hydrograph went in. theta 0.7 (de Almeida et al. 2012, the q-centred scheme's
# recommended value) cures the first and not the second; theta 0.7 with cfl 0.5 cures both
# (0.0% / -0.2%). A run whose LISFLOOD-FP volume error is over STABILITY_RETRY_PCT is run
# again with them (workflow.run_dam); a run that balances is never changed.
STABLE_THETA = 0.7
STABLE_CFL = 0.5
STABILITY_RETRY_PCT = 1.0
STABILITY_MARKER = "stable_settings.json"

# Audit P2 (2026-11-07): the LISFLOOD-FP child had no wall-clock limit, so a solver that hung or
# collapsed its time step held QGIS's run for ever. Every run now has one: the setting
# (LISFLOOD_MAX_WALL_S / model.max_wall_s; 0 = automatic) as given, else WALL_ESTIMATE_FACTOR x
# the run-time estimate of core/estimate.py when this computer's speed is known (a calibrate()
# rate on the run), else DEFAULT_MAX_WALL_S. The automatic limit is never under WALL_FLOOR_S.
DEFAULT_MAX_WALL_S = 24 * 3600.0
WALL_ESTIMATE_FACTOR = 6.0
WALL_FLOOR_S = 3600.0
WALL_SETTING_HINT = ("'LISFLOOD-FP: longest wall-clock time for one run' (LISFLOOD_MAX_WALL_S, "
                     "the CLI's model.max_wall_s; 0 = automatic)")
# CFL collapse detection: LISFLOOD-FP's time step is cfl x cell / sqrt(g x depth) (ACC), so a
# step this short means water deeper than DT_COLLAPSE_DEPTH_M somewhere - numerical garbage on
# a North Dakota flood (DWR-2865's checkerboarding made +567% water, 3.15.1). The run logs the
# step each progress tick and warns ONCE when it falls below dt_collapse_s(); it never stops.
DT_COLLAPSE_DEPTH_M = 100.0
G_M_S2 = 9.80665


def wall_limit_s(max_wall_s=None, cells=None, sim_time_s=None, cell_m=None, rate=None,
                 cal_cell_m=10.0):
    """The wall-clock limit (s) of one LISFLOOD-FP run: `max_wall_s` when it is a number over 0;
    else WALL_ESTIMATE_FACTOR x core/estimate.seconds_for(...) when `rate` (cell-seconds per wall
    second from calibrate()) and the run's size are known, at least WALL_FLOOR_S; else
    DEFAULT_MAX_WALL_S. Always a positive number: no run is left without a limit."""
    try:
        v = float(max_wall_s or 0.0)
    except (TypeError, ValueError):
        v = 0.0
    if v > 0:
        return v
    try:
        if rate and cells and sim_time_s and cell_m:
            from .estimate import seconds_for
            one = seconds_for(float(cells), float(sim_time_s), float(cell_m), float(rate),
                              cal_cell_m)
            if one > 0 and math.isfinite(one):
                return max(WALL_FLOOR_S, WALL_ESTIMATE_FACTOR * one)
    except (TypeError, ValueError, ZeroDivisionError):
        pass
    return DEFAULT_MAX_WALL_S


def dt_collapse_s(cell_m, cfl=None):
    """The time step below which a run is flagged: the CFL step of `cell_m` with
    DT_COLLAPSE_DEPTH_M of water (cfl: the run's, else LISFLOOD-FP's own 0.7)."""
    c = float(cfl) if cfl else 0.7
    return c * float(cell_m) / math.sqrt(G_M_S2 * DT_COLLAPSE_DEPTH_M)


def _wall_limit_text(limit_s, elapsed_s):
    return ("LISFLOOD-FP ran for {:.2f} h of wall time, past this run's limit of {:.2f} h, and "
            "was stopped. The limit is {} - automatic means {:g} x the run-time estimate when "
            "this computer's LISFLOOD-FP speed is known (the Estimate tool / the CLI's "
            "model.lisflood_rate), else {:.0f} h. If the run was healthy, raise the setting; if "
            "the log shows the time step collapsing, the solver went unstable - try a coarser "
            "cell or the ACC stable settings (theta 0.7, CFL 0.5).").format(
                elapsed_s / 3600.0, limit_s / 3600.0, WALL_SETTING_HINT, WALL_ESTIMATE_FACTOR,
                DEFAULT_MAX_WALL_S / 3600.0)

# ---------------------------------------------------------------------------------------------
# Built-in engine (M5, suite 1.14.1 parity). core/builtin_lisflood.py is the same local-inertial
# equations in numpy, so a run works out of the box WITHOUT a LISFLOOD-FP binary. It is a
# screening approximation, NOT LISFLOOD-FP, and it is STRICTLY OPT-IN: the run guard still errors
# when no engine is chosen (see algorithms.find_lisflood). The word "builtin" selects it; its
# "executable" is its .py file, so every "is the executable there" check and the run manifest keep
# working. resolve_exe() deliberately does NOT fall back to the built-in on an empty value (unlike
# the nd_ sibling): choosing it is always an explicit act, so the numpy approximation can never be
# substituted silently for LISFLOOD-FP in a hazard classification.
HERE = os.path.dirname(os.path.abspath(__file__))
BUILTIN_EXE = os.path.join(HERE, "builtin_lisflood.py")
BUNDLED_DIR = os.path.join(os.path.dirname(HERE), "bin")
BUILTIN_WORDS = ("builtin", "built-in", "built in", "internal")


def bundled_platform(system=None, machine=None):
    """The bin/<platform> folder name for this computer (or the given uname-style names), or
    None when no build is made for it: windows-x64, linux-x64, macos-arm64, macos-x64.
    3.16.11 (2026-11-07, release workflow): the GitHub release zip of the Runner carries
    bin/linux-x64/lisflood, bin/windows-x64/lisflood.exe and bin/macos-arm64/lisflood (built
    from LISFLOOD-FP 8.3 with scripts/patch_lisflood.py by .github/workflows/release.yml); a
    checkout or a zip built by tools/build_release.py alone has no bin/ and this returns the
    name all the same, bundled_exe() then finds no file."""
    import platform
    system = (system or ("windows" if os.name == "nt" else sys.platform)).lower()
    mach = (machine or platform.machine()).lower()
    if mach in ("x86_64", "amd64"):
        arch = "x64"
    elif mach in ("arm64", "aarch64"):
        arch = "arm64"
    else:
        return None
    if system.startswith("win"):
        return "windows-x64" if arch == "x64" else None
    if system.startswith("linux"):
        return "linux-x64" if arch == "x64" else None
    if system.startswith("darwin") or system.startswith("mac"):
        return "macos-" + arch
    return None


def bundled_exe():
    """The LISFLOOD-FP bundled with this plugin for this computer (bin/<platform>/, see
    bundled_platform), or None: a plain checkout ships no bin/ dir, so this is None there. The
    execute bit an install from ZIP drops is put back."""
    import stat
    plat = bundled_platform()
    if plat is None:
        return None
    p = os.path.join(BUNDLED_DIR, plat, "lisflood.exe" if os.name == "nt" else "lisflood")
    if not os.path.isfile(p):
        return None
    if os.name != "nt" and not os.access(p, os.X_OK):
        try:
            os.chmod(p, os.stat(p).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        except OSError:
            return None
    return p


def is_builtin(exe):
    return bool(exe) and os.path.abspath(str(exe)) == BUILTIN_EXE


def resolve_exe(exe):
    """The flood engine for a setting or parameter value. The word "builtin" (any BUILTIN_WORDS
    spelling) selects the built-in engine; anything else is returned verbatim. An empty value
    stays empty on purpose: the caller's guard then raises, so the built-in is never used unless
    the user asked for it."""
    v = (str(exe).strip() if exe else "")
    if v.lower() in BUILTIN_WORDS:
        return BUILTIN_EXE
    return v


def describe_exe(exe):
    """One line for logs and reports: which program runs the floods."""
    if is_builtin(exe):
        return "the built-in engine (local inertial, in numpy; NOT LISFLOOD-FP)"
    if exe and os.path.abspath(str(exe)).startswith(BUNDLED_DIR + os.sep):
        return "LISFLOOD-FP bundled with the plugin ({})".format(exe)
    return "LISFLOOD-FP at {}".format(exe)


def engine_label(exe):
    """A concise model name for the report "Model" row / final_model / manifest: it distinguishes
    the built-in screening engine from LISFLOOD-FP so a run can never be read as LISFLOOD-FP when
    it was not."""
    if is_builtin(exe):
        return "built-in engine (local inertial, numpy; NOT LISFLOOD-FP)"
    return "LISFLOOD-FP"


def engine_name(exe):
    """A short noun for log lines and prose: "the built-in engine" or "LISFLOOD-FP"."""
    return "the built-in engine" if is_builtin(exe) else "LISFLOOD-FP"


class ModelError(RuntimeError):
    pass


def _not_a_program(err):
    """True when the system refused to start a file because it is no program for it: Windows
    errors 193 ("not a valid Win32 application") and 216 ("not compatible with the version of
    Windows you're running": what a file named .exe that is no program gets, and a build for
    another kind of processor), 'Exec format error' elsewhere."""
    import errno
    return getattr(err, "winerror", None) in (193, 216) or err.errno == errno.ENOEXEC


class Cancelled(RuntimeError):
    pass


class LisfloodSettings:
    def __init__(self, exe, threads=0, manning_n=0.06, manning_grid=None,
                 arrival_depth_m=ARRIVAL_DEPTH_FT * FT, sim_time_s=6 * 3600.0, massint_s=300.0,
                 initial_tstep_s=10.0, velocity_min_depth_m=0.03, extra_par=None,
                 initial_depth=None, gauges=None, snapshot_s=None, extra_sources=None,
                 solver=SOLVER_ACC, closed_edges=(), allow_unpatched=False, theta=1.0,
                 cfl=None, limit_slopes=True, max_wall_s=0.0, rate=None):
        if solver not in SOLVERS:
            raise ModelError("unknown LISFLOOD-FP solver '{}' (one of {})".format(
                solver, ", ".join(SOLVERS)))
        self.solver = solver                    # v3.4.0: "acceleration" (default) or "fv1"
        # 3.16.0: "limitslopes" for FV2 / MWDG2 (ignored by the other solvers)
        self.limit_slopes = bool(limit_slopes)
        # 3.15.1: ACC theta (1.0 = LISFLOOD-FP's own, nothing written) and cfl (None = LISFLOOD-FP's
        # own 0.7, nothing written); STABLE_THETA / STABLE_CFL for a run that lost its balance
        self.theta = float(theta)
        if not 0.0 < self.theta <= 1.0:
            raise ModelError("ACC theta must be in (0, 1], got {}".format(self.theta))
        self.cfl = None if cfl is None else float(cfl)
        if self.cfl is not None and not 0.0 < self.cfl <= 1.0:
            raise ModelError("ACC cfl must be in (0, 1], got {}".format(self.cfl))
        # v3.11.0: domain edges written as closed walls ("N", "S", "E", "W") instead of free
        # outflow. The near-dam FV1 run closes the edges that face the reservoir: with FV1 a
        # free edge across ponded water brings water INTO the model (see core/refine.py).
        self.closed_edges = tuple(sorted({str(e).upper() for e in (closed_edges or ())}))
        bad = [e for e in self.closed_edges if e not in EDGE_LETTERS]
        if bad:
            raise ModelError("unknown domain edge(s) to close: {}".format(", ".join(bad)))
        self.exe = exe
        self.threads = threads
        self.manning_n = manning_n
        self.manning_grid = manning_grid
        self.arrival_depth_m = arrival_depth_m
        self.sim_time_s = sim_time_s
        self.massint_s = massint_s
        self.initial_tstep_s = initial_tstep_s
        self.velocity_min_depth_m = velocity_min_depth_m
        self.extra_par = extra_par or []
        self.initial_depth = initial_depth      # Grid of initial water depth (m) or None
        self.gauges = gauges or []              # [{"name", "xy", "direction", "width_m"}]
        self.snapshot_s = snapshot_s            # write depth snapshots this often (animation)
        # more point sources: [{"name", "xy", "t_s", "q_cms", optional "line"}], e.g. ambient
        # river flow entering the domain
        self.extra_sources = extra_sources or []
        # Accept a LISFLOOD-FP build WITHOUT the arrival-depth patch (it ignores inittm_depth).
        # Off by default so _run() refuses such a build; on, the run proceeds with a loud warning
        # and `unpatched_used` is set, because arrival times then mark the first wetting (~1 mm)
        # rather than the arrival depth. For users who cannot build a patched LISFLOOD-FP (e.g. a
        # locked-down Mac).
        self.allow_unpatched = allow_unpatched
        self.unpatched_used = False
        # Audit P2: the wall-clock limit of this run (0 = automatic, see wall_limit_s) and this
        # computer's calibrate() rate (cell-seconds per wall second; None = unknown), which the
        # automatic limit scales from. _run fills wall_limit_used and dt_collapsed for the report.
        self.max_wall_s = float(max_wall_s or 0.0)
        self.rate = rate
        self.wall_limit_used = None
        self.dt_collapsed = None       # (t_s, tstep_s) at the first collapsed step, or None


class Envelopes:
    """Maximum depth (m), maximum speed (m/s), arrival (hr), max WSE (m) on the
    model grid, plus run statistics."""

    def __init__(self, depth, velocity, arrival_hr, wse, stats=None, notes=None, gauges=None):
        self.gauges = gauges or {}       # name -> (t_s, q_cms)
        self.depth = depth
        self.velocity = velocity
        self.arrival_hr = arrival_hr
        self.wse = wse
        self.stats = stats or {}
        self.notes = notes or []


def inflow_cells(dem, inflow_xy, line=None):
    """Cells that share the breach inflow: the cells under `line` (a segment
    across the flow, about the breach width) or just the inflow cell. Spreading
    the inflow avoids an unrealistic velocity spike in a single cell."""
    cells = []
    r, c = dem.rc(*inflow_xy)
    if not dem.inside(r, c) or not dem.valid()[r, c]:
        raise ModelError("the breach inflow point is outside the DEM or on no-data")
    if line is not None:
        (x0, y0), (x1, y1) = line
        n = max(2, int(np.hypot(x1 - x0, y1 - y0) / (0.5 * dem.dx)) + 1)
        for t in np.linspace(0.0, 1.0, n):
            rr, cc = dem.rc(x0 + t * (x1 - x0), y0 + t * (y1 - y0))
            if dem.inside(rr, cc) and dem.valid()[rr, cc] and (rr, cc) not in cells:
                cells.append((rr, cc))
    if (r, c) not in cells:
        cells.append((r, c))
    return cells


def local_grid(dem):
    """The same cells with the lower-left corner at (0, 0). The default
    LISFLOOD-FP build is single precision, which resolves only 0.5 m at UTM
    northings of 5,000,000 m; local coordinates keep the boundary cells exact.
    Results are arrays, so they map straight back onto the real DEM."""
    return Grid(dem.data, 0.0, dem.ny * dem.dx, dem.dx, dem.nodata, dem.crs_wkt)


def gauge_section(center_xy, flow_dir, width_m):
    """A gauge that measures the flow along `flow_dir` (unit vector) across a
    section of `width_m` centred on `center_xy`. LISFLOOD-FP sections run along
    a grid axis: east/west gauges span north-south, north/south gauges span
    east-west. Returns a gauge dict."""
    ux, uy = flow_dir
    if abs(ux) >= abs(uy):
        direction = "E" if ux >= 0 else "W"
    else:
        direction = "N" if uy >= 0 else "S"
    return {"xy": tuple(center_xy), "direction": direction, "width_m": float(width_m)}


def _gauge_line(gx, gy, direction, width_m, nx, ny, dx):
    """One .gauge line (x, y, direction, length): the section centred on the gauge, as long as
    LISFLOOD-FP will measure. gx, gy are the centre in grid coordinates (lower-left origin).
    E/W sections run south from (x, y), N/S sections east.

    LISFLOOD-FP limits a section's cell count against the other axis (input.cpp, LoadGauges):
    an E/W section to (columns - 1 - the gauge's column), a N/S section to (rows - 1 - the
    gauge's row, counted from the top). Two things follow.
    * A section longer than the grid is across is read past the flux arrays and the discharge
      is garbage (1e297): it is clipped here, ending at least one cell short of the far edge.
    * LISFLOOD-FP keeps the first cells of a section it shortens, so a long section that
      starts at the grid's edge can end before it reaches the river and reads 0 for the whole
      run (3.20.4: Lake Delhi 2010, three of four gauges). The section is shortened here
      instead, to what LISFLOOD-FP allows, and stays centred on the gauge."""
    if direction in ("E", "W"):
        cap = max(nx - 1 - min(max(int(math.floor(gx / dx)), 0), nx - 1), 0) * dx
        half = 0.5 * min(width_m, cap)
        top = min(gy + half, (ny - 0.5) * dx)               # start inside the top row
        r0 = ny - 1 - int(math.floor(top / dx))             # LISFLOOD-FP's start row
        length = min(top - (gy - half), cap, (ny - 1 - r0) * dx)
        return gx, top, direction, max(length, 0.0)
    cap = min(max(int(math.floor(gy / dx)), 0), ny - 1) * dx
    half = 0.5 * min(width_m, cap)
    left = max(gx - half, 0.5 * dx)
    c0 = int(math.floor(left / dx))
    length = min((gx + half) - left, cap, (nx - 1 - c0) * dx)
    return left, gy, direction, max(length, 0.0)


def gauge_reach_m(gx, gy, direction, nx, ny, dx):
    """The longest section LISFLOOD-FP measures for a gauge here, in metres (see _gauge_line):
    it shrinks towards the east edge for E/W gauges and towards the south edge for N/S ones."""
    if direction in ("E", "W"):
        return max(nx - 1 - min(max(int(math.floor(gx / dx)), 0), nx - 1), 0) * dx
    return min(max(int(math.floor(gy / dx)), 0), ny - 1) * dx



def _near_line(x, y, line_xy, width_m):
    """True if (x, y) is within width_m/2 of any segment of the dam line."""
    half = width_m / 2.0
    pts = list(line_xy)
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        vx, vy = x1 - x0, y1 - y0
        L2 = vx * vx + vy * vy
        t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((x - x0) * vx + (y - y0) * vy) / L2))
        d = ((x - (x0 + t * vx)) ** 2 + (y - (y0 + t * vy)) ** 2) ** 0.5
        if d <= half:
            return True
    return False


def raise_crest(dem, crest_xy, inflow_xy, top_m, length_m=200.0, width_m=12.0,
                line_xy=None):
    """Raise the crest to the top of dam in the model grid only.

    The dam line is burned across its length. A point and a length are only
    the fallback, and that fallback was raising one cell on a long embankment.
    The source DEM is not changed.
    """
    if dem is None or top_m is None or (crest_xy is None and not line_xy):
        return 0
    use_line = line_xy is not None and len(list(line_xy)) >= 2
    vx = vy = px = py = 0.0
    half = 0.0
    if not use_line:
        vx = (inflow_xy[0] - crest_xy[0]) if inflow_xy else 0.0
        vy = (inflow_xy[1] - crest_xy[1]) if inflow_xy else 1.0
        n = (vx * vx + vy * vy) ** 0.5 or 1.0
        px, py = -vy / n, vx / n
        vx, vy = vx / n, vy / n
        half = max(length_m, dem.dx) / 2.0
    data = dem.data
    # Evaluate the same hit test on every cell at once with numpy instead of a
    # per-cell Python double loop. x_centers()/y_centers() give the identical
    # cell-centre coordinates dem.xy(r, c) returns, and the arithmetic mirrors
    # the scalar path (and _near_line) exactly, so the raised DEM and the count
    # are byte-identical to the loop -- only faster.
    xs = dem.x_centers()[None, :]            # cell-centre x per column, shape (1, nx)
    ys = dem.y_centers()[:, None]            # cell-centre y per row, shape (ny, 1)
    if use_line:
        half_line = max(width_m, dem.dx) / 2.0
        hit = np.zeros(data.shape, dtype=bool)
        pts = list(line_xy)
        for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
            sx, sy = x1 - x0, y1 - y0
            L2 = sx * sx + sy * sy
            if L2 == 0:
                t = 0.0
            else:
                t = np.clip(((xs - x0) * sx + (ys - y0) * sy) / L2, 0.0, 1.0)
            d = ((xs - (x0 + t * sx)) ** 2 + (ys - (y0 + t * sy)) ** 2) ** 0.5
            hit |= d <= half_line
    else:
        dx, dy = xs - crest_xy[0], ys - crest_xy[1]
        along = dx * px + dy * py
        across = np.abs(dx * vx + dy * vy)
        hit = (np.abs(along) <= half) & (across <= width_m / 2.0)
    raise_mask = hit & (data == data) & (data < top_m)
    data[raise_mask] = top_m
    return int(np.count_nonzero(raise_mask))

def write_inputs(workdir, dem_real, inflow_xy, hydrograph, st, name="run", inflow_line=None):
    os.makedirs(workdir, exist_ok=True)
    cells = inflow_cells(dem_real, inflow_xy, inflow_line)
    dem = local_grid(dem_real)
    dem.write_asc(os.path.join(workdir, name + ".dem.asc"))
    xmin, ymin, xmax, ymax = dem.bounds
    with open(os.path.join(workdir, name + ".bci"), "w", encoding="utf-8") as f:
        for rr, cc in cells:
            px, py = dem.xy(rr, cc)
            f.write("P {:.3f} {:.3f} QVAR breach\n".format(px, py))
        closed = getattr(st, "closed_edges", ())      # a closed edge is simply not listed
        for edge, lo, hi in (("N", xmin, xmax), ("S", xmin, xmax),
                             ("E", ymin, ymax), ("W", ymin, ymax)):
            if edge not in closed:
                f.write("{} {:.3f} {:.3f} FREE\n".format(edge, lo, hi))
    # QVAR at a point source is per unit width (m2/s); the code multiplies by
    # dx. Each of the n inflow cells carries 1/n of the breach outflow.
    t, q = hydrograph.resampled(30.0)      # keeps every original breakpoint
    n = len(cells)
    extra = []
    for k, src in enumerate(st.extra_sources):
        sname = "src{}".format(k + 1)
        try:
            sc = inflow_cells(dem_real, src["xy"], src.get("line"))
        except ModelError:
            raise ModelError("inflow '{}' is outside the DEM or on no-data".format(
                src.get("name", sname)))
        extra.append((sname, sc, np.asarray(src["t_s"], float), np.asarray(src["q_cms"], float)))
        with open(os.path.join(workdir, name + ".bci"), "a", encoding="utf-8") as f:
            for rr, cc in sc:
                px, py = dem.xy(rr, cc)
                f.write("P {:.3f} {:.3f} QVAR {}\n".format(px, py, sname))
    with open(os.path.join(workdir, name + ".bdy"), "w", encoding="utf-8") as f:
        f.write("breach hydrograph: m3/s / (cell size x {} inflow cells)\n".format(n))
        f.write("breach\n")
        f.write("{} seconds\n".format(len(t)))
        for tv, qv in zip(t, q):
            f.write("{:.6f} {:.4f}\n".format(qv / (dem.dx * n), tv))
        for sname, sc, ts_, qs_ in extra:
            f.write("{}\n".format(sname))
            f.write("{} seconds\n".format(len(ts_)))
            for tv, qv in zip(ts_, qs_):
                f.write("{:.6f} {:.4f}\n".format(qv / (dem.dx * len(sc)), tv))
    lines = [
        "DEMfile {}.dem.asc".format(name),
        "resroot {}".format(name),
        "dirroot results",
        "sim_time {:.0f}".format(st.sim_time_s),
        "initial_tstep {:.3f}".format(st.initial_tstep_s),
        "massint {:.0f}".format(st.massint_s),
        "saveint {:.0f}".format(st.snapshot_s if st.snapshot_s else st.sim_time_s + 10.0),
        "bcifile {}.bci".format(name),
        "bdyfile {}.bdy".format(name),
        getattr(st, "solver", SOLVER_ACC),
    ]
    if getattr(st, "solver", SOLVER_ACC) in LIMIT_SLOPES_SOLVERS and \
            getattr(st, "limit_slopes", True):
        lines.append("limitslopes")
    lines += [
        "hazard",
        "elevoff",
        "inittm_depth {:.4f}".format(st.arrival_depth_m),
    ]
    if not st.snapshot_s:
        lines.insert(lines.index("elevoff") + 1, "depthoff")
    if getattr(st, "solver", SOLVER_ACC) == SOLVER_ACC:
        theta = float(getattr(st, "theta", 1.0) or 1.0)
        if theta < 1.0:
            lines.append("theta {:.3f}".format(theta))
        cfl = getattr(st, "cfl", None)
        if cfl is not None:
            lines.append("cfl {:.3f}".format(float(cfl)))
    if st.manning_grid is not None:
        st.manning_grid.write_asc(os.path.join(workdir, name + ".n.asc"), fmt="%.4f")
        lines.append("manningfile {}.n.asc".format(name))
        lines.append("fpfric {:.4f}".format(st.manning_n))
    else:
        lines.append("fpfric {:.4f}".format(st.manning_n))
    if st.initial_depth is not None:
        init = st.initial_depth.data
        if init.shape != dem.data.shape:
            raise ModelError("the initial depth grid does not match the DEM")
        init_grid = dem.like(np.where(np.isfinite(init), init, 0.0))
        init_grid.write_asc(os.path.join(workdir, name + ".start"), fmt="%.3f")
        lines.append("startfile {}.start".format(name))
    if st.gauges:
        xmin_l, ymin_l = dem.x0, dem.yll
        with open(os.path.join(workdir, name + ".gauge"), "w", encoding="utf-8") as f:
            f.write("{}\n".format(len(st.gauges)))
            for g in st.gauges:
                gx, gy = g["xy"][0] - dem_real.x0, g["xy"][1] - dem_real.yll
                f.write("{:.3f} {:.3f} {} {:.3f}\n".format(
                    *_gauge_line(gx, gy, g["direction"], g["width_m"], dem.nx, dem.ny, dem.dx)))
        lines.append("gaugefile {}.gauge".format(name))
    lines += list(st.extra_par)
    par = os.path.join(workdir, name + ".par")
    with open(par, "w", encoding="utf-8") as f:
        f.write("# written by ND Dam Breach Runner\n")
        f.write("\n".join(lines) + "\n")
    return par


def _read_mass_rows(path):
    """Parse the LISFLOOD-FP .mass file ONCE. Returns a float ndarray of shape (n, 5) whose
    columns are [time_s, domain_vol_m3, Qin_cms, Qout_cms, interval_err_m3] (the .mass columns
    0, 5, 6, 8, 10) for every data row with >= 11 fields; an empty (0, 5) array when the file is
    missing or short. _read_mass / _read_series / late_inflow all read through this so read_results
    parses the (often multi-thousand-row) file once instead of three times."""
    rows = []
    try:
        with open(path, encoding="utf-8") as f:
            next(f)
            for line in f:
                p = line.split()
                if len(p) >= 11:
                    rows.append((float(p[0]), float(p[5]), float(p[6]),
                                 float(p[8]), float(p[10])))
    except (OSError, StopIteration, ValueError):
        pass
    return np.asarray(rows, dtype=float).reshape(-1, 5)


MASS_TAIL_BYTES = 4096      # the end of a .mass file read each progress tick (audit P4)


def read_mass_tail(path, tail_bytes=MASS_TAIL_BYTES):
    """The last COMPLETE data row of a LISFLOOD-FP .mass file while it is still being written:
    {"time_s", "tstep_s", "min_tstep_s", "steps", "vol_m3", "qin_cms"} (the .mass columns 0, 1,
    2, 3, 5, 6), or None when the file is missing, has no complete row yet, or the row cannot
    be parsed. Only the last `tail_bytes` of the file are read (seek to max(0, size - tail)),
    so the progress poll of a long run is a few kilobytes a second instead of the whole file
    (audit P4, 2026-11-07: the run loop re-parsed every row of a multi-thousand-row file every
    second). A trailing line without its newline is a partial write and is ignored; so is the
    header. _read_mass_rows still parses the whole file once at the end of the run."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            start = max(0, size - int(tail_bytes))
            f.seek(start)
            chunk = f.read()
    except (OSError, ValueError):
        return None
    if not chunk:
        return None
    text = chunk.decode("utf-8", errors="replace")
    lines = text.split("\n")
    if start > 0:
        lines = lines[1:]          # the first piece is the tail of a row cut by the seek
    if not text.endswith("\n"):
        lines = lines[:-1]         # a row still being written
    for line in reversed(lines):
        p = line.split()
        if len(p) < 7:
            continue
        try:
            return {"time_s": float(p[0]), "tstep_s": float(p[1]), "min_tstep_s": float(p[2]),
                    "steps": int(float(p[3])), "vol_m3": float(p[5]), "qin_cms": float(p[6])}
        except ValueError:
            continue               # the header, or a row the solver garbled
    return None


def _mass_summary(rows):
    """_read_mass's value from shared rows. The explicit loop reproduces the original exactly:
    sequential cum (left-to-right, not pairwise), qin_max/worst floored at 0.0 (so a negative Qin
    leaves qin_max == 0.0), last-row last_t/vol, all zeros for an empty file."""
    last_t = cum = vol = qin_max = worst = 0.0
    for t, v, qin, _qo, e in rows:
        last_t = float(t)
        vol = float(v)
        qin_max = max(qin_max, float(qin))
        e = float(e)
        cum += e
        worst = max(worst, abs(e))
    return last_t, cum, vol, qin_max, worst


def _series_cols(rows):
    """_read_series's value (time s, domain volume m3, inflow m3/s, outflow m3/s) from shared
    rows; shape-(0,) float64 arrays for an empty file, matching the old np.asarray([])."""
    return rows[:, 0], rows[:, 1], rows[:, 2], rows[:, 3]


def _read_mass(path):
    """Last time (s), cumulative volume error (m3, signed sum of the interval
    errors), last domain volume (m3), largest inflow (m3/s), largest single
    interval error (m3)."""
    return _mass_summary(_read_mass_rows(path))


def _read_series(path):
    """The mass file as arrays (time s, domain volume m3, inflow m3/s, outflow m3/s)."""
    return _series_cols(_read_mass_rows(path))


def _late_inflow(t, qi, t_end_s, tol_cms=0.5):
    """late_inflow's math on already-parsed series (so read_results needs no extra parse)."""
    if len(t) < 2:
        return 0.0, 0.0, 0.0
    late = t > t_end_s
    if not late.any():
        return 0.0, 0.0, 0.0
    tl, ql = t[late], qi[late]
    dt = np.diff(np.concatenate([[max(t_end_s, t[~late][-1] if (~late).any() else 0.0)], tl]))
    vol = float(np.sum(np.clip(ql - tol_cms, 0.0, None) * dt))
    k = int(np.argmax(ql))
    if ql[k] <= tol_cms:
        return 0.0, 0.0, 0.0
    return vol, float(ql[k]), float(tl[k])


def late_inflow(path, t_end_s, tol_cms=0.5):
    """Water that comes in through the domain edges after the hydrograph has ended, from
    the mass file. Returns (volume m3, largest inflow m3/s, time of the largest inflow s).
    Once the point source has stopped, the only inflow left is what a boundary adds, so
    anything above `tol_cms` is the boundary importing water (LISFLOOD-FP 8.1's FV1 free
    edge does this where water stands against it)."""
    t, _v, qi, _qo = _read_series(path)
    return _late_inflow(t, qi, t_end_s, tol_cms)


def _par_grid_size(par):
    """(cells, cell size m) of the DEM a .par file names (its ESRI ASCII header), or
    (None, None) when it cannot be read. Used for the automatic wall-clock limit and the
    collapsed-step threshold (audit P2); never for the hydraulics."""
    try:
        dem = None
        with open(par, encoding="utf-8", errors="replace") as f:
            for line in f:
                p = line.split()
                if len(p) >= 2 and p[0].lower() == "demfile":
                    dem = p[1]
                    break
        if not dem:
            return None, None
        path = dem if os.path.isabs(dem) else os.path.join(os.path.dirname(par), dem)
        hdr = {}
        with open(path, encoding="utf-8", errors="replace") as f:
            for _ in range(6):
                p = f.readline().split()
                if len(p) == 2:
                    hdr[p[0].lower()] = float(p[1])
        return int(hdr["ncols"]) * int(hdr["nrows"]), float(hdr["cellsize"])
    except (OSError, KeyError, ValueError):
        return None, None


def _run_builtin(workdir, par, st, progress=None, cancel=None, log=None):
    """Run core/builtin_lisflood.py in-process (the numpy local-inertial engine). Writes
    lisflood.log whose first line is the engine name, and surfaces the mass error and the
    donor-cell limiter's clipped volume so steep-terrain clipping cannot pass unnoticed."""
    from . import builtin_lisflood as bl
    t0 = time.time()
    lines = []

    def say(msg):
        lines.append(msg)
        if log:
            log(msg)

    def prog(t, sim):
        if progress:
            progress(min(100.0, 100.0 * t / sim), "built-in engine t = {:.2f} h".format(t / 3600.0))
    try:
        stats = bl.run_par(par, progress=prog, cancel=cancel, log=say)
    except bl.Cancelled:
        raise Cancelled("cancelled")
    except bl.EngineError as e:
        raise ModelError("built-in engine: {}".format(e))
    finally:
        with open(os.path.join(workdir, "lisflood.log"), "w", encoding="utf-8") as f:
            f.write(bl.NAME + "\n" + "\n".join(lines) + "\n")
    # surface the limiter loss: the donor-cell limiter keeps depth >= 0 (mass conserved) by
    # clipping outflow, which on steep terrain quietly removes conveyance; report it as a share
    # of what was injected so a reviewer sees when the screening result leaned on clipping.
    inj = float(stats.get("injected_m3") or 0.0)
    clip = float(stats.get("clipped_m3") or 0.0)
    if inj > 0 and clip > 0.01 * inj:
        say("Built-in engine: the stability limiter removed {:.3g} m3 ({:.1f}% of the {:.4g} m3 "
            "injected) on steep terrain; treat near-breach velocities and extent as "
            "screening-grade.".format(clip, 100.0 * clip / inj, inj))
    return time.time() - t0


def run(workdir, par, st, progress=None, cancel=None, log=None, name="run"):
    # Keep-awake runs beside the solver, not around it, so Cancel still reaches the model
    # and a missing systemd bus cannot stop the run.
    from ..damsuite.keepawake import keep_awake
    with keep_awake(engine_name(st.exe), log=log):
        return _run(workdir, par, st, progress, cancel, log, name)


def _run(workdir, par, st, progress=None, cancel=None, log=None, name="run"):
    exe = st.exe
    if is_builtin(exe):
        return _run_builtin(workdir, par, st, progress, cancel, log)
    if not exe or not os.path.isfile(exe):
        raise ModelError("LISFLOOD-FP executable not found: {}".format(exe))
    env = dict(os.environ)
    if st.threads:
        env["OMP_NUM_THREADS"] = str(int(st.threads))
    log_path = os.path.join(workdir, "lisflood.log")
    mass = os.path.join(workdir, "results", name + ".mass")
    # audit P2: the wall-clock limit of this run (from the setting, else the estimate, else 24 h)
    # and the time step under which the solver has collapsed
    ncells, cell_m = _par_grid_size(par)
    limit_s = wall_limit_s(getattr(st, "max_wall_s", 0.0), ncells, st.sim_time_s, cell_m,
                           getattr(st, "rate", None))
    st.wall_limit_used = limit_s
    collapse_s = dt_collapse_s(cell_m, getattr(st, "cfl", None)) if cell_m else None
    st.dt_collapsed = None
    if log:
        log("LISFLOOD-FP wall-clock limit for this run: {:.1f} h{}".format(
            limit_s / 3600.0, "" if getattr(st, "max_wall_s", 0.0) else " (automatic)"))
    from .wallclock import Deadline
    deadline = Deadline(limit_s, what="LISFLOOD-FP", log=log)
    t0 = time.time()
    last_dt_log = -1e9
    with open(log_path, "w", encoding="utf-8") as logf:
        try:
            proc = subprocess.Popen([exe, "-v", os.path.basename(par)], cwd=workdir, env=env,
                                    stdout=logf, stderr=subprocess.STDOUT, **NO_WINDOW)
        except OSError as e:
            if not _not_a_program(e):
                raise
            # 3.20.5: Windows says "[WinError 193] %1 is not a valid Win32 application" for a
            # file that is no program (a build for another system, a script, a download that
            # is a web page). Say which file and what is wrong with it.
            raise ModelError("LISFLOOD-FP could not be started: {} is not executable on this "
                             "computer (it is not a program this system can run: {})".format(
                                 exe, e))
        while proc.poll() is None:
            time.sleep(1.0)
            if cancel and cancel():
                proc.kill()
                proc.wait()
                raise Cancelled("cancelled")
            if deadline.expired():
                proc.kill()
                proc.wait()
                elapsed = deadline.elapsed()
                logf.write("\nStopped by ND Dam Breach Runner: wall-clock limit {:.0f} s "
                           "reached after {:.0f} s\n".format(limit_s, elapsed))
                raise ModelError(_wall_limit_text(limit_s, elapsed))
            row = read_mass_tail(mass) if (progress or log) else None   # the tail only (P4)
            if row is None:
                continue
            tt = row["time_s"]
            if progress:
                progress(min(100.0, 100.0 * tt / st.sim_time_s),
                         "LISFLOOD-FP t = {:.2f} h, dt {:.3g} s".format(tt / 3600.0,
                                                                       row["tstep_s"]))
            if log and time.time() - last_dt_log >= 60.0:     # the solver's own dt, once a minute
                last_dt_log = time.time()
                log("LISFLOOD-FP t = {:.2f} h: time step {:.4g} s (smallest so far {:.4g} s), "
                    "{:,} steps".format(tt / 3600.0, row["tstep_s"], row["min_tstep_s"],
                                        row["steps"]))
            if (collapse_s and st.dt_collapsed is None and row["tstep_s"] > 0
                    and row["tstep_s"] < collapse_s):
                st.dt_collapsed = (tt, row["tstep_s"])
                if log:
                    log("WARNING: LISFLOOD-FP's time step collapsed to {:.4g} s at t = {:.2f} h "
                        "(a {:g} m cell needs water over {:.0f} m deep for a step that short, "
                        "CFL {:g}): the solver is probably unstable and this run may hit its "
                        "{:.1f} h wall-clock limit. Check the volume error when it finishes; "
                        "the ACC stable settings (theta {:g}, CFL {:g}) or a coarser cell "
                        "usually cure it.".format(
                            row["tstep_s"], tt / 3600.0, cell_m, DT_COLLAPSE_DEPTH_M,
                            float(getattr(st, "cfl", None) or 0.7), limit_s / 3600.0,
                            STABLE_THETA, STABLE_CFL))
    with open(log_path, errors="replace", encoding="utf-8") as f:
        logtxt = f.read()
    if proc.returncode != 0:
        raise ModelError("LISFLOOD-FP stopped with code {}:\n{}".format(proc.returncode,
                                                                      logtxt[-1500:]))
    if patch_missing(logtxt):
        # 3.16.0: FV2 / MWDG2 are never run without the patch, whatever allow_unpatched says
        if getattr(st, "allow_unpatched", False) and \
                getattr(st, "solver", SOLVER_ACC) in (SOLVER_ACC, SOLVER_FV1):
            st.unpatched_used = True
            if log:
                log("WARNING: " + PATCH_MISSING_WARNING)
        else:
            raise ModelError(PATCH_MISSING_MSG)
    if log:
        log("LISFLOOD-FP finished in {:.1f} min".format((time.time() - t0) / 60.0))
    return time.time() - t0


def read_gauges(workdir, st, name="run"):
    """Discharge time series of the virtual gauges: {gauge name: (t_s, q_cms)}.
    The .discharge columns are in m3/s (checked against a steady inflow)."""
    path = os.path.join(workdir, "results", name + ".discharge")
    if not st.gauges or not os.path.exists(path):
        return {}
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            p = line.split()
            if len(p) == 1 + len(st.gauges):
                try:
                    rows.append([float(v) for v in p])
                except ValueError:
                    continue
    if not rows:
        return {}
    a = np.asarray(rows)
    out = {}
    for i, g in enumerate(st.gauges):
        out[g.get("name", "gauge{}".format(i + 1))] = (a[:, 0], np.clip(a[:, 1 + i], 0.0, None))
    return out


def _hydrograph_peak_cfs(hydrograph):
    """The breach inflow hydrograph's peak (cfs), or None when it has none."""
    try:
        q = float(hydrograph.peak_cfs)
    except Exception:
        return None
    return round(q, 0) if q > 0 else None


def read_results(workdir, dem, st, hydrograph, name="run"):
    res = os.path.join(workdir, "results")

    def load(ext, required=True):
        p = os.path.join(res, name + ext)
        if not os.path.exists(p):
            if required:
                raise ModelError("The flood model did not write {}".format(p))
            return None
        # audit P3 (3.16.10): the result files are read as float32. LISFLOOD-FP writes them
        # to 3 decimals (1 mm, 0.01 ft/s, 0.001 h) from its own single-precision arrays, so
        # float32 (7 significant digits: 0.0001 m at a 500 m WSE) loses nothing the file has
        # and halves the four full-domain arrays. Elevation grids stay float64 (grid.Grid).
        g = Grid.read_asc(p, dtype=RESULT_DTYPE)
        a = g.data
        a[a == RESULT_DTYPE(g.nodata)] = np.nan
        if a.shape != dem.data.shape:
            raise ModelError("{} has shape {} but the DEM is {}".format(p, a.shape,
                                                                        dem.data.shape))
        return a

    f32 = RESULT_DTYPE
    hmax = load(".max")
    vmax = load(".maxVc", required=False)
    vmax_written = vmax is not None
    arr = load(".inittm", required=False)
    wse = load(".mxe", required=False)
    notes = []
    hmax = np.where(np.isfinite(hmax), hmax, f32(0.0))
    # the thresholds are compared in float64 (as before 3.16.10) so a cell at exactly the
    # arrival depth is counted the same way the float64 read counted it
    hmax64 = hmax.astype(np.float64)
    wet = hmax64 >= st.arrival_depth_m - ARRIVAL_TOL_M
    if vmax is None:
        notes.append("The flood model wrote no maximum velocity (.maxVc).")
        vmax = np.full(hmax.shape, np.nan, dtype=f32)
    vmax = np.where(np.isfinite(vmax) & (hmax64 >= st.velocity_min_depth_m), vmax, f32(0.0))
    if arr is None:
        arr = np.full(hmax.shape, np.nan, dtype=f32)
    arr = np.where(np.isfinite(arr) & (arr >= 0) & wet, arr, f32(np.nan))
    if wse is None:
        # the DEM is float64: the sum is taken there and rounded once into float32
        wse = np.where(hmax > 0, dem.data + hmax64, np.nan).astype(f32)
    wse = np.where(hmax > 0, wse, f32(np.nan))
    mass_rows = _read_mass_rows(os.path.join(res, name + ".mass"))   # parse the .mass file once
    last_t, verr, vol, qin_max, worst = _mass_summary(mass_rows)
    injected = hydrograph.volume_until(last_t) or hydrograph.volume_m3
    injected += sum(source_volume(src, last_t) for src in st.extra_sources)
    stats = {
        "cells": int(dem.data.size), "cell_size_m": dem.dx,
        "sim_time_hr": round(last_t / 3600.0, 2),
        "max_depth_ft": round(float(np.nanmax(hmax64)) / FT, 2),
        "max_velocity_fps": round(float(np.nanmax(vmax)) / FT, 2),
        "flooded_acres": round(float((hmax64 >= st.arrival_depth_m).sum()) * dem.dx ** 2
                               / ACRE, 1),
        "peak_inflow_cfs": round(qin_max / CFS, 0),
        # 3.13.2 (DWR-4031 toe10m): peak_inflow_cfs is the largest Qin at the .mass output
        # samples (every massint s), which can miss a short breach peak (1,570 against
        # the hydrograph's 1,696 cfs); the report gives the hydrograph peak as the inflow
        "peak_inflow_input_cfs": _hydrograph_peak_cfs(hydrograph),
        "massint_s": float(st.massint_s),
        "max_volume_error_pct": round(100.0 * abs(verr) / injected, 3) if injected else None,
        "worst_interval_error_pct": round(100.0 * worst / injected, 3) if injected else None,
        "injected_acft": round(injected / ACFT, 2),
        "in_domain_acft": round(vol / ACFT, 2),
        "initial_water_acft": 0.0,
        "velocity_written": vmax_written,
        "solver": SOLVER_SHORT.get(getattr(st, "solver", SOLVER_ACC), getattr(st, "solver", "")),
        "acc_theta": getattr(st, "theta", 1.0) if st.solver == SOLVER_ACC else None,
        "acc_cfl": getattr(st, "cfl", None) if st.solver == SOLVER_ACC else None,
    }
    if st.initial_depth is not None:
        init_d = st.initial_depth.data
        stats["initial_water_acft"] = round(float(np.nansum(np.where(np.isfinite(init_d),
                                            init_d, 0.0))) * dem.dx ** 2 / ACFT, 2)
    # v3.11.0: what the model holds against what was put in. A free FV1 edge can import water
    # (it grew to 6.5e10 m3 against 9.7e6 m3 injected at Elm River Dam 1); the near-dam refine
    # checks these before it merges anything. Only meaningful without ambient sources.
    t_ser, v_ser, qi_ser, qo_ser = _series_cols(mass_rows)
    if injected and len(v_ser):
        stats["max_domain_volume_ratio"] = round(float(v_ser.max()) / injected, 3)
    # H08: water that left the model through its downstream edges (LISFLOOD-FP Qout). The
    # domain-extension loop only tests how DEEP the water is on each edge; a wide, shallow sheet
    # can carry a large share of the breach volume out while every edge stays under the depth
    # tolerance, so the flood area and consequences are understated. Reported here and flagged
    # in workflow.run_dam. Negative Qout is boundary inflow (late_boundary_inflow_*).
    if len(qo_ser) and len(t_ser) == len(qo_ser):
        trap = getattr(np, "trapezoid", None) or np.trapz
        out_vol = float(trap(np.clip(qo_ser, 0.0, None), t_ser))
        stats["boundary_outflow_acft"] = round(out_vol / ACFT, 2)
        if injected:
            stats["boundary_outflow_pct"] = round(100.0 * out_vol / injected, 2)
    if not st.extra_sources:
        lv, lq, lt = _late_inflow(t_ser, qi_ser, float(hydrograph.duration_s))
        stats["late_boundary_inflow_acft"] = round(lv / ACFT, 2)
        stats["late_boundary_inflow_max_cfs"] = round(lq / CFS, 1)
        stats["late_boundary_inflow_pct"] = round(100.0 * lv / injected, 2) if injected else None
        if injected and lv > 0.02 * injected:
            notes.append(
                "Water came IN through the domain edges after the breach hydrograph ended "
                "({:,.0f} ac-ft, {:.1f}% of the {:,.0f} ac-ft injected; largest inflow {:,.0f} "
                "cfs at {:.1f} h). The edge is bringing water into the model, so the volumes "
                "and depths are not reliable. Close that edge or use the ACC solver.".format(
                    lv / ACFT, 100.0 * lv / injected,
                    injected / ACFT, lq / CFS, lt / 3600.0))
    try:                    # v3.10.27 (#91): reporting only
        from . import report_metrics
        stats.update(report_metrics.pass_stats(dem, hmax, vmax))
    except Exception:       # noqa: BLE001
        pass
    if stats["max_volume_error_pct"] and stats["max_volume_error_pct"] > 1.0:
        notes.append("The flood model's mass-balance error is {}% of the injected volume (limit "
                     "1%); try a smaller cell size or initial time step.".format(
                         stats["max_volume_error_pct"]))
    return Envelopes(dem.like(hmax, nodata=np.nan, dtype=f32), dem.like(vmax, nodata=np.nan, dtype=f32),
                     dem.like(arr, nodata=np.nan, dtype=f32), dem.like(wse, nodata=np.nan, dtype=f32),
                     stats, notes, read_gauges(workdir, st, name))


def source_volume(src, t_end):
    """Volume (m3) a constant or time-varying extra source adds up to t_end."""
    t = np.asarray(src["t_s"], float)
    q = np.asarray(src["q_cms"], float)
    tt = np.unique(np.concatenate([t[t < t_end], [min(t_end, t[-1])]]))
    qq = np.interp(tt, t, q)
    return float(np.sum(0.5 * (qq[1:] + qq[:-1]) * np.diff(tt))) if len(tt) > 1 else 0.0


def snapshot_files(workdir, name="run"):
    """Depth snapshots LISFLOOD-FP wrote (saveint): [(index, path)] in time order.
    File k holds the depth at k x saveint seconds."""
    res = os.path.join(workdir, "results")
    out = []
    if not os.path.isdir(res):
        return out
    pre = name + "-"
    for fn in os.listdir(res):
        if fn.startswith(pre) and fn.endswith(".wd"):
            try:
                out.append((int(fn[len(pre):-3]), os.path.join(res, fn)))
            except ValueError:
                continue
    return sorted(out)


def collect_snapshots(workdir, st, out_path, name="run", step=1, remove=True):
    """Pack the depth snapshots into one compressed .npz (float32, metres, NaN
    dry) with their times and delete the text files. Returns (times_s, path) or
    (None, None) when there are none."""
    files = snapshot_files(workdir, name)
    if not files or not st.snapshot_s:
        return None, None
    arrays, times = [], []
    for k, (idx, p) in enumerate(files):
        if k % max(1, int(step)) == 0:
            g = Grid.read_asc(p)
            a = g.data.astype(np.float32)
            a[(a == g.nodata) | (a <= 0.0)] = np.nan
            arrays.append(a)
            times.append(idx * st.snapshot_s)
        if remove:
            for ext in (".wd", ".Vx", ".Vy", ".Qx", ".Qy", ".elev", ".Vc", ".Hz"):
                try:
                    os.remove(p[:-3] + ext)
                except OSError:
                    pass
    np.savez_compressed(out_path, times_s=np.asarray(times), depth=np.stack(arrays))
    return np.asarray(times), out_path


def check_lisflood(exe, workdir, solver=SOLVER_ACC):
    """Run a 2 km synthetic channel for 20 minutes. Returns a dict with 'ok',
    'patched' (arrival-depth keyword honoured), 'velocity' (a maximum velocity was written) and
    'message'. 3.16.0: `solver` checks that solver (workflow.run_dam does before an FV2 / MWDG2
    run)."""
    from .breach import Hydrograph
    os.makedirs(workdir, exist_ok=True)
    nr, nc, dx = 20, 100, 20.0
    X, Y = np.meshgrid((np.arange(nc) + 0.5) * dx, (np.arange(nr) + 0.5) * dx)
    z = 100.0 - 0.002 * X + 0.02 * np.abs(Y - nr * dx / 2)
    dem = Grid(z[::-1], 0.0, nr * dx, dx)
    hy = Hydrograph(np.array([0.0, 600.0, 1200.0]), np.array([30.0, 30.0, 0.0]), "test")
    st = LisfloodSettings(exe, arrival_depth_m=0.3, sim_time_s=1200.0, massint_s=60.0,
                          initial_tstep_s=1.0, solver=solver)
    try:
        par = write_inputs(workdir, dem, (30.0, nr * dx / 2 + 5), hy, st)
        run(workdir, par, st)
        env = read_results(workdir, dem, st, hy)
    except Exception as e:  # report, don't raise
        return {"ok": False, "patched": False, "velocity": False, "message": str(e)}
    p_init = os.path.join(workdir, "results", "run.inittm")
    if not os.path.exists(p_init):
        return {"ok": True, "patched": False, "velocity": bool(env.stats.get("velocity_written")),
                "message": "{} ran {} but wrote no arrival time.".format(
                    describe_exe(exe), SOLVER_SHORT.get(solver, solver))}
    raw = Grid.read_asc(p_init).data
    raw_wet = int((raw >= 0).sum())
    thr_wet = int(np.isfinite(env.arrival_hr.data).sum())
    # Patched: arrival is recorded only where the depth passed 0.3 m. Unpatched:
    # it is recorded wherever there was any water, which is far more cells.
    deep = int((env.depth.data >= 0.3 - ARRIVAL_TOL_M).sum())
    patched = raw_wet <= 1.03 * deep + 3
    who = "The built-in engine" if is_builtin(exe) else "LISFLOOD-FP"
    msg = ("{} ran: {} cells reached 0.3 m.".format(who, thr_wet))
    if is_builtin(exe):
        msg += (" This is the built-in numpy screening engine (local inertial), NOT LISFLOOD-FP; "
                "its results are preliminary estimates.")
    if not patched and not is_builtin(exe):
        msg += (" The arrival-depth patch is NOT active (arrival time will be the first "
                "wetting, about 1 mm). Rebuild with scripts/build_lisflood_linux.sh on Linux or "
                "scripts/build_lisflood_macos.sh on macOS (both apply scripts/patch_lisflood.py).")
    return {"ok": True, "patched": patched, "velocity": bool(env.stats.get("velocity_written")),
            "message": msg}


RATES = {}       # audit P2: calibrate() rates of this session, by executable path


def known_rate(exe):
    """The calibrate() rate measured in this session for `exe` (cell-seconds per wall second),
    or None. workflow.run_dam uses it for the automatic wall-clock limit when the run settings
    carry no lisflood_rate (the Estimate tool or the wizard ran the test flood)."""
    if not exe:
        return None
    return RATES.get(os.path.abspath(str(exe)))


def calibrate(exe, workdir, threads=0, cell_m=10.0, sim_s=1800.0):
    """Time a synthetic breach flood to measure this computer's LISFLOOD-FP
    speed. Returns cell-seconds of simulation per wall second at `cell_m`
    (steps scale with 1/cell size), or None when it could not run."""
    import time
    from .breach import Hydrograph
    os.makedirs(workdir, exist_ok=True)
    nr, nc = 160, 400
    X, Y = np.meshgrid((np.arange(nc) + 0.5) * cell_m, (np.arange(nr) + 0.5) * cell_m)
    z = 100.0 - 0.001 * X + 0.004 * np.abs(Y - nr * cell_m / 2)
    dem = Grid(z[::-1], 0.0, nr * cell_m, cell_m)
    hy = Hydrograph(np.array([0.0, 600.0, sim_s]), np.array([300.0, 300.0, 0.0]), "calibration")
    st = LisfloodSettings(exe, threads=threads, arrival_depth_m=0.3, sim_time_s=sim_s,
                          massint_s=300.0)
    try:
        par = write_inputs(workdir, dem, (40.0, nr * cell_m / 2 + 5), hy, st)
        t0 = time.time()
        run(workdir, par, st)
        dt = time.time() - t0
    except Exception:
        return None
    rate = nr * nc * sim_s / max(dt, 1e-3)
    if exe:
        RATES[os.path.abspath(str(exe))] = rate
    return rate
