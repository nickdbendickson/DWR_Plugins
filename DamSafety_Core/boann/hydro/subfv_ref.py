"""CPU reference for the sub-grid finite-volume solver (RESEARCH).

A local-inertial (ACC-family) shallow-water solver whose cell storage and face conveyance come from
the sub-grid stage tables of subgrid.py instead of a flat cell. It is the float64 oracle a Metal/CPU
kernel will mirror, exactly as sgc_ref.py is for the sub-grid-channel engine - and it is built on the
same local-inertial base as sgc_ref so the two reductions are provable to machine precision:

  - FLAT sub-cells  -> identical to the plain ACC engine (no channels);
  - a rectangular channel -> the SGC storage/area below bank, and the analytic Manning normal depth
    at steady state.

The HLLC finite-volume flux the spec names reads these SAME curves and is the next increment; the
sub-grid tables (the novel part) are scheme-independent, so validating them on the proven local-
inertial base de-risks them first. State is the ABSOLUTE water-surface elevation eta (m); a dry cell
sits at its lowest sub-cell (volume 0). Momentum per face (Bates et al. 2010, compound-section
conveyance K):  q_new = (q_old - g A dt S) / (1 + g A dt |q_old| / K^2),  S = (eta_r - eta_l)/dx.
On a flat face this is the plain ACC update; the friction term g A |q| / K^2 reduces to the Manning
g n^2 |q| / (R^{4/3} A) exactly.
"""
import numpy as np

from . import subgrid as sg

G = sg.G
DRY = 1e-3
FREE_N, FREE_S, FREE_E, FREE_W = 1, 2, 4, 8


def _face_q(eta_l, eta_r, zf, nf2, q_old, dx, dt, wsub):
    """Signed discharge (m3/s, + toward +axis) for a line of interior faces."""
    etaface = np.maximum(eta_l, eta_r)
    A, W, K = sg.face_hydraulics(etaface, zf, nf2, wsub)
    S = (eta_r - eta_l) / dx
    A_ = np.maximum(A, 1e-12)
    K2 = np.maximum(K * K, 1e-18)
    q = (q_old - G * A_ * dt * S) / (1.0 + G * A_ * dt * np.abs(q_old) / K2)
    return np.where(A > 1e-9, q, 0.0)


def _free_q(eta_c, eta_in, zf, nf2, q_old, dx, dt, wsub):
    """FREE-edge outflow MAGNITUDE (>= 0): the edge cell drained by the surface slope to its
    inner neighbour (mirrors sgc_ref._free_mag)."""
    A, W, K = sg.face_hydraulics(eta_c, zf, nf2, wsub)
    slope = np.abs(eta_c - eta_in) / dx
    A_ = np.maximum(A, 1e-12)
    K2 = np.maximum(K * K, 1e-18)
    q = (np.abs(q_old) + G * A_ * dt * slope) / (1.0 + G * A_ * dt * np.abs(q_old) / K2)
    return np.where(A > 1e-9, q, 0.0)


class SubFVRef:
    """A 2-D sub-grid local-inertial run on the tables of subgrid.Tables."""

    def __init__(self, tables, ok=None, free_mask=0):
        self.t = tables
        self.ny, self.nx = tables.ny, tables.nx
        self.dx = tables.dx
        self.ok = np.ones((self.ny, self.nx), bool) if ok is None else np.asarray(ok, bool)
        self.free_mask = int(free_mask)
        self.eta = tables.zbed.copy()          # start dry: water surface at the cell's lowest sub-cell
        self.qx = np.zeros((self.ny, self.nx + 1))
        self.qy = np.zeros((self.ny + 1, self.nx))

    def set_eta(self, eta):
        self.eta = np.where(self.ok, np.asarray(eta, float), self.t.zbed)

    def set_depth_above_bank(self, h):
        """Convenience: set eta = zbank + h (h may be negative, i.e. water in the channel)."""
        self.eta = np.where(self.ok, self.t.zbank + np.asarray(h, float), self.t.zbed)

    def volume(self):
        return sg.cell_volume(self.eta, self.t.s, self.t.asub)

    def _faces(self, dt):
        t, eta, dx = self.t, self.eta, self.dx
        ny, nx = self.ny, self.nx
        qx = np.zeros((ny, nx + 1))
        qy = np.zeros((ny + 1, nx))
        okx = self.ok[:, :-1] & self.ok[:, 1:]
        qx[:, 1:nx] = np.where(okx, _face_q(eta[:, :-1], eta[:, 1:], t.zfx, t.nf2x,
                                            self.qx[:, 1:nx], dx, dt, t.wsub), 0.0)
        oky = self.ok[:-1, :] & self.ok[1:, :]
        qy[1:ny, :] = np.where(oky, _face_q(eta[:-1, :], eta[1:, :], t.zfy, t.nf2y,
                                            self.qy[1:ny, :], dx, dt, t.wsub), 0.0)
        fm = self.free_mask
        if (fm & FREE_W) and nx > 1:
            zf, nf2 = t.edges["W"]
            q = _free_q(eta[:, 0], eta[:, 1], zf, nf2, self.qx[:, 0], dx, dt, t.wsub)
            qx[:, 0] = np.where(self.ok[:, 0], -q, 0.0)
        if (fm & FREE_E) and nx > 1:
            zf, nf2 = t.edges["E"]
            q = _free_q(eta[:, -1], eta[:, -2], zf, nf2, self.qx[:, nx], dx, dt, t.wsub)
            qx[:, nx] = np.where(self.ok[:, -1], q, 0.0)
        if (fm & FREE_N) and ny > 1:
            zf, nf2 = t.edges["N"]
            q = _free_q(eta[0, :], eta[1, :], zf, nf2, self.qy[0, :], dx, dt, t.wsub)
            qy[0, :] = np.where(self.ok[0, :], -q, 0.0)
        if (fm & FREE_S) and ny > 1:
            zf, nf2 = t.edges["S"]
            q = _free_q(eta[-1, :], eta[-2, :], zf, nf2, self.qy[ny, :], dx, dt, t.wsub)
            qy[ny, :] = np.where(self.ok[-1, :], q, 0.0)
        return qx, qy

    def step(self, dt, src=None):
        ny, nx, dx = self.ny, self.nx, self.dx
        src = np.zeros((ny, nx)) if src is None else np.asarray(src, float)
        qx, qy = self._faces(dt)
        V = self.volume()
        # donor-cell limiter (volume form): a cell cannot send more than it holds this step
        out = (np.maximum(qx[:, 1:], 0.0) + np.maximum(-qx[:, :-1], 0.0)
               + np.maximum(qy[1:, :], 0.0) + np.maximum(-qy[:-1, :], 0.0)) * dt
        avail = V + np.maximum(src, 0.0) * dt
        scl = np.minimum(np.where(out > avail, avail / np.maximum(out, 1e-30), 1.0), 1.0)
        sx = np.ones((ny, nx + 1))
        sy = np.ones((ny + 1, nx))
        sx[:, 1:nx] = np.where(qx[:, 1:nx] > 0, scl[:, :nx - 1], scl[:, 1:nx])
        sx[:, 0] = np.where(qx[:, 0] < 0, scl[:, 0], 1.0)
        sx[:, nx] = np.where(qx[:, nx] > 0, scl[:, nx - 1], 1.0)
        sy[1:ny, :] = np.where(qy[1:ny, :] > 0, scl[:ny - 1, :], scl[1:ny, :])
        sy[0, :] = np.where(qy[0, :] < 0, scl[0, :], 1.0)
        sy[ny, :] = np.where(qy[ny, :] > 0, scl[ny - 1, :], 1.0)
        qx *= sx
        qy *= sy
        out_edge = dt * (np.maximum(qx[:, nx], 0.0).sum() + np.maximum(-qx[:, 0], 0.0).sum()
                         + np.maximum(qy[ny, :], 0.0).sum() + np.maximum(-qy[0, :], 0.0).sum())
        dV = dt * (qx[:, :-1] - qx[:, 1:] + qy[:-1, :] - qy[1:, :]) + src * dt
        V = V + dV
        self.eta = np.where(self.ok, sg.invert_eta(V, self.t.s, self.t.csum, self.t.asub),
                            self.t.zbed)
        self.qx, self.qy = qx, qy
        return out_edge

    def max_depth(self):
        """Largest water depth over any wet sub-cell column, for a CFL time step."""
        h = self.eta - self.t.zbed
        return max(float(np.max(h)) if h.size else 0.0, DRY)

    def run(self, t_end, src=None, cfl=0.4, dt_max=None):
        """Advance to t_end with a still-water CFL step. src: (ny, nx) m3/s added each step."""
        t = 0.0
        while t < t_end - 1e-9:
            dt = cfl * self.dx / np.sqrt(G * self.max_depth())
            if dt_max:
                dt = min(dt, dt_max)
            dt = min(dt, t_end - t)
            self.step(dt, src=src)
            t += dt
        return t
