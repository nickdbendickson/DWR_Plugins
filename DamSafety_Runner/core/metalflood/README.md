# GPU flood engine (Metal)

LISFLOOD-FP's local-inertial ("acceleration", ACC) solver rewritten as GPU compute kernels for
the Apple GPU. LISFLOOD-FP runs on a GPU only through NVIDIA CUDA, so on a Mac it runs on the
CPU; this engine runs the same scheme on the Mac's own GPU.

It reads the LISFLOOD-FP input deck the Runner already writes (`.par`, `.dem.asc`, `.bci`,
`.bdy`, optional `.n.asc`, `.start`, `.gauge`) and writes the result files the Runner already
reads (`.max`, `.maxVc`, `.inittm`, `.maxtm`, `.mxe`, `.mass`, `.discharge`, `-NNNN.wd`), so it
plugs into the Runner as an engine of its own.

**Choosing it (Runner 3.22.0, Screening 0.21.0):** the Runner's run tools have "Flood engine"
(LISFLOOD-FP or GPU engine), and the word `gpu` works wherever a LISFLOOD-FP executable is asked
for (also CLI `model.lisflood_exe`). Screening tool 7 has engine "GPU engine". On a Mac with a
Metal GPU it runs there; elsewhere on the CPU build of the same kernels (needs a C++ compiler).
`DAMSAFETY_MF_BACKEND=metal|cpu` forces one. Screening holds a copy of this folder's engine files
(`DamSafety_Screening/core/metalflood`, checked byte for byte by its tests).

## Files

| File | What it is |
|---|---|
| `kernels.metal` | The scheme, as seven Metal compute kernels (one thread per cell or face). |
| `mf_shim.h`, `cpu_kernels.cpp` | The CPU build: the same `kernels.metal` compiled as C++ (one core). |
| `backends.py` | `MetalBackend` (the GPU through the Metal framework, called with `ctypes`) and `CpuBackend`. |
| `engine.py` | Reads the deck, runs batches of steps, writes the results. `run_par()` is the same call as `builtin_lisflood.run_par()`. |
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
<python with numpy> -m nd_breach_runner.core.metalflood.bench [--size small|medium|large]
```

(`nd_breach_runner` is the repository's test alias for `DamSafety_Runner`; inside QGIS it is
`DamSafety_Runner.core.metalflood`.)

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

## Not done (phase 2 and later)

- Speed: one command buffer per batch, seven dispatches per step encoded from Python; an
  indirect command buffer or kernel fusion would cut the host cost on small grids.
- The CPU build is one core and slower than LISFLOOD-FP on one core; it exists to test the
  kernels where there is no Apple GPU, not to replace LISFLOOD-FP.
- Shock-capturing finite-volume scheme for steep dam-break fronts.
