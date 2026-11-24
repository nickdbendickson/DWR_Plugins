# core/metalterrain — BoannGrid terrain conditioner

The depression-fill is **Priority-Flood+ε** (Barnes, Lehman and Mulla 2014), one pass, in C++,
on one CPU core. That is the default for `fill_dem` on every platform, including a Mac with an
Apple GPU: a priority queue does not get faster on the GPU. On a 3.5 M-cell 10 m DEM the old
relaxation took about 205 s and this takes under a second. It copies the relaxation's float32
arithmetic (the same datum-shifted float32 ground, `w = max(z, float32(lowest neighbour + ε))`),
so it lands on the relaxation's own fixed point: within a float32 step or two on every cell (the
relaxation's 1e-6 m dead band), with the same census, flow network and carves downstream. It is
deliberately NOT float64: a float64 surface (the arithmetic of `core/terrain.priority_flood`, the
oracle) rounds differently when the flow step takes it back to float32, makes flats the
relaxation does not have and moves flow directions and carve IDs.

```python
from nd_breach_runner.core.metalterrain import fill
filled, info = fill.fill_dem(dem, eps=1e-5)   # priority-flood; NaN / -9999 = NoData
```

The previous fill, a Planchon & Darboux relaxation written as a Metal kernel (and the same
source built as C++), is still there. Pass `method="relax"` (or `--fill-method relax`). It runs
in float32 like the priority-flood and is the cross-check, not the default. `backend` (`auto` / `metal` / `cpu`) applies only to that path.

If the C++ library cannot be built or loaded, `method="auto"` uses the Metal relaxation when a
Metal GPU is present, otherwise a pure-Python twin of the C++ fill (same numbers, slow).
`method="priority-flood"` raises instead of falling back.

## What it computes

For a DEM `z`, the depression-filled surface `w ≥ z` where every cell has a non-ascending path
to an outlet — the surface a flow model needs so water never ponds in a pit that is really a
DEM artifact. `eps` (default 1e-5 m) is a minimal drainage increment so flats still drain, the
same convention as `terrain.priority_flood`.

**Outlets** are the grid border and cells next to NoData. NoData cells act as outlets for their
valid neighbours, so a basin ringed by NoData drains **to that NoData** rather than filling
without bound — the failure the Phase-A CPU conditioner hit (a NoData collar turned the whole
island into one infinite-volume depression).

## Why it is a GPU kernel, and why it is deterministic

Planchon & Darboux is a **monotone relaxation**: `w[c] = max(z[c], min(w[neighbours]) + eps)`,
iterated to convergence from a large starting level. Water levels only ever decrease, so the
fixed point is unique and reached from any thread order. That makes it a natural per-cell GPU
kernel *and* makes the converged surface **byte-identical** on the CPU build and on any Metal
GPU — the basis of the determinism test. (Priority-flood's serial heap does not parallelise;
this is the queue-free form that fits the GPU and Boann's iterate-to-convergence machinery.)

## Reuse, not a fork

This package reuses `core/metalflood`'s backend: the same ctypes Metal/CPU plumbing
(`backends.MetalBackend` / `backends.CpuBackend`), driven by a `backends.Spec` that names this
engine's kernel source, its `TParams`, its C++ source and its one-kernel dispatch plan. It also
reuses metalflood's `mf_shim.h` for the CPU build. `backends.py` gained the `Spec` indirection
with the flood engine as the default, so the flood engine is unchanged (its tests pass
byte-for-byte).

## Files

```
kernels_fill.metal      the Planchon–Darboux relax kernel (Metal + C++ via the shim)
cpu_kernels_fill.cpp    the CPU build: the kernel once per cell per sweep, nsteps sweeps
fill.py                 fill_dem(): seeding, the convergence loop, the backend Spec
cases.py                synthetic DEMs: a pitted plane, and a NoData-ringed pit
burn.py                 breakline enforcement on the conditioned DEM:
                        burn_channel()      low-ground breakline — lower a thalweg/ditch line to an
                                            enforced downstream fall (only lowers; freeze_mask guards crests)
                        raise_levee()       high-ground breakline — raise a levee/road crest line to its
                                            crest elevation or a freeboard (only raises; channel_mask guards channels)
                        reconnect_crossings()  auto-sweep: carve culvert notches through road/rail fills
__main__.py             verify / bench / fill CLI
```

## Check it

```bash
python -m nd_breach_runner.core.metalterrain verify            # both fills vs priority_flood and each other
python -m nd_breach_runner.core.metalterrain verify -backend metal   # relaxation also on the Apple GPU
python -m nd_breach_runner.core.metalterrain bench -size medium      # priority-flood and relax, with times
python -m nd_breach_runner.core.metalterrain fill in.asc out.asc --fill-method relax
```

`tests/test_metalterrain.py` checks, through the CPU build (so it runs in the cloud with no
GPU): the fill equals `priority_flood` to float precision, two runs are byte-identical, a
NoData-ringed basin drains finitely (the Phase-A bug), the `TParams`/buffer layout matches the
kernel, and the backend is metalflood's. On a Mac with a Metal GPU the Apple-GPU fill is also
checked against the CPU build.

## Status

Depression-fill is the reference, correct, GPU-and-CPU piece. Built on it in Python decision logic
(each with a delta-log class, the freeze-mask dam-safety invariant, and tests): artifact triage
(`audit.py`), crossing reconnection and channel burn (`burn.py`), culvert/HDS-5 ratings
(`culvert.py`), and the below-water channel-prism carve (`prism.py`). The carve is **RESEARCH**:
`carve_channels` derives the channel network and drainage area from the flow accumulation, traces
ordered flowlines, and cuts a regional-hydraulic-geometry prism (width/depth = a·DA^b) below the
LiDAR surface, which sees the water, not the bed. Its default coefficients (Bieger et al. 2015
national) are **not** calibrated for North Dakota, so it is not wired into a default conditioner
run; a hazard-affecting carve must pass a verified regional / StreamStats regression. See the
conditioner design notes.
