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
    rr, cc = np.nonzero(depr)
    return _carve_path_from(dem, valid, depr, rr, cc, floor)


def _carve_path_from(dem, valid, in_depr, rows, cols, floor):
    """_carve_path's BFS from the depression cells (rows, cols) in row-major order. `in_depr` is
    anything indexable by [r, c] that is True inside the depression (a bool mask, or a label grid
    compared to one id). The visited set is a dict, so the cost is the cells the search reaches,
    not the whole grid (Runner 3.51.0: a full-grid parent array per depression made
    reconnect_crossings O(n_depressions x cells))."""
    ny, nx = dem.shape
    parent = {}
    q = deque()
    for r, c in zip(rows.tolist(), cols.tolist()):
        parent[(r, c)] = (r, c)            # a source points to itself
        q.append((r, c))
    exit_rc = None
    while q:
        r, c = q.popleft()
        if (not in_depr[r, c]) and valid[r, c] and dem[r, c] < floor:
            exit_rc = (r, c)
            break
        for dr, dc in ((-1, 0), (1, 0), (0, -1), (0, 1)):
            rr, cc = r + dr, c + dc
            if 0 <= rr < ny and 0 <= cc < nx and valid[rr, cc] and (rr, cc) not in parent:
                parent[(rr, cc)] = (r, c)
                q.append((rr, cc))
    if exit_rc is None:
        return None
    path = []
    cur = exit_rc
    while True:
        path.append(cur)
        p = parent[cur]
        if p == cur:
            break
        cur = p
    path.reverse()                         # depression edge -> ... -> exit
    return path


class _IsLabel:
    """labels[r, c] == lid, read cell by cell (no full-grid mask)."""
    __slots__ = ("labels", "lid")

    def __init__(self, labels, lid):
        self.labels, self.lid = labels, lid

    def __getitem__(self, rc):
        return self.labels[rc] == self.lid


def limit_refusals(records):
    """The `reconnect_crossings` records refused by a limit rather than the freeze mask: a carve
    deeper than `max_cut_m` ("max_cut") or through the `protect_mask` ("protected"). The ground is
    left as it was; the delta-log FLAGs each one for review (delta_log.log_reconnect)."""
    return [r for r in records if r.get("refused") in ("max_cut", "protected")]


def grow_mask(mask, cells):
    """`mask` (bool) grown by `cells` cells in every direction (a square buffer), e.g. building
    footprints plus a 25 m margin on a 30 m grid is grow_mask(footprints, 1)."""
    m = np.asarray(mask, dtype=bool)
    k = int(cells)
    if k <= 0:
        return m.copy()
    ny, nx = m.shape
    pad = np.pad(m, k, constant_values=False)
    out = np.zeros_like(m)
    for dr in range(-k, k + 1):
        for dc in range(-k, k + 1):
            out |= pad[k + dr:k + dr + ny, k + dc:k + dc + nx]
    return out


# What reconnect_crossings carves by default (Runner 3.57.0). Absaraka at 30 m (DWR-2801, PR #473
# rerun): carving every drained depression of any class made 39,048 notches up to 19.5 m deep,
# 108 of them through Casselton house lots, and 107 houses read as "lethal" in water that sits
# below the uncut ground. Carving only the census' "crossing" class (a long, thin depression, the
# shape of a channel ponded behind a road or rail fill) gave 418 carves and none of that.
DEFAULT_CLASSES = ("crossing",)
# The deepest cut one carve may make, in metres below the ground it cuts. A culvert-less road or
# rail fill on a prairie channel is a few metres high; a carve deeper than this is not a missing
# culvert, so it is refused and flagged instead.
DEFAULT_MAX_CUT_M = 6.0


def freeze_refusals(records):
    """The subset of `reconnect_crossings` records that were refused because their carve path hit a
    frozen (embankment / dam-crest) cell. `len(freeze_refusals(records))` is the aggregate count the
    caller must not ignore (it is also surfaced in delta_log.summary()'s Bottom line)."""
    return [r for r in records if r.get("refused") == "freeze"]


def reconnect_crossings(dem, min_accum_cells=20, cell_m=1.0, eps=1e-5, backend="auto",
                        filled=None, labels=None, items=None, accum=None, freeze_mask=None,
                        refuse_freeze="record", log=None, classes=DEFAULT_CLASSES,
                        max_cut_m=DEFAULT_MAX_CUT_M, protect_mask=None):
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
    Nothing is lowered silently through the mask in either mode.

    Three limits (Runner 3.57.0) keep the carve to real crossings:
      classes      : only depressions the census classed as one of these are candidates (default
                     ("crossing",); an item with no "klass" is not one). None = every class, the
                     pre-3.57.0 behaviour that carved ponds, pits and noise alike.
      max_cut_m    : a carve whose deepest cut below the ground it crosses is more than this many
                     metres is refused whole (never cut short, which would leave a plug) and
                     recorded as {"refused": "max_cut", "max_cut_m": <needed>}. None = no cap.
      protect_mask : bool array, True = keep out (building footprints, town limits; grow it with
                     grow_mask for a margin). A carve whose path lowers a protected cell is
                     refused whole and recorded as {"refused": "protected", "protected_cells": n}.
    Limit refusals leave the ground as it was and are FLAGGED in the delta-log for review
    (delta_log.log_reconnect); limit_refusals(records) lists them. They do not stop an export the
    way a freeze refusal does: not carving is the conservative side."""
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
    # Each depression's cells and peak drainage area from one pass over the grid (Runner 3.51.0),
    # not a `labels == id` mask per depression: same cells, same order, same max, same carves.
    labels = np.asarray(labels)
    nx = labels.shape[1]
    n_lab = int(labels.max()) if labels.size else 0
    for it in items:
        n_lab = max(n_lab, int(it["id"]))
    _flat, cells, starts = A.label_cells(labels, n_lab)
    acc_flat = np.asarray(accum, dtype=np.float64).ravel()
    keep = None if classes is None else frozenset(classes)
    if protect_mask is not None:
        protect_mask = np.asarray(protect_mask, dtype=bool)
        if protect_mask.shape != a.shape:
            raise ValueError("protect_mask shape {} != dem shape {}".format(
                protect_mask.shape, a.shape))
    for it in items:
        if keep is not None and it.get("klass") not in keep:
            continue                       # not a crossing-shaped depression
        lid = int(it["id"])
        ci = cells[starts[lid - 1]:starts[lid]] if 1 <= lid <= n_lab else cells[:0]
        vals = acc_flat[ci]
        vals = vals[~np.isnan(vals)]
        amax = float(vals.max()) if vals.size else float("nan")
        if amax < min_accum_cells:
            continue                       # a puddle, not a blocked channel
        floor = it["floor_elev_m"]
        rr, cc = np.divmod(ci, nx)
        path = _carve_path_from(a, valid, _IsLabel(labels, lid), rr, cc, floor)
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
        cut = []
        for k, (r, c) in enumerate(path):
            t = k / (n - 1)
            ramp = floor + t * (ze - floor)          # floor at the depression, exit level downstream
            if ramp < out[r, c]:
                cut.append((r, c, ramp, float(a[r, c]) - ramp))
        base = {"id": it["id"], "pour": path[0], "exit": exit_rc, "barrier_cells": n,
                "accum_cells": amax, "floor_elev_m": floor, "spill_before_m": it["spill_elev_m"]}
        deepest = max((x[3] for x in cut), default=0.0)
        if protect_mask is not None:
            hit = sum(1 for (r, c, _z, _d) in cut if protect_mask[r, c])
            if hit:
                records.append(dict(base, refused="protected", protected_cells=hit,
                                    max_cut_m=deepest, klass=it.get("klass")))
                continue                   # never carve through a house lot or a town
        if max_cut_m is not None and deepest > max_cut_m:
            records.append(dict(base, refused="max_cut", max_cut_m=deepest,
                                klass=it.get("klass")))
            continue                       # deeper than a missing culvert: leave it, flag it
        for (r, c, ramp, _d) in cut:
            if ramp < out[r, c]:
                out[r, c] = ramp                     # carve: only ever lower
        records.append(base)
    limited = limit_refusals(records)
    if limited and log is not None:
        nc = sum(1 for r in limited if r["refused"] == "max_cut")
        log("[boanngrid] {} crossing(s) NOT carved by a limit ({} deeper than {} m, {} through the "
            "protect mask); FLAGGED in the delta-log for review.".format(
                len(limited), nc, max_cut_m, len(limited) - nc))
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
