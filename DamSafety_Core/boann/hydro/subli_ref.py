"""CPU reference for Boann's sub-grid local-inertial solver (float64 numpy; the oracle that
kernels_subli.metal mirrors, as sgc_ref.py is for the sub-grid-channel engine).

The scheme is the local-inertial ("acceleration") step of kernels.metal run on a COARSE grid whose
cells keep their r x r fine (LiDAR) sub-cells as stage tables (subgrid.py). It follows Nithila Devi
and Kuiry (2024, Water Resources Research 60, e2023WR035334, "A novel local-inertial formulation
representing subgrid scale topographic effects for urban flood simulation"):

  - one free surface eta per coarse cell; the cell stores the wetted VOLUME under it, the sum over
    its sub-cells of max(eta - z, 0) (their Eq. 10);
  - a face is the COMMON EDGE of the two cells, each sub-segment at the higher of the two sub-beds
    that meet there, wetted at the higher of the two cells' surfaces (their Eq. 11);
  - continuity updates the volume from the face discharges and eta is recovered from the volume
    (their Eq. 6); the fine depth map is max(eta - z, 0) per sub-cell (their Eq. 12);
  - the discharge of a face is blended with its UPWIND neighbour face by an adaptive weight
    theta = 1 - (dt / dx) min(|u|, c) (their Eq. 7 and 8, after Sridharan et al. 2020).

and departs from the paper in two places, on purpose:

  - STAGE CURVES ARE EXACT. The paper fits a polynomial per cell (2nd degree, face area against
    level; 3rd degree, level against volume). Here volume and face area are evaluated from the
    sub-cells themselves, so there is no fit error and eta(V) is the exact inverse of V(eta).
  - FRICTION USES THE COMPOUND CONVEYANCE of the face, K = sum over its sub-segments of
    w h^(5/3) / n, not one mean depth. The paper's h_flow = (face area) / dX spreads a narrow
    channel over the whole cell width before applying Manning, so a channel k of r sub-cells wide
    runs (r / k)^0.4 too deep in bank (1.74 times for 1 of 4). With K the in-bank depth is the
    Manning normal depth (tests/test_subli.py pins both numbers). `friction="mean"` selects the
    paper's form, for tests and comparisons only.

Both reduce to the paper's result on a uniformly wet face. Consistently, the wave speed in theta is
that of the hydraulic depth A / W (W the wetted width of the face), which is the paper's
sqrt(g h_flow) when the whole face is wet.

Per face, with Q the whole-face discharge (m3/s, + toward +axis), A its wetted area, S the surface
slope (eta_r - eta_l) / dx and Qv = |Q| (or the 2-D discharge vector, as LISFLOOD-FP 8):

    Q_new = (theta Q + (1 - theta) Q_upwind - g A dt S) / (1 + g A dt Qv / K^2)

On flat sub-cells (r = 1, or a fine DEM with no relief inside a cell) A = dx h_flow and
K = dx h_flow^(5/3) / n, and with `weighting="none"` this is the plain engine's face update term for
term: a flat run equals the plain ACC engine at its default theta 1, and with 1-D friction and the
mass-conserving inflow order it equals subfv_ref.py (and through it sgc_ref.py) step for step.

The weighting, measured (tests/test_subli_kernel.py, engine_subli.py): it blends a face with the
face upwind ALONG THE GRID AXIS, which is the right neighbour only where the flow runs along the
grid. On a reach that crosses the grid at an angle that neighbour lies partly outside the channel
and the blend takes momentum out of the flow: the 5 m meandering test valley on 20 m cells reads
about 0.2 m deep with it and within 2 cm of the 5 m run without it. So the engine runs
`weighting="none"` unless the deck asks for 'upwind'. Two more things the weighting needs, both
found by running it:

  - an open (FREE) edge has to take the same blend with the next face inward (see _free);
  - the faces of a cell a point source is feeding must NOT be weighted (see _q): the paper feeds
    its models through boundaries, a breach deck through point sources.

`weighting="centred"` (theta < 1) is LISFLOOD-FP's q-centred form (de Almeida et al. 2012), the
plain engine's damped step. It is in this reference only, to tie the neighbour-face bookkeeping to
the shipped ACC kernel on flat cells; the kernel does not carry it (on sub-grid faces of unequal
area it hands a thin face a share of a deep neighbour's discharge).
"""
import numpy as np

from . import subgrid as sg

G = sg.G
DRY = 1e-3
MAX_HFLOW = 10.0
FREE_N, FREE_S, FREE_E, FREE_W = 1, 2, 4, 8
WEIGHTINGS = ("upwind", "centred", "none")
FRICTIONS = ("compound", "mean")


def face_hydraulics(etaface, zf, nf2, wsub, maxhflow=MAX_HFLOW, friction="compound"):
    """Wet area A, wet width W, conveyance K and deepest sub-segment depth of a line of faces.

    etaface : (nf,) the controlling surface (the higher of the two cells').
    zf, nf2 : (nf, r) the common-edge sub-bed and Manning n^2 of each sub-segment.
    Each sub-segment's flow depth is capped at maxhflow (LISFLOOD-FP's MaxHflow, as the plain
    engine caps h_flow). friction "compound": K = sum w h^(5/3) / n. "mean" (the paper's Eq. 9):
    K = dX h_flow^(5/3) / n_mean with h_flow = A / dX, dX the whole face length.
    """
    hsub = np.clip(etaface[..., None] - zf, 0.0, maxhflow)
    wet = hsub > 0.0
    A = wsub * hsub.sum(-1)
    W = wsub * wet.sum(-1)
    hdeep = hsub.max(-1)
    if friction == "mean":
        length = wsub * zf.shape[-1]
        n_mean = np.sqrt(np.maximum(nf2.mean(-1), 1e-12))
        K = length * (A / length) ** (5.0 / 3.0) / n_mean
    else:
        nsub = np.sqrt(np.maximum(nf2, 1e-12))
        K = wsub * np.where(wet, hsub ** (5.0 / 3.0) / nsub, 0.0).sum(-1)
    return A, W, K, hdeep


class SubLIRef:
    """A 2-D sub-grid local-inertial run on the tables of subgrid.Tables."""

    def __init__(self, tables, ok=None, free_mask=0, weighting="upwind", theta=1.0, fric2d=True,
                 inflow_lf_order=True, maxhflow=MAX_HFLOW, friction="compound", dry=DRY):
        if weighting not in WEIGHTINGS:
            raise ValueError("weighting must be one of {}".format(WEIGHTINGS))
        if friction not in FRICTIONS:
            raise ValueError("friction must be one of {}".format(FRICTIONS))
        self.t = tables
        self.ny, self.nx = tables.ny, tables.nx
        self.dx = tables.dx
        self.ok = np.ones((self.ny, self.nx), bool) if ok is None else np.asarray(ok, bool)
        self.free_mask = int(free_mask)
        self.weighting = weighting
        self.theta = float(theta)
        self.fric2d = bool(fric2d)
        self.inflow_lf_order = bool(inflow_lf_order)
        self.maxhflow = float(maxhflow)
        self.friction = friction
        self.dry = float(dry)                  # a face whose deepest sub-segment is shallower is shut
        self.zfx, self.nf2x, self.zfy, self.nf2y = sg.all_faces(tables)
        self.V = np.zeros((self.ny, self.nx))
        self.eta = tables.zbed.copy()          # dry: the surface sits on the cell's lowest sub-cell
        self.qx = np.zeros((self.ny, self.nx + 1))
        self.qy = np.zeros((self.ny + 1, self.nx))
        self.vx = np.zeros((self.ny, self.nx + 1))     # face velocities of the last step (Q / A)
        self.vy = np.zeros((self.ny + 1, self.nx))
        self.clipped = 0.0

    # -- state -------------------------------------------------------------------------------

    def set_eta(self, eta):
        eta = np.where(self.ok, np.maximum(np.asarray(eta, float), self.t.zbed), self.t.zbed)
        self.V = sg.cell_volume(eta, self.t.s, self.t.asub)
        self.eta = eta

    def set_volume(self, V):
        self.V = np.where(self.ok, np.maximum(np.asarray(V, float), 0.0), 0.0)
        self.eta = np.where(self.ok, sg.invert_eta(self.V, self.t.s, self.t.csum, self.t.asub),
                            self.t.zbed)

    def volume(self):
        return self.V

    def depth(self):
        """Water depth above each cell's lowest sub-cell."""
        return np.maximum(self.eta - self.t.zbed, 0.0)

    def max_depth(self):
        h = self.depth()
        return max(float(np.max(h)) if h.size else 0.0, DRY)

    # -- one face line ------------------------------------------------------------------------

    def _q(self, q0, qup, qdn, qperp, eta_l, eta_r, zf, nf2, both_ok, dt, blend):
        """New discharge of a block of interior faces (the + cell is 'r'). blend: False on the
        faces of a cell a point source is feeding, which are never weighted. The water there
        comes out of the source, not from the face upwind; blended with that face (which carries
        little or nothing) the source cell cannot pass its inflow on and mounds, by metres at a
        breach-sized inflow."""
        dx = self.dx
        A, W, K, hdeep = face_hydraulics(np.maximum(eta_l, eta_r), zf, nf2, self.t.wsub,
                                         self.maxhflow, self.friction)
        wet = both_ok & (hdeep > self.dry)
        A_ = np.maximum(A, 1e-12)
        K2 = np.maximum(K * K, 1e-30)
        S = (eta_r - eta_l) / dx
        qv = np.sqrt(q0 * q0 + qperp * qperp) if self.fric2d else np.abs(q0)
        den = 1.0 + G * A_ * dt * qv / K2
        push = G * A_ * dt * S
        plain = (q0 - push) / den
        if self.weighting == "centred" and self.theta < 1.0:
            qc = (self.theta * q0 + 0.5 * (1.0 - self.theta) * (qup + qdn) - push) / den
            # LISFLOOD-FP: where the blended value flows uphill, the semi-implicit one is used
            # (and, being the plain engine's form term for term, no source-cell exemption)
            q = np.where(qc * S > 0.0, plain, qc)
        elif self.weighting == "upwind":
            u = np.abs(q0) / A_
            c = np.sqrt(G * A_ / np.maximum(W, 1e-12))
            th = np.clip(1.0 - dt / dx * np.minimum(u, c), 0.0, 1.0)
            qn = np.where(q0 > 0.0, qup, np.where(q0 < 0.0, qdn, q0))
            qc = (th * q0 + (1.0 - th) * qn - push) / den
            q = np.where((qc * S > 0.0) | ~blend, plain, qc)
        else:
            q = plain
        q = np.where(wet, q, 0.0)
        v = np.where(q != 0.0, q / A_, 0.0)
        return q, v

    def _free(self, eta_c, eta_in, zf, nf2, q_old, q_inner, okc, dt, blend):
        """FREE-edge outflow MAGNITUDE (>= 0): the edge cell drained by the surface slope to its
        inner neighbour through the edge's own sub-bed profile (the plain engine's mf_free).

        q_inner is the discharge of the next face inward, + toward the edge. With the upwind
        weighting it is this face's upwind neighbour, and the edge takes the same blend as an
        interior face. It has to: the weighting carries discharge down a reach on a flatter
        surface than friction alone would need, and an edge that answered only to the local slope
        would not pass it on - the reach then fills level behind the open edge and empties in
        surges (tests/test_subli.py subli_narrow_channel_runs_at_manning_normal_depth)."""
        A, W, K, hdeep = face_hydraulics(eta_c, zf, nf2, self.t.wsub, self.maxhflow, self.friction)
        A_ = np.maximum(A, 1e-12)
        K2 = np.maximum(K * K, 1e-30)
        q0 = np.abs(q_old)
        qb = q0
        if self.weighting == "upwind":
            u = q0 / A_
            c = np.sqrt(G * A_ / np.maximum(W, 1e-12))
            th = np.clip(1.0 - dt / self.dx * np.minimum(u, c), 0.0, 1.0)
            qb = np.where(blend, th * q0 + (1.0 - th) * np.maximum(q_inner, 0.0), q0)
        q = (qb + G * A_ * dt * np.abs(eta_c - eta_in) / self.dx) / (1.0 + G * A_ * dt * q0 / K2)
        return np.where(okc & (hdeep > self.dry), q, 0.0)

    def _faces(self, dt, src):
        eta, ok = self.eta, self.ok
        dry = src == 0.0                       # cells no source is feeding this step
        ny, nx = self.ny, self.nx
        qxo, qyo = self.qx, self.qy
        qx = np.zeros((ny, nx + 1)); vx = np.zeros((ny, nx + 1))
        qy = np.zeros((ny + 1, nx)); vy = np.zeros((ny + 1, nx))
        if nx > 1:
            q0 = qxo[:, 1:nx]
            qup = qxo[:, 0:nx - 1].copy()
            qdn = qxo[:, 2:nx + 1].copy()
            # a closed domain edge carries no discharge: the first / last interior face then takes
            # its own value for the missing neighbour (as the plain engine)
            qup[:, 0] = np.where(qup[:, 0] == 0.0, q0[:, 0], qup[:, 0])
            qdn[:, -1] = np.where(qdn[:, -1] == 0.0, q0[:, -1], qdn[:, -1])
            qperp = 0.25 * (qyo[:-1, :-1] + qyo[:-1, 1:] + qyo[1:, :-1] + qyo[1:, 1:])
            qx[:, 1:nx], vx[:, 1:nx] = self._q(
                q0, qup, qdn, qperp, eta[:, :-1], eta[:, 1:], self.zfx[:, 1:nx], self.nf2x[:, 1:nx],
                ok[:, :-1] & ok[:, 1:], dt, dry[:, :-1] & dry[:, 1:])
        if ny > 1:
            q0 = qyo[1:ny, :]
            qup = qyo[0:ny - 1, :].copy()
            qdn = qyo[2:ny + 1, :].copy()
            qup[0, :] = np.where(qup[0, :] == 0.0, q0[0, :], qup[0, :])
            qdn[-1, :] = np.where(qdn[-1, :] == 0.0, q0[-1, :], qdn[-1, :])
            qperp = 0.25 * (qxo[:-1, :-1] + qxo[:-1, 1:] + qxo[1:, :-1] + qxo[1:, 1:])
            qy[1:ny, :], vy[1:ny, :] = self._q(
                q0, qup, qdn, qperp, eta[:-1, :], eta[1:, :], self.zfy[1:ny, :], self.nf2y[1:ny, :],
                ok[:-1, :] & ok[1:, :], dt, dry[:-1, :] & dry[1:, :])
        fm = self.free_mask
        if (fm & FREE_W) and nx > 1:
            qx[:, 0] = -self._free(eta[:, 0], eta[:, 1], self.zfx[:, 0], self.nf2x[:, 0],
                                   qxo[:, 0], -qxo[:, 1], ok[:, 0], dt, dry[:, 0])
        if (fm & FREE_E) and nx > 1:
            qx[:, nx] = self._free(eta[:, -1], eta[:, -2], self.zfx[:, nx], self.nf2x[:, nx],
                                   qxo[:, nx], qxo[:, nx - 1], ok[:, -1], dt, dry[:, -1])
        if (fm & FREE_N) and ny > 1:
            qy[0, :] = -self._free(eta[0, :], eta[1, :], self.zfy[0, :], self.nf2y[0, :],
                                   qyo[0, :], -qyo[1, :], ok[0, :], dt, dry[0, :])
        if (fm & FREE_S) and ny > 1:
            qy[ny, :] = self._free(eta[-1, :], eta[-2, :], self.zfy[ny, :], self.nf2y[ny, :],
                                   qyo[ny, :], qyo[ny - 1, :], ok[-1, :], dt, dry[-1, :])
        return qx, qy, vx, vy

    # -- one step -----------------------------------------------------------------------------

    def step(self, dt, src=None):
        """Advance one step of dt. src: (ny, nx) m3/s. Returns the volume that left through the
        FREE edges (m3)."""
        ny, nx = self.ny, self.nx
        src = np.zeros((ny, nx)) if src is None else np.asarray(src, float)
        qx, qy, vx, vy = self._faces(dt, src)
        V = self.V
        # donor-cell limiter: a cell never sends more water than it holds. In LISFLOOD-FP's order
        # (inflow_lf_order) this step's inflow is not yet available to flow out.
        out = (np.maximum(qx[:, 1:], 0.0) + np.maximum(-qx[:, :-1], 0.0)
               + np.maximum(qy[1:, :], 0.0) + np.maximum(-qy[:-1, :], 0.0)) * dt
        avail = V + (0.0 if self.inflow_lf_order else src * dt)
        scl = np.where((out > avail) & (out > 0.0), avail / np.maximum(out, 1e-300), 1.0)
        sx = np.ones((ny, nx + 1))
        sy = np.ones((ny + 1, nx))
        sx[:, 1:nx] = np.where(qx[:, 1:nx] > 0, scl[:, :nx - 1],
                               np.where(qx[:, 1:nx] < 0, scl[:, 1:nx], 1.0))
        sx[:, 0] = np.where(qx[:, 0] < 0, scl[:, 0], 1.0)
        sx[:, nx] = np.where(qx[:, nx] > 0, scl[:, nx - 1], 1.0)
        sy[1:ny, :] = np.where(qy[1:ny, :] > 0, scl[:ny - 1, :],
                               np.where(qy[1:ny, :] < 0, scl[1:ny, :], 1.0))
        sy[0, :] = np.where(qy[0, :] < 0, scl[0, :], 1.0)
        sy[ny, :] = np.where(qy[ny, :] > 0, scl[ny - 1, :], 1.0)
        qx = qx * sx
        qy = qy * sy
        out_edge = dt * (np.maximum(qx[:, nx], 0.0).sum() + np.maximum(-qx[:, 0], 0.0).sum()
                         + np.maximum(qy[ny, :], 0.0).sum() + np.maximum(-qy[0, :], 0.0).sum())
        Vn = V + dt * (qx[:, :-1] - qx[:, 1:] + qy[:-1, :] - qy[1:, :]) + src * dt
        self.clipped += float(np.maximum(-Vn, 0.0).sum())
        Vn = np.where(self.ok, np.maximum(Vn, 0.0), 0.0)
        self.V = Vn
        self.eta = np.where(self.ok, sg.invert_eta(Vn, self.t.s, self.t.csum, self.t.asub),
                            self.t.zbed)
        self.qx, self.qy, self.vx, self.vy = qx, qy, vx, vy
        return out_edge

    def speed(self):
        """Cell speed as the engine records it: |(largest |v| of its two x faces, of its two y
        faces)|, from the face velocities of the last step."""
        vx = np.maximum(np.abs(self.vx[:, :-1]), np.abs(self.vx[:, 1:]))
        vy = np.maximum(np.abs(self.vy[:-1, :]), np.abs(self.vy[1:, :]))
        return np.sqrt(vx * vx + vy * vy)

    def run(self, t_end, src=None, cfl=0.7, dt_max=None):
        """Advance to t_end with the engine's time step, cfl dx / sqrt(g h_deepest)."""
        t = 0.0
        while t < t_end - 1e-9:
            dt = cfl * self.dx / np.sqrt(G * self.max_depth())
            if dt_max:
                dt = min(dt, dt_max)
            dt = min(dt, t_end - t)
            self.step(dt, src=src)
            t += dt
        return t
