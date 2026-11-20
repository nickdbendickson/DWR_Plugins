"""2-D numpy reference for sub-grid channels (SGC) on the local-inertial (ACC) scheme - the oracle
the Metal/CPU SGC kernels will mirror cell-by-cell, exactly as metalflood's builtin reference does
for the plain ACC engine. Generalises the validated 1-D prototype (sgc_proto.py) to the engine's
2-D grid: x-faces (ny, nx+1) and y-faces (ny+1, nx), each carrying a channel discharge and a
floodplain discharge in m3/s (not per-unit-width).

State h[r,c] = water surface RELATIVE TO THE DEM z[r,c] (the bank). In a channel cell (w>0) h goes
NEGATIVE: the bed sits D below the DEM, so h in [-D, 0] is water IN the channel, h > 0 is water on
the floodplain. w=0 cells behave exactly like plain ACC (storage dx^2 h, floodplain-only faces).

A face is a CHANNEL face when BOTH its cells have w>0; its channel width is min(w_l, w_r) and the
floodplain there is the rest of the cell width (Neal et al. 2012 Fig 1C). Matches LISFLOOD-FP 8.3
SGC2 (sgm_fast.cpp): CalculateQ, SGC2_CalcA/CalcR, the storage stage<->volume, and Q = Q_SG + Q_FP.

This runs float64 (the reference); the kernels run float32. Float32 care mirrors the ACC engine: the
surface slope is ((z_r - z_l) + (h_r - h_l))/dx, never eta_r - eta_l of two large numbers.
"""
import numpy as np

G = 9.80665
DRY = 1e-3
MAXHF = 10.0
FREE_N, FREE_S, FREE_E, FREE_W = 1, 2, 4, 8


def storage(h, w, D, dx):
    """Cell water volume (m3) for water level above DEM h (negative in channel)."""
    chan = w > 0
    Vbank = w * dx * D
    below = chan & (h < 0.0)
    return np.where(below, w * dx * np.maximum(h + D, 0.0),
                    np.where(chan, Vbank + dx * dx * np.maximum(h, 0.0),
                             dx * dx * np.maximum(h, 0.0)))


def invert(V, w, D, dx):
    """Water level above DEM h from cell volume V (inverse of storage)."""
    chan = w > 0
    Vbank = w * dx * D
    wdx = np.where(chan, w * dx, 1.0)
    h_in = np.where(chan, V / wdx - D, 0.0)
    h_above = np.where(chan, (V - Vbank) / (dx * dx), 0.0)
    h_fp = V / (dx * dx)
    return np.where(~chan, h_fp, np.where(V <= Vbank, h_in, h_above))


def _calcQ(q_old, A, R, S, n2, dt):
    """LISFLOOD-FP SGC2 CalculateQ. S = surface slope (eta_r - eta_l)/dx (q>0 flows +axis)."""
    R = np.maximum(R, 1e-9)
    A = np.maximum(A, 1e-12)
    return (q_old - G * A * dt * S) / (1.0 + dt * G * n2 * np.abs(q_old) / (R ** (4.0 / 3.0) * A))


def _axis_Q(hl, hr, zl, zr, wl, wr, Dl, Dr, qc_old, qf_old, nchan2, nfp2, dx, dt):
    """Channel + floodplain discharge (m3/s) for a line of faces between left cells (hl,zl,wl,Dl)
    and right cells (hr,zr,wr,Dr). Arrays of equal shape; returns (qc, qf)."""
    etal, etar = zl + hl, zr + hr
    bedl = zl - np.where(wl > 0, Dl, 0.0)
    bedr = zr - np.where(wr > 0, Dr, 0.0)
    etamax = np.maximum(etal, etar)
    S = ((zr - zl) + (hr - hl)) / dx
    # channel: both cells must have a channel
    wface = np.minimum(wl, wr)
    wface = np.where((wl > 0) & (wr > 0), wface, 0.0)
    Dface = np.where(wface > 0, 0.5 * (Dl + Dr), 0.0)
    bedmax = np.maximum(bedl, bedr)
    chan_h = np.minimum(etamax - bedmax, MAXHF)
    has_ch = (wface > 0) & (chan_h > DRY)
    A_c = np.where(has_ch, wface * chan_h, 0.0)
    R_c = np.where(has_ch, A_c / (wface + 2.0 * np.minimum(chan_h, Dface) + 1e-12), 0.0)
    qc = np.where(has_ch, _calcQ(qc_old, A_c, R_c, S, nchan2, dt), 0.0)
    # floodplain: above bank only
    hflow = np.minimum(etamax - np.maximum(zl, zr), MAXHF)
    wratio = np.minimum(np.where(wface > 0, wface / dx, 0.0), 1.0)
    has_fp = hflow > DRY
    Wfp = dx * (1.0 - wratio)
    A_f = np.where(has_fp, Wfp * hflow, 0.0)
    qf = np.where(has_fp, _calcQ(qf_old, A_f, np.maximum(hflow, 1e-9), S, nfp2, dt) * (1.0 - wratio),
                  0.0)
    return qc, qf


def _free_mag(zc, hc, wc, Dc, etain, qc_old, qf_old, nchan2, nfp2, dx, dt):
    """Channel + floodplain FREE-edge outflow magnitude (>=0) of an edge line (mirrors
    kernels_sgc.sgc_free): the edge cell depth driven by the surface slope to its inner neighbour."""
    etac = zc + hc
    slope = np.abs(etac - etain) / dx
    bed = zc - np.where(wc > 0, Dc, 0.0)
    chan_h = np.minimum(etac - bed, MAXHF)
    has_ch = (wc > 0) & (chan_h > DRY)
    A_c = np.where(has_ch, wc * chan_h, 0.0)
    R_c = np.where(has_ch, A_c / (wc + 2.0 * np.minimum(chan_h, Dc) + 1e-12), 0.0)
    qc = np.where(has_ch, (np.abs(qc_old) + G * dt * A_c * slope)
                  / (1.0 + G * dt * nchan2 * np.abs(qc_old)
                     / (np.maximum(R_c, 1e-9) ** (4.0 / 3.0) * np.maximum(A_c, 1e-12))), 0.0)
    hflow = np.minimum(np.maximum(hc, 0.0), MAXHF)
    has_fp = hflow > DRY
    wratio = np.minimum(np.where(wc > 0, wc / dx, 0.0), 1.0)
    A_f = np.where(has_fp, dx * (1.0 - wratio) * hflow, 0.0)
    qf = np.where(has_fp, (np.abs(qf_old) + G * dt * A_f * slope)
                  / (1.0 + G * dt * nfp2 * np.abs(qf_old)
                     / (np.maximum(hflow, 1e-9) ** (4.0 / 3.0) * np.maximum(A_f, 1e-12)))
                  * (1.0 - wratio), 0.0)
    return qc, qf


class SGCRef:
    """A 2-D SGC local-inertial run, state and stepping laid out as the kernels will keep them."""

    def __init__(self, z, w, D, nchan, nfp, dx, ok=None, free_mask=0):
        self.z = np.asarray(z, float)
        self.ny, self.nx = self.z.shape
        self.w = np.asarray(w, float)
        self.D = np.asarray(D, float)
        self.nchan2 = float(nchan) ** 2
        self.nfp2 = float(nfp) ** 2
        self.dx = float(dx)
        self.ok = np.ones(self.z.shape, bool) if ok is None else np.asarray(ok, bool)
        self.free_mask = int(free_mask)
        self.h = np.where(self.ok, -self.D * (self.w > 0), 0.0)   # channels empty, plain cells dry
        ny, nx = self.ny, self.nx
        self.qxc = np.zeros((ny, nx + 1)); self.qxf = np.zeros((ny, nx + 1))
        self.qyc = np.zeros((ny + 1, nx)); self.qyf = np.zeros((ny + 1, nx))
        self.vmax = np.zeros((ny, nx))

    def set_h(self, h):
        self.h = np.where(self.ok, np.asarray(h, float), 0.0)

    def _faces(self, dt):
        z, h, w, D, dx = self.z, self.h, self.w, self.D, self.dx
        ny, nx = self.ny, self.nx
        qxc = np.zeros((ny, nx + 1)); qxf = np.zeros((ny, nx + 1))
        qyc = np.zeros((ny + 1, nx)); qyf = np.zeros((ny + 1, nx))
        # interior x-faces: between (r, c-1) and (r, c), c = 1..nx-1
        okx = self.ok[:, :-1] & self.ok[:, 1:]
        qc, qf = _axis_Q(h[:, :-1], h[:, 1:], z[:, :-1], z[:, 1:], w[:, :-1], w[:, 1:],
                         D[:, :-1], D[:, 1:], self.qxc[:, 1:nx], self.qxf[:, 1:nx],
                         self.nchan2, self.nfp2, dx, dt)
        qxc[:, 1:nx] = np.where(okx, qc, 0.0); qxf[:, 1:nx] = np.where(okx, qf, 0.0)
        # interior y-faces: between (r-1, c) and (r, c), r = 1..ny-1
        oky = self.ok[:-1, :] & self.ok[1:, :]
        qc, qf = _axis_Q(h[:-1, :], h[1:, :], z[:-1, :], z[1:, :], w[:-1, :], w[1:, :],
                         D[:-1, :], D[1:, :], self.qyc[1:ny, :], self.qyf[1:ny, :],
                         self.nchan2, self.nfp2, dx, dt)
        qyc[1:ny, :] = np.where(oky, qc, 0.0); qyf[1:ny, :] = np.where(oky, qf, 0.0)
        # FREE edges (outflow magnitude, with the outward sign)
        fm = self.free_mask
        if (fm & FREE_W) and nx > 1:
            qc, qf = _free_mag(z[:, 0], h[:, 0], w[:, 0], D[:, 0], z[:, 1] + h[:, 1],
                               self.qxc[:, 0], self.qxf[:, 0], self.nchan2, self.nfp2, dx, dt)
            m = self.ok[:, 0]
            qxc[:, 0] = np.where(m, -qc, 0.0); qxf[:, 0] = np.where(m, -qf, 0.0)
        if (fm & FREE_E) and nx > 1:
            qc, qf = _free_mag(z[:, -1], h[:, -1], w[:, -1], D[:, -1], z[:, -2] + h[:, -2],
                               self.qxc[:, nx], self.qxf[:, nx], self.nchan2, self.nfp2, dx, dt)
            m = self.ok[:, -1]
            qxc[:, nx] = np.where(m, qc, 0.0); qxf[:, nx] = np.where(m, qf, 0.0)
        if (fm & FREE_N) and ny > 1:
            qc, qf = _free_mag(z[0, :], h[0, :], w[0, :], D[0, :], z[1, :] + h[1, :],
                               self.qyc[0, :], self.qyf[0, :], self.nchan2, self.nfp2, dx, dt)
            m = self.ok[0, :]
            qyc[0, :] = np.where(m, -qc, 0.0); qyf[0, :] = np.where(m, -qf, 0.0)
        if (fm & FREE_S) and ny > 1:
            qc, qf = _free_mag(z[-1, :], h[-1, :], w[-1, :], D[-1, :], z[-2, :] + h[-2, :],
                               self.qyc[ny, :], self.qyf[ny, :], self.nchan2, self.nfp2, dx, dt)
            m = self.ok[-1, :]
            qyc[ny, :] = np.where(m, qc, 0.0); qyf[ny, :] = np.where(m, qf, 0.0)
        return qxc, qxf, qyc, qyf

    def step(self, dt, src=None):
        ny, nx, dx = self.ny, self.nx, self.dx
        src = np.zeros((ny, nx)) if src is None else np.asarray(src, float)
        qxc, qxf, qyc, qyf = self._faces(dt)
        Qx = qxc + qxf
        Qy = qyc + qyf
        V = storage(self.h, self.w, self.D, dx)
        # donor-cell limiter in volume: a cell cannot send more than it holds this step
        out = (np.maximum(Qx[:, 1:], 0.0) + np.maximum(-Qx[:, :-1], 0.0)
               + np.maximum(Qy[1:, :], 0.0) + np.maximum(-Qy[:-1, :], 0.0)) * dt
        avail = V + np.maximum(src, 0.0) * dt
        scl = np.where(out > avail, avail / np.maximum(out, 1e-30), 1.0)
        scl = np.minimum(scl, 1.0)
        # scale each face by its donor cell's share (interior + the FREE-edge boundary faces)
        sx = np.ones((ny, nx + 1)); sy = np.ones((ny + 1, nx))
        posx = Qx[:, 1:nx] > 0
        sx[:, 1:nx] = np.where(posx, scl[:, :nx-1], scl[:, 1:nx])
        sx[:, 0] = np.where(Qx[:, 0] < 0, scl[:, 0], 1.0)          # W edge donor = col 0
        sx[:, nx] = np.where(Qx[:, nx] > 0, scl[:, nx-1], 1.0)      # E edge donor = col nx-1
        posy = Qy[1:ny, :] > 0
        sy[1:ny, :] = np.where(posy, scl[:ny-1, :], scl[1:ny, :])
        sy[0, :] = np.where(Qy[0, :] < 0, scl[0, :], 1.0)
        sy[ny, :] = np.where(Qy[ny, :] > 0, scl[ny-1, :], 1.0)
        qxc *= sx; qxf *= sx; qyc *= sy; qyf *= sy
        Qx = qxc + qxf; Qy = qyc + qyf
        out_edge = dt * (np.maximum(Qx[:, nx], 0.0).sum() + np.maximum(-Qx[:, 0], 0.0).sum()
                         + np.maximum(Qy[ny, :], 0.0).sum() + np.maximum(-Qy[0, :], 0.0).sum())
        dV = dt * (Qx[:, :-1] - Qx[:, 1:] + Qy[:-1, :] - Qy[1:, :]) + src * dt
        V = V + dV
        self.h = np.where(self.ok, invert(V, self.w, self.D, dx), 0.0)
        self.qxc, self.qxf, self.qyc, self.qyf = qxc, qxf, qyc, qyf
        return out_edge

    def hmax_for_dt(self):
        chan = self.w > 0
        parts = [np.max(np.maximum(self.h, 0.0))]
        if chan.any():
            parts.append(np.max((self.h + self.D)[chan]))
        return max(max(parts), DRY)

    def run(self, src, T, dt0=1.0, cfl=0.7):
        t = 0.0
        injected = 0.0
        out_edge = 0.0
        V0 = storage(self.h, self.w, self.D, self.dx).sum()
        while t < T - 1e-9:
            dt = min(dt0, cfl * self.dx / np.sqrt(G * self.hmax_for_dt()), T - t)
            out_edge += self.step(dt, src)
            injected += src.sum() * dt
            t += dt
        Vf = storage(self.h, self.w, self.D, self.dx).sum()
        return dict(V0=V0, Vfinal=Vf, injected=injected, outflow=out_edge,
                    mass_err=Vf - (V0 + injected - out_edge))


