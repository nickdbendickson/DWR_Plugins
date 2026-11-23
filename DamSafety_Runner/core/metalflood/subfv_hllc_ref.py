"""Shock-capturing CPU reference for the sub-grid finite-volume solver (RESEARCH).

A well-balanced, Godunov HLLC shallow-water solver whose cell storage and face hydraulics come from
the sub-grid stage tables of subgrid.py. It is the float64 oracle a Metal/CPU kernel will mirror,
exactly as subfv_ref.py is for the local-inertial sub-grid engine and kernels_fv.metal is for the
plain HLLC engine. Where subfv_ref.py smooths a dam-break front (the ACC/local-inertial momentum),
this resolves it: the HLLC Riemann flux gives the sharp shock at the analytic wave speed.

The scheme is the sub-grid ("porosity") generalisation of kernels_fv.metal, and reduces to it
EXACTLY on a flat fine DEM, step for step, to ~float64 precision:

  - conserved per cell : water VOLUME V (m3) and the two total momenta Px, Py (m4/s), so the cell
    velocity is u = Px / V. The water-surface elevation eta is V inverted through the sub-grid
    hypsometric curve (subgrid.invert_eta); a dry cell has V = 0 at its lowest sub-cell;
  - per x/y face : Audusse hydrostatic reconstruction on the SHARED sub-bed sill, giving each side's
    wet area A, wet (top) width W and first moment I1 = integral of 1/2 h^2 over the sub-cells. The
    HLLC Riemann flux is solved in cross-sectional (A, Q) space using the hydraulic depth A/W for the
    wave speeds and g*I1 for the hydrostatic pressure, so the flux is the whole-face discharge /
    momentum, not a per-unit-width value;
  - well-balanced Audusse bed source : BedSrc = g*(I1 at the +face - I1 at the -face), reconstructed
    from the cell's own eta. At rest this cancels the face pressure flux to machine precision over any
    sub-grid topography (lake at rest stays at rest), and reduces to the plain 0.5 g (h0^2 - h*^2)
    face source on a flat cell;
  - friction : semi-implicit Manning on the updated cell momentum, with the cell hydraulic depth
    V/P and the cell composite roughness tables.ncell. On uniform roughness this is the plain
    g n^2 |u| / h^{4/3} factor exactly, so a prismatic channel reaches the analytic Manning normal
    depth.

Every face quantity is dx times the plain per-unit-width quantity on a flat cell (A = dx h,
W = dx, I1 = 1/2 dx h^2), and the HLLC algebra is homogeneous in (A, Q, I1), so the flat run is the
plain HLLC run exactly. Absolute elevation throughout; float64 (the GPU kernel is float32).
"""
import numpy as np

from . import subgrid as sg

G = sg.G
DRY = 1e-3
HVEL = 1e-6          # depth floor for velocity desingularisation (matches kernels_fv MF_HVEL)
CFL_CAP = 0.45       # 2-D explicit Godunov stability ceiling (kernels_fv MF_CFL_CAP)
SLOPE_EPS = 1e-6     # FREE-edge normal-depth floor deadband (matches kernels_* MF_SLOPE_EPS): a
#                      surface flat to within this slope has no drainage drive, and sqrt(slope) is
#                      ill-conditioned as slope -> 0, so below it the transmissive flux stands.
FREE_N, FREE_S, FREE_E, FREE_W = 1, 2, 4, 8


def _recon(eta, zf, wsub):
    """Audusse reconstruction of a line of face states at level eta over the sub-bed zf.

    eta : (...,) controlling surface of the cell on this side of the face.
    zf  : (..., r) the shared face sub-bed (max of the two cells' sub-beds, Audusse sill).
    Returns wet area A, wet top width W, first moment I1 = sum wsub * 0.5 * h^2  (each (...,)).
    """
    hsub = np.maximum(eta[..., None] - zf, 0.0)
    A = wsub * hsub.sum(-1)
    W = wsub * (hsub > 0.0).sum(-1)
    I1 = wsub * (0.5 * hsub * hsub).sum(-1)
    return A, W, I1


def _hllc(AL, WL, I1L, uL, vL, AR, WR, I1R, uR, vR):
    """Cross-sectional HLLC flux across a line of faces (Toro, Einfeldt-style wave speeds).

    Normal velocity u (+ toward +axis), tangential velocity v. Mirrors mf_hllc in kernels_fv.metal
    but in (area, discharge) variables: returns the whole-face fluxes
      FA  mass            (m3/s),
      FN  normal momentum (m4/s2)  = sum A u^2 + g I1,
      FT  tangential momentum (m4/s2) = FA * (vL or vR, chosen by the contact),
      smax largest |wave speed| (m/s), for the CFL step.
    Reduces to dx * mf_hllc on a flat cell (A = dx h, I1 = 0.5 dx h^2, W = dx)."""
    dryL = AL <= 0.0
    dryR = AR <= 0.0
    hcL = AL / np.maximum(WL, 1e-12)        # hydraulic (celerity) depth = area / top width
    hcR = AR / np.maximum(WR, 1e-12)
    cL = np.sqrt(G * np.maximum(hcL, 0.0))
    cR = np.sqrt(G * np.maximum(hcR, 0.0))
    # wet/wet two-rarefaction estimate, then the dry-front overrides (as in mf_hllc)
    ustar = 0.5 * (uL + uR) + cL - cR
    cstar = 0.5 * (cL + cR) + 0.25 * (uL - uR)
    SL = np.minimum(uL - cL, ustar - cstar)
    SR = np.maximum(uR + cR, ustar + cstar)
    SL = np.where(dryL, uR - 2.0 * cR, SL)
    SR = np.where(dryL, uR + cR, SR)
    SL = np.where(dryR, uL - cL, SL)
    SR = np.where(dryR, uL + 2.0 * cL, SR)
    # physical fluxes
    FAL = AL * uL
    FAR = AR * uR
    FNL = AL * uL * uL + G * I1L
    FNR = AR * uR * uR + G * I1R
    denom = np.where(np.abs(SR - SL) > 1e-12, SR - SL, 1.0)
    inv = 1.0 / denom
    FA_star = (SR * FAL - SL * FAR + SL * SR * (AR - AL)) * inv
    FN_star = (SR * FNL - SL * FNR + SL * SR * (AR * uR - AL * uL)) * inv
    # contact speed picks which side's tangential velocity the mass carries
    num = SL * AR * (uR - SR) - SR * AL * (uL - SL)
    den = AR * (uR - SR) - AL * (uL - SL)
    Sstar = np.where(np.abs(den) > 1e-12, num / np.where(np.abs(den) > 1e-12, den, 1.0),
                     0.5 * (SL + SR))
    left = SL >= 0.0
    right = SR <= 0.0
    FA = np.where(left, FAL, np.where(right, FAR, FA_star))
    FN = np.where(left, FNL, np.where(right, FNR, FN_star))
    vsel = np.where(left, vL, np.where(right, vR, np.where(Sstar >= 0.0, vL, vR)))
    FT = FA * vsel
    both = dryL & dryR
    FA = np.where(both, 0.0, FA)
    FN = np.where(both, 0.0, FN)
    FT = np.where(both, 0.0, FT)
    smax = np.maximum(np.abs(SL), np.abs(SR))
    smax = np.where(both, 0.0, smax)
    return FA, FN, FT, smax


def _free_und(etac, etain, A, W, nc2, dx):
    """Manning normal-depth outflow velocity driven by the water-surface slope to the inner cell
    (0 where the surface does not slope outward, or the face is dry). The FREE-edge drainage floor:
    at a coarse cell a narrow sub-grid outlet carries flow far faster than the cell's bulk velocity,
    so the transmissive flux under-drains and the valley ponds; this restores it. Mirrors
    mf_subfv_und in kernels_subfv.metal."""
    slope = np.maximum((etain - etac) / dx, 0.0)
    Rh = A / np.maximum(W, 1e-9)
    und = np.sqrt(slope) * Rh ** (2.0 / 3.0) / np.sqrt(np.maximum(nc2, 1e-12))
    return np.where((A > 0.0) & (slope > SLOPE_EPS), und, 0.0)


class SubFVHLLCRef:
    """A 2-D sub-grid shock-capturing (HLLC) run on the tables of subgrid.Tables."""

    def __init__(self, tables, ok=None, free_mask=0, cfl=CFL_CAP):
        self.t = tables
        self.ny, self.nx = tables.ny, tables.nx
        self.dx = tables.dx
        self.ok = np.ones((self.ny, self.nx), bool) if ok is None else np.asarray(ok, bool)
        self.free_mask = int(free_mask)
        self.cfl = min(float(cfl), CFL_CAP)
        self.ncell2 = (tables.ncell ** 2 if tables.ncell is not None
                       else np.zeros((self.ny, self.nx)))
        self.V = np.zeros((self.ny, self.nx))      # start dry (eta at the lowest sub-cell)
        self.Px = np.zeros((self.ny, self.nx))
        self.Py = np.zeros((self.ny, self.nx))
        self.clipped = 0.0                          # spurious volume the V >= 0 clip added
        self._smax = 0.0

    # -- state --------------------------------------------------------------------------------
    @property
    def eta(self):
        e = sg.invert_eta(self.V, self.t.s, self.t.csum, self.t.asub)
        return np.where(self.ok, e, self.t.zbed)

    def set_eta(self, eta):
        self.V = np.where(self.ok, sg.cell_volume(np.asarray(eta, float), self.t.s, self.t.asub), 0.0)
        self.Px[:] = 0.0
        self.Py[:] = 0.0

    def volume(self):
        return self.V

    def _vel(self):
        """Cell x/y velocity, desingularised (a cell with less than HVEL mean depth carries none)."""
        vfloor = HVEL * self.dx * self.dx
        wet = self.V > vfloor
        u = np.where(wet, self.Px / np.where(wet, self.V, 1.0), 0.0)
        v = np.where(wet, self.Py / np.where(wet, self.V, 1.0), 0.0)
        return u, v

    def max_wave_speed(self):
        eta = self.eta
        P = sg.cell_area(eta, self.t.s, self.t.asub)
        hR = np.where(P > 0, self.V / np.maximum(P, 1e-12), 0.0)
        u, v = self._vel()
        c = np.sqrt(G * np.maximum(hR, 0.0))
        w = np.maximum(np.abs(u), np.abs(v)) + c
        return max(float(w.max()) if w.size else 0.0, np.sqrt(G * DRY))

    # -- one step -----------------------------------------------------------------------------
    def _faces(self):
        """HLLC fluxes at every x/y face. Returns FAx,FNx,FTx (ny,nx+1) and FAy,FNy,FTy (ny+1,nx),
        plus I1xL/I1xR (ny,nx+1) and I1yU/I1yD (ny+1,nx) for the well-balanced bed source."""
        t, dx = self.t, self.dx
        ny, nx = self.ny, self.nx
        eta = self.eta
        u, v = self._vel()
        wsub = t.wsub

        FAx = np.zeros((ny, nx + 1))
        FNx = np.zeros((ny, nx + 1))
        FTx = np.zeros((ny, nx + 1))
        FAy = np.zeros((ny + 1, nx))
        FNy = np.zeros((ny + 1, nx))
        FTy = np.zeros((ny + 1, nx))
        I1xL = np.zeros((ny, nx + 1))   # first moment reconstructed from the LEFT cell of each x-face
        I1xR = np.zeros((ny, nx + 1))   # ... from the RIGHT cell
        I1yU = np.zeros((ny + 1, nx))
        I1yD = np.zeros((ny + 1, nx))
        smax = 0.0

        # interior x-faces (between cols j and j+1), normal = +x, tangential = y
        if nx > 1:
            zf = t.zfx                                   # (ny, nx-1, r)
            AL, WL, ilL = _recon(eta[:, :-1], zf, wsub)
            AR, WR, ilR = _recon(eta[:, 1:], zf, wsub)
            okf = self.ok[:, :-1] & self.ok[:, 1:]
            FA, FN, FT, sm = _hllc(AL, WL, ilL, u[:, :-1], v[:, :-1],
                                   AR, WR, ilR, u[:, 1:], v[:, 1:])
            FAx[:, 1:nx] = np.where(okf, FA, 0.0)
            FNx[:, 1:nx] = np.where(okf, FN, 0.0)
            FTx[:, 1:nx] = np.where(okf, FT, 0.0)
            I1xL[:, 1:nx] = ilL
            I1xR[:, 1:nx] = ilR
            smax = max(smax, float(np.max(np.where(okf, sm, 0.0))) if sm.size else 0.0)

        # interior y-faces (between rows i and i+1), normal = +y (south), tangential = x
        if ny > 1:
            zf = t.zfy                                   # (ny-1, nx, r)
            AU, WU, ilU = _recon(eta[:-1, :], zf, wsub)
            AD, WD, ilD = _recon(eta[1:, :], zf, wsub)
            okf = self.ok[:-1, :] & self.ok[1:, :]
            FA, FN, FT, sm = _hllc(AU, WU, ilU, v[:-1, :], u[:-1, :],
                                   AD, WD, ilD, v[1:, :], u[1:, :])
            FAy[1:ny, :] = np.where(okf, FA, 0.0)
            FNy[1:ny, :] = np.where(okf, FN, 0.0)
            FTy[1:ny, :] = np.where(okf, FT, 0.0)
            I1yU[1:ny, :] = ilU
            I1yD[1:ny, :] = ilD
            smax = max(smax, float(np.max(np.where(okf, sm, 0.0))) if sm.size else 0.0)

        # domain-boundary faces: FREE = transmissive outflow with a Manning normal-depth floor (so a
        # coarse cell's diluted bulk velocity still drains the valley), else a reflective wall. Either
        # way the cell reconstructs against its own edge sub-bed; the first moment is stored so the bed
        # source cancels the wall/edge pressure at rest. nc2 = self.ncell2 (per-cell roughness n^2).
        fm = self.free_mask
        nc2 = self.ncell2
        zfw, _ = t.edges["W"]
        Aw, Ww, ilw = _recon(eta[:, 0], zfw, wsub)
        I1xR[:, 0] = ilw                                 # cell 0 is the + side of the west face
        if fm & FREE_W:
            uw = u[:, 0]
            if nx > 1:
                und = _free_und(eta[:, 0], eta[:, 1], Aw, Ww, nc2[:, 0], dx)
                uw = np.where(und > 0.0, np.minimum(uw, -und), uw)   # at least und outward (-x)
            FAx[:, 0] = np.where(self.ok[:, 0], Aw * uw, 0.0)
            FNx[:, 0] = np.where(self.ok[:, 0], Aw * uw * uw + G * ilw, 0.0)
            FTx[:, 0] = np.where(self.ok[:, 0], Aw * uw * v[:, 0], 0.0)
        else:
            FNx[:, 0] = np.where(self.ok[:, 0], G * ilw, 0.0)   # reflective: pressure only

        zfe, _ = t.edges["E"]
        Ae, We, ile = _recon(eta[:, -1], zfe, wsub)
        I1xL[:, nx] = ile                                # cell nx-1 is the - side of the east face
        if fm & FREE_E:
            ue = u[:, -1]
            if nx > 1:
                und = _free_und(eta[:, -1], eta[:, -2], Ae, We, nc2[:, -1], dx)
                ue = np.where(und > 0.0, np.maximum(ue, und), ue)   # at least und outward (+x)
            FAx[:, nx] = np.where(self.ok[:, -1], Ae * ue, 0.0)
            FNx[:, nx] = np.where(self.ok[:, -1], Ae * ue * ue + G * ile, 0.0)
            FTx[:, nx] = np.where(self.ok[:, -1], Ae * ue * v[:, -1], 0.0)
        else:
            FNx[:, nx] = np.where(self.ok[:, -1], G * ile, 0.0)

        zfn, _ = t.edges["N"]
        An, Wn, iln = _recon(eta[0, :], zfn, wsub)
        I1yD[0, :] = iln                                 # row 0 is the + (south) side of the north face
        if fm & FREE_N:
            vn = v[0, :]
            if ny > 1:
                und = _free_und(eta[0, :], eta[1, :], An, Wn, nc2[0, :], dx)
                vn = np.where(und > 0.0, np.minimum(vn, -und), vn)   # at least und outward (-y)
            FAy[0, :] = np.where(self.ok[0, :], An * vn, 0.0)
            FNy[0, :] = np.where(self.ok[0, :], An * vn * vn + G * iln, 0.0)
            FTy[0, :] = np.where(self.ok[0, :], An * vn * u[0, :], 0.0)
        else:
            FNy[0, :] = np.where(self.ok[0, :], G * iln, 0.0)

        zfs, _ = t.edges["S"]
        As, Ws, ils = _recon(eta[-1, :], zfs, wsub)
        I1yU[ny, :] = ils                                # row ny-1 is the - side of the south face
        if fm & FREE_S:
            vs = v[-1, :]
            if ny > 1:
                und = _free_und(eta[-1, :], eta[-2, :], As, Ws, nc2[-1, :], dx)
                vs = np.where(und > 0.0, np.maximum(vs, und), vs)   # at least und outward (+y)
            FAy[ny, :] = np.where(self.ok[-1, :], As * vs, 0.0)
            FNy[ny, :] = np.where(self.ok[-1, :], As * vs * vs + G * ils, 0.0)
            FTy[ny, :] = np.where(self.ok[-1, :], As * vs * u[-1, :], 0.0)
        else:
            FNy[ny, :] = np.where(self.ok[-1, :], G * ils, 0.0)

        self._smax = smax
        return FAx, FNx, FTx, FAy, FNy, FTy, I1xL, I1xR, I1yU, I1yD

    def step(self, dt, src=None):
        """Advance dt seconds. src: (ny, nx) m3/s added to each cell. Returns the edge outflow (m3)."""
        ny, nx, dx = self.ny, self.nx, self.dx
        src = np.zeros((ny, nx)) if src is None else np.asarray(src, float)
        (FAx, FNx, FTx, FAy, FNy, FTy, I1xL, I1xR, I1yU, I1yD) = self._faces()

        # mass: dV = -dt * div(FA) + src dt
        dV = -dt * ((FAx[:, 1:] - FAx[:, :-1]) + (FAy[1:, :] - FAy[:-1, :])) + src * dt
        # well-balanced bed source: g * (I1 at the +face - I1 at the -face), from the cell's own eta
        bedx = G * (I1xL[:, 1:] - I1xR[:, :-1])
        bedy = G * (I1yU[1:, :] - I1yD[:-1, :])
        dPx = -dt * ((FNx[:, 1:] - FNx[:, :-1]) + (FTy[1:, :] - FTy[:-1, :])) + dt * bedx
        dPy = -dt * ((FTx[:, 1:] - FTx[:, :-1]) + (FNy[1:, :] - FNy[:-1, :])) + dt * bedy

        V = self.V + dV
        Px = self.Px + dPx
        Py = self.Py + dPy

        # edge outflow (positive = leaving the domain), for a mass-balance check
        out_edge = dt * (np.maximum(FAx[:, nx], 0.0).sum() + np.maximum(-FAx[:, 0], 0.0).sum()
                         + np.maximum(FAy[ny, :], 0.0).sum() + np.maximum(-FAy[0, :], 0.0).sum())

        # positivity: clip V < 0 (record the spurious volume added), drop that cell's momentum
        neg = V < 0.0
        self.clipped += float((-V[neg]).sum())
        V = np.where(neg, 0.0, V)
        Px = np.where(neg, 0.0, Px)
        Py = np.where(neg, 0.0, Py)

        # semi-implicit Manning friction on the updated momentum (unconditionally stable)
        eta = sg.invert_eta(V, self.t.s, self.t.csum, self.t.asub)
        P = sg.cell_area(np.where(self.ok, eta, self.t.zbed), self.t.s, self.t.asub)
        vfloor = HVEL * dx * dx
        wet = V > vfloor
        u = np.where(wet, Px / np.where(wet, V, 1.0), 0.0)
        v = np.where(wet, Py / np.where(wet, V, 1.0), 0.0)
        hR = np.where(P > 0, V / np.maximum(P, 1e-12), 0.0)
        spd = np.sqrt(u * u + v * v)
        safeh = np.maximum(hR, HVEL)
        cf = 1.0 + G * self.ncell2 * dt * spd / safeh ** (4.0 / 3.0)
        cf = np.where((spd > 0.0) & (hR > HVEL), cf, 1.0)
        Px = Px / cf
        Py = Py / cf

        # keep masked / dry cells clean
        Px = np.where(self.ok & wet, Px, 0.0)
        Py = np.where(self.ok & wet, Py, 0.0)
        V = np.where(self.ok, V, 0.0)
        self.V, self.Px, self.Py = V, Px, Py
        return out_edge

    def run(self, t_end, src=None, dt_max=None):
        """Advance to t_end with a wave-speed CFL step (dt = cfl dx / max(|u| + c))."""
        t = 0.0
        while t < t_end - 1e-9:
            w = self.max_wave_speed()
            dt = self.cfl * self.dx / max(w, 1e-9)
            if dt_max:
                dt = min(dt, dt_max)
            dt = min(dt, t_end - t)
            self.step(dt, src=src)
            t += dt
        return t
