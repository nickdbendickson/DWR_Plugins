# Boann — GPU flood engine (Metal)

**Boann** (the Irish river goddess) is the suite's own flood engine: two solvers as GPU compute
kernels for the Apple GPU, on one shared Metal/CPU machinery. It is not LISFLOOD-FP — reports, the
run manifest and the classification record name it and say so.

The two solvers mirror LISFLOOD-FP's own pair:

1. **ACC** (`engine.py`, `kernels.metal`) — LISFLOOD-FP's local-inertial ("acceleration") solver.
   LISFLOOD-FP runs on a GPU only through NVIDIA CUDA, so on a Mac it runs on the CPU; this runs
   the same scheme on the Mac's own GPU.
2. **Shock-capturing FV** (`engine_fv.py`, `kernels_fv.metal`, Runner 3.23.0) — a well-balanced,
   positivity-preserving **HLLC finite-volume** solver of the full 2-D shallow-water equations.
   It resolves hydraulic jumps and dam-break fronts the ACC scheme diffuses, at the cost of a
   shorter time step (CFL on `|u| + √(gh)`, not `√(gh)` alone).

It reads the LISFLOOD-FP input deck the Runner already writes (`.par`, `.dem.asc`, `.bci`,
`.bdy`, optional `.n.asc`, `.start`, `.gauge`) and writes the result files the Runner already
reads (`.max`, `.maxVc`, `.inittm`, `.maxtm`, `.mxe`, `.mass`, `.discharge`, `-NNNN.wd`), so it
plugs into the Runner as an engine of its own.

**Choosing it (Runner 3.22.0, Screening 0.21.0):** the Runner's run tools have "Flood engine"
(LISFLOOD-FP, GPU engine, or — Runner 3.23.0 — GPU engine, shock-capturing), and the words `gpu`
(ACC) and `shock` / `gpu-fv` (the FV scheme) work wherever a LISFLOOD-FP executable is asked for
(also CLI `model.lisflood_exe`). Screening tool 7 has engine "GPU engine". On a Mac with a
Metal GPU it runs there; elsewhere on the CPU build of the same kernels (needs a C++ compiler).
`DAMSAFETY_MF_BACKEND=metal|cpu` forces one. Screening holds a copy of this folder's engine files
(`DamSafety_Screening/core/metalflood`, checked byte for byte by its tests).

Both are GPU engines, not LISFLOOD-FP: reports, the run manifest and the classification record
name them and say so. Confirm a final hazard classification with a LISFLOOD-FP run.

## Files

| File | What it is |
|---|---|
| `kernels.metal` | The ACC scheme, as seven Metal compute kernels (one thread per cell or face). |
| `kernels_fv.metal` | The shock-capturing HLLC finite-volume scheme, as four Metal kernels. |
| `mf_shim.h`, `cpu_kernels.cpp`, `cpu_kernels_fv.cpp` | The CPU build: the same `*.metal` kernels compiled as C++ (one core). |
| `backends.py` | `MetalBackend` / `CpuBackend`, parameterised by a `Spec` (`FLOOD_SPEC` or `FV_SPEC`): one Metal/`ctypes` and one C++ build path, shared by both schemes. |
| `engine.py` | ACC driver: reads the deck, runs batches of steps, writes the results. `run_par()` is the same call as `builtin_lisflood.run_par()`. |
| `engine_fv.py` | FV driver: the same deck and result files, the HLLC scheme. `run_par()` is the same call. |
| `cases.py` | Synthetic valley decks for the tests and the speed test. |
| `bench.py` | The speed test (GPU, CPU build, LISFLOOD-FP if found), with `--profile`. |
| `cli.py`, `boann.py`, `version.py` | The standalone command line, its launcher, and Boann's own version number. |

Nothing to install on a Mac: Metal compiles the kernel source at run time, and the framework is
called with `ctypes` (no PyObjC, no Xcode). It needs a Mac with a Metal GPU (Apple silicon; no
float atomics are used, so the paravirtual GPU of a macOS virtual machine works too). The CPU build needs a C++ compiler (`c++`, `clang++` or
`g++`); it is compiled once into `~/Library/Caches/DamSafety/metalflood` (macOS) or
`~/.cache/DamSafety/metalflood`.

## Run it

**As a program of its own (Runner 3.62.0, Boann 1.0.0)** - no QGIS, only Python 3 and numpy:

```
python3 boann.py run DECK.par [--solver acc|sgc|fv|fv2|dg2|subfv|subli] [--backend auto|metal|cpu]
                              [--no-limiter] [--progress] [--quiet] [--stats-json stats.json]
                              [--profile]
python3 boann.py check        # the device a run would use here
python3 boann.py solvers      # the solvers and the Runner engine words that pick each
python3 boann.py version      # Boann's version (version.py) and each solver's
```

`boann.py` loads this folder as a package named `boann`; nothing here imports from outside it
(`cli.py`, `tests/test_boann_cli.py`), except that the `sgc` solver builds channels from the DEM
with the host plugin's `core/sgc` when it is there. Standalone it needs the deck's SGC channel
files and refuses without them. Boann's version is recorded in the Runner's `run_manifest.json`
as `software.boann`.

From inside a plugin (and the older ACC-only form):

```
<python with numpy> -m nd_breach_runner.core.metalflood run.par [-backend metal|cpu] [-v]
<python with numpy> -m nd_breach_runner.core.metalflood.engine_fv run.par [-backend metal|cpu] [-v]
<python with numpy> -m nd_breach_runner.core.metalflood.bench [--size small|medium|large]
```

(`nd_breach_runner` is the repository's test alias for `DamSafety_Runner`; inside QGIS it is
`DamSafety_Runner.core.metalflood`.)

## Where the time goes (Runner 3.80.0, Boann 5.2.0)

`--profile` on `boann.py run` and on the speed test (or `DAMSAFETY_MF_PROFILE=1` for a run from
the plugin) reports where a run's time went. It changes no result: every result file is byte
for byte the same with it on (`tests/test_boann_profile.py`, on the CPU build and on the GPU).

```
python3 boann.py run DECK.par --profile --stats-json stats.json
python3 -m nd_breach_runner.core.metalflood.bench --size medium --case diagonal --profile
```

| Number | What it is | What it decides |
|---|---|---|
| Python encoding, s and us a step | Wall time building and committing each batch's command buffer. The GPU is idle for it. | Whether to overlap encoding with GPU work, or encode less (fewer dispatches a step). |
| GPU's own time, us a step | `GPUEndTime - GPUStartTime` of each batch's command buffer. | The base every GPU-side change is measured against. |
| GPU time by kernel | The first 2 steps of each batch go one kernel per command buffer (Apple GPUs sample counters at pass boundaries only). Each buffer costs the GPU a fixed time whatever it holds (on the CI Mac, more than a whole batched step), so read the **net** columns: the time above the cheapest one-thread kernel's. | Which kernels a fusion would be worth. |
| The window's share of the grid | The wet bounding box plus its margin, over the grid. | |
| Wet share of the window | Cells holding water, of the cells the kernels run. | How much any dry skip could save at best. |
| Tiles 16 / 32 / 64 | Cells in the tiles that hold water or touch a tile that does. | What a dry-tile skip on the GPU would still run, by tile size. |
| Row ranges | Cells in each row's column range under the CPU dry-cell skip's rule (an estimate from the water alone: 14.6% against the 13.5% the real skip ran on the speed test's small diagonal valley). | What the CPU dry-cell skip would still run, for the solvers that do not have it. |
| Dry-cell skip ran (CPU build, ACC) | The cell updates the kernels really ran, over the window's. | What the skip of Runner 3.76.0 saves on this deck. |

The host's times are kept on every run (`stats["profile"]`, mode `basic`); the GPU's times and the
wet shares only with the profile on (mode `full`), since they cost a little: the split steps add
command buffers, and the wet look reads the window (it is skipped while it would cost more than
2% of the run). The speed test's `--case diagonal` (a corner-to-corner valley: the window is
mostly dry ground) and `--case compact` (a breach on a closed plain: the window is mostly water)
are the two ends.

`DAMSAFETY_MF_TG=WxH` sets the Metal threadgroup size of every kernel in place of the default
(the execution width by up to 8 rows), for A/B speed tests. The kernels use no threadgroup
memory, so the grids do not change; the outflow and clipped totals are summed per SIMD group
before rounding, so the `.mass` file's totals can move by a rounding unit.

## The scheme, and how it follows LISFLOOD-FP 8.3

Per step: `dt = min(initial_tstep, cfl dx / sqrt(g h_max))`; every face
`q = (θ q + (1-θ)/2 (q_up + q_down) - g h_f dt dS) / (1 + g dt n² |q_vec| / h_f^(7/3))` with
`h_f = max(η) - max(z)` (no flow at 1 mm or less, `h_f` capped at 10 m); FREE edges as
LISFLOOD-FP's with no slope given; continuity; then the running maxima. This is the published
local-inertial (ACC) scheme - Bates, Horritt & Fewtrell (2010), *J. Hydrol.* 387:33-45, with the
theta-weighted flux of de Almeida et al. (2012), *Water Resour. Res.* 48:W05528 - here read directly
from LISFLOOD-FP 8.3's source (`fp_acc.cpp`, `iterateq.cpp`, `boundary.cpp`, `util.cpp`) so the
kernels reproduce 8.3's exact behaviour. Where the numpy built-in engine
(`core/builtin_lisflood.py`) differs:

- **2-D friction** (`|q_vec|` from the face's discharge and the mean of the four crossing faces),
  LISFLOOD-FP's default since version 8; `1Dfriction` in the `.par` turns it off.
- **`theta` and `cfl`** from the `.par` (the Runner's damped rerun writes `theta 0.7`, `cfl 0.5`),
  with LISFLOOD-FP's fallback to the semi-implicit value where the centred one flows uphill.
- **Velocity** as LISFLOOD-FP records it for `.maxVc`: each face's new discharge over the flow
  depth it was computed with, the cell's value from its largest x and y face speeds.
- **Maxima and arrival** only for water deeper than 1 mm and from the first step on (a
  start-depth pond's initial depth is not a maximum).
- **Inflow** taken at the start of each step, the last value held after the series ends.
- Steps are never cut to land on an output time; outputs are written when the time passes them.

Kept from the built-in engine: the donor-cell limiter (a cell never sends more water than it
holds, so no water is created by clipping negative depths; LISFLOOD-FP clips instead), and
FREE applies to a whole edge (the Runner always writes whole edges).

Arithmetic is float32 (Apple GPUs have no float64). Bed elevations are stored relative to the
lowest cell and the water-surface slope is formed as `(z_r - z_l) + (h_r - h_l)`, so depths keep
about 1e-5 m of resolution; volumes are summed in float64 on the host.

The kernels run only on the wet part of the grid plus a margin wider than one batch of steps
(the front moves at most one cell a step); the host looks in after each batch (up to 64 steps)
for output times, the wet box and progress. A window as big as the grid gives the same numbers
bit for bit (test).

## Checks (cloud, CPU build of the kernels, 2026-11-18)

| Case | vs LISFLOOD-FP 8.3 (same deck) | Built-in numpy engine, same deck |
|---|---|---|
| Test valley, 3 h | depth MAE 4 mm, CSI 1.00 at 0.1 m, max velocity 6.03 vs 6.02 m/s, arrival MAE 0.001 h | depth MAE 78 mm, CSI 0.95, max velocity 87 m/s (spike) |
| Same, `theta 0.7`, `cfl 0.5` | depth MAE 9 mm, CSI 1.00 | depth MAE 266 mm, CSI 0.83 (ignores theta and cfl) |
| DWR-2801, 30 m, 24 h | CSI 1.000, depth MAE 0.001 ft, P99 velocity equal, arrival MAE 0.001 h | CSI 0.92, MAE 0.080 ft, max velocity 247 ft/s |
| Lake Delhi 2010, whole Runner, 30 m, 48 h | 8 USGS high-water marks: mean +2.06 ft, largest +3.69 ft, as LISFLOOD-FP; max depths within 0.003 ft | |

The breach inflow cell matches LISFLOOD-FP by default (2801 at peak: 3.32 m, the same to the mm).
LISFLOOD-FP's DryCheck caps a cell's outflow at the water it held at the start of the step and adds
the point source after, so the breach cell empties and refills every step: its depth is Q dt / dx^2,
which with the CFL step is (cfl Q / (dx sqrt(g)))^(2/3) and changes with cfl (3.32 m at 0.7, 2.65 m
at 0.5). This engine follows that order unless `physicalinflow` is set in the `.par`: then k_scale
counts the step's inflow as available water (the `inflow_lf_order` Param gates the `src[i] * dt`
term in `avail`), the cell settles where outflow balances inflow (2.09 m here) the same at any cfl -
a mass-conserving order that differs from LISFLOOD-FP at the breach cell and its next cells by up to
0.2 m, under 4 mm beyond about 300 m.

## The sub-grid local-inertial scheme (`gpu-subli`, Runner 3.79.0, Boann 5.3.0)

The ACC scheme run on **coarse cells that keep the DEM's detail**. The deck's DEM is the fine grid;
`subgrid <r>` in the `.par` (1 to 8, default 4) makes each solver cell r x r DEM cells. A coarse
cell holds one water level and knows, from its own DEM cells, how much water that level stores and
how much of each of its four edges is under water. The step is r times longer and there are r x r
fewer cells, and every result raster is still written at the DEM cell. It follows Nithila Devi and
Kuiry (2024, *Water Resources Research* 60, e2023WR035334), with two departures made on purpose:

- **Exact stage curves.** The paper fits a polynomial per cell (level against volume, face area
  against level). Here both come from the cell's sorted DEM cells, so level is the exact inverse of
  volume and there is no fit to check.
- **Compound-channel friction.** The paper applies Manning to one mean depth across the whole face,
  which spreads a channel narrower than the cell over the full cell width and runs it
  (cell width / channel width)^0.4 too deep in bank (1.74 times for a channel a quarter of the
  cell). Here the face's conveyance is the sum over its sub-segments, `K = sum w h^(5/3) / n`, and
  an in-bank channel runs at the Manning normal depth (`subli_narrow_channel_runs_at_manning_normal_depth`).

| File | What it is |
|---|---|
| `subgrid.py` | Builds the per-cell and per-face tables from the fine DEM (shared with sub-grid FV). |
| `subli_ref.py` | The float64 reference: states the scheme, the paper's equations and the departures. |
| `kernels_subli.metal`, `cpu_kernels_subli.cpp` | The kernels (seven per step, 28 buffers) and their CPU build. |
| `subli_backend.py` | The kernel's `Params`, buffer layout and dispatch plan (Runner only, not in Screening). |
| `engine_subli.py` | The driver: reads an ordinary deck, runs on the wet window, writes fine rasters. |

**Deck keywords** beyond the ACC engine's: `subgrid <r>`; `upwind` turns on the paper's flux
weighting (its Eq. 7 and 8, adaptive theta after Sridharan et al. 2020). `cfl`, `1Dfriction`,
`physicalinflow` work as for ACC. `theta` is ignored. In the Runner: Flood engine "Advanced:
Boann, sub-grid local inertial", the engine word `gpu-subli`, `SUBGRID_R` / `model.subgrid_r`, and
`model.subgrid_upwind` for the weighting.

**The flux weighting is off by default** (Nick, 2026-11-23, on these numbers). It blends each
face's discharge with the face upwind along the grid axis. On a reach that crosses the grid at an
angle that neighbour lies partly outside the channel, and the blend takes momentum out of the flow.
Two more things it needed before it ran a breach deck at all, both in the code: an open edge takes
the same blend with the next face inward (otherwise a reach ponds level behind the edge and
empties in surges), and the faces of a cell a point source is feeding are not weighted (otherwise
the source cell mounds by metres).

**Checks (cloud, CPU build, 2026-11-23).** Maximum depth against the 5 m plain ACC run of the same
meandering test valley (0.9 km, slope 0.004, 250 m3/s peak), where that run is over 0.1 m:

| Run | Mean difference | Median size | Steps (5 m run: 3,549) |
|---|---|---|---|
| sub-grid, 10 m cells (r = 2) | -0.004 m | 0.009 m | 1,495 |
| sub-grid, 20 m cells (r = 4) | -0.014 m | 0.024 m | 713 |
| sub-grid, 40 m cells (r = 8) | -0.022 m | 0.057 m | 357 |
| sub-grid, 20 m cells, `upwind` | +0.227 m | 0.231 m | 740 |
| plain ACC on a 20 m DEM | +0.223 m | 0.232 m | |

- r = 1 gives the plain ACC engine's flood on a whole deck (same steps, maximum depths within
  1 mm), and the kernel on flat cells is the shipped ACC kernel to 1e-4 m.
- The kernel matches the float64 reference to 5e-7 m over a dam break with a source and an open
  edge on terrain 1,400 m up, with and without the weighting; a lake at rest stays at rest; a
  closed basin keeps its volume at the full CFL step with nothing clipped.
- The wet window changes no number; the multi-core CPU build equals one core bit for bit.
- Lake Delhi 2010 (the validation gate's 30 m case, 8 surveyed high-water marks below the dam,
  cloud CPU build on 2 cores; model minus surveyed, feet):

  | Run | Mean | Mean size | Largest | Steps | Whole run |
  |---|---|---|---|---|---|
  | Boann ACC, 30 m cells | +1.24 | 1.28 | 2.67 | | 36 min |
  | sub-grid, 60 m cells (r = 2) | +1.13 | 1.19 | 2.55 | 34,625 | 4.3 min |
  | sub-grid, 120 m cells (r = 4, the gate's run) | +0.76 | 0.94 | 2.25 | 17,733 | 2.0 min |
  | sub-grid, 240 m cells (r = 8) | +0.39 | 0.82 | 1.56 | 17,280 | 0.7 min |
  | sub-grid, 120 m cells, `upwind` | +1.88 | 1.88 | 3.69 | | 2.0 min |
  | sub-grid FV (`gpu-subfv`), 120 m cells | +1.57 | 1.61 | 4.31 | | 2.8 min |

  Read it with care. The coarse runs sit LOWER than the 30 m run as the cells grow (0.11, 0.48,
  0.85 ft lower), and the 30 m run reads high against the survey, so the coarser runs land closer
  to the marks. That is the coarse cells drifting from the fine answer in a helpful direction on
  this reach, not the method getting more accurate with bigger cells: r = 2 is the honest "same
  flood, 8 times sooner" case, r = 4 trades half a foot for another factor of two. Volume error
  5 to 10 m3 of 1.11e8 m3 at every r. The weighting reads 1.1 ft higher than without it.
- Lake Delhi 2010 at 10 m, the DEM's own cell, on an Apple M2 Pro GPU (Nick's MacBook, 2026-11-23;
  whole 48 h event, 4.6 million DEM cells). The reference is plain ACC at 10 m with `cfl 0.5`;
  feet, coarse minus reference over every cell wet in either run:

  | Run | Solve | Marks, mean / mean size | Median cell | 90% within | 99% within | Same flooded area |
  |---|---|---|---|---|---|---|
  | plain ACC, 10 m cells, `cfl 0.5` | 9.5 min | +0.34 / 0.95 | | | | |
  | sub-grid, 20 m cells (r = 2) | 5.0 min | +0.30 / 0.95 | 0.036 | 0.085 | 0.200 | 99.8 % |
  | sub-grid, 40 m cells (r = 4) | 2.5 min | +0.08 / 0.97 | 0.157 | 0.410 | 0.823 | 97.5 % |
  | plain ACC, 10 m cells, default `cfl` 0.7 | 18.1 min | +8.11 / 10.61 | 3.022 | 20.9 | 43.2 | 84.9 % |

  So on a real dam break the coarse run gives the fine run's flood: 1 cm in the median cell at
  r = 2 in half the time, 5 cm at r = 4 in a quarter. The Metal kernels equal their CPU build on
  that GPU to the last printed digit (largest depth difference 0.00000 m on the 30 m deck).
  The last row is NOT this engine: plain ACC at LISFLOOD-FP's default Courant number goes unstable
  at the breach peak on this 10 m deck (a checkerboard peak surface, 126 ft of water at its
  deepest, 8 ft high at the marks) and keeps its volume while it does, so the Runner's re-run of a
  run that lost its balance does not fire. `cfl 0.5` alone steadies it and `cfl 0.35` gives the
  same flood. The sub-grid runs above use the default `cfl` and are smooth.

**What a fine cell is given.** Depth and water surface: the coarse cell's highest level less the
DEM cell's own ground, for water over 1 mm. Arrival time, time of maximum and maximum velocity:
those of the coarse cell the DEM cell lies in, where the DEM cell got wet. So arrival is a little
early for DEM cells high in a coarse cell (1.6 min early at r = 2, 4.3 at r = 4, 7.4 at r = 8 on
the test valley) and the velocity is the coarse cell's fastest face (discharge over the WET face
area), which is high for DEM cells at the water's edge (+0.07, +0.22 and +0.42 m/s on average
against 1.69 m/s; the 99th percentile agrees within 2 %).

**Not done.** The Apple-GPU build has run only in the macOS CI job, not on a real dam; there is no
3 m or 1 m real-dam run yet (the gain is there: a 10 m or 30 m DEM has already averaged the
channel away). One level per coarse cell cannot hold a leveed or perched channel apart from the
land beside it inside the same cell. No per-sub-segment velocity. Not in Screening.

## Not done (phase 2 and later)

- Speed: one command buffer per batch, seven dispatches per step encoded from Python; an
  indirect command buffer or kernel fusion would cut the host cost on small grids.
- The CPU build is one core and slower than LISFLOOD-FP on one core; it exists to test the
  kernels where there is no Apple GPU, not to replace LISFLOOD-FP.
- Shock-capturing finite-volume scheme for steep dam-break fronts.

## The shock-capturing FV scheme

The full 2-D shallow-water equations in conserved form (`h`, `hu`, `hv`), first order in space,
forward Euler in time:

- **Audusse et al. (2004) hydrostatic reconstruction** at each face
  (`h*_L = max(0, η_L − z_face)`, `h*_R = max(0, η_R − z_face)`, `z_face = max(z_L, z_R)`): this is
  what keeps a lake at rest and keeps depths non-negative across a wet/dry front.
- **HLLC** approximate Riemann flux (Toro) with Einfeldt-style wave speeds; the tangential
  momentum rides the contact (HLL would smear the shear).
- The **Audusse bed source** `½ g (h² − h*²)` added per face so still water stays still.
- Semi-implicit Manning friction (unconditionally stable); a velocity desingularisation floor far
  finer than the 1 mm output "dry", so a dam-break film's momentum is not thrown away at the front.
- `dt = min(initial_tstep, cfl · dx / max_cell(|u|+c, |v|+c))`, `cfl` capped at 0.45 (the 2-D
  explicit stability limit), computed on the GPU every step. FREE edges are transmissive; closed
  edges are reflective walls.

Same float32 care as the ACC scheme (bed relative to the lowest cell; host volumes in float64),
same wet-window batching, same fixed-point atomic reductions.

**Validation (CPU build of the kernels, 2026-11-18):**

| Case | Result |
|---|---|
| Lake at rest, 20×20 bumpy bed, closed, 200 s | surface drift 7×10⁻⁷ m, spurious velocity 2×10⁻⁶ m/s (float32 round-off): well balanced |
| Ritter dry-bed dam break, 801 cells, 8 s | matches a textbook float64 HLLC bit-close; depth at the dam 0.458 m (Ritter 4h₀/9 = 0.444); front a few cells behind the exact h=0 front, as any first-order scheme |
| Closed valley with inflow | mass error under 10⁻³ of the volume injected |

## Not done (phase 2 and later)

- Speed: one command buffer per batch, dispatches per step encoded from Python; an indirect
  command buffer or kernel fusion would cut the host cost on small grids.
- The CPU build is one core and slower than LISFLOOD-FP on one core; it exists to test the
  kernels where there is no Apple GPU, not to replace LISFLOOD-FP.
- FV: second-order (MUSCL) reconstruction and an SSP-RK2 step; a head-to-head of the FV scheme
  against LISFLOOD-FP's own FV1 solver on a steep ND dam-break.
