"""BoannGrid fine-DEM channel prism carve (RESEARCH).

LiDAR sees the water surface, not the channel bed, so a fine DEM is missing the below-water prism
exactly where a flood model needs conveyance. This carves a trapezoidal channel prism along a
flowline into the DEM, sized by a regional hydraulic-geometry regression on drainage area
(since 3.53.0 the North Dakota curve from 79 USGS gauges, ND_USGS_2026: W = 1.946*DA^0.304 m,
depth = 0.221*DA^0.256 m, DA in km^2; before, Bieger et al. 2015, US). The bank stays
the DEM; the bed is carved depth below it, with the centerline forced to fall downstream.

`carve_channels` runs the whole stage from a DEM alone: it derives the channel network and the
per-cell drainage area from the flow accumulation (core/metalterrain.flow), traces ordered
flowlines, carves each with `carve_prism`, honours the embankment / dam-crest freeze mask, and
returns per-flowline records for the delta-log (delta_log.log_prism). A real 3DHP / NHD flowline
is used instead by rasterising it to (row, col) and passing it straight to `carve_prism`.

The default coefficients are the ND curve (Nick, 2026-11-23; fit and gauges in
tools/validation/nd_hydraulic_geometry/). It is not a published regression; `coeffs` takes
another. This module is not wired into the default model-terrain conditioner run. The
subgrid-topography variant (Devi & Kuiry 2024 tables) is a separate research item with the flood
engine.

    from nd_breach_runner.core.metalterrain import prism
    carved, channels, refusals = prism.carve_channels(dem, cell_m=1.0)   # DEM-intrinsic flowlines
    carved, reaches = prism.carve_prism(dem, flowline, da_km2, cell_m=1.0)  # a supplied flowline
"""
import math

import numpy as np

from . import flow as FL


BIEGER_2015_US = {"a_w": 2.70, "b_w": 0.352, "a_d": 0.30, "b_d": 0.213}
# North Dakota bankfull hydraulic geometry (W = a_w DA^b_w, D = a_d DA^b_d; m, DA km2 that
# contributes), fitted 2026-11-23 to 79 USGS gauges (45 to 10,600 km2): width and mean depth at
# the 1.5-year peak from each gauge's open-water discharge measurements. Method, every gauge and
# the fit: tools/validation/nd_hydraulic_geometry/ (fit.py, RESULTS.md). ND channels are about
# half as wide as the national curve at the same area (median 0.53) and as deep (median 1.03).
# The default `coeffs` since 3.53.0 (Nick, 2026-11-23); pass coeffs=BIEGER_2015_US for the old one.
ND_USGS_2026 = {"a_w": 1.946, "b_w": 0.304, "a_d": 0.221, "b_d": 0.256}


def bieger_geometry(da_km2, a_w=2.70, b_w=0.352, a_d=0.30, b_d=0.213):
    """Regional channel width (m) and bankfull depth (m) for drainage area `da_km2` (km^2).

    The default coefficients are the Bieger et al. (2015) *national* (conterminous-US) regional
    curves (W = 2.70*DA^0.352 m, D = 0.30*DA^0.213 m, DA in km^2). They are US-wide averages and
    are NOT calibrated for North Dakota: a national curve can be wide of a given physiographic
    region by a large factor, so do not carve a prism into a hazard-affecting DEM on these defaults
    without first checking them against a local/State hydraulic-geometry regression (and pass that
    regression's a_w/b_w/a_d/b_d here). `carve_prism` / `carve_channels` pass ND_USGS_2026 by
    default (3.53.0)."""
    da = max(float(da_km2), 0.0)
    return a_w * da ** b_w, a_d * da ** b_d


def da_from_accum(accum_cells, cell_m):
    """Drainage area (km^2) from a flow-accumulation cell count (core/metalterrain.flow)."""
    return np.asarray(accum_cells, dtype=np.float64) * (cell_m * cell_m) / 1.0e6


def carve_prism(dem, flowline, da_km2, cell_m=1.0, min_da_km2=0.05, coeffs=None,
                freeze_mask=None, bank_src=None):
    """Carve a trapezoidal channel prism along `flowline` (ordered (row, col), upstream ->
    downstream) into `dem`. `da_km2` is the drainage area per flowline cell. Returns (carved,
    reaches); each reach records the cell, its DA, width, depth and bed elevation, for the
    delta-log. Only ever lowers ground, and the carved bed falls monotonically downstream.

    `bank_src` is the surface the running-minimum bank is read from (default: `dem` itself). A
    driver that carves several flowlines into one array in turn passes the *original* DEM here, so
    a tributary's bank fall is measured on the untouched ground rather than on a trunk another
    flowline already carved.

    `freeze_mask` (bool array, True = frozen embankment / dam crest) is the dam-safety invariant:
    a frozen cell is NEVER lowered. A frozen cell inside the prism disk is simply left alone
    (the bank there stays the embankment). A frozen cell *on the centreline* is a mapped channel
    running through a dam crest - `carve_channels` refuses the whole flowline for that, LOUD; this
    low-level routine just does not lower it, so a stray mask cell cannot silently dam the carve."""
    out = np.asarray(dem, dtype=np.float64).copy()
    bank = out if bank_src is None else np.asarray(bank_src, dtype=np.float64)
    ny, nx = out.shape
    kw = coeffs or ND_USGS_2026
    run_bank = math.inf
    reaches = []
    for (r, c), da in zip(flowline, da_km2):
        if not (0 <= r < ny and 0 <= c < nx) or da < min_da_km2:
            continue
        w_m, depth = bieger_geometry(da, **kw)
        hw = max(1.0, (w_m / 2.0) / cell_m)              # half-width in cells
        run_bank = min(run_bank, float(bank[r, c]))      # enforced downstream fall of the bank
        bed = run_bank - depth
        rad = int(math.ceil(hw))
        for dr in range(-rad, rad + 1):
            for dc in range(-rad, rad + 1):
                rr, cc = r + dr, c + dc
                if not (0 <= rr < ny and 0 <= cc < nx):
                    continue
                if freeze_mask is not None and freeze_mask[rr, cc]:
                    continue                              # never lower a frozen embankment cell
                dist = math.hypot(dr, dc)
                if dist > hw:
                    continue
                prof = bed + (dist / hw) * depth          # bed at centre, up to bank at the edge
                if prof < out[rr, cc]:
                    out[rr, cc] = prof
        reaches.append({"cell": (r, c), "da_km2": float(da), "width_m": float(w_m),
                        "depth_m": float(depth), "bed_elev_m": float(bed)})
    return out, reaches


def trace_flowlines(direction, accum, min_accum_cells, conn=4):
    """Extract the channel network from a flow field as ordered flowlines, upstream -> downstream.

    `direction` and `accum` are `flow.flow_accum`'s outputs (direction codes and drainage area in
    cell counts). A cell is on a channel when its drainage area reaches `min_accum_cells`. Each
    returned flowline is a list of (row, col) that follows the flow direction from a channel head
    (a channel cell with no channel cell draining into it) down to where the channel ends - the
    outlet, the grid edge, or a trunk another flowline already claimed (the junction cell is
    included so the tributary connects). Every channel cell is emitted on exactly one flowline, so
    `carve_channels` carves each cell once, at its own full drainage area.

    This is the DEM-intrinsic flowline: it needs no external vector. A real 3DHP / NHD flowline is
    used instead by rasterising it to (row, col) and passing it straight to `carve_prism` with the
    drainage area sampled along it."""
    direction = np.asarray(direction)
    accum = np.asarray(accum, dtype=np.float64)
    ny, nx = direction.shape
    code_drc = FL.drc(conn)
    none_c = FL.none_code(conn)
    is_chan = np.isfinite(accum) & (accum >= float(min_accum_cells)) & (direction >= 0)
    chan = set(zip(*(a.tolist() for a in np.where(is_chan))))

    def downstream(r, c):
        d = int(direction[r, c])
        if d == none_c or d not in code_drc:
            return None
        dr, dc = code_drc[d]
        rr, cc = r + dr, c + dc
        if 0 <= rr < ny and 0 <= cc < nx:
            return (rr, cc)
        return None

    has_upstream = set()
    for cell in chan:
        ds = downstream(*cell)
        if ds in chan:
            has_upstream.add(ds)
    heads = sorted((c for c in chan if c not in has_upstream),
                   key=lambda rc: (-accum[rc], rc[0], rc[1]))   # deterministic order

    claimed = set()
    lines = []
    for head in heads:
        if head in claimed:
            continue
        path = []
        cur = head
        while cur in chan and cur not in claimed and len(path) <= len(chan):
            path.append(cur)
            claimed.add(cur)
            nxt = downstream(*cur)
            if nxt not in chan:
                cur = None
                break
            cur = nxt
        if cur in chan and cur in claimed and (not path or path[-1] != cur):
            path.append(cur)            # connect the tributary to the claimed trunk at the junction
        if path:
            lines.append(path)
    return lines


def carve_channels(dem, cell_m=1.0, eps=1e-5, min_da_km2=0.05, coeffs=None, freeze_mask=None,
                   conn=8, backend="auto", direction=None, accum=None, filled=None, log=None):
    """Carve the below-water channel prism of a whole DEM: derive the channel network from flow
    accumulation, then carve each flowline with `carve_prism`. Returns (carved, channels, refusals).

    `conn` is the flow connectivity for channel extraction (8 = D8, the GIS standard, by default).
    Pass a precomputed `direction`/`accum`/`filled` to skip the flow solve. `min_da_km2` sets both
    the channel threshold and the smallest reach `carve_prism` will cut. `coeffs` overrides the
    hydraulic-geometry regression (default ND_USGS_2026; see `bieger_geometry` for the form). `freeze_mask` protects embankment / dam-crest
    cells: a flowline whose centreline runs through a frozen cell is REFUSED whole (returned in
    `refusals`, never carved), the dam-safety invariant, rather than burning a channel through a dam.

    Each entry of `channels` aggregates one flowline (cells carved, drainage-area and depth range,
    dz statistics) for the delta-log; `delta_log.log_prism` records them."""
    base = np.asarray(dem, dtype=np.float64)
    if direction is None or accum is None:
        direction, accum, _info = FL.flow_accum(base, eps=eps, backend=backend, filled=filled,
                                                 conn=conn, log=log)
    cell_area_km2 = (cell_m * cell_m) / 1.0e6
    min_accum = max(1.0, float(min_da_km2) / cell_area_km2)
    flowlines = trace_flowlines(direction, accum, min_accum, conn=conn)

    out = base.copy()
    channels = []
    refusals = []
    for line in flowlines:
        da_line = np.array([accum[r, c] for (r, c) in line], dtype=np.float64) * cell_area_km2
        frozen = [(r, c) for (r, c) in line
                  if freeze_mask is not None and freeze_mask[r, c]]
        if frozen:
            refusals.append({
                "head": list(line[0]), "outlet": list(line[-1]),
                "frozen_cells": [list(c) for c in frozen], "n_cells": len(line),
                "da_max_km2": float(np.nanmax(da_line)) if len(da_line) else 0.0})
            continue
        before = out.copy()
        out, reaches = carve_prism(out, line, da_line, cell_m=cell_m, min_da_km2=min_da_km2,
                                   coeffs=coeffs, freeze_mask=freeze_mask, bank_src=base)
        if not reaches:
            continue
        changed = (out < before - 1e-9)
        dz = (before - out)[changed]
        da_vals = [rc["da_km2"] for rc in reaches]
        channels.append({
            "head": list(line[0]), "outlet": list(line[-1]),
            "n_reaches": len(reaches), "n_cells": int(changed.sum()),
            "da_min_km2": float(min(da_vals)), "da_max_km2": float(max(da_vals)),
            "width_max_m": float(max(rc["width_m"] for rc in reaches)),
            "depth_max_m": float(max(rc["depth_m"] for rc in reaches)),
            "dz_min_m": float(dz.min()) if dz.size else 0.0,
            "dz_mean_m": float(dz.mean()) if dz.size else 0.0,
            "dz_max_m": float(dz.max()) if dz.size else 0.0})
    return out, channels, refusals
