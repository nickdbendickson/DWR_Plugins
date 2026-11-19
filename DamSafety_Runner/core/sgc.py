"""Sub-grid river channels for LISFLOOD-FP (its SGC model, Neal et al. 2012).

A 10-30 m model grid does not see a creek a few metres wide, and hydro-flattened LiDAR shows a
river's WATER SURFACE, not its bed: the channel that carries a breach flood away below the dam
is missing from the terrain. LISFLOOD-FP's sub-grid channel model puts a rectangular channel
INSIDE the cells it runs through (a width narrower than the cell and a bed below the DEM), so the
channel conveys and stores water the grid cannot resolve, and the floodplain flow starts where
the water leaves the bank.

This module builds the three channel rasters LISFLOOD-FP reads (SGCwidth, SGCbank, SGCbed) from
the model DEM alone, so it needs no download:

1. Drainage area: depressions filled (terrain.priority_flood, Barnes et al. 2014), D8 steepest
   descent, flow accumulation in cells x cell area. The area above the dam lies mostly OUTSIDE
   the model domain; `upstream_km2` (the dam's drainage area, when known) is added at the breach
   inflow cell so the channel below the dam is sized for its real watershed. Without it the
   channel below the dam is sized only from the area inside the domain (too small) and the
   report says so.
2. Channel cells: every cell draining at least `min_area_km2`.
3. Bankfull width and depth from drainage area by regional hydraulic geometry,
   W = a A^b and D = c A^f (A in km2, W and D in m). Default: Bieger et al. (2015), the
   conterminous-US equations (W = 2.70 A^0.352, D = 0.30 A^0.213; "Development and evaluation
   of bankfull hydraulic geometry relationships for the physiographic regions of the United
   States", JAWRA 51(4)). Replace a, b, c, f with a regional curve where one exists.
4. Bank = the DEM (the cell's ground, or the water surface of hydro-flattened LiDAR); bed = DEM
   - D. The width is capped at the cell size (a river wider than the cell is in the DEM already;
   only its missing bed is added).

Starting water: a channel cell whose DEM is flat water (hydro-flattened LiDAR, terrain.flat_water)
had water in it when it was flown, so it starts filled to the bank; every other channel cell
(a prairie draw, an intermittent creek) starts empty. `start` = "auto" (that rule), "empty" or
"full".

What LISFLOOD-FP then writes and how lisflood.read_results reads it: the SGC solver's maximum
depth is measured from the channel BED in a channel cell. The Runner reports the depth ABOVE THE
BANK (the depth a person or building on the ground sees), i.e. maximum depth - channel depth,
so a flood that stays in the channel is not counted as flooded ground. With the patched
LISFLOOD-FP (scripts/patch_lisflood.py, edit 4) the arrival time is likewise the first time the
water stands the arrival depth above the bank."""
import math

import numpy as np

from .terrain import priority_flood

# Bieger, Rathjens, Allen & Arnold (2015), conterminous US: W = a A^b, D = c A^f (m, A km2)
BIEGER_2015_US = (2.70, 0.352, 0.30, 0.213)
DEFAULT_MIN_AREA_KM2 = 2.0      # about 0.8 sq mi: W ~ 3.4 m, D ~ 0.35 m
DEFAULT_CHANNEL_N = 0.035       # LISFLOOD-FP's own SGCn default (a clean natural channel)
START_MODES = ("auto", "empty", "full")
MIN_DEPTH_M = 0.05              # a channel shallower than this adds nothing (and LISFLOOD-FP
                                # keeps the bed at least 0.01 m under the DEM anyway)

# D8 neighbours (dr, dc) and their distances in cells
_D8 = [(-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1)]
_DIST = [math.sqrt(2), 1.0, math.sqrt(2), 1.0, 1.0, math.sqrt(2), 1.0, math.sqrt(2)]


class SGCError(ValueError):
    pass


class Channels:
    """The channel rasters of one model grid. width / bed / depth are float64 arrays on the
    DEM's rows and columns (0 / DEM / 0 off the channel); start_depth is the starting water
    depth from the bed (0 where the channel starts empty)."""

    def __init__(self, dem, width, depth, area_km2, start_depth, channel_n, info):
        self.dem = dem
        self.width = width
        self.depth = depth
        self.area_km2 = area_km2
        self.start_depth = start_depth
        self.channel_n = float(channel_n)
        self.info = info

    @property
    def mask(self):
        return self.width > 0

    @property
    def bed(self):
        z = self.dem.data
        return np.where(self.mask, z - self.depth, z)

    def start_volume_m3(self):
        """Water in the channels at the start (m3): width x cell length x depth."""
        return float((self.width * self.start_depth).sum() * self.dem.dx)

    def capacity_m3(self):
        """Bankfull volume of all channels (m3), what an empty channel can absorb."""
        return float((self.width * self.depth).sum() * self.dem.dx)

    def grids(self):
        """(width, bank, bed) Grids for LISFLOOD-FP's SGCwidth / SGCbank / SGCbed."""
        z = self.dem.data
        valid = np.isfinite(z) & (z != self.dem.nodata)
        nd = self.dem.nodata
        w = np.where(valid, self.width, nd)
        bank = np.where(valid, z, nd)
        bed = np.where(valid, self.bed, nd)
        return self.dem.like(w), self.dem.like(bank), self.dem.like(bed)


def d8_receivers(filled, valid):
    """Index (flat) of the D8 steepest-descent receiver of every cell of a depression-filled
    surface; -1 at an outlet (no lower neighbour, the grid edge or next to no-data)."""
    ny, nx = filled.shape
    big = np.where(valid, filled, np.inf)
    pad = np.pad(big, 1, constant_values=np.inf)       # outside / no-data: never a receiver
    best = np.zeros(filled.shape)
    rec = np.full(filled.shape, -1, dtype=np.int64)
    rr, cc = np.mgrid[0:ny, 0:nx]
    with np.errstate(invalid="ignore"):
        for k, (dr, dc) in enumerate(_D8):
            nb = pad[1 + dr:1 + dr + ny, 1 + dc:1 + dc + nx]
            drop = (big - nb) / _DIST[k]                # -inf toward outside / no-data
            better = drop > best
            best = np.where(better, drop, best)
            rec = np.where(better, (rr + dr) * nx + (cc + dc), rec)
    rec[~valid] = -1
    return rec


def flow_accumulation(dem, extra_cells=None):
    """Drainage area of every cell (km2) from the DEM: priority-flood fill, D8, accumulation.
    `extra_cells` = [(row, col, km2)] adds area entering from outside the grid at those cells
    (the watershed above the dam). No-data cells get 0."""
    z = np.asarray(dem.data, dtype=float)
    valid = np.isfinite(z) & (z != dem.nodata)
    zz = np.where(valid, z, np.nan)
    filled = priority_flood(zz)
    rec = d8_receivers(filled, valid)
    cell_km2 = dem.dx * dem.dx / 1e6
    acc = np.where(valid, cell_km2, 0.0).ravel()
    for r, c, km2 in (extra_cells or ()):
        if 0 <= r < dem.ny and 0 <= c < dem.nx and valid[r, c]:
            acc[r * dem.nx + c] += float(km2)
    order = np.argsort(np.where(valid, filled, -np.inf).ravel(), kind="stable")[::-1]
    recf = rec.ravel()
    # highest first: every cell hands its total to its receiver before the receiver is visited
    order = order[valid.ravel()[order]]
    acc_l = acc.tolist()
    rec_l = recf.tolist()
    for i in order.tolist():
        j = rec_l[i]
        if j >= 0:
            acc_l[j] += acc_l[i]
    return np.asarray(acc_l).reshape(z.shape)


def hydraulic_geometry(area_km2, curve=BIEGER_2015_US):
    a, b, c, f = curve
    A = np.maximum(np.asarray(area_km2, dtype=float), 0.0)
    return a * A ** b, c * A ** f


def build(dem, inflow_rc=None, upstream_km2=None, min_area_km2=DEFAULT_MIN_AREA_KM2,
          curve=BIEGER_2015_US, channel_n=DEFAULT_CHANNEL_N, start="auto", flat_water=None):
    """Channels of `dem` (a grid.Grid in metres). `inflow_rc` = (row, col) of the breach inflow
    and `upstream_km2` the drainage area at the dam (added there); `flat_water` = a boolean
    mask of hydro-flattened water (computed with terrain.flat_water when None and start is
    "auto")."""
    if start not in START_MODES:
        raise SGCError("channel start must be one of {}, got '{}'".format(START_MODES, start))
    if not min_area_km2 or min_area_km2 <= 0:
        raise SGCError("the smallest channel drainage area must be positive")
    if len(curve) != 4 or min(curve) <= 0:
        raise SGCError("the hydraulic-geometry curve needs four positive numbers (a, b, c, f)")
    extra = []
    if inflow_rc is not None and upstream_km2:
        extra.append((int(inflow_rc[0]), int(inflow_rc[1]), float(upstream_km2)))
    area = flow_accumulation(dem, extra)
    z = np.asarray(dem.data, dtype=float)
    valid = np.isfinite(z) & (z != dem.nodata)
    chan = valid & (area >= float(min_area_km2))
    w, d = hydraulic_geometry(np.where(chan, area, 0.0), curve)
    chan &= d >= MIN_DEPTH_M
    width = np.where(chan, np.minimum(w, dem.dx), 0.0)
    depth = np.where(chan, d, 0.0)
    capped = int((chan & (w > dem.dx)).sum())
    if start == "full":
        full = chan
    elif start == "empty":
        full = np.zeros_like(chan)
    else:
        if flat_water is None:
            from .terrain import flat_water as _fw
            try:
                lab, bodies = _fw(dem)
                flat_water = np.isin(lab, [b["id"] for b in bodies]) if bodies else \
                    np.zeros_like(chan)
            except Exception:       # noqa: BLE001 - no flat water found / odd grid: start empty
                flat_water = np.zeros_like(chan)
        full = chan & np.asarray(flat_water, dtype=bool)
    start_depth = np.where(full, depth, 0.0)
    at_dam = None
    if inflow_rc is not None and 0 <= inflow_rc[0] < dem.ny and 0 <= inflow_rc[1] < dem.nx:
        at_dam = float(area[int(inflow_rc[0]), int(inflow_rc[1])])
    info = {
        "cells": int(chan.sum()),
        "length_km": round(float(chan.sum()) * dem.dx / 1000.0, 2),
        "min_area_km2": float(min_area_km2),
        "curve": [float(v) for v in curve],
        "channel_n": float(channel_n),
        "start": start,
        "cells_start_full": int(full.sum()),
        "max_width_m": round(float(width.max()), 2) if chan.any() else 0.0,
        "max_depth_m": round(float(depth.max()), 2) if chan.any() else 0.0,
        "width_capped_cells": capped,
        "upstream_km2": float(upstream_km2) if upstream_km2 else None,
        "area_at_inflow_km2": round(at_dam, 3) if at_dam is not None else None,
    }
    ch = Channels(dem, width, depth, area, start_depth, channel_n, info)
    info["capacity_m3"] = round(ch.capacity_m3(), 1)
    info["start_volume_m3"] = round(ch.start_volume_m3(), 1)
    return ch


def describe(info):
    """One line for the run log / report."""
    if not info.get("cells"):
        return ("Sub-grid channels: none (no cell drains {:g} km2 or more inside the model "
                "domain).".format(info.get("min_area_km2", 0)))
    s = ("Sub-grid channels (LISFLOOD-FP SGC): {:,} cells, {:.1f} km of channel, up to {:.1f} m "
         "wide and {:.2f} m deep (W = {:g} A^{:g}, D = {:g} A^{:g}), channel n {:g}; {:,} cells "
         "start full.".format(info["cells"], info["length_km"], info["max_width_m"],
                              info["max_depth_m"], *info["curve"], info["channel_n"],
                              info["cells_start_full"]))
    if not info.get("upstream_km2"):
        s += (" The drainage area at the dam was not given, so the channel below the dam is "
              "sized only from the area inside the model domain (too small for a dam whose "
              "watershed reaches outside it).")
    return s
