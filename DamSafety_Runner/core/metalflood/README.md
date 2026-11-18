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
| `backends.py` | `MetalBackend` / `CpuBackend`, parameterised by a `KernelSet` (`ACC` or `FV`): one Metal/`ctypes` and one C++ build path, shared by both schemes. |
| `engine.py` | ACC driver: reads the deck, runs batches of steps, writes the results. `run_par()` is the same call as `builtin_lisflood.run_par()`. |
| `engine_fv.py` | FV driver: the same deck and result files, the HLLC scheme. `run_par()` is the same call. |
| `cases.py` | Synthetic valley decks for the tests and the speed test. |
| `bench.py` | The speed test (GPU, CPU build, LISFLOOD-FP if found). |

Nothing to install on a Mac: Metal compiles the kernel source at run time, and the framework is
called with `ctypes` (no PyObjC, no Xcode). It needs a Mac with a Metal GPU (Apple silicon; no
float atomics are used, so the paravirtual GPU of a macOS virtual machine works too). The CPU build needs a C++ compiler (`c++`, `clang++` or
`g++`); it is compiled once into `~/Library/Caches/DamSafety/metalflood` (macOS) or
`~/.cache/DamSafety/metalflood`.

## Run it

```
<python with numpy> -m nd_breach_runner.core.metalflood run.par [-backend metal|cpu] [-v]
<python with numpy> -m nd_breach_runner.core.metalflood.engine_fv run.par [-backend metal|cpu] [-v]
<python with numpy> -m nd_breach_runner.core.metalflood.bench [--size small|medium|large]
```

(`nd_breach_runner` is the repository's test alias for `DamSafety_Runner`; inside QGIS it is
`DamSafety_Runner.core.metalflood`.)

## The scheme, and how it follows LISFLOOD-FP 8.3

Per step: `dt = min(initial_tstep, cfl dx / sqrt(g h_max))`; every face
`q = (θ q + (1-θ)/2 (q_up + q_down) - g h_f dt dS) / (1 + g dt n² |q_vec| / h_f^(7/3))` with
`h_f = max(η) - max(z)` (no flow at 1 mm or less, `h_f` capped at 10 m); FREE edges as
LISFLOOD-FP's with no slope given; continuity; then the running maxima. What it takes from
LISFLOOD-FP 8.3's source (`fp_acc.cpp`, `iterateq.cpp`, `boundary.cpp`, `util.cpp`), where the
numpy built-in engine (`core/builtin_lisflood.py`) differs:

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

The inflow cell itself can differ from LISFLOOD-FP by a few tenths of a metre at the peak of the
hydrograph (the neighbouring cells agree to mm); not yet explained.

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
