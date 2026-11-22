"""Sub-grid topography tables for Boann's sub-grid finite-volume solver (RESEARCH, CPU reference).

A coarse flood cell is normally a flat-bottomed bucket at one elevation, so it loses the channel the
fine (LiDAR) grid resolves inside it. Here each coarse cell keeps its r x r sub-cells as stage curves:

  - per cell : wetted VOLUME V(eta) and free-surface PLAN AREA P(eta) of a water surface at eta;
  - per face : wetted cross-sectional AREA A(eta), conveyance K(eta) and wet width W(eta).

Each is an integral over the sub-cells, built ONCE from the fine grid; the solver then reads the
curves and never touches the fine grid again (subfv_ref.py). The reductions the tests pin:

  - FLAT cell  -> V = dx^2 (eta - z), A = dx * hflow, Manning K -> identical to the plain engine;
  - a single rectangular channel -> the sub-grid-channel (SGC) closed forms, below bank exactly.

Integral ("porosity") form of the shallow-water equations: Casulli 2009; Volp, van Prooijen &
Stelling 2013; Devi & Kuiry 2024. Pure numpy; a Metal/CPU kernel will later sample the same packed
tables. Everything is in ABSOLUTE elevation (m), so there is no bank/datum bookkeeping in the solver.
"""
import numpy as np

G = 9.80665


class Tables:
    """Sub-grid stage tables for a (ny, nx) coarse grid. Built by build_from_fine().

    Cell curves are kept as the sorted sub-cell elevations s (ny, nx, m), m = r*r, with the
    sub-cell plan area asub = (dx/r)^2. Face curves are kept as the per-sub-cell edge bed profile
    zf (nf, r) and the per-sub-cell Manning n^2 nf2 (nf, r), with the sub-cell length wsub = dx/r.
    An x-face profile is the elementwise MAX of the two cells' shared-edge sub-beds (Audusse: flow
    over a face sees the higher sill), so a flat pair reduces to the plain hflow = eta - max(zl, zr).
    """

    def __init__(self, dx, r, s, csum, asub, zfx, nf2x, zfy, nf2y, zbed, zbank, edges,
                 ncell=None):
        self.dx = float(dx)
        self.r = int(r)
        self.s = s                 # (ny, nx, m) sorted sub-elevations
        self.csum = csum           # (ny, nx, m) cumulative sum of s along the last axis
        self.asub = float(asub)    # (dx/r)^2
        self.wsub = float(dx) / r  # dx/r
        # plan-area-mean Manning n per cell, for a cell-centred friction term (PROVISIONAL
        # composite; a stage-dependent conveyance composite is a later refinement, roughness-gated
        # like the bed-carve coeffs). Only the shock-capturing cell-momentum solver reads it; the
        # face conveyance nf2* above is what the local-inertial solver uses.
        self.ncell = ncell         # (ny, nx) or None
        self.zfx = zfx             # (ny, nx-1, r) interior x-face sub-bed profiles
        self.nf2x = nf2x           # (ny, nx-1, r) interior x-face Manning n^2
        self.zfy = zfy             # (ny-1, nx, r) interior y-face sub-bed profiles
        self.nf2y = nf2y
        self.zbed = zbed           # (ny, nx) lowest sub-cell (channel invert), for dt / reporting
        self.zbank = zbank         # (ny, nx) highest sub-cell (bank top), for depth-above-bank
        self.ny, self.nx = zbed.shape
        # domain-boundary edge sub-profiles for FREE outflow: {"W":(zf,nf2), "E":.., "N":.., "S":..}
        self.edges = edges


def build_from_fine(dem_fine, n_fine, dx, r, fine_shape=None):
    """Build sub-grid tables from a fine DEM and roughness grid.

    dem_fine, n_fine : (ny*r, nx*r) fine bed elevation (m) and Manning n.
    dx               : COARSE cell size (m).  r : fine cells per coarse cell per axis.
    fine_shape       : (rows, cols) of the TRUE fine grid when dem_fine was padded on its south and
                       east sides up to a whole multiple of r (fewer than r rows / columns of
                       padding). The south and east domain-edge profiles are then read from the last
                       true row / column, not from the padding: a padded edge would otherwise be a
                       wall of padding and an open (FREE) edge could never drain. None = not padded.
    """
    dem = np.asarray(dem_fine, float)
    nn = np.asarray(n_fine, float)
    R = int(r)
    if dem.shape != nn.shape or dem.shape[0] % R or dem.shape[1] % R:
        raise ValueError("fine grids must share a shape that is a whole multiple of r")
    ny, nx = dem.shape[0] // R, dem.shape[1] // R
    if fine_shape is None:
        true_rows, true_cols = dem.shape
    else:
        true_rows, true_cols = int(fine_shape[0]), int(fine_shape[1])
        if not (dem.shape[0] - R < true_rows <= dem.shape[0]
                and dem.shape[1] - R < true_cols <= dem.shape[1]):
            raise ValueError("fine_shape must be the padded shape less under r rows / columns")
    # (ny, nx, r, r) blocks -> (ny, nx, m) sorted sub-elevations
    blk = dem.reshape(ny, R, nx, R).transpose(0, 2, 1, 3).reshape(ny, nx, R * R)
    s = np.sort(blk, axis=-1)
    csum = np.cumsum(s, axis=-1)
    asub = (dx / R) ** 2
    zbed = s[:, :, 0].copy()
    zbank = s[:, :, -1].copy()
    nblk = nn.reshape(ny, R, nx, R).transpose(0, 2, 1, 3)   # (ny, nx, r_row, r_col)
    ncell = nblk.reshape(ny, nx, R * R).mean(-1)            # plan-area-mean Manning n per cell
    # x-face between coarse cols j and j+1: the shared vertical edge, r sub-rows tall. Left cell's
    # east sub-column is fine col (j*R + R-1); right cell's west sub-column is fine col ((j+1)*R).
    left_e = dem[:, R - 1::R][:, :-1]             # (ny*R, nx-1) east edge of each left cell
    right_w = dem[:, 0::R][:, 1:]                 # (ny*R, nx-1) west edge of each right cell
    zfx = np.maximum(left_e, right_w).reshape(ny, R, nx - 1).transpose(0, 2, 1)   # (ny, nx-1, r)
    nle = nn[:, R - 1::R][:, :-1]
    nrw = nn[:, 0::R][:, 1:]
    nf2x = (0.5 * (nle + nrw)).reshape(ny, R, nx - 1).transpose(0, 2, 1) ** 2
    # y-face between coarse rows i and i+1: shared horizontal edge, r sub-cols wide.
    top_s = dem[R - 1::R, :][:-1, :]             # (ny-1, nx*R) south edge of each upper cell
    bot_n = dem[0::R, :][1:, :]                  # (ny-1, nx*R) north edge of each lower cell
    zfy = np.maximum(top_s, bot_n).reshape(ny - 1, nx, R)                          # (ny-1, nx, r)
    nts = nn[R - 1::R, :][:-1, :]
    nbn = nn[0::R, :][1:, :]
    nf2y = (0.5 * (nts + nbn)).reshape(ny - 1, nx, R) ** 2
    # domain-boundary edge sub-profiles (one cell only; no max with a neighbour). The east / south
    # profiles are the last TRUE fine column / row (fine_shape), so padding never walls an edge.
    ce, rs = true_cols - 1, true_rows - 1
    edges = {
        "W": (dem[:, 0].reshape(ny, R), nn[:, 0].reshape(ny, R) ** 2),
        "E": (dem[:, ce].reshape(ny, R), nn[:, ce].reshape(ny, R) ** 2),
        "N": (dem[0, :].reshape(nx, R), nn[0, :].reshape(nx, R) ** 2),
        "S": (dem[rs, :].reshape(nx, R), nn[rs, :].reshape(nx, R) ** 2),
    }
    return Tables(dx, R, s, csum, asub, zfx, nf2x, zfy, nf2y, zbed, zbank, edges, ncell)


# --- cell stage curves (vectorised over all cells) ------------------------------------------------

def cell_volume(eta, s, asub):
    """Wetted volume V(eta) per cell. eta: (ny, nx); s: (ny, nx, m)."""
    return asub * np.maximum(eta[..., None] - s, 0.0).sum(-1)


def cell_area(eta, s, asub):
    """Free-surface plan area P(eta) per cell."""
    return asub * (s < eta[..., None]).sum(-1)


def invert_eta(V, s, csum, asub):
    """Water-surface elevation eta from cell volume V (inverse of cell_volume).

    V(eta) is piecewise linear and increasing with breakpoints at the sorted sub-elevations s.
    On the segment above the k-th sub-cell, k+1 sub-cells are wet and
        V = asub * ((k+1) eta - sum_{j<=k} s[j])   ->   eta = (V/asub + csum[k]) / (k+1).
    """
    m = s.shape[-1]
    V0 = np.maximum(np.asarray(V, float), 0.0)
    kk = np.arange(m)
    pref = csum - s                                  # sum_{j<k} s[j]
    Vb = asub * (kk * s - pref)                      # V at each breakpoint eta = s[k]
    k = np.clip((Vb <= V0[..., None]).sum(-1) - 1, 0, m - 1)
    csum_k = np.take_along_axis(csum, k[..., None], -1)[..., 0]
    return (V0 / asub + csum_k) / (k + 1)


def all_faces(tables):
    """Assemble the kernel's all-faces sub-bed and Manning-n^2 profiles from a Tables.

    The Tables store the INTERIOR face profiles (zfx (ny,nx-1,r), zfy (ny-1,nx,r)) and the four
    domain-edge profiles separately (tables.edges). The Metal/CPU kernel wants them as single
    all-faces arrays with the FREE-edge sub-bed in the boundary slots:
      zfx_all, nf2x_all : (ny, nx+1, r)   face column 0 = west edge, nx = east edge
      zfy_all, nf2y_all : (ny+1, nx, r)   face row 0 = north edge, ny = south edge
    Returns (zfx_all, nf2x_all, zfy_all, nf2y_all)."""
    t = tables
    ny, nx, r = t.ny, t.nx, t.r
    zfx = np.zeros((ny, nx + 1, r)); nf2x = np.zeros((ny, nx + 1, r))
    if nx > 1:
        zfx[:, 1:nx, :] = t.zfx
        nf2x[:, 1:nx, :] = t.nf2x
    zfx[:, 0, :], nf2x[:, 0, :] = t.edges["W"]
    zfx[:, nx, :], nf2x[:, nx, :] = t.edges["E"]
    zfy = np.zeros((ny + 1, nx, r)); nf2y = np.zeros((ny + 1, nx, r))
    if ny > 1:
        zfy[1:ny, :, :] = t.zfy
        nf2y[1:ny, :, :] = t.nf2y
    zfy[0, :, :], nf2y[0, :, :] = t.edges["N"]
    zfy[ny, :, :], nf2y[ny, :, :] = t.edges["S"]
    return zfx, nf2x, zfy, nf2y


def fine_depth(eta, dem_fine, r):
    """Downscale a coarse water-surface eta back to the fine (LiDAR) grid.

    eta      : (ny, nx) coarse free-surface elevation (from the sub-grid solver).
    dem_fine : (ny*r, nx*r) the fine DEM the tables were built from.
    r        : fine sub-cells per coarse cell per axis.

    Each fine sub-cell takes its coarse cell's eta (the FV1 state is piecewise-constant over the
    coarse cell's wet plan) and its depth is max(eta - its own fine bed, 0). So a COARSE sub-grid run
    yields a LiDAR-resolution inundation map: the channel is wet and the banks dry inside one coarse
    cell, which a flat coarse cell cannot show. By construction it integrates back to the exact coarse
    cell volume - sum over a cell's sub-cells of asub * depth == V(eta) - so the fine map adds no water.
    Returns the (ny*r, nx*r) fine depth (0 where dry)."""
    eta = np.asarray(eta, float)
    dem = np.asarray(dem_fine, float)
    r = int(r)
    up = np.repeat(np.repeat(eta, r, axis=0), r, axis=1)
    return np.maximum(up - dem, 0.0)


# --- face stage curves ----------------------------------------------------------------------------

def face_hydraulics(etaface, zf, nf2, wsub):
    """Wet area A, wet width W and conveyance K of a line of faces at water level etaface.

    etaface : (nf,) the controlling surface (max of the two cell surfaces, like SGC's hflow).
    zf, nf2 : (nf, r) the sub-cell edge bed and Manning n^2.  Returns A, W, K each (nf,).
    K = sum_sub (dx/r) * h^{5/3} / n  (compound-section Manning conveyance: Q = K * sqrt(S)).
    """
    hsub = np.maximum(etaface[..., None] - zf, 0.0)
    wet = hsub > 0.0
    A = wsub * hsub.sum(-1)
    W = wsub * wet.sum(-1)
    nsub = np.sqrt(np.maximum(nf2, 1e-12))
    K = wsub * np.where(wet, hsub ** (5.0 / 3.0) / nsub, 0.0).sum(-1)
    return A, W, K
