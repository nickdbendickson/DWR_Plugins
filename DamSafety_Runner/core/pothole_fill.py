"""Water in the closed depressions (potholes) of a model DEM at the start of a run.

A dry pothole stores flood water that a real pothole, partly full before the event, would pass
on: the 2026-11-18 rain-on-grid test lost most of its excess rain to the empty closed depressions
of a 30 m DEM, and starting them full restored the peak size. The prairie pothole literature
models the same thing as fill-and-spill with an antecedent storage state (Shook & Pomeroy's
Pothole Cascade Model; the HGD model of Mekonnen et al.): how much of a basin passes water on
depends on how full its depressions already are.

    depth, info = start_fill(z, fraction=0.5, cell_m=10.0)

`fraction` is the share of each depression's volume that already holds water: 0 = dry (the
Runner's default, no change), 1 = full to its spill level. Each depression is filled to the one
level that holds that share of its volume, so the water surface is flat and at rest.

Depressions are found with a 4-connected priority flood (Barnes et al. 2014) with no drainage
increment: LISFLOOD-FP and Boann pass water only across cell faces (D4), so a pit that drains
only diagonally still holds water in the model, and a D8 fill would leave it empty. The grid
border and NoData cells are outlets (a basin open to NoData is not filled: no water is put where
it could leave the model at once). numpy only.
"""
import heapq

import numpy as np

MIN_DEPTH_M = 1e-3     # a filled depth under this is float noise of the DEM, not a depression


def fill_d4(z, valid=None):
    """Depression-filled surface of `z` (2-D, NaN = NoData) with 4-connected drainage and no
    drainage increment (a filled pit is flat at its spill level). Outlets: the grid border and
    valid cells 4-adjacent to NoData. Returns float64, NaN where NoData."""
    a = np.asarray(z, dtype=np.float64)
    if valid is None:
        valid = np.isfinite(a)
    ny, nx = a.shape
    filled = np.where(valid, a, np.nan)
    closed = ~valid.copy()
    seed = np.zeros_like(valid)
    seed[0, :] = seed[-1, :] = seed[:, 0] = seed[:, -1] = True
    nod = ~valid
    seed[1:, :] |= nod[:-1, :]
    seed[:-1, :] |= nod[1:, :]
    seed[:, 1:] |= nod[:, :-1]
    seed[:, :-1] |= nod[:, 1:]
    seed &= valid
    flat = filled.ravel()
    cl = closed.ravel()
    heap = [(flat[i], int(i)) for i in np.flatnonzero(seed.ravel())]
    heapq.heapify(heap)
    cl[np.flatnonzero(seed.ravel())] = True
    pit = []                                    # cells raised to the current level (Barnes 2014)
    push, pop = heapq.heappush, heapq.heappop
    while heap or pit:
        if pit:
            lev, i = pit.pop()
        else:
            lev, i = pop(heap)
        r, c = divmod(i, nx)
        for j, ok in ((i - nx, r > 0), (i + nx, r < ny - 1), (i - 1, c > 0), (i + 1, c < nx - 1)):
            if ok and not cl[j]:
                cl[j] = True
                if flat[j] <= lev:
                    flat[j] = lev
                    pit.append((lev, j))
                else:
                    push(heap, (flat[j], j))
    return filled


def _label4(mask):
    try:
        from scipy import ndimage
        lab, n = ndimage.label(mask)            # 4-connected by default
        return lab.astype(np.int64), int(n)
    except Exception:                                    # noqa - scipy is not everywhere
        from .metalterrain.audit import _label4 as lab4
        lab, n = lab4(mask)
        return lab.astype(np.int64), int(n)


def start_fill(z, fraction, cell_m, valid=None, min_depth_m=MIN_DEPTH_M):
    """Start depth (m) that puts `fraction` (0-1) of each closed depression's volume in it.
    Returns (depth float64 with 0 outside depressions and NaN on NoData, info dict)."""
    f = float(fraction)
    if not 0.0 <= f <= 1.0:
        raise ValueError("pothole start fill must be 0-1, got {}".format(fraction))
    a = np.asarray(z, dtype=np.float64)
    if valid is None:
        valid = np.isfinite(a)
    depth = np.where(valid, 0.0, np.nan)
    info = {"fraction": f, "depressions": 0, "capacity_m3": 0.0, "volume_m3": 0.0, "area_m2": 0.0,
            "connectivity": 4}
    if f <= 0.0 or not valid.any():
        return depth, info
    filled = fill_d4(a, valid)
    d_full = np.where(valid, filled - a, 0.0)
    lab, n = _label4(d_full > min_depth_m)
    if n == 0:
        return depth, info
    cell_a = float(cell_m) ** 2
    idx = np.flatnonzero(lab.ravel())
    labs = lab.ravel()[idx]
    zz = a.ravel()[idx]
    spill = filled.ravel()[idx]
    order = np.lexsort((zz, labs))              # by depression, then ground up
    idx, labs, zz, spill = idx[order], labs[order], zz[order], spill[order]
    starts = np.flatnonzero(np.r_[True, labs[1:] != labs[:-1]])
    ends = np.r_[starts[1:], len(labs)]
    out = depth.ravel()
    cap_tot = vol_tot = 0.0
    for s, e in zip(starts, ends):
        zs = zz[s:e]                            # ascending
        S = float(spill[s])
        cap = float(np.sum(S - zs))             # m of water x cells, at the spill level
        cap_tot += cap
        if f >= 1.0:
            L = S
        else:
            target = f * cap
            k = np.arange(1, len(zs) + 1)
            cz = np.cumsum(zs)
            v_at = k * zs - cz                  # volume (cells x m) with the level at zs[k-1]
            m = int(np.searchsorted(v_at, target, side="right"))   # cells under the level
            L = (target + cz[m - 1]) / m if m > 0 else float(zs[0])
            L = min(L, S)
        dd = np.maximum(L - zs, 0.0)
        out[idx[s:e]] = dd
        vol_tot += float(dd.sum())
    depth = out.reshape(a.shape)
    wet = depth > 0
    info.update(depressions=int(n), capacity_m3=cap_tot * cell_a, volume_m3=vol_tot * cell_a,
                area_m2=float(wet.sum()) * cell_a)
    return depth, info


def describe(info):
    """One log / report line."""
    if not info.get("fraction"):
        return "Potholes start dry."
    return ("Potholes start {:.0%} full: {:,} closed depressions (4-connected) hold {:,.0f} m3 of their "
            "{:,.0f} m3 over {:,.1f} ha at the start. This water is not counted as flooding.").format(
        info["fraction"], info["depressions"], info["volume_m3"], info["capacity_m3"],
        info["area_m2"] / 1e4)
