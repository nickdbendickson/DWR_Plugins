# core/metalterrain — BoannGrid terrain conditioner (GPU depression-fill)

The first harvested piece of the **BoannGrid** terrain conditioner: a GPU **depression-fill**
(Planchon & Darboux relaxation) written as a Metal compute kernel for the Apple GPU, with a C++
build of the same kernel source for the cloud and computers without a Metal GPU.

It is the one kernel of the Lumo/"Terra" Swift spike that reviewed as correct, reimplemented in
the real Python + Metal stack (not Swift) and validated against
`core/terrain.priority_flood` — the serial Barnes priority-flood — as the ground-truth oracle.

```python
from nd_breach_runner.core.metalterrain import fill
filled, info = fill.fill_dem(dem, eps=1e-5, backend="auto")   # NaN / -9999 = NoData
```

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
python -m nd_breach_runner.core.metalterrain verify            # CPU build vs priority_flood + determinism
python -m nd_breach_runner.core.metalterrain verify -backend metal   # on a Mac, the Apple GPU
python -m nd_breach_runner.core.metalterrain bench -size medium
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
