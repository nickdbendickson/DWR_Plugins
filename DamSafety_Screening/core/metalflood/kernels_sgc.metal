// DamSafety flood engine, SGC variant: the local-inertial (ACC) scheme of kernels.metal WITH
// LISFLOOD-FP's sub-grid channels (Neal et al. 2012; LISFLOOD-FP 8.3 SGC2, sgm_fast.cpp). A cell
// may carry a rectangular channel narrower than the cell (width w <= dx) and below the DEM
// (bankfull depth D, bed = z - D) that conveys and stores a flood the grid cannot resolve.
//
// It mirrors core/metalflood/sgc_ref.py (the float64 numpy reference) cell for cell, and is
// compiled two ways like kernels.metal: by Metal at run time, and as C++ with mf_shim.h
// (cpu_kernels_sgc.cpp) for the cloud tests and computers without Metal.
//
// State  h[i] = water surface RELATIVE TO THE DEM z[i] (the bank). In a channel cell (w>0) h goes
// NEGATIVE: h in [-D, 0] is water in the channel, h > 0 is water on the floodplain. A w=0 cell
// behaves exactly like plain ACC (storage dx^2 h, floodplain-only faces).
//
// Per cell:   width w[i] (0 = none), bankfull depth D[i] (bed = z - D), channel n^2 (nc2[i]);
//             floodplain n^2 is n2[i] as in the ACC engine.
// Storage V(h):  below bank (w>0, h<0): V = w dx (h+D);  above: V = w dx D + dx^2 h;  no channel: dx^2 h.
// Face Q (m3/s) = Q_chan + Q_fp, each a LISFLOOD-FP SGC2 CalculateQ with its OWN stored old discharge:
//   chan (both cells w>0):  chan_h = max(eta) - max(bed); A=wf*chan_h; R=A/(wf+2 min(chan_h,Df)); n=nc2
//   flood (above bank):     hflow = max(eta) - max(z);    A=Wfp*hflow;  R=hflow;  n=n2;  Wfp=dx(1-wf/dx)
//                           Q_fp scaled by (1 - wf/dx)  (Neal 2012 Fig 1C)
//
// Float32 care as in kernels.metal: z is stored relative to the lowest valid cell (engine), and the
// surface slope is ((z_r - z_l) + (h_r - h_l))/dx, never eta_r - eta_l of two large numbers.
//
// FREE edges drain both the channel and the floodplain off the boundary (sgc_free), the split form
// of kernels.metal's mf_free; the outflow is summed into S_QOUT in k_limit_sgc.

#ifndef MF_CPU
#include <metal_stdlib>
using namespace metal;
#define B(i) [[buffer(i)]]
#define GID [[thread_position_in_grid]]
#endif

#define MF_NODATA (-9999.0f)
#define MF_TEPS 1.0e-3f
#define MF_DRY 1.0e-3f

// edges in Params.free_mask
#define MF_N 1u
#define MF_S 2u
#define MF_E 4u
#define MF_W 8u

// state words (same layout as kernels.metal)
#define S_T 0
#define S_DT 1
#define S_HMAX 2
#define S_HNEXT 3
#define S_VIN 4
#define S_VOUT 5
#define S_CLIP 6
#define S_STEPS 7
#define S_MINDT 8
#define S_RMIN 9
#define S_RMAX 10
#define S_CMIN 11
#define S_CMAX 12
#define S_QIN 13
#define S_QOUT 14
#define S_LASTDT 15
#define S_WORDS 32
#define MF_FIX 1.0e-4f

struct Params {
    uint nx, ny;
    uint r0, c0;
    uint wr, wc;
    uint free_mask;
    uint n_src;
    uint n_series;
    uint fric2d;
    uint clip_end;
    uint inflow_lf_order;
    float dx;
    float dt0;
    float alpha;
    float g;
    float dry;
    float maxhflow;
    float velmin;
    float arr_depth;
    float t0;
    float t_end;
    float theta;
    float pad4;
};

// 0..29 data buffers, Params at 30 (31 slots, the Metal argument-table cap). The channel/floodplain
// split needs a stored old discharge for each (qxco/qxfo, qyco/qyfo) on top of the ACC buffers.
#define MF_ARGS \
    device const float* z B(0), \
    device const float* n2 B(1), \
    device float* h B(2), \
    device float* qxc B(3), \
    device float* qxf B(4), \
    device float* qyc B(5), \
    device float* qyf B(6), \
    device float* src B(7), \
    device float* scl B(8), \
    device float* hmax B(9), \
    device float* tmax B(10), \
    device float* arr B(11), \
    device float* vmax B(12), \
    device const uchar* ok B(13), \
    device const uint* s_cell B(14), \
    device const uint* s_series B(15), \
    device const uint* ser_off B(16), \
    device const float* ser_t B(17), \
    device const float* ser_q B(18), \
    device uint* cursor B(19), \
    device uint* st B(20), \
    device float* qxco B(21), \
    device float* qxfo B(22), \
    device float* qyco B(23), \
    device float* qyfo B(24), \
    device float* vxf B(25), \
    device float* vyf B(26), \
    device const float* w B(27), \
    device const float* Dd B(28), \
    device const float* nc2 B(29), \
    constant Params& p B(30), \
    uint2 gid GID

// -- helpers ------------------------------------------------------------------------------

// cell water volume (m3) for water level above DEM hc (negative in channel)
inline float sgc_storage(float hc, float wc, float Dc, float dx) {
    if (wc > 0.0f) {
        float Vbank = wc * dx * Dc;
        if (hc < 0.0f) {
            return fmax(wc * dx * (hc + Dc), 0.0f);
        }
        return Vbank + dx * dx * hc;
    }
    return dx * dx * fmax(hc, 0.0f);
}

// water level above DEM from cell volume (inverse of sgc_storage)
inline float sgc_invert(float V, float wc, float Dc, float dx) {
    if (wc > 0.0f) {
        float Vbank = wc * dx * Dc;
        if (V <= Vbank) {
            return V / (wc * dx) - Dc;              // in channel (h in [-D, 0])
        }
        return (V - Vbank) / (dx * dx);             // above bank
    }
    return V / (dx * dx);
}

// LISFLOOD-FP SGC2 CalculateQ: S = surface slope (eta_r - eta_l)/dx
inline float sgc_calcQ(float q_old, float A, float R, float S, float n2f, float g, float dt) {
    R = fmax(R, 1e-9f);
    A = fmax(A, 1e-12f);
    return (q_old - g * A * dt * S) / (1.0f + dt * g * n2f * fabs(q_old) / (pow(R, 4.0f / 3.0f) * A));
}

// channel + floodplain discharge of one interior face between cells l (- side) and r (+ side).
// writes *qc and *qf (m3/s); returns the velocity LISFLOOD-FP records (new total Q over flow depth)
inline float sgc_face(float zl, float hl, float wl, float Dl,
                      float zr, float hr, float wr, float Dr,
                      float qc_old, float qf_old, float nc2f, float nf2, float dt,
                      constant Params& p, thread float* qc, thread float* qf) {
    float etal = zl + hl;
    float etar = zr + hr;
    float etamax = fmax(etal, etar);
    float S = ((zr - zl) + (hr - hl)) / p.dx;
    float bedl = zl - (wl > 0.0f ? Dl : 0.0f);
    float bedr = zr - (wr > 0.0f ? Dr : 0.0f);
    // channel: both cells must have a channel
    float wface = (wl > 0.0f && wr > 0.0f) ? fmin(wl, wr) : 0.0f;
    float Dface = wface > 0.0f ? 0.5f * (Dl + Dr) : 0.0f;
    float chan_h = fmin(etamax - fmax(bedl, bedr), p.maxhflow);
    float qcn = 0.0f;
    if (wface > 0.0f && chan_h > MF_DRY) {
        float A = wface * chan_h;
        float R = A / (wface + 2.0f * fmin(chan_h, Dface) + 1e-12f);
        qcn = sgc_calcQ(qc_old, A, R, S, nc2f, p.g, dt);
    }
    // floodplain: above bank only
    float hflow = fmin(etamax - fmax(zl, zr), p.maxhflow);
    float wratio = wface > 0.0f ? fmin(wface / p.dx, 1.0f) : 0.0f;
    float qfn = 0.0f;
    if (hflow > MF_DRY) {
        float Wfp = p.dx * (1.0f - wratio);
        float A = Wfp * hflow;
        qfn = sgc_calcQ(qf_old, A, hflow, S, nf2, p.g, dt) * (1.0f - wratio);
    }
    *qc = qcn;
    *qf = qfn;
    // velocity for maxVc: total new discharge over the wider of the two flow depths, like ACC
    float hv = fmax(chan_h, hflow);
    if (hv <= MF_DRY || (qcn == 0.0f && qfn == 0.0f)) {
        return 0.0f;
    }
    float width = fmax(wface, p.dx * (1.0f - wratio));
    return (qcn + qfn) / fmax(width * fmin(hv, p.maxhflow), 1e-6f);
}

// FREE edge: outward channel + floodplain discharge magnitudes (>=0, m3/s) from the edge cell's
// depth and the water-surface slope to its inner neighbour (etain). The split form of kernels.metal's
// mf_free; the caller applies the outward sign (-x / -y on the W / N edges, +x / +y on E / S).
inline void sgc_free(float zc, float hc, float wc, float Dc, float etain,
                     float qc_old, float qf_old, float nc2c, float nf2c, float dt,
                     constant Params& p, thread float* qc, thread float* qf) {
    float etac = zc + hc;
    float slope = fabs(etac - etain) / p.dx;
    float bed = zc - (wc > 0.0f ? Dc : 0.0f);
    float chan_h = fmin(etac - bed, p.maxhflow);
    float qcm = 0.0f;
    if (wc > 0.0f && chan_h > MF_DRY) {
        float A = wc * chan_h;
        float R = A / (wc + 2.0f * fmin(chan_h, Dc) + 1e-12f);
        qcm = (fabs(qc_old) + p.g * dt * A * slope)
              / (1.0f + p.g * dt * nc2c * fabs(qc_old) / (pow(R, 4.0f / 3.0f) * A));
    }
    float hflow = fmin(fmax(hc, 0.0f), p.maxhflow);
    float qfm = 0.0f;
    if (hflow > MF_DRY) {
        float wratio = wc > 0.0f ? fmin(wc / p.dx, 1.0f) : 0.0f;
        float A = p.dx * (1.0f - wratio) * hflow;
        if (A > 0.0f) {
            qfm = (fabs(qf_old) + p.g * dt * A * slope)
                  / (1.0f + p.g * dt * nf2c * fabs(qf_old) / (pow(hflow, 4.0f / 3.0f) * A))
                  * (1.0f - wratio);
        }
    }
    *qc = qcm;
    *qf = qfm;
}

// one inflow series at time tm (LISFLOOD-FP InterpolateTimeSeries), identical to kernels.metal
inline float mf_series(uint k, float tm, device const uint* ser_off,
                              device const float* ser_t, device const float* ser_q,
                              device uint* cursor) {
    uint a = ser_off[k];
    uint b = ser_off[k + 1];
    if (b <= a) {
        return 0.0f;
    }
    if (tm >= ser_t[b - 1]) {
        return ser_q[b - 1];
    }
    if (tm <= ser_t[a]) {
        return ser_q[a];
    }
    uint j = cursor[k];
    if (j < a || j + 1 >= b || ser_t[j] > tm) {
        j = a;
    }
    while (j + 2 < b && ser_t[j + 1] < tm) {
        j += 1u;
    }
    cursor[k] = j;
    float span = ser_t[j + 1] - ser_t[j];
    float f = span > 0.0f ? (tm - ser_t[j]) / span : 1.0f;
    return ser_q[j] + f * (ser_q[j + 1] - ser_q[j]);
}

// the "celerity depth" that sets the CFL time step: channel total depth in a channel cell, else
// the floodplain depth
inline float sgc_cel_h(float hc, float wc, float Dc) {
    return wc > 0.0f ? fmax(hc + Dc, 0.0f) : fmax(hc, 0.0f);
}

// -- kernels --------------------------------------------------------------------------------

// one thread: this step's dt from the deepest water, and the source rates at the step start
kernel void k_begin_sgc(MF_ARGS) {
    device float* sf = (device float*)st;
    float t = sf[S_T];
    float rem = p.t_end - t;
    if (!(rem > MF_TEPS)) {
        sf[S_DT] = 0.0f;
        return;
    }
    float hm = as_type<float>(st[S_HMAX]);
    float dt = p.dt0;
    if (hm > p.dry) {
        dt = fmin(dt, p.alpha * p.dx / sqrt(p.g * hm));
    }
    if (p.clip_end != 0u && dt >= rem) {
        dt = rem;
    }
    sf[S_DT] = dt;
    st[S_HNEXT] = 0u;
    st[S_QOUT] = 0u;
    float tm = p.t0 + t;
    for (uint i = 0; i < p.n_src; ++i) {
        src[s_cell[i]] = 0.0f;
    }
    float qin = 0.0f;
    for (uint i = 0; i < p.n_src; ++i) {
        float q = mf_series(s_series[i], tm, ser_off, ser_t, ser_q, cursor);
        src[s_cell[i]] += q;
        qin += q;
    }
    sf[S_QIN] = qin;
    sf[S_VIN] += qin * dt;
}

// every face of the window: channel + floodplain discharge (interior faces, and FREE-edge outflow)
kernel void k_faces_sgc(MF_ARGS) {
    device float* sf = (device float*)st;
    float dt = sf[S_DT];
    if (dt <= 0.0f) {
        return;
    }
    uint nx = p.nx;
    uint ny = p.ny;
    uint r = p.r0 + gid.y;
    uint c = p.c0 + gid.x;
    if (gid.y < p.wr) {
        uint f = r * (nx + 1u) + c;
        float qc = 0.0f, qf = 0.0f, v = 0.0f;
        if (c == 0u) {
            if ((p.free_mask & MF_W) != 0u && nx > 1u) {
                uint i = r * nx;
                if (ok[i] != 0) {
                    float qcm, qfm;
                    sgc_free(z[i], h[i], w[i], Dd[i], z[i + 1u] + h[i + 1u], qxco[f], qxfo[f],
                             nc2[i], n2[i], dt, p, &qcm, &qfm);
                    qc = -qcm; qf = -qfm;              // outflow to -x
                }
            }
        } else if (c == nx) {
            if ((p.free_mask & MF_E) != 0u && nx > 1u) {
                uint i = r * nx + nx - 1u;
                if (ok[i] != 0) {
                    float qcm, qfm;
                    sgc_free(z[i], h[i], w[i], Dd[i], z[i - 1u] + h[i - 1u], qxco[f], qxfo[f],
                             nc2[i], n2[i], dt, p, &qcm, &qfm);
                    qc = qcm; qf = qfm;
                }
            }
        } else {
            uint l = r * nx + c - 1u;
            uint rr = l + 1u;
            if (ok[l] != 0 && ok[rr] != 0) {
                v = sgc_face(z[l], h[l], w[l], Dd[l], z[rr], h[rr], w[rr], Dd[rr],
                             qxco[f], qxfo[f], 0.5f * (nc2[l] + nc2[rr]), 0.5f * (n2[l] + n2[rr]),
                             dt, p, &qc, &qf);
            }
        }
        qxc[f] = qc;
        qxf[f] = qf;
        vxf[f] = v;
    }
    if (gid.x < p.wc) {
        uint f = r * nx + c;
        float qc = 0.0f, qf = 0.0f, v = 0.0f;
        if (r == 0u) {
            if ((p.free_mask & MF_N) != 0u && ny > 1u) {
                uint i = c;
                if (ok[i] != 0) {
                    float qcm, qfm;
                    sgc_free(z[i], h[i], w[i], Dd[i], z[i + nx] + h[i + nx], qyco[f], qyfo[f],
                             nc2[i], n2[i], dt, p, &qcm, &qfm);
                    qc = -qcm; qf = -qfm;              // outflow to -y
                }
            }
        } else if (r == ny) {
            if ((p.free_mask & MF_S) != 0u && ny > 1u) {
                uint i = (ny - 1u) * nx + c;
                if (ok[i] != 0) {
                    float qcm, qfm;
                    sgc_free(z[i], h[i], w[i], Dd[i], z[i - nx] + h[i - nx], qyco[f], qyfo[f],
                             nc2[i], n2[i], dt, p, &qcm, &qfm);
                    qc = qcm; qf = qfm;
                }
            }
        } else {
            uint u = (r - 1u) * nx + c;
            uint dn = u + nx;
            if (ok[u] != 0 && ok[dn] != 0) {
                v = sgc_face(z[u], h[u], w[u], Dd[u], z[dn], h[dn], w[dn], Dd[dn],
                             qyco[f], qyfo[f], 0.5f * (nc2[u] + nc2[dn]), 0.5f * (n2[u] + n2[dn]),
                             dt, p, &qc, &qf);
            }
        }
        qyc[f] = qc;
        qyf[f] = qf;
        vyf[f] = v;
    }
}

// every cell: the share of its outflows it can afford this step (donor-cell limiter, in VOLUME)
kernel void k_scale_sgc(MF_ARGS) {
    device float* sf = (device float*)st;
    float dt = sf[S_DT];
    if (dt <= 0.0f) {
        return;
    }
    uint nx = p.nx;
    uint r = p.r0 + gid.y;
    uint c = p.c0 + gid.x;
    uint i = r * nx + c;
    uint fx = r * (nx + 1u) + c;
    float Qxr = qxc[fx + 1u] + qxf[fx + 1u];
    float Qxl = qxc[fx] + qxf[fx];
    float Qyd = qyc[i + nx] + qyf[i + nx];
    float Qyu = qyc[i] + qyf[i];
    float out = (fmax(Qxr, 0.0f) + fmax(-Qxl, 0.0f) + fmax(Qyd, 0.0f) + fmax(-Qyu, 0.0f)) * dt;
    float V = sgc_storage(h[i], w[i], Dd[i], p.dx);
    float avail = V + (p.inflow_lf_order ? 0.0f : src[i] * dt);
    scl[i] = (out > avail && out > 0.0f) ? avail / out : 1.0f;
}

// every face: scale each discharge by its donor cell's share, carry it as next step's old
// discharge; (FREE-edge outflow summing is a later increment)
kernel void k_limit_sgc(MF_ARGS) {
    device float* sf = (device float*)st;
    float dt = sf[S_DT];
    if (dt <= 0.0f) {
        return;
    }
    uint nx = p.nx;
    uint ny = p.ny;
    uint r = p.r0 + gid.y;
    uint c = p.c0 + gid.x;
    float out = 0.0f;                                  // m3/s leaving through the free edges
    if (gid.y < p.wr) {
        uint f = r * (nx + 1u) + c;
        float s = 1.0f;
        float Qt = qxc[f] + qxf[f];
        if (Qt > 0.0f && c > 0u) {
            s = scl[r * nx + c - 1u];
        } else if (Qt < 0.0f && c < nx) {
            s = scl[r * nx + c];
        }
        qxc[f] *= s; qxf[f] *= s;
        qxco[f] = qxc[f]; qxfo[f] = qxf[f];
        float Q = qxc[f] + qxf[f];
        if (c == 0u && Q < 0.0f) {
            out += -Q;
        } else if (c == nx && Q > 0.0f) {
            out += Q;
        }
    }
    if (gid.x < p.wc) {
        uint f = r * nx + c;
        float s = 1.0f;
        float Qt = qyc[f] + qyf[f];
        if (Qt > 0.0f && r > 0u) {
            s = scl[(r - 1u) * nx + c];
        } else if (Qt < 0.0f && r < ny) {
            s = scl[r * nx + c];
        }
        qyc[f] *= s; qyf[f] *= s;
        qyco[f] = qyc[f]; qyfo[f] = qyf[f];
        float Q = qyc[f] + qyf[f];
        if (r == 0u && Q < 0.0f) {
            out += -Q;
        } else if (r == ny && Q > 0.0f) {
            out += Q;
        }
    }
    float tot = simd_sum(out);
    if (simd_is_first() && tot != 0.0f) {
        atomic_fetch_add_explicit((device atomic_uint*)(st + S_QOUT),
                                  uint(tot / MF_FIX + 0.5f), memory_order_relaxed);
    }
}

// every cell: continuity in volume, invert to stage, the V >= 0 clip, and the running maxima
kernel void k_cont_sgc(MF_ARGS) {
    device float* sf = (device float*)st;
    float dt = sf[S_DT];
    if (dt <= 0.0f) {
        return;
    }
    uint nx = p.nx;
    uint r = p.r0 + gid.y;
    uint c = p.c0 + gid.x;
    uint i = r * nx + c;
    uint fx = r * (nx + 1u) + c;
    float Qxl = qxc[fx] + qxf[fx];
    float Qxr = qxc[fx + 1u] + qxf[fx + 1u];
    float Qyu = qyc[i] + qyf[i];
    float Qyd = qyc[i + nx] + qyf[i + nx];
    float V = sgc_storage(h[i], w[i], Dd[i], p.dx);
    V += dt * (Qxl - Qxr + Qyu - Qyd) + src[i] * dt;
    float clip = 0.0f;
    if (V < 0.0f) {
        clip = -V;
        V = 0.0f;
    }
    float hn = (ok[i] == 0) ? 0.0f : sgc_invert(V, w[i], Dd[i], p.dx);
    if (ok[i] == 0) {
        hn = 0.0f;
    }
    h[i] = hn;
    float th = (p.t0 + sf[S_T] + dt) / 3600.0f;
    float habove = fmax(hn, 0.0f);                 // depth ABOVE the DEM (reported, like LISFLOOD-FP SGC)
    if (habove > p.dry) {
        if (habove > hmax[i]) {
            hmax[i] = habove;
            tmax[i] = th;
        }
        if (habove > p.arr_depth && arr[i] == MF_NODATA) {
            arr[i] = th;
        }
    }
    float celh = sgc_cel_h(hn, w[i], Dd[i]);        // channel depth sets the CFL where in-channel
    bool wet = celh > 0.0f;
    float hm = simd_max(wet ? celh : 0.0f);
    uint rmin = simd_min(wet ? r : 0xffffffffu);
    uint rmax = simd_max(wet ? r : 0u);
    uint cmin = simd_min(wet ? c : 0xffffffffu);
    uint cmax = simd_max(wet ? c : 0u);
    float cl = simd_sum(clip);
    if (simd_is_first()) {
        if (hm > 0.0f) {
            atomic_fetch_max_explicit((device atomic_uint*)(st + S_HNEXT), as_type<uint>(hm),
                                      memory_order_relaxed);
            atomic_fetch_min_explicit((device atomic_uint*)(st + S_RMIN), rmin, memory_order_relaxed);
            atomic_fetch_max_explicit((device atomic_uint*)(st + S_RMAX), rmax, memory_order_relaxed);
            atomic_fetch_min_explicit((device atomic_uint*)(st + S_CMIN), cmin, memory_order_relaxed);
            atomic_fetch_max_explicit((device atomic_uint*)(st + S_CMAX), cmax, memory_order_relaxed);
        }
        if (cl > 0.0f) {
            atomic_fetch_add_explicit((device atomic_uint*)(st + S_CLIP),
                                      uint(cl / MF_FIX + 0.5f), memory_order_relaxed);
        }
    }
}

// every cell: Vc from the face velocities k_faces_sgc recorded, into the running maximum
kernel void k_speed_sgc(MF_ARGS) {
    device float* sf = (device float*)st;
    float dt = sf[S_DT];
    if (dt <= 0.0f) {
        return;
    }
    uint nx = p.nx;
    uint r = p.r0 + gid.y;
    uint c = p.c0 + gid.x;
    uint i = r * nx + c;
    uint fx = r * (nx + 1u) + c;
    float vx = fmax(fabs(vxf[fx]), fabs(vxf[fx + 1u]));
    float vy = fmax(fabs(vyf[i]), fabs(vyf[i + nx]));
    float v = sqrt(vx * vx + vy * vy);
    if (v > vmax[i]) {
        vmax[i] = v;
    }
}

// one thread: close the step
kernel void k_end_sgc(MF_ARGS) {
    device float* sf = (device float*)st;
    float dt = sf[S_DT];
    if (dt <= 0.0f) {
        return;
    }
    sf[S_T] = sf[S_T] + dt;
    st[S_STEPS] = st[S_STEPS] + 1u;
    sf[S_MINDT] = fmin(sf[S_MINDT], dt);
    sf[S_LASTDT] = dt;
    sf[S_VOUT] += float(st[S_QOUT]) * MF_FIX * dt;
    st[S_HMAX] = st[S_HNEXT];
}
