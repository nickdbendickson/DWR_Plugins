"""BoannGrid channel burn + crossing reconnection (conditioner Stage 4 / 4c).

Two operations on a DEM, both pure numpy on top of the fill, flow and audit pieces:

  burn_channel(dem, path, depth)       lower the ground along an ordered channel path so it falls
                                       monotonically downstream (an enforced-fall invert burn);
                                       only ever lowers, never raises.

  raise_levee(dem, path, crest_elev)   raise the ground along an ordered levee / road crest line to
                                       the crest elevation (or a freeboard above ground), filling a
                                       barrier the DEM under-represents - the uphill twin of
                                       burn_channel; only ever raises, never lowers.

  reconnect_crossings(dem, ...)        find filled depressions that a real channel drains into -
                                       a channel blocked by a road/rail fill with no culvert - and
                                       carve a notch through the barrier so the channel reconnects,
                                       instead of leaving it dammed into a reservoir the fill
                                       invented. The "biggest true gap" the conditioner closes.

A crossing is detected by flow, not by shape alone: a depression whose cells carry at least
`min_accum_cells` of upstream drainage area is a blocked channel, not a puddle. Each carve is
recorded for the delta-log (its pour point, exit, barrier length and the drainage area it
reconnects), so nothing the conditioner does to the ground is silent.
"""
from collections import deque

import numpy as np

from . import fill as F
from . import flow as FL
from . import audit as A

NODATA = F.NODATA


def burn_channel(dem, path, depth=0.0, freeze_mask=None):
    """Lower `dem` along `path` (an ordered list of (row, col), upstream -> downstream) so it never
    rises going downstream, burning `depth` m below the running minimum. Returns a new array; only
    the path cells change and they only ever drop.

    If `freeze_mask` is given (a bool array, True = frozen embankment / dam crest), a path cell that
    is frozen raises RuntimeError LOUDLY: this op burns the exact path the caller asked for, and a
    burn through a dam crest is the catastrophe the mask exists to prevent - it is an error to
    surface, never a cell to silently skip."""
    out = np.asarray(dem, dtype=np.float64).copy()
    run = np.inf
    for r, c in path:
        if freeze_mask is not None and freeze_mask[r, c]:
            raise RuntimeError(
                "burn_channel blocked: cell ({}, {}) is frozen (embankment / dam-crest mask); "
                "refusing to lower it - this is the dam-safety invariant".format(r, c))
        run = min(run, float(out[r, c]))
        out[r, c] = run - depth
    return out


def raise_levee(dem, path, crest_elev=None, height=None, channel_mask=None):
    """Raise `dem` along `path` (an ordered list of (row, col) following a levee / road / embankment
    crest) so the ground sits at least at the crest, filling a gap the DEM under-represents. The
    uphill twin of burn_channel: returns a new array; only the path cells change and they only ever
    RISE, never drop. Use it when a known barrier (surveyed levee, road embankment) is thinner than a
    cell or missed by LiDAR, so the bare DEM lets the model overtop a crest that really holds.

    Give EXACTLY ONE target:
      crest_elev : absolute crest elevation in the DEM's vertical datum. Every path cell is raised to
                   at least this, so no notch on the line sits below it - use when the design /
                   surveyed crest elevation is known. (A NODATA path cell is set straight to it.)
      height     : metres of freeboard above each cell's OWN ground - each path cell is raised to its
                   current ground + height - for a road / levee of roughly constant height above the
                   surrounding terrain. A NODATA path cell has no ground to add to and is left alone.

    If `channel_mask` is given (a bool array, True = a known channel / thalweg that must stay open), a
    path cell that is a channel cell raises RuntimeError LOUDLY: damming a live channel into a phantom
    reservoir is the mirror of burn_channel breaching a dam crest - an error to surface, never a cell
    to silently raise. (This is the uphill analogue of burn_channel's freeze_mask guard.)"""
    if (crest_elev is None) == (height is None):
        raise ValueError("raise_levee: give exactly one of crest_elev or height")
    out = np.asarray(dem, dtype=np.float64).copy()
    for r, c in path:
        if channel_mask is not None and channel_mask[r, c]:
            raise RuntimeError(
                "raise_levee blocked: cell ({}, {}) is a channel / thalweg (channel mask); refusing "
                "to raise it - raising ground across a live channel would dam it into a phantom "
                "reservoir".format(r, c))
        cur = float(out[r, c])
        if crest_elev is not None:
            target = float(crest_elev)
            if cur == NODATA or target > cur:      # fill a NODATA gap, else raise only
                out[r, c] = target
        else:
            if cur == NODATA:                      # no ground to add freeboard to
                continue
            out[r, c] = cur + float(height)
    return out


def _carve_path(dem, valid, depr, floor):
    """Multi-source BFS from the depression `depr` outward over valid cells to the nearest cell
    below `floor` (the downstream channel). Returns the path (list of (r,c)) from a depression-edge
    cell across the barrier to that exit, or None. 4-connected."""
    ny, nx = dem.shape
    parent = -np.ones((ny, nx), dtype=np.int64)
    q = deque()
    for r, c in np.argwhere(depr):
        parent[r, c] = r * nx + c          # a source points to itself
        q.append((r, c))
    exit_rc = None
    while q:
        r, c = q.popleft()
        if (not depr[r, c]) and valid[r, c] and dem[r, c] < floor:
            exit_rc = (r, c)
            break
        for dr, dc in ((-1, 0), (1, 0), (0, -1), (0, 1)):
            rr, cc = r + dr, c + dc
            if 0 <= rr < ny and 0 <= cc < nx and valid[rr, cc] and parent[rr, cc] < 0:
                parent[rr, cc] = r * nx + c
                q.append((rr, cc))
    if exit_rc is None:
        return None
    path = []
    r, c = exit_rc
    while True:
        path.append((r, c))
        p = int(parent[r, c])
        pr, pc = divmod(p, nx)
        if (pr, pc) == (r, c):
            break
        r, c = pr, pc
    path.reverse()                         # depression edge -> ... -> exit
    return path


def freeze_refusals(records):
    """The subset of `reconnect_crossings` records that were refused because their carve path hit a
    frozen (embankment / dam-crest) cell. `len(freeze_refusals(records))` is the aggregate count the
    caller must not ignore (it is also surfaced in delta_log.summary()'s Bottom line)."""
    return [r for r in records if r.get("refused") == "freeze"]


def reconnect_crossings(dem, min_accum_cells=20, cell_m=1.0, eps=1e-5, backend="auto",
                        filled=None, labels=None, items=None, accum=None, freeze_mask=None,
                        refuse_freeze="record", log=None):
    """Carve a notch through the barrier of every filled depression a real channel drains into (at
    least `min_accum_cells` of upstream area), so the channel reconnects. Returns (conditioned_dem,
    records). Each carved record: id, pour (r,c), exit (r,c), barrier_cells, accum_cells,
    floor_elev_m, spill_before_m. Pass precomputed fill / census / accum to avoid recomputing.

    If `freeze_mask` is given (a bool array, True = frozen embankment / dam crest), a crossing whose
    carve path would lower a frozen cell is NOT carved: the dam-safety invariant is that the
    conditioner never breaches a frozen embankment (a real dam can look like a "blocked channel"
    with upstream area - breaching it is the exact catastrophe the mask prevents).

    `refuse_freeze` says how loud a freeze-refusal is, because a whole-DEM sweep must not finish
    with uncarved dams the report never mentions:
      "record" (default, for batch sweeps): the crossing is left uncarved and a FLAGGED record
          ({"refused": "freeze", "frozen_cells": [...]}) is kept so it reaches the delta-log /
          review; a loud line is sent to `log` with the AGGREGATE count. The caller must surface
          `len(freeze_refusals(records))` (delta_log.log_reconnect + summary() do).
      "raise": any freeze-refusal raises RuntimeError naming the aggregate count and the dams - the
          mode for a single-dam / interactive run where a frozen crossing should stop the conditioner
          outright rather than be deferred to a register.
    Nothing is lowered silently through the mask in either mode."""
    if refuse_freeze not in ("record", "raise"):
        raise ValueError("refuse_freeze must be 'record' or 'raise', not {!r}".format(refuse_freeze))
    a = np.asarray(dem, dtype=np.float64)
    valid = np.isfinite(a) & (a != NODATA)
    if filled is None:
        filled, _ = F.fill_dem(a, eps=eps, backend=backend, log=log)
    if labels is None or items is None:
        labels, items = A.depression_census(a, filled, cell_m=cell_m)
    if accum is None:
        _dirn, accum, _ = FL.flow_accum(a, filled=filled, backend=backend)
    out = a.copy()
    records = []
    for it in items:
        m = labels == it["id"]
        amax = float(np.nanmax(np.where(m, accum, np.nan)))
        if amax < min_accum_cells:
            continue                       # a puddle, not a blocked channel
        floor = it["floor_elev_m"]
        path = _carve_path(a, valid, m, floor)
        if path is None or len(path) < 2:
            continue
        exit_rc = path[-1]
        ze = float(a[exit_rc])
        n = len(path)
        # dam-safety: refuse to carve a path that would lower any frozen (embankment / dam) cell
        if freeze_mask is not None:
            frozen = [(r, c) for (r, c) in path if freeze_mask[r, c]]
            if frozen:
                records.append({"id": it["id"], "pour": path[0], "exit": exit_rc,
                                "barrier_cells": n, "accum_cells": amax, "floor_elev_m": floor,
                                "spill_before_m": it["spill_elev_m"],
                                "refused": "freeze", "frozen_cells": frozen})
                continue                   # never lower through the freeze mask
        for k, (r, c) in enumerate(path):
            t = k / (n - 1)
            ramp = floor + t * (ze - floor)          # floor at the depression, exit level downstream
            if ramp < out[r, c]:
                out[r, c] = ramp                     # carve: only ever lower
        records.append({"id": it["id"], "pour": path[0], "exit": exit_rc,
                        "barrier_cells": n, "accum_cells": amax, "floor_elev_m": floor,
                        "spill_before_m": it["spill_elev_m"]})
    refused = freeze_refusals(records)
    if refused:
        msg = ("{} crossing(s) refused: a frozen embankment / dam-crest cell lies on the carve "
               "path - NOT carved (depressions {}).".format(
                   len(refused), ", ".join(str(r["id"]) for r in refused)))
        if refuse_freeze == "raise":
            raise RuntimeError("reconnect_crossings (refuse_freeze='raise'): " + msg +
                               " The dam-safety invariant stops the conditioner here.")
        if log is not None:
            log("[boanngrid] WARNING: " + msg + " They are FLAGGED in the delta-log for an "
                "operator decision (real culvert? mask wrong?).")
    return out, records
