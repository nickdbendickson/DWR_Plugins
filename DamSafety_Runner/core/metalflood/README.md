# GPU flood engine (Metal) — phase 1 prototype

LISFLOOD-FP's local-inertial ("acceleration", ACC) solver rewritten as GPU compute kernels for
the Apple GPU. LISFLOOD-FP runs on a GPU only through NVIDIA CUDA, so on a Mac it runs on the
CPU; this engine runs the same scheme on the Mac's own GPU.

It reads the LISFLOOD-FP input deck the Runner already writes (`.par`, `.dem.asc`, `.bci`,
`.bdy`, optional `.n.asc`, `.start`, `.gauge`) and writes the result files the Runner already
reads (`.max`, `.maxVc`, `.inittm`, `.maxtm`, `.mxe`, `.mass`, `.discharge`, `-NNNN.wd`), so
plugging it into the Runner (phase 2) is a third engine choice, not a new pipeline. It is not
wired into the Runner yet.

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
| DWR-2801, 30 m, 24 h | see the pull request | |

The inflow cell itself can differ from LISFLOOD-FP by a few tenths of a metre at the peak of the
hydrograph (the neighbouring cells agree to mm); not yet explained.

## Not done (phase 2 and later)

- Runner integration: a third engine choice next to LISFLOOD-FP and ANUGA (the word `metal`
  like `builtin`, `core/lisflood.py`).
- Speed: one command buffer per batch, seven dispatches per step encoded from Python; an
  indirect command buffer or kernel fusion would cut the host cost on small grids.
- The CPU build is one core and slower than LISFLOOD-FP on one core; it exists to test the
  kernels where there is no Apple GPU, not to replace LISFLOOD-FP.
- Shock-capturing finite-volume scheme for steep dam-break fronts.
