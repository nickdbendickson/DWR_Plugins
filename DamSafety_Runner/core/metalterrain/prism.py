"""BoannGrid fine-DEM channel prism carve (RESEARCH).

LiDAR sees the water surface, not the channel bed, so a fine DEM is missing the below-water prism
exactly where a flood model needs conveyance. This carves a trapezoidal channel prism along a
flowline into the DEM, sized by a regional hydraulic-geometry regression on drainage area
(Bieger et al. 2015, US: W = 2.70*DA^0.352 m, depth = 0.30*DA^0.213 m, DA in km^2). The bank stays
the DEM; the bed is carved depth below it, with the centerline forced to fall downstream.

RESEARCH, not production: it needs a real fine DEM, a real flowline (3DHP/NHD), and a real
drainage-area field (flow accumulation or StreamStats), and the regression should be the local
one. It is here as a validated algorithm, not a wired conditioner stage. The subgrid-topography
variant (Devi & Kuiry 2024 tables) is a separate research item with the flood engine.

    from nd_breach_runner.core.metalterrain import prism
    carved, reaches = prism.carve_prism(dem, flowline, da_km2, cell_m=1.0)
"""
import math

import numpy as np


def bieger_geometry(da_km2, a_w=2.70, b_w=0.352, a_d=0.30, b_d=0.213):
    """Regional channel width (m) and bankfull depth (m) for drainage area `da_km2` (km^2), from
    the Bieger et al. (2015) US regional curves. Swap the coefficients for a local regression."""
    da = max(float(da_km2), 0.0)
    return a_w * da ** b_w, a_d * da ** b_d


def da_from_accum(accum_cells, cell_m):
    """Drainage area (km^2) from a flow-accumulation cell count (core/metalterrain.flow)."""
    return np.asarray(accum_cells, dtype=np.float64) * (cell_m * cell_m) / 1.0e6


def carve_prism(dem, flowline, da_km2, cell_m=1.0, min_da_km2=0.05, coeffs=None):
    """Carve a trapezoidal channel prism along `flowline` (ordered (row, col), upstream ->
    downstream) into `dem`. `da_km2` is the drainage area per flowline cell. Returns (carved,
    reaches); each reach records the cell, its DA, width, depth and bed elevation, for the
    delta-log. Only ever lowers ground, and the carved bed falls monotonically downstream."""
    out = np.asarray(dem, dtype=np.float64).copy()
    ny, nx = out.shape
    kw = coeffs or {}
    run_bank = math.inf
    reaches = []
    for (r, c), da in zip(flowline, da_km2):
        if not (0 <= r < ny and 0 <= c < nx) or da < min_da_km2:
            continue
        w_m, depth = bieger_geometry(da, **kw)
        hw = max(1.0, (w_m / 2.0) / cell_m)              # half-width in cells
        run_bank = min(run_bank, float(dem[r, c]))       # enforced downstream fall of the bank
        bed = run_bank - depth
        rad = int(math.ceil(hw))
        for dr in range(-rad, rad + 1):
            for dc in range(-rad, rad + 1):
                rr, cc = r + dr, c + dc
                if not (0 <= rr < ny and 0 <= cc < nx):
                    continue
                dist = math.hypot(dr, dc)
                if dist > hw:
                    continue
                prof = bed + (dist / hw) * depth          # bed at centre, up to bank at the edge
                if prof < out[rr, cc]:
                    out[rr, cc] = prof
        reaches.append({"cell": (r, c), "da_km2": float(da), "width_m": float(w_m),
                        "depth_m": float(depth), "bed_elev_m": float(bed)})
    return out, reaches
