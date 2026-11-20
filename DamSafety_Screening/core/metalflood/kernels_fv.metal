// DamSafety shock-capturing flood kernels: a well-balanced, positivity-preserving finite-volume
// solver for the 2-D shallow-water equations, one GPU thread per cell or face. It is the
// companion of kernels.metal (the local-inertial / "acceleration" scheme): same buffers-as-numpy
// design, same fixed-point atomic reductions, same CPU build path, but it resolves hydraulic
// jumps and dam-break fronts instead of smoothing them.
//
// Numerics:
//   - conserved cell variables U = (h, hu, hv); bed z (relative to the lowest cell, float32 care);
//   - Audusse et al. (2004) hydrostatic reconstruction at each face: h*_L = max(0, eta_L - z_face),
//     h*_R = max(0, eta_R - z_face), z_face = max(z_L, z_R); velocities from the cell depth. This
//     is what keeps a lake at rest and keeps depths non-negative over a wet/dry front;
//   - HLLC approximate Riemann flux (Toro) with Einfeldt-style wave speeds; the tangential
//     momentum rides the contact (passive advection), which HLL would smear;
//   - the Audusse bed source (0.5 g (h^2 - h*^2)) added per face so still water stays still;
//   - forward Euler in time; dt = min(initial_tstep, cfl dx / max_cell(|u|+c, |v|+c)), cfl capped
//     at 0.45 for the 2-D explicit stability limit, computed on the GPU every step;
//   - FREE domain edges are transmissive (zero-gradient ghost); closed edges are reflective walls.
//
// Compiled two ways, exactly like kernels.metal: by Metal at run time on a Mac, and as C++ with
// mf_shim.h (cpu_kernels_fv.cpp) everywhere else. So: plain functions, B(i)/GID for the Metal
// attributes, metal:: math names <cmath> also has, simd_*/atomic_* the shim maps to serial code.
// Every kernel shares one argument list (MF_ARGS_FV) so one dispatcher serves all.

#ifndef MF_CPU
#include <metal_stdlib>
using namespace metal;
#define B(i) [[buffer(i)]]
#define GID [[thread_position_in_grid]]
#endif

#define MF_NODATA (-9999.0f)
#define MF_TEPS 1.0e-3f
#define MF_CFL_CAP 0.45f         // 2-D explicit Godunov stability ceiling (per the sum of the
                                 // two directional CFLs staying under 1)
#define MF_HVEL 1.0e-6f          // depth below which a cell carries no velocity: a
                                 // desingularisation floor, far finer than the 1 mm output "dry",
                                 // so a dam-break film's momentum is not thrown away at the front

// edges in Params.free_mask (shared with kernels.metal)
#define MF_N 1u
#define MF_S 2u
#define MF_E 4u
#define MF_W 8u

// words of the state buffer (uint; the float ones are read through a float pointer). Shared with
// kernels.metal except S_WMAX/S_WNEXT carry the max wave speed, not the max depth: the FV dt comes
// from |u|+c, not from sqrt(g h) alone.
#define S_T 0          // float: time since the batch started (s)
#define S_DT 1         // float: this step's dt (0 = the batch already reached its end time)
#define S_WMAX 2       // uint (float bits): max wave speed now (m/s), sets the next dt
#define S_WNEXT 3      // uint (float bits), atomic: max wave speed after this step
#define S_VIN 4        // float: volume injected by the sources in this batch (m3)
#define S_VOUT 5       // float: volume out through the free edges in this batch (m3)
#define S_CLIP 6       // uint, atomic: volume the depth >= 0 clip added in this batch (MF_FIX m3)
#define S_STEPS 7      // uint: steps taken in this batch
#define S_MINDT 8      // float: shortest dt in this batch
#define S_RMIN 9       // uint, atomic: wet rows / columns seen in this batch
#define S_RMAX 10
#define S_CMIN 11
#define S_CMAX 12
#define S_QIN 13       // float: inflow of the sources this step (m3/s)
#define S_QOUT 14      // uint, atomic: outflow through the free edges this step (MF_FIX m3/s)
#define S_LASTDT 15    // float: dt of the last step taken
#define S_WORDS 32
#define MF_FIX 1.0e-4f

struct Params {
    uint nx, ny;
    uint r0, c0;
    uint wr, wc;
    uint free_mask;
    uint n_src;
    uint n_series;
    uint fric2d;          // unused by the FV scheme (friction is a cell source term here)
    uint clip_end;
    uint pad2;
    float dx;
    float dt0;
    float alpha;          // CFL factor from the deck (capped at MF_CFL_CAP internally)
    float g;
    float dry;            // 1e-3 m: below this a cell carries no momentum
    float maxhflow;       // unused by the FV scheme (kept for the layout)
    float velmin;         // unused
    float arr_depth;      // depth that counts as arrival (inittm_depth)
    float t0;
    float t_end;
    float theta;          // unused
    float pad4;
};

// Buffer layout for the FV scheme (its own order, 0..23; Params at 24):
#define MF_ARGS_FV \
    device const float* z B(0), \
    device const float* n2 B(1), \
    device float* h B(2), \
    device float* hu B(3), \
    device float* hv B(4), \
    device float* src B(5), \
    device float* hmax B(6), \
    device float* tmax B(7), \
    device float* arr B(8), \
    device float* vmax B(9), \
    device const uchar* ok B(10), \
    device const uint* s_cell B(11), \
    device const uint* s_series B(12), \
    device const uint* ser_off B(13), \
    device const float* ser_t B(14), \
    device const float* ser_q B(15), \
    device uint* cursor B(16), \
    device uint* st B(17), \
    device float* fxh B(18), \
    device float* fxhu B(19), \
    device float* fxhv B(20), \
    device float* fyh B(21), \
    device float* fyhu B(22), \
    device float* fyhv B(23), \
    constant Params& p B(24), \
    uint2 gid GID

struct F3 { float h, mn, mt; };   // flux: mass, normal momentum, tangential momentum

// -- helpers ------------------------------------------------------------------------------

// velocity from a discharge, desingularised so a near-dry film stays bounded (no 0/0)
inline float mf_vel(float hq, float hh) {
    return (hh > MF_HVEL) ? hq / hh : 0.0f;
}

// HLLC flux across a face, in the face-normal frame: the + side moves in +normal. hn is the
// normal velocity, ht the tangential one. Returns (mass, normal-momentum, tangential-momentum).
inline F3 mf_hllc(float hL, float hnL, float htL, float hR, float hnR, float htR, float g) {
    F3 f;
    bool dryL = hL <= 0.0f;
    bool dryR = hR <= 0.0f;
    if (dryL && dryR) {
        f.h = 0.0f; f.mn = 0.0f; f.mt = 0.0f;
        return f;
    }
    float cL = sqrt(g * fmax(hL, 0.0f));
    float cR = sqrt(g * fmax(hR, 0.0f));
    float SL, SR;
    if (dryL) {                       // dry left: the front runs into the dry bed
        SL = hnR - 2.0f * cR;
        SR = hnR + cR;
    } else if (dryR) {
        SL = hnL - cL;
        SR = hnL + 2.0f * cL;
    } else {
        float ustar = 0.5f * (hnL + hnR) + cL - cR;
        float cstar = 0.5f * (cL + cR) + 0.25f * (hnL - hnR);
        SL = fmin(hnL - cL, ustar - cstar);
        SR = fmax(hnR + cR, ustar + cstar);
    }
    // physical fluxes (normal frame)
    float FhL = hL * hnL;
    float FmL = hL * hnL * hnL + 0.5f * g * hL * hL;
    float FhR = hR * hnR;
    float FmR = hR * hnR * hnR + 0.5f * g * hR * hR;
    if (SL >= 0.0f) {
        f.h = FhL; f.mn = FmL; f.mt = FhL * htL;
        return f;
    }
    if (SR <= 0.0f) {
        f.h = FhR; f.mn = FmR; f.mt = FhR * htR;
        return f;
    }
    float inv = 1.0f / (SR - SL);
    float Fh = (SR * FhL - SL * FhR + SL * SR * (hR - hL)) * inv;
    float Fm = (SR * FmL - SL * FmR + SL * SR * (hR * hnR - hL * hnL)) * inv;
    // contact speed sets which side's tangential velocity the mass carries
    float num = SL * hR * (hnR - SR) - SR * hL * (hnL - SL);
    float den = hR * (hnR - SR) - hL * (hnL - SL);
    float Sstar = (fabs(den) > 1.0e-12f) ? num / den : 0.5f * (SL + SR);
    f.h = Fh;
    f.mn = Fm;
    f.mt = Fh * (Sstar >= 0.0f ? htL : htR);
    return f;
}

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
    float ff = span > 0.0f ? (tm - ser_t[j]) / span : 1.0f;
    return ser_q[j] + ff * (ser_q[j + 1] - ser_q[j]);
}

// -- kernels --------------------------------------------------------------------------------

// one thread: the step's dt from the CFL condition (max wave speed over the window) and the
// source rates at the start of the step
kernel void k_begin_fv(MF_ARGS_FV) {
    device float* sf = (device float*)st;
    float t = sf[S_T];
    float rem = p.t_end - t;
    if (!(rem > MF_TEPS)) {
        sf[S_DT] = 0.0f;
        return;
    }
    float wm = as_type<float>(st[S_WMAX]);
    float cfl = fmin(p.alpha, MF_CFL_CAP);
    float dt = p.dt0;
    if (wm > 1.0e-6f) {
        dt = fmin(dt, cfl * p.dx / wm);
    }
    if (p.clip_end != 0u && dt >= rem) {
        dt = rem;
    }
    sf[S_DT] = dt;
    st[S_WNEXT] = 0u;
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

// every face of the window: HLLC flux of the hydrostatically reconstructed states. Thread (x, y)
// owns the x-face (row r0+y, face col c0+x) when y < wr, and the y-face (face row r0+y, col c0+x)
// when x < wc. Domain-edge faces use a ghost cell (transmissive if FREE, else a reflecting wall)
// and add their outward mass flux to the batch's edge outflow.
kernel void k_faces_fv(MF_ARGS_FV) {
    device float* sf = (device float*)st;
    float dt = sf[S_DT];
    if (dt <= 0.0f) {
        return;
    }
    uint nx = p.nx;
    uint ny = p.ny;
    uint r = p.r0 + gid.y;
    uint c = p.c0 + gid.x;
    float g = p.g;
    float dry = p.dry;
    float outflow = 0.0f;

    if (gid.y < p.wr) {               // x-face at (row r, face column c), 0..nx
        uint f = r * (nx + 1u) + c;
        F3 fl;
        fl.h = 0.0f; fl.mn = 0.0f; fl.mt = 0.0f;
        if (c > 0u && c < nx) {       // interior face between l and rr
            uint l = r * nx + c - 1u;
            uint rr = l + 1u;
            if (ok[l] != 0 && ok[rr] != 0) {
                float zf = fmax(z[l], z[rr]);
                float hLs = fmax(0.0f, z[l] + h[l] - zf);
                float hRs = fmax(0.0f, z[rr] + h[rr] - zf);
                float uL = mf_vel(hu[l], h[l]);
                float vL = mf_vel(hv[l], h[l]);
                float uR = mf_vel(hu[rr], h[rr]);
                float vR = mf_vel(hv[rr], h[rr]);
                fl = mf_hllc(hLs, uL, vL, hRs, uR, vR, g);
            }
        } else if (c == 0u) {         // left domain edge, cell i is the + side
            uint i = r * nx;
            if (ok[i] != 0 && h[i] > MF_HVEL) {
                float ui = mf_vel(hu[i], h[i]);
                float vi = mf_vel(hv[i], h[i]);
                if ((p.free_mask & MF_W) != 0u) {
                    fl = mf_hllc(h[i], ui, vi, h[i], ui, vi, g);   // transmissive
                    outflow += fmax(-fl.h, 0.0f);
                } else {
                    fl = mf_hllc(h[i], -ui, vi, h[i], ui, vi, g);  // wall
                }
            }
        } else {                      // c == nx, right domain edge, cell i is the - side
            uint i = r * nx + nx - 1u;
            if (ok[i] != 0 && h[i] > MF_HVEL) {
                float ui = mf_vel(hu[i], h[i]);
                float vi = mf_vel(hv[i], h[i]);
                if ((p.free_mask & MF_E) != 0u) {
                    fl = mf_hllc(h[i], ui, vi, h[i], ui, vi, g);
                    outflow += fmax(fl.h, 0.0f);
                } else {
                    fl = mf_hllc(h[i], ui, vi, h[i], -ui, vi, g);
                }
            }
        }
        fxh[f] = fl.h;
        fxhu[f] = fl.mn;             // normal = x-momentum
        fxhv[f] = fl.mt;             // tangential = y-momentum
    }

    if (gid.x < p.wc) {               // y-face at (face row r, col c), 0..ny. Normal = y (south +).
        uint f = r * nx + c;
        F3 fl;
        fl.h = 0.0f; fl.mn = 0.0f; fl.mt = 0.0f;
        if (r > 0u && r < ny) {
            uint u = (r - 1u) * nx + c;
            uint d = u + nx;
            if (ok[u] != 0 && ok[d] != 0) {
                float zf = fmax(z[u], z[d]);
                float hUs = fmax(0.0f, z[u] + h[u] - zf);
                float hDs = fmax(0.0f, z[d] + h[d] - zf);
                // normal velocity is +y (toward larger row index = south); tangential is +x
                float nU = mf_vel(hv[u], h[u]);
                float tU = mf_vel(hu[u], h[u]);
                float nD = mf_vel(hv[d], h[d]);
                float tD = mf_vel(hu[d], h[d]);
                fl = mf_hllc(hUs, nU, tU, hDs, nD, tD, g);
            }
        } else if (r == 0u) {         // north domain edge, cell i is the + (south) side
            uint i = c;
            if (ok[i] != 0 && h[i] > MF_HVEL) {
                float ni = mf_vel(hv[i], h[i]);
                float ti = mf_vel(hu[i], h[i]);
                if ((p.free_mask & MF_N) != 0u) {
                    fl = mf_hllc(h[i], ni, ti, h[i], ni, ti, g);
                    outflow += fmax(-fl.h, 0.0f);
                } else {
                    fl = mf_hllc(h[i], -ni, ti, h[i], ni, ti, g);
                }
            }
        } else {                      // r == ny, south domain edge, cell i is the - side
            uint i = (ny - 1u) * nx + c;
            if (ok[i] != 0 && h[i] > MF_HVEL) {
                float ni = mf_vel(hv[i], h[i]);
                float ti = mf_vel(hu[i], h[i]);
                if ((p.free_mask & MF_S) != 0u) {
                    fl = mf_hllc(h[i], ni, ti, h[i], ni, ti, g);
                    outflow += fmax(fl.h, 0.0f);
                } else {
                    fl = mf_hllc(h[i], ni, ti, h[i], -ni, ti, g);
                }
            }
        }
        fyh[f] = fl.h;
        fyhu[f] = fl.mt;             // tangential = x-momentum
        fyhv[f] = fl.mn;             // normal = y-momentum
    }

    float tot = simd_sum(outflow);
    if (simd_is_first() && tot != 0.0f) {
        atomic_fetch_add_explicit((device atomic_uint*)(st + S_QOUT),
                                  uint(tot * p.dx / MF_FIX + 0.5f), memory_order_relaxed);
    }
}

// every cell of the window: finite-volume update with the Audusse bed source, friction, the
// depth >= 0 clip, the running maxima, and the next step's max wave speed.
kernel void k_cont_fv(MF_ARGS_FV) {
    device float* sf = (device float*)st;
    float dt = sf[S_DT];
    if (dt <= 0.0f) {
        return;
    }
    uint nx = p.nx;
    uint ny = p.ny;
    uint r = p.r0 + gid.y;
    uint c = p.c0 + gid.x;
    uint i = r * nx + c;
    uint fx = r * (nx + 1u) + c;      // left x-face of the cell; right is fx+1
    float dx = p.dx;
    float g = p.g;

    float h0 = h[i];
    float etaI = z[i] + h0;
    // Audusse bed source: reconstructed depths at the cell's own four faces
    float zxR = fmax(z[i], (c + 1u < nx) ? z[i + 1u] : z[i]);
    float zxL = fmax((c > 0u) ? z[i - 1u] : z[i], z[i]);
    float zyD = fmax(z[i], (r + 1u < ny) ? z[i + nx] : z[i]);
    float zyU = fmax((r > 0u) ? z[i - nx] : z[i], z[i]);
    float hxR = fmax(0.0f, etaI - zxR);
    float hxL = fmax(0.0f, etaI - zxL);
    float hyD = fmax(0.0f, etaI - zyD);
    float hyU = fmax(0.0f, etaI - zyU);
    float half_g = 0.5f * g;
    float SxR = half_g * (h0 * h0 - hxR * hxR);   // added to the right x-face normal momentum
    float SxL = half_g * (h0 * h0 - hxL * hxL);   // added to the left  x-face normal momentum
    float SyD = half_g * (h0 * h0 - hyD * hyD);
    float SyU = half_g * (h0 * h0 - hyU * hyU);

    float invdx = dt / dx;
    float dh  = -invdx * ((fxh[fx + 1u] - fxh[fx]) + (fyh[i + nx] - fyh[i]));
    float dhu = -invdx * (((fxhu[fx + 1u] + SxR) - (fxhu[fx] + SxL))
                          + (fyhu[i + nx] - fyhu[i]));
    float dhv = -invdx * ((fxhv[fx + 1u] - fxhv[fx])
                          + ((fyhv[i + nx] + SyD) - (fyhv[i] + SyU)));

    float hn = h0 + dh + src[i] * dt / (dx * dx);
    float hun = hu[i] + dhu;
    float hvn = hv[i] + dhv;

    float clip = 0.0f;
    if (hn <= 0.0f) {
        clip = -hn * dx * dx;
        hn = 0.0f;
        hun = 0.0f;
        hvn = 0.0f;
    } else {
        // semi-implicit Manning friction on the updated momentum (unconditionally stable); the
        // momentum is kept down to a desingularisation floor so the wet/dry front is not stalled
        float un = mf_vel(hun, hn);
        float vn = mf_vel(hvn, hn);
        float spd = sqrt(un * un + vn * vn);
        if (spd > 0.0f && hn > MF_HVEL) {
            float cf = 1.0f + g * n2[i] * dt * spd / pow(hn, 4.0f / 3.0f);
            hun /= cf;
            hvn /= cf;
        }
    }
    if (ok[i] == 0) {
        hn = 0.0f; hun = 0.0f; hvn = 0.0f;
    }
    h[i] = hn;
    hu[i] = hun;
    hv[i] = hvn;

    float th = (p.t0 + sf[S_T] + dt) / 3600.0f;
    float wcell = 0.0f;
    float vmagn = 0.0f;
    if (hn > MF_HVEL) {
        float un = mf_vel(hun, hn);
        float vn = mf_vel(hvn, hn);
        float cc = sqrt(g * hn);
        wcell = fmax(fabs(un) + cc, fabs(vn) + cc);
        vmagn = sqrt(un * un + vn * vn);
    }
    if (hn > p.dry) {                 // maxima on LISFLOOD-FP's 1 mm "dry", from the first step on
        if (hn > hmax[i]) {
            hmax[i] = hn;
            tmax[i] = th;
        }
        if (hn > p.arr_depth && arr[i] == MF_NODATA) {
            arr[i] = th;
        }
    }
    if (vmagn > vmax[i]) {
        vmax[i] = vmagn;
    }

    bool wet = hn > 0.0f;
    float wm = simd_max(wcell);
    uint rmin = simd_min(wet ? r : 0xffffffffu);
    uint rmax = simd_max(wet ? r : 0u);
    uint cmin = simd_min(wet ? c : 0xffffffffu);
    uint cmax = simd_max(wet ? c : 0u);
    float cl = simd_sum(clip);
    if (simd_is_first()) {
        if (wm > 0.0f) {
            atomic_fetch_max_explicit((device atomic_uint*)(st + S_WNEXT), as_type<uint>(wm),
                                      memory_order_relaxed);
        }
        if (wet) {
            atomic_fetch_min_explicit((device atomic_uint*)(st + S_RMIN), rmin,
                                      memory_order_relaxed);
            atomic_fetch_max_explicit((device atomic_uint*)(st + S_RMAX), rmax,
                                      memory_order_relaxed);
            atomic_fetch_min_explicit((device atomic_uint*)(st + S_CMIN), cmin,
                                      memory_order_relaxed);
            atomic_fetch_max_explicit((device atomic_uint*)(st + S_CMAX), cmax,
                                      memory_order_relaxed);
        }
        if (cl > 0.0f) {
            atomic_fetch_add_explicit((device atomic_uint*)(st + S_CLIP),
                                      uint(cl / MF_FIX + 0.5f), memory_order_relaxed);
        }
    }
}

// one thread: close the step
kernel void k_end_fv(MF_ARGS_FV) {
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
    st[S_WMAX] = st[S_WNEXT];
}
