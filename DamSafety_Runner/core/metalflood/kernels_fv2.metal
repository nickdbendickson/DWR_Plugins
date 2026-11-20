// DamSafety shock-capturing flood kernels, 2nd order (FV2): a well-balanced, positivity-preserving
// MUSCL finite-volume solver for the 2-D shallow-water equations, one GPU thread per cell or face.
// It is the 2nd-order companion of kernels_fv.metal (the first-order HLLC scheme): same HLLC flux,
// same buffers-as-numpy design, same fixed-point atomic reductions, same CPU build path, but it is
// second order in space (slope-limited reconstruction) and in time (SSP-RK2), so it holds a
// dam-break bore and a hydraulic jump even sharper and with less numerical diffusion than FV1.
//
// Numerics (differences from FV1 noted):
//   - conserved cell variables U = (h, hu, hv); bed z (relative to the lowest cell, float32 care);
//   - MUSCL reconstruction (2nd order in space): per cell, minmod-limited slopes of the free
//     surface eta = z + h and of the discharges qx = hu, qy = hv, and of the bed z, in x and y.
//     At a face the + and - states are the cell-centre values extrapolated a half cell with the
//     limited slope. Reconstructing eta (not h) is what keeps a lake at rest at 2nd order; the
//     minmod limiter is TVD, so no new extrema and no spurious oscillation at the front;
//   - Audusse et al. (2004) hydrostatic reconstruction at each face on the *reconstructed* states:
//     face bed z_face = max(z_L+, z_R-); h*_L = max(0, eta_L+ - z_face), h*_R = max(0, eta_R- -
//     z_face). Positivity-preserving over a wet/dry front;
//   - HLLC approximate Riemann flux (Toro) with Einfeldt-style wave speeds (identical mf_hllc as
//     FV1); the tangential momentum rides the contact;
//   - the Audusse bed source (0.5 g (h_rec^2 - h*^2)) added per face, built from the *reconstructed*
//     cell-edge depths so a lake at rest stays still at 2nd order;
//   - SSP-RK2 (Heun) in time: U* = U^n + dt L(U^n); U^{n+1} = 1/2 U^n + 1/2 (U* + dt L(U*)). Each
//     stage is a full slopes -> faces -> cont sweep; dt is set once from U^n. The source, friction,
//     depth>=0 clip and the running maxima are applied as in FV1, the maxima on the final stage;
//   - dt = min(initial_tstep, cfl dx / max_cell(|u|+c, |v|+c)), cfl capped at MF_CFL_CAP, computed
//     on the GPU every step from the wave speeds of U^n;
//   - FREE domain edges are transmissive (zero-gradient ghost); closed edges are reflective walls.
//
// Compiled two ways, exactly like kernels_fv.metal: by Metal at run time on a Mac, and as C++ with
// mf_shim.h (cpu_kernels_fv2.cpp) everywhere else. So: plain functions, B(i)/GID for the Metal
// attributes, metal:: math names <cmath> also has, simd_*/atomic_* the shim maps to serial code.
// Every kernel shares one argument list (MF_ARGS_FV2) so one dispatcher serves all.

#ifndef MF_CPU
#include <metal_stdlib>
using namespace metal;
#define B(i) [[buffer(i)]]
#define GID [[thread_position_in_grid]]
#endif

#define MF_NODATA (-9999.0f)
#define MF_TEPS 1.0e-3f
#define MF_CFL_CAP 0.45f         // 2-D explicit Godunov stability ceiling
#define MF_HVEL 1.0e-6f          // depth below which a cell carries no velocity (desingularisation)

// edges in Params.free_mask (shared with kernels.metal)
#define MF_N 1u
#define MF_S 2u
#define MF_E 4u
#define MF_W 8u

// words of the state buffer (uint; the float ones are read through a float pointer). Shared with
// kernels_fv.metal: S_WMAX/S_WNEXT carry the max wave speed. S_STAGE carries the RK stage (0 or 1).
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
#define S_STAGE 16     // uint: current RK2 stage (0 = predictor, 1 = corrector)
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

// Buffer layout for the FV2 scheme (its own order, 0..32; Params at 33). Extends FV1 with the
// previous-step state (h0/hu0/hv0) for the RK2 blend and the per-cell limited slopes.
#define MF_ARGS_FV2 \
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
    device float* h0 B(24), \
    device float* hu0 B(25), \
    device float* hv0 B(26), \
    device float* sxe B(27), \
    device float* sye B(28), \
    device float* sxu B(29), \
    device float* syu B(30), \
    device float* sxv B(31), \
    device float* syv B(32), \
    constant Params& p B(33), \
    uint2 gid GID

struct F3 { float h, mn, mt; };   // flux: mass, normal momentum, tangential momentum

// -- helpers ------------------------------------------------------------------------------

inline float mf_vel(float hq, float hh) {
    return (hh > MF_HVEL) ? hq / hh : 0.0f;
}

// minmod of two slopes: 0 if they disagree in sign, else the one of smaller magnitude. TVD.
inline float mf_minmod(float a, float b) {
    if (a > 0.0f && b > 0.0f) {
        return fmin(a, b);
    }
    if (a < 0.0f && b < 0.0f) {
        return fmax(a, b);
    }
    return 0.0f;
}

// HLLC flux across a face, in the face-normal frame (identical to kernels_fv.metal).
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
    if (dryL) {
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

// one thread each: set the RK2 stage for the dispatches that follow in the plan.
kernel void k_stage_p_fv2(MF_ARGS_FV2) { st[S_STAGE] = 0u; }
kernel void k_stage_c_fv2(MF_ARGS_FV2) { st[S_STAGE] = 1u; }

// one thread: at stage 0, the step's dt from the CFL condition (max wave speed of U^n over the
// window) and the source rates at the start of the step, and save U^n for the RK2 blend. At stage
// 1 dt is already set (reused) and U^n is already saved, so this only resets the per-step atomics.
kernel void k_begin_fv2(MF_ARGS_FV2) {
    device float* sf = (device float*)st;
    uint stage = st[S_STAGE];
    if (stage == 0u) {
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
    st[S_WNEXT] = 0u;
    st[S_QOUT] = 0u;
}

// every cell of the window: minmod-limited slopes of eta = z + h, qx = hu, qy = hv in x and y. At a
// domain edge (or next to a NoData cell) the one-sided neighbour is taken equal to the cell, so the
// slope there collapses to 0 (first order at the boundary), which keeps the reconstruction safe.
kernel void k_slopes_fv2(MF_ARGS_FV2) {
    device float* sf = (device float*)st;
    if (sf[S_DT] <= 0.0f) {
        return;
    }
    uint nx = p.nx;
    uint ny = p.ny;
    uint r = p.r0 + gid.y;
    uint c = p.c0 + gid.x;
    uint i = r * nx + c;
    if (st[S_STAGE] == 0u) {          // predictor: save U^n for the RK2 blend before cont overwrites
        h0[i] = h[i];
        hu0[i] = hu[i];
        hv0[i] = hv[i];
    }
    if (ok[i] == 0) {
        sxe[i] = 0.0f; sye[i] = 0.0f; sxu[i] = 0.0f;
        syu[i] = 0.0f; sxv[i] = 0.0f; syv[i] = 0.0f;
        return;
    }
    float eI = z[i] + h[i];
    float uI = hu[i];
    float vI = hv[i];
    // x neighbours (fall back to self at an edge or a NoData neighbour)
    bool okL = (c > 0u) && ok[i - 1u] != 0;
    bool okR = (c + 1u < nx) && ok[i + 1u] != 0;
    float eL = okL ? (z[i - 1u] + h[i - 1u]) : eI;
    float eR = okR ? (z[i + 1u] + h[i + 1u]) : eI;
    float uL = okL ? hu[i - 1u] : uI;
    float uR = okR ? hu[i + 1u] : uI;
    float vL = okL ? hv[i - 1u] : vI;
    float vR = okR ? hv[i + 1u] : vI;
    sxe[i] = mf_minmod(eI - eL, eR - eI);
    sxu[i] = mf_minmod(uI - uL, uR - uI);
    sxv[i] = mf_minmod(vI - vL, vR - vI);
    // y neighbours
    bool okU = (r > 0u) && ok[i - nx] != 0;
    bool okD = (r + 1u < ny) && ok[i + nx] != 0;
    float eU = okU ? (z[i - nx] + h[i - nx]) : eI;
    float eD = okD ? (z[i + nx] + h[i + nx]) : eI;
    float uU = okU ? hu[i - nx] : uI;
    float uD = okD ? hu[i + nx] : uI;
    float vU = okU ? hv[i - nx] : vI;
    float vD = okD ? hv[i + nx] : vI;
    sye[i] = mf_minmod(eI - eU, eD - eI);
    syu[i] = mf_minmod(uI - uU, uD - uI);
    syv[i] = mf_minmod(vI - vU, vD - vI);
}

// every face of the window: HLLC flux of the MUSCL-reconstructed, hydrostatically reconstructed
// states. Same face ownership and domain-edge ghosting as kernels_fv.metal, but each side's state
// is extrapolated a half cell with its limited slope. The Audusse bed source is added in k_cont_fv2
// (it recomputes the cell's own reconstructed edge depths from the same slopes), well-balanced at
// 2nd order because the eta-slope vanishes at rest and FV2 then reduces exactly to FV1.
kernel void k_faces_fv2(MF_ARGS_FV2) {
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
    float outflow = 0.0f;

    if (gid.y < p.wr) {               // x-face at (row r, face column c), 0..nx
        uint f = r * (nx + 1u) + c;
        F3 fl;
        fl.h = 0.0f; fl.mn = 0.0f; fl.mt = 0.0f;
        if (c > 0u && c < nx) {       // interior face between l and rr
            uint l = r * nx + c - 1u;
            uint rr = l + 1u;
            if (ok[l] != 0 && ok[rr] != 0) {
                // reconstructed free surface at the shared face: L from its +x edge, R from its -x
                float etaL = (z[l] + h[l]) + 0.5f * sxe[l];
                float etaR = (z[rr] + h[rr]) - 0.5f * sxe[rr];
                float zf = fmax(z[l], z[rr]);     // first-order face bed (eta-slope is 0 at rest)
                float hLs = fmax(0.0f, etaL - zf);
                float hRs = fmax(0.0f, etaR - zf);
                float uL = mf_vel(hu[l] + 0.5f * sxu[l], fmax(0.0f, etaL - z[l]));
                float vL = mf_vel(hv[l] + 0.5f * sxv[l], fmax(0.0f, etaL - z[l]));
                float uR = mf_vel(hu[rr] - 0.5f * sxu[rr], fmax(0.0f, etaR - z[rr]));
                float vR = mf_vel(hv[rr] - 0.5f * sxv[rr], fmax(0.0f, etaR - z[rr]));
                fl = mf_hllc(hLs, uL, vL, hRs, uR, vR, g);
            }
        } else if (c == 0u) {         // left domain edge, cell i is the + side
            uint ii = r * nx;
            if (ok[ii] != 0 && h[ii] > MF_HVEL) {
                float ui = mf_vel(hu[ii], h[ii]);
                float vi = mf_vel(hv[ii], h[ii]);
                if ((p.free_mask & MF_W) != 0u) {
                    fl = mf_hllc(h[ii], ui, vi, h[ii], ui, vi, g);
                    outflow += fmax(-fl.h, 0.0f);
                } else {
                    fl = mf_hllc(h[ii], -ui, vi, h[ii], ui, vi, g);
                }
            }
        } else {                      // c == nx, right domain edge, cell i is the - side
            uint ii = r * nx + nx - 1u;
            if (ok[ii] != 0 && h[ii] > MF_HVEL) {
                float ui = mf_vel(hu[ii], h[ii]);
                float vi = mf_vel(hv[ii], h[ii]);
                if ((p.free_mask & MF_E) != 0u) {
                    fl = mf_hllc(h[ii], ui, vi, h[ii], ui, vi, g);
                    outflow += fmax(fl.h, 0.0f);
                } else {
                    fl = mf_hllc(h[ii], ui, vi, h[ii], -ui, vi, g);
                }
            }
        }
        fxh[f] = fl.h;
        fxhu[f] = fl.mn;
        fxhv[f] = fl.mt;
    }

    if (gid.x < p.wc) {               // y-face at (face row r, col c), 0..ny. Normal = y (south +).
        uint f = r * nx + c;
        F3 fl;
        fl.h = 0.0f; fl.mn = 0.0f; fl.mt = 0.0f;
        if (r > 0u && r < ny) {
            uint u = (r - 1u) * nx + c;
            uint d = u + nx;
            if (ok[u] != 0 && ok[d] != 0) {
                float etaU = (z[u] + h[u]) + 0.5f * sye[u];
                float etaD = (z[d] + h[d]) - 0.5f * sye[d];
                float zf = fmax(z[u], z[d]);
                float hUs = fmax(0.0f, etaU - zf);
                float hDs = fmax(0.0f, etaD - zf);
                float nU = mf_vel(hv[u] + 0.5f * syv[u], fmax(0.0f, etaU - z[u]));
                float tU = mf_vel(hu[u] + 0.5f * syu[u], fmax(0.0f, etaU - z[u]));
                float nD = mf_vel(hv[d] - 0.5f * syv[d], fmax(0.0f, etaD - z[d]));
                float tD = mf_vel(hu[d] - 0.5f * syu[d], fmax(0.0f, etaD - z[d]));
                fl = mf_hllc(hUs, nU, tU, hDs, nD, tD, g);
            }
        } else if (r == 0u) {         // north domain edge, cell i is the + (south) side
            uint ii = c;
            if (ok[ii] != 0 && h[ii] > MF_HVEL) {
                float ni = mf_vel(hv[ii], h[ii]);
                float ti = mf_vel(hu[ii], h[ii]);
                if ((p.free_mask & MF_N) != 0u) {
                    fl = mf_hllc(h[ii], ni, ti, h[ii], ni, ti, g);
                    outflow += fmax(-fl.h, 0.0f);
                } else {
                    fl = mf_hllc(h[ii], -ni, ti, h[ii], ni, ti, g);
                }
            }
        } else {                      // r == ny, south domain edge, cell i is the - side
            uint ii = (ny - 1u) * nx + c;
            if (ok[ii] != 0 && h[ii] > MF_HVEL) {
                float ni = mf_vel(hv[ii], h[ii]);
                float ti = mf_vel(hu[ii], h[ii]);
                if ((p.free_mask & MF_S) != 0u) {
                    fl = mf_hllc(h[ii], ni, ti, h[ii], ni, ti, g);
                    outflow += fmax(fl.h, 0.0f);
                } else {
                    fl = mf_hllc(h[ii], ni, ti, h[ii], -ni, ti, g);
                }
            }
        }
        fyh[f] = fl.h;
        fyhu[f] = fl.mt;
        fyhv[f] = fl.mn;
    }

    float tot = simd_sum(outflow);
    if (simd_is_first() && tot != 0.0f) {
        atomic_fetch_add_explicit((device atomic_uint*)(st + S_QOUT),
                                  uint(tot * p.dx / MF_FIX + 0.5f), memory_order_relaxed);
    }
}

// every cell of the window: the RK2 update. At stage 0 this forms U* = U^n + dt L(U^n) and saves it
// in place (U^n having been copied to h0/hu0/hv0 by the driver before stage 0). At stage 1 it forms
// U^{n+1} = 1/2 U^n + 1/2 (U* + dt L(U*)). The Audusse bed source uses the reconstructed cell-edge
// depths of the *current* stage's state. Source, friction, clip and maxima as in FV1; maxima and
// the next wave speed are taken on the corrector (stage 1) only.
kernel void k_cont_fv2(MF_ARGS_FV2) {
    device float* sf = (device float*)st;
    float dt = sf[S_DT];
    if (dt <= 0.0f) {
        return;
    }
    uint stage = st[S_STAGE];
    uint nx = p.nx;
    uint ny = p.ny;
    uint r = p.r0 + gid.y;
    uint c = p.c0 + gid.x;
    uint i = r * nx + c;
    uint fx = r * (nx + 1u) + c;
    float dx = p.dx;
    float g = p.g;

    float h0c = h[i];
    float etaI = z[i] + h0c;
    // reconstructed cell-edge depths for the Audusse source, consistent with k_faces_fv2
    float etaxR = etaI + 0.5f * sxe[i];
    float etaxL = etaI - 0.5f * sxe[i];
    float etayD = etaI + 0.5f * sye[i];
    float etayU = etaI - 0.5f * sye[i];
    float zxR = fmax(z[i], (c + 1u < nx) ? z[i + 1u] : z[i]);
    float zxL = fmax((c > 0u) ? z[i - 1u] : z[i], z[i]);
    float zyD = fmax(z[i], (r + 1u < ny) ? z[i + nx] : z[i]);
    float zyU = fmax((r > 0u) ? z[i - nx] : z[i], z[i]);
    float hxR = fmax(0.0f, etaxR - zxR);
    float hxL = fmax(0.0f, etaxL - zxL);
    float hyD = fmax(0.0f, etayD - zyD);
    float hyU = fmax(0.0f, etayU - zyU);
    float hcxR = fmax(0.0f, etaxR - z[i]);
    float hcxL = fmax(0.0f, etaxL - z[i]);
    float hcyD = fmax(0.0f, etayD - z[i]);
    float hcyU = fmax(0.0f, etayU - z[i]);
    float half_g = 0.5f * g;
    float SxR = half_g * (hcxR * hcxR - hxR * hxR);
    float SxL = half_g * (hcxL * hcxL - hxL * hxL);
    float SyD = half_g * (hcyD * hcyD - hyD * hyD);
    float SyU = half_g * (hcyU * hcyU - hyU * hyU);

    float invdx = dt / dx;
    float dh  = -invdx * ((fxh[fx + 1u] - fxh[fx]) + (fyh[i + nx] - fyh[i]));
    float dhu = -invdx * (((fxhu[fx + 1u] + SxR) - (fxhu[fx] + SxL))
                          + (fyhu[i + nx] - fyhu[i]));
    float dhv = -invdx * ((fxhv[fx + 1u] - fxhv[fx])
                          + ((fyhv[i + nx] + SyD) - (fyhv[i] + SyU)));

    float srcterm = src[i] * dt / (dx * dx);
    float hL, huL, hvL;
    if (stage == 0u) {               // predictor: U* = U^n + dt L(U^n) + source
        hL = h0c + dh + srcterm;
        huL = hu[i] + dhu;
        hvL = hv[i] + dhv;
    } else {                         // corrector: U^{n+1} = 1/2 U^n + 1/2 (U* + dt L(U*) + source)
        hL = 0.5f * (h0[i] + (h0c + dh + srcterm));
        huL = 0.5f * (hu0[i] + (hu[i] + dhu));
        hvL = 0.5f * (hv0[i] + (hv[i] + dhv));
    }

    float clip = 0.0f;
    if (hL <= 0.0f) {
        clip = -hL * dx * dx;
        hL = 0.0f;
        huL = 0.0f;
        hvL = 0.0f;
    } else {
        float un = mf_vel(huL, hL);
        float vn = mf_vel(hvL, hL);
        float spd = sqrt(un * un + vn * vn);
        if (spd > 0.0f && hL > MF_HVEL) {
            float cf = 1.0f + g * n2[i] * dt * spd / pow(hL, 4.0f / 3.0f);
            huL /= cf;
            hvL /= cf;
        }
    }
    if (ok[i] == 0) {
        hL = 0.0f; huL = 0.0f; hvL = 0.0f;
    }
    h[i] = hL;
    hu[i] = huL;
    hv[i] = hvL;

    // the predictor's clip volume is a transient of the half step; count only the corrector's
    if (stage == 0u) {
        return;
    }

    float th = (p.t0 + sf[S_T] + dt) / 3600.0f;
    float wcell = 0.0f;
    float vmagn = 0.0f;
    if (hL > MF_HVEL) {
        float un = mf_vel(huL, hL);
        float vn = mf_vel(hvL, hL);
        float cc = sqrt(g * hL);
        wcell = fmax(fabs(un) + cc, fabs(vn) + cc);
        vmagn = sqrt(un * un + vn * vn);
    }
    if (hL > p.dry) {
        if (hL > hmax[i]) {
            hmax[i] = hL;
            tmax[i] = th;
        }
        if (hL > p.arr_depth && arr[i] == MF_NODATA) {
            arr[i] = th;
        }
    }
    if (vmagn > vmax[i]) {
        vmax[i] = vmagn;
    }

    bool wet = hL > 0.0f;
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

// one thread: close the step (only after the corrector stage).
kernel void k_end_fv2(MF_ARGS_FV2) {
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
