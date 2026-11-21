// DamSafety shock-capturing flood kernels, DG2: a well-balanced, positivity-preserving second-order
// Runge-Kutta discontinuous Galerkin (RKDG2) solver for the 2-D shallow-water equations, one GPU
// thread per cell or face. It is the discontinuous-Galerkin companion of kernels_fv2.metal (the
// 2nd-order MUSCL finite-volume scheme): same HLLC flux, same Audusse well-balancing, same
// buffers-as-numpy design, same SSP-RK2 time stepping and CPU build path. The difference is the
// 2nd-order representation: DG2 carries, per cell and per conserved variable, a local planar
// expansion (an element-average coefficient and an x- and a y-slope coefficient) that is EVOLVED as
// its own unknown through the Galerkin weak form, rather than reconstructed from the neighbours each
// step (MUSCL). This is the "slope-decoupled" DG2 of Kesserwani et al. (2018), the scheme in
// LISFLOOD-FP 8.0: the x-slope evolves from the x-direction fluxes, the y-slope from the y-direction
// fluxes, so it maps onto the raster the same way the finite-volume schemes do.
//
// Numerics:
//   - per cell and per variable w in {h, hu, hv}: three modal coefficients (w0, w1x, w1y) in the
//     orthonormal Legendre basis {1, sqrt(3) xi, sqrt(3) eta}, xi,eta in [-1,1] over the cell. The
//     value at the +x cell edge is w0 + sqrt(3) w1x, at -x it is w0 - sqrt(3) w1x; the two internal
//     x-Gauss points (xi = +/- 1/sqrt(3)) carry w0 +/- w1x. Likewise in y. The modal coefficients
//     are stored packed, three per cell: Uh[3 i + 0] = h0, Uh[3 i + 1] = h1x, Uh[3 i + 2] = h1y;
//   - the bed z is piecewise constant per cell (its cell average, relative to the lowest cell), as
//     in kernels_fv2.metal: the sub-cell free-surface variation lives in the h slopes, and a lake at
//     rest has eta = z + h flat so every h slope is zero and DG2 reduces exactly to FV1 there;
//   - MUSCL is replaced by the DG weak form. At each interface the + and - edge states are the two
//     cells' modal edge values; Audusse et al. (2004) hydrostatic reconstruction (face bed
//     z_face = max(z_L, z_R), h* = max(0, eta_edge - z_face)) then an HLLC flux (identical mf_hllc as
//     the FV kernels). The element-average coefficient is updated from the interface fluxes exactly
//     as FV2 (with the Audusse bed source per face); each slope coefficient is updated from the
//     interface fluxes weighted by the edge basis value sqrt(3) MINUS the physical flux at the two
//     internal Gauss points, which is the DG2 volume term. The "combined" face momentum
//     (HLLC flux + Audusse source) is used for both the average and the slope, which makes the slope
//     equation well balanced: at rest every face and internal flux is 0.5 g (eta - z_cell)^2 and the
//     weighted combination cancels, so the slopes stay zero;
//   - positivity / wetting-drying: a cell at or below the dry depth is treated as first order (its
//     slopes are read as zero), and every reconstructed edge/Gauss depth is clamped to >= 0;
//   - an optional local slope limiter (Params.limiter != 0, the LISFLOOD-FP "limitslopes" option):
//     a minmod limiter of the modal slopes against the neighbour element-average jumps, limiting the
//     free surface eta for mass (so it stays well balanced) and the discharge slopes directly. Off
//     by default, which is what LISFLOOD-FP's plain dg2 uses and what Kesserwani et al. report is
//     better for typical flood flows; on, it is applied only where it actually clips a slope;
//   - SSP-RK2 (Heun) in time, exactly as kernels_fv2.metal: U* = U^n + dt L(U^n);
//     U^{n+1} = 1/2 U^n + 1/2 (U* + dt L(U*)); dt set once from U^n;
//   - dt = min(initial_tstep, cfl dx / max_cell(|u|+c, |v|+c)), cfl capped at MF_DG2_CFL = 1/3 (the
//     RKDG2 linear-stability limit, below the finite-volume 0.45), computed on the GPU every step;
//   - FREE domain edges are transmissive (zero-gradient ghost); closed edges are reflective walls.
//
// Compiled two ways, exactly like kernels_fv2.metal: by Metal at run time on a Mac, and as C++ with
// mf_shim.h (cpu_kernels_dg2.cpp) everywhere else. Every kernel shares one argument list
// (MF_ARGS_DG2, 27 buffers 0..26 + Params at 27, within Metal's 31-slot cap) so one dispatcher
// serves all.

#ifndef MF_CPU
#include <metal_stdlib>
using namespace metal;
#define B(i) [[buffer(i)]]
#define GID [[thread_position_in_grid]]
#endif

#define MF_NODATA (-9999.0f)
#define MF_TEPS 1.0e-3f
#define MF_DG2_CFL 0.3333333f    // RKDG2 linear-stability ceiling (1/3), below the FV 0.45
#define MF_HVEL 1.0e-6f          // depth below which a cell carries no velocity (desingularisation)
#define MF_SQ3 1.7320508075688772f   // sqrt(3): edge value = w0 +/- sqrt(3) w1
#define MF_DRYSLOPE 1.0e-3f      // at/below this cell-average depth a cell is first order (slopes 0)

// edges in Params.free_mask (shared with kernels.metal)
#define MF_N 1u
#define MF_S 2u
#define MF_E 4u
#define MF_W 8u

// words of the state buffer (shared with kernels_fv2.metal)
#define S_T 0
#define S_DT 1
#define S_WMAX 2
#define S_WNEXT 3
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
    uint limiter;         // DG2: 0 = no slope limiter (default), 1 = local minmod ("limitslopes")
    uint clip_end;
    uint pad2;
    float dx;
    float dt0;
    float alpha;          // CFL factor from the deck (capped at MF_DG2_CFL internally)
    float g;
    float dry;            // 1e-3 m: below this a cell carries no momentum
    float maxhflow;       // unused by the DG scheme (kept for the layout)
    float velmin;         // unused
    float arr_depth;      // depth that counts as arrival (inittm_depth)
    float t0;
    float t_end;
    float theta;          // unused
    float pad4;
};

// Buffer layout for the DG2 scheme (its own order, 0..26; Params at 27). The three conserved
// variables and their previous-step copy are stored packed (three modal coefficients per cell):
// Uh/Uhu/Uhv and Uh0/Uhu0/Uhv0 each hold 3*cells floats. The bed z is one float per cell (piecewise
// constant). The interface fluxes fxh..fyhv are as in the FV kernels. 27 buffers keep DG2 inside
// Metal's 31-slot argument-table cap.
#define MF_ARGS_DG2 \
    device const float* z B(0), \
    device const float* n2 B(1), \
    device float* Uh B(2), \
    device float* Uhu B(3), \
    device float* Uhv B(4), \
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
    device float* Uh0 B(24), \
    device float* Uhu0 B(25), \
    device float* Uhv0 B(26), \
    constant Params& p B(27), \
    uint2 gid GID

struct F3 { float h, mn, mt; };   // flux: mass, normal momentum, tangential momentum

// -- helpers ------------------------------------------------------------------------------

inline float mf_vel(float hq, float hh) {
    return (hh > MF_HVEL) ? hq / hh : 0.0f;
}

inline float mf_minmod(float a, float b) {
    if (a > 0.0f && b > 0.0f) { return fmin(a, b); }
    if (a < 0.0f && b < 0.0f) { return fmax(a, b); }
    return 0.0f;
}

inline float mf_minmod3(float a, float b, float c) {
    return mf_minmod(a, mf_minmod(b, c));
}

// HLLC flux across a face, in the face-normal frame (identical to kernels_fv2.metal / kernels_fv.metal).
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
    if (b <= a) { return 0.0f; }
    if (tm >= ser_t[b - 1]) { return ser_q[b - 1]; }
    if (tm <= ser_t[a]) { return ser_q[a]; }
    uint j = cursor[k];
    if (j < a || j + 1 >= b || ser_t[j] > tm) { j = a; }
    while (j + 2 < b && ser_t[j + 1] < tm) { j += 1u; }
    cursor[k] = j;
    float span = ser_t[j + 1] - ser_t[j];
    float ff = span > 0.0f ? (tm - ser_t[j]) / span : 1.0f;
    return ser_q[j] + ff * (ser_q[j + 1] - ser_q[j]);
}

// the modal slope of a cell's variable, read as 0 when the cell is at or below the dry depth (a
// dry / near-dry cell is first order: positivity-preserving at a wet/dry front). `comp` is 1 for the
// x-slope, 2 for the y-slope within the packed triplet.
inline float mf_sx(device const float* U, uint i, float h0c) {
    return (h0c > MF_DRYSLOPE) ? U[3u * i + 1u] : 0.0f;
}
inline float mf_sy(device const float* U, uint i, float h0c) {
    return (h0c > MF_DRYSLOPE) ? U[3u * i + 2u] : 0.0f;
}

// -- kernels --------------------------------------------------------------------------------

// one thread each: set the RK2 stage for the dispatches that follow in the plan.
kernel void k_stage_p_dg2(MF_ARGS_DG2) { st[S_STAGE] = 0u; }
kernel void k_stage_c_dg2(MF_ARGS_DG2) { st[S_STAGE] = 1u; }

// one thread: at stage 0, the step's dt from the CFL condition (max wave speed of U^n over the
// window, DG2's CFL = 1/3) and the source rates at the start of the step. At stage 1 dt is reused.
kernel void k_begin_dg2(MF_ARGS_DG2) {
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
        float cfl = fmin(p.alpha, MF_DG2_CFL);
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

// every cell of the window: at the predictor (stage 0) save U^n (all three modal coefficients of
// each variable) for the RK2 blend, before k_cont_dg2 overwrites the state. A no-op at the corrector.
kernel void k_save_dg2(MF_ARGS_DG2) {
    device float* sf = (device float*)st;
    if (sf[S_DT] <= 0.0f || st[S_STAGE] != 0u) {
        return;
    }
    uint r = p.r0 + gid.y;
    uint c = p.c0 + gid.x;
    uint i = r * p.nx + c;
    uint j = 3u * i;
    for (uint m = 0u; m < 3u; ++m) {
        Uh0[j + m] = Uh[j + m];
        Uhu0[j + m] = Uhu[j + m];
        Uhv0[j + m] = Uhv[j + m];
    }
}

// every cell of the window: the optional local slope limiter (Params.limiter != 0). A minmod limiter
// of the modal slopes against the neighbour element-average jumps: the free surface eta = z + h for
// the mass slope (so a limited lake stays well balanced), the discharges directly. Applied before
// the face sweep of each RK stage, only where a slope is actually clipped. A no-op when limiter == 0.
kernel void k_limit_dg2(MF_ARGS_DG2) {
    device float* sf = (device float*)st;
    if (sf[S_DT] <= 0.0f || p.limiter == 0u) {
        return;
    }
    uint nx = p.nx;
    uint ny = p.ny;
    uint r = p.r0 + gid.y;
    uint c = p.c0 + gid.x;
    uint i = r * nx + c;
    if (ok[i] == 0) { return; }
    float h0c = Uh[3u * i + 0u];
    if (h0c <= MF_DRYSLOPE) {       // dry / near-dry: already first order, nothing to limit
        Uh[3u * i + 1u] = 0.0f; Uh[3u * i + 2u] = 0.0f;
        Uhu[3u * i + 1u] = 0.0f; Uhu[3u * i + 2u] = 0.0f;
        Uhv[3u * i + 1u] = 0.0f; Uhv[3u * i + 2u] = 0.0f;
        return;
    }
    // the edge value is w0 +/- sqrt(3) w1, so the modal slope that matches a neighbour-average jump
    // dW is dW / sqrt(3). Limit sqrt(3) w1 against the one-sided jumps, then divide back.
    float etaC = z[i] + h0c;
    bool hlx = (c > 0u) && ok[i - 1u] != 0;
    bool hhx = (c + 1u < nx) && ok[i + 1u] != 0;
    float dEtaL = hlx ? (etaC - (z[i - 1u] + Uh[3u * (i - 1u)])) : 0.0f;
    float dEtaR = hhx ? ((z[i + 1u] + Uh[3u * (i + 1u)]) - etaC) : 0.0f;
    Uh[3u * i + 1u] = mf_minmod3(MF_SQ3 * Uh[3u * i + 1u], dEtaL, dEtaR) / MF_SQ3;
    float dUL = hlx ? (Uhu[3u * i] - Uhu[3u * (i - 1u)]) : 0.0f;
    float dUR = hhx ? (Uhu[3u * (i + 1u)] - Uhu[3u * i]) : 0.0f;
    Uhu[3u * i + 1u] = mf_minmod3(MF_SQ3 * Uhu[3u * i + 1u], dUL, dUR) / MF_SQ3;
    float dVL = hlx ? (Uhv[3u * i] - Uhv[3u * (i - 1u)]) : 0.0f;
    float dVR = hhx ? (Uhv[3u * (i + 1u)] - Uhv[3u * i]) : 0.0f;
    Uhv[3u * i + 1u] = mf_minmod3(MF_SQ3 * Uhv[3u * i + 1u], dVL, dVR) / MF_SQ3;
    bool hly = (r > 0u) && ok[i - nx] != 0;
    bool hhy = (r + 1u < ny) && ok[i + nx] != 0;
    float dEtaN = hly ? (etaC - (z[i - nx] + Uh[3u * (i - nx)])) : 0.0f;
    float dEtaS = hhy ? ((z[i + nx] + Uh[3u * (i + nx)]) - etaC) : 0.0f;
    Uh[3u * i + 2u] = mf_minmod3(MF_SQ3 * Uh[3u * i + 2u], dEtaN, dEtaS) / MF_SQ3;
    float dUN = hly ? (Uhu[3u * i] - Uhu[3u * (i - nx)]) : 0.0f;
    float dUS = hhy ? (Uhu[3u * (i + nx)] - Uhu[3u * i]) : 0.0f;
    Uhu[3u * i + 2u] = mf_minmod3(MF_SQ3 * Uhu[3u * i + 2u], dUN, dUS) / MF_SQ3;
    float dVN = hly ? (Uhv[3u * i] - Uhv[3u * (i - nx)]) : 0.0f;
    float dVS = hhy ? (Uhv[3u * (i + nx)] - Uhv[3u * i]) : 0.0f;
    Uhv[3u * i + 2u] = mf_minmod3(MF_SQ3 * Uhv[3u * i + 2u], dVN, dVS) / MF_SQ3;
}

// FREE-edge outflow floor: the Manning normal-depth velocity that the water-surface slope to the
// inner cell drives, so a flooded basin that decelerates into a FREE boundary still drains instead
// of ponding. 0 when the surface does not slope outward, and at an established outflow the cell's
// own velocity already exceeds it, so the transmissive outflow is only ever raised, never cut.
inline float mf_free_und(float etac, float etain, float hc, float n2c, float dx) {
    float slope = (etain - etac) / dx;
    if (slope <= 0.0f || hc <= MF_HVEL) {
        return 0.0f;
    }
    return sqrt(slope) * pow(hc, 2.0f / 3.0f) / sqrt(fmax(n2c, 1e-12f));
}

// every face of the window: HLLC flux of the DG2 modal edge states, Audusse hydrostatically
// reconstructed. Same face ownership and domain-edge ghosting as kernels_fv2.metal; each side's edge
// value is w0 +/- sqrt(3) w1 (the modal value at the shared edge). The Audusse bed source is added in
// k_cont_dg2 (per cell, from the same modal edge depths).
kernel void k_faces_dg2(MF_ARGS_DG2) {
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
        if (c > 0u && c < nx) {       // interior face between l (+x edge) and rr (-x edge)
            uint l = r * nx + c - 1u;
            uint rr = l + 1u;
            if (ok[l] != 0 && ok[rr] != 0) {
                float h0l = Uh[3u * l], h0r = Uh[3u * rr];
                float sel = mf_sx(Uh, l, h0l), ser = mf_sx(Uh, rr, h0r);
                float etaL = z[l] + h0l + MF_SQ3 * sel;      // L's +x edge
                float etaR = z[rr] + h0r - MF_SQ3 * ser;     // R's -x edge
                float zf = fmax(z[l], z[rr]);
                float hLs = fmax(0.0f, etaL - zf);
                float hRs = fmax(0.0f, etaR - zf);
                float huL = Uhu[3u * l] + MF_SQ3 * mf_sx(Uhu, l, h0l);
                float hvL = Uhv[3u * l] + MF_SQ3 * mf_sx(Uhv, l, h0l);
                float huR = Uhu[3u * rr] - MF_SQ3 * mf_sx(Uhu, rr, h0r);
                float hvR = Uhv[3u * rr] - MF_SQ3 * mf_sx(Uhv, rr, h0r);
                float uL = mf_vel(huL, fmax(0.0f, etaL - z[l]));
                float vL = mf_vel(hvL, fmax(0.0f, etaL - z[l]));
                float uR = mf_vel(huR, fmax(0.0f, etaR - z[rr]));
                float vR = mf_vel(hvR, fmax(0.0f, etaR - z[rr]));
                fl = mf_hllc(hLs, uL, vL, hRs, uR, vR, g);
            }
        } else if (c == 0u) {         // left domain edge, cell i is the + side
            uint ii = r * nx;
            float h0i = Uh[3u * ii];
            if (ok[ii] != 0 && h0i > MF_HVEL) {
                float ui = mf_vel(Uhu[3u * ii], h0i);
                float vi = mf_vel(Uhv[3u * ii], h0i);
                if ((p.free_mask & MF_W) != 0u) {
                    float uo = ui;
                    if (nx > 1u) {
                        float und = mf_free_und(z[ii] + h0i, z[ii + 1u] + Uh[3u * (ii + 1u)], h0i, n2[ii], p.dx);
                        if (und > 0.0f) uo = fmin(ui, -und);          // at least und outward (-x)
                    }
                    fl = mf_hllc(h0i, uo, vi, h0i, uo, vi, g);
                    outflow += fmax(-fl.h, 0.0f);
                } else {
                    fl = mf_hllc(h0i, -ui, vi, h0i, ui, vi, g);
                }
            }
        } else {                      // c == nx, right domain edge, cell i is the - side
            uint ii = r * nx + nx - 1u;
            float h0i = Uh[3u * ii];
            if (ok[ii] != 0 && h0i > MF_HVEL) {
                float ui = mf_vel(Uhu[3u * ii], h0i);
                float vi = mf_vel(Uhv[3u * ii], h0i);
                if ((p.free_mask & MF_E) != 0u) {
                    float uo = ui;
                    if (nx > 1u) {
                        float und = mf_free_und(z[ii] + h0i, z[ii - 1u] + Uh[3u * (ii - 1u)], h0i, n2[ii], p.dx);
                        if (und > 0.0f) uo = fmax(ui, und);           // at least und outward (+x)
                    }
                    fl = mf_hllc(h0i, uo, vi, h0i, uo, vi, g);
                    outflow += fmax(fl.h, 0.0f);
                } else {
                    fl = mf_hllc(h0i, ui, vi, h0i, -ui, vi, g);
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
                float h0u = Uh[3u * u], h0d = Uh[3u * d];
                float seu = mf_sy(Uh, u, h0u), sed = mf_sy(Uh, d, h0d);
                float etaU = z[u] + h0u + MF_SQ3 * seu;      // U's +y edge
                float etaD = z[d] + h0d - MF_SQ3 * sed;      // D's -y edge
                float zf = fmax(z[u], z[d]);
                float hUs = fmax(0.0f, etaU - zf);
                float hDs = fmax(0.0f, etaD - zf);
                float nU = mf_vel(Uhv[3u * u] + MF_SQ3 * mf_sy(Uhv, u, h0u), fmax(0.0f, etaU - z[u]));
                float tU = mf_vel(Uhu[3u * u] + MF_SQ3 * mf_sy(Uhu, u, h0u), fmax(0.0f, etaU - z[u]));
                float nD = mf_vel(Uhv[3u * d] - MF_SQ3 * mf_sy(Uhv, d, h0d), fmax(0.0f, etaD - z[d]));
                float tD = mf_vel(Uhu[3u * d] - MF_SQ3 * mf_sy(Uhu, d, h0d), fmax(0.0f, etaD - z[d]));
                fl = mf_hllc(hUs, nU, tU, hDs, nD, tD, g);
            }
        } else if (r == 0u) {         // north domain edge, cell i is the + (south) side
            uint ii = c;
            float h0i = Uh[3u * ii];
            if (ok[ii] != 0 && h0i > MF_HVEL) {
                float ni = mf_vel(Uhv[3u * ii], h0i);
                float ti = mf_vel(Uhu[3u * ii], h0i);
                if ((p.free_mask & MF_N) != 0u) {
                    float no = ni;
                    if (ny > 1u) {
                        float und = mf_free_und(z[ii] + h0i, z[ii + nx] + Uh[3u * (ii + nx)], h0i, n2[ii], p.dx);
                        if (und > 0.0f) no = fmin(ni, -und);          // at least und outward (-y)
                    }
                    fl = mf_hllc(h0i, no, ti, h0i, no, ti, g);
                    outflow += fmax(-fl.h, 0.0f);
                } else {
                    fl = mf_hllc(h0i, -ni, ti, h0i, ni, ti, g);
                }
            }
        } else {                      // r == ny, south domain edge, cell i is the - side
            uint ii = (ny - 1u) * nx + c;
            float h0i = Uh[3u * ii];
            if (ok[ii] != 0 && h0i > MF_HVEL) {
                float ni = mf_vel(Uhv[3u * ii], h0i);
                float ti = mf_vel(Uhu[3u * ii], h0i);
                if ((p.free_mask & MF_S) != 0u) {
                    float no = ni;
                    if (ny > 1u) {
                        float und = mf_free_und(z[ii] + h0i, z[ii - nx] + Uh[3u * (ii - nx)], h0i, n2[ii], p.dx);
                        if (und > 0.0f) no = fmax(ni, und);           // at least und outward (+y)
                    }
                    fl = mf_hllc(h0i, no, ti, h0i, no, ti, g);
                    outflow += fmax(fl.h, 0.0f);
                } else {
                    fl = mf_hllc(h0i, ni, ti, h0i, -ni, ti, g);
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

// every cell of the window: the DG2 RK2 update of all three modal coefficients of each variable. The
// element-average coefficient is updated from the interface fluxes (with the Audusse bed source) as
// FV2; each slope coefficient from the interface fluxes weighted by sqrt(3) minus the physical flux
// at the two internal Gauss points (the DG2 volume term). The friction, the depth >= 0 clip and the
// running maxima act on the element average, on the corrector stage.
kernel void k_cont_dg2(MF_ARGS_DG2) {
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
    uint j = 3u * i;
    uint fx = r * (nx + 1u) + c;
    float dx = p.dx;
    float g = p.g;
    float half_g = 0.5f * g;

    // current-stage modal coefficients (slopes zeroed when the cell is dry / near-dry)
    float h0 = Uh[j], hu0 = Uhu[j], hv0 = Uhv[j];
    float hx = mf_sx(Uh, i, h0), hy = mf_sy(Uh, i, h0);
    float ux = mf_sx(Uhu, i, h0), uy = mf_sy(Uhu, i, h0);
    float vx = mf_sx(Uhv, i, h0), vy = mf_sy(Uhv, i, h0);

    // -- Audusse bed source per face, from this cell's own modal edge depths (as FV2) --------
    float etaI = z[i] + h0;
    float etaxR = etaI + MF_SQ3 * hx, etaxL = etaI - MF_SQ3 * hx;
    float etayD = etaI + MF_SQ3 * hy, etayU = etaI - MF_SQ3 * hy;
    float zxR = fmax(z[i], (c + 1u < nx) ? z[i + 1u] : z[i]);
    float zxL = fmax((c > 0u) ? z[i - 1u] : z[i], z[i]);
    float zyD = fmax(z[i], (r + 1u < ny) ? z[i + nx] : z[i]);
    float zyU = fmax((r > 0u) ? z[i - nx] : z[i], z[i]);
    float hxR = fmax(0.0f, etaxR - zxR), hxL = fmax(0.0f, etaxL - zxL);
    float hyD = fmax(0.0f, etayD - zyD), hyU = fmax(0.0f, etayU - zyU);
    float hcxR = fmax(0.0f, etaxR - z[i]), hcxL = fmax(0.0f, etaxL - z[i]);
    float hcyD = fmax(0.0f, etayD - z[i]), hcyU = fmax(0.0f, etayU - z[i]);
    float SxR = half_g * (hcxR * hcxR - hxR * hxR);
    float SxL = half_g * (hcxL * hcxL - hxL * hxL);
    float SyD = half_g * (hcyD * hcyD - hyD * hyD);
    float SyU = half_g * (hcyU * hcyU - hyU * hyU);

    // "combined" face fluxes (HLLC + Audusse source) used by both the average and the slope updates
    float ChuE = fxhu[fx + 1u] + SxR, ChuW = fxhu[fx] + SxL;   // x normal momentum
    float ChvS = fyhv[i + nx] + SyD, ChvN = fyhv[i] + SyU;     // y normal momentum

    // -- internal Gauss-point physical fluxes (cell's own bed; xi,eta = +/- 1/sqrt(3)) -------
    // x direction: states w0 +/- w1x
    float hxp = fmax(0.0f, h0 + hx), hxm = fmax(0.0f, h0 - hx);
    float huxp = hu0 + ux, huxm = hu0 - ux;
    float hvxp = hv0 + vx, hvxm = hv0 - vx;
    float uxp = mf_vel(huxp, hxp), uxm = mf_vel(huxm, hxm);
    float Fh_xp = huxp,                         Fh_xm = huxm;                       // mass
    float Fhu_xp = huxp * uxp + half_g * hxp * hxp, Fhu_xm = huxm * uxm + half_g * hxm * hxm;
    float Fhv_xp = huxp * mf_vel(hvxp, hxp),    Fhv_xm = huxm * mf_vel(hvxm, hxm);  // tangential
    // y direction: states w0 +/- w1y
    float hyp = fmax(0.0f, h0 + hy), hym = fmax(0.0f, h0 - hy);
    float huyp = hu0 + uy, huym = hu0 - uy;
    float hvyp = hv0 + vy, hvym = hv0 - vy;
    float vyp = mf_vel(hvyp, hyp), vym = mf_vel(hvym, hym);
    float Gh_yp = hvyp,                         Gh_ym = hvym;                       // mass
    float Ghv_yp = hvyp * vyp + half_g * hyp * hyp, Ghv_ym = hvym * vym + half_g * hym * hym;
    float Ghu_yp = hvyp * mf_vel(huyp, hyp),    Ghu_ym = hvym * mf_vel(huym, hym);  // tangential

    float invdx = dt / dx;
    float s3 = MF_SQ3 * invdx;

    // -- residuals L(U) for the three modal coefficients of each variable --------------------
    // element average (coefficient 0): interface-flux divergence + source (as FV2)
    float dh0  = -invdx * ((fxh[fx + 1u] - fxh[fx]) + (fyh[i + nx] - fyh[i]));
    float dhu0 = -invdx * ((ChuE - ChuW) + (fyhu[i + nx] - fyhu[i]));
    float dhv0 = -invdx * ((fxhv[fx + 1u] - fxhv[fx]) + (ChvS - ChvN));
    // x-slope (coefficient 1): sqrt(3) (interface sum) - sqrt(3) (internal Gauss sum), x only
    float dhx  = -s3 * ((fxh[fx + 1u] + fxh[fx]) - (Fh_xp + Fh_xm));
    float dhux = -s3 * ((ChuE + ChuW) - (Fhu_xp + Fhu_xm));
    float dhvx = -s3 * ((fxhv[fx + 1u] + fxhv[fx]) - (Fhv_xp + Fhv_xm));
    // y-slope (coefficient 2): y only
    float dhy  = -s3 * ((fyh[i + nx] + fyh[i]) - (Gh_yp + Gh_ym));
    float dhuy = -s3 * ((fyhu[i + nx] + fyhu[i]) - (Ghu_yp + Ghu_ym));
    float dhvy = -s3 * ((ChvS + ChvN) - (Ghv_yp + Ghv_ym));

    float srcterm = src[i] * dt / (dx * dx);     // point source into the element average

    // -- SSP-RK2 blend, per modal coefficient ------------------------------------------------
    float nh0, nhu0, nhv0, nhx, nhux, nhvx, nhy, nhuy, nhvy;
    if (stage == 0u) {               // predictor: U* = U^n + dt L(U^n)
        nh0 = h0 + dh0 + srcterm;  nhu0 = hu0 + dhu0;  nhv0 = hv0 + dhv0;
        nhx = hx + dhx;  nhux = ux + dhux;  nhvx = vx + dhvx;
        nhy = hy + dhy;  nhuy = uy + dhuy;  nhvy = vy + dhvy;
    } else {                         // corrector: U^{n+1} = 1/2 U^n + 1/2 (U* + dt L(U*))
        nh0  = 0.5f * (Uh0[j]      + (h0 + dh0 + srcterm));
        nhu0 = 0.5f * (Uhu0[j]     + (hu0 + dhu0));
        nhv0 = 0.5f * (Uhv0[j]     + (hv0 + dhv0));
        nhx  = 0.5f * (Uh0[j + 1u] + (hx + dhx));
        nhux = 0.5f * (Uhu0[j + 1u] + (ux + dhux));
        nhvx = 0.5f * (Uhv0[j + 1u] + (vx + dhvx));
        nhy  = 0.5f * (Uh0[j + 2u] + (hy + dhy));
        nhuy = 0.5f * (Uhu0[j + 2u] + (uy + dhuy));
        nhvy = 0.5f * (Uhv0[j + 2u] + (vy + dhvy));
    }

    float clip = 0.0f;
    if (nh0 <= 0.0f) {               // positivity: a dry element average is first order and still
        clip = -nh0 * dx * dx;
        nh0 = 0.0f;
        nhu0 = 0.0f; nhv0 = 0.0f;
        nhx = 0.0f; nhux = 0.0f; nhvx = 0.0f;
        nhy = 0.0f; nhuy = 0.0f; nhvy = 0.0f;
    } else {
        float un = mf_vel(nhu0, nh0);
        float vn = mf_vel(nhv0, nh0);
        float spd = sqrt(un * un + vn * vn);
        if (spd > 0.0f && nh0 > MF_HVEL) {       // semi-implicit Manning friction on the average
            float cf = 1.0f + g * n2[i] * dt * spd / pow(nh0, 4.0f / 3.0f);
            nhu0 /= cf;
            nhv0 /= cf;
        }
    }
    if (ok[i] == 0) {
        nh0 = 0.0f; nhu0 = 0.0f; nhv0 = 0.0f;
        nhx = 0.0f; nhux = 0.0f; nhvx = 0.0f;
        nhy = 0.0f; nhuy = 0.0f; nhvy = 0.0f;
    }
    Uh[j] = nh0;   Uh[j + 1u] = nhx;   Uh[j + 2u] = nhy;
    Uhu[j] = nhu0; Uhu[j + 1u] = nhux; Uhu[j + 2u] = nhuy;
    Uhv[j] = nhv0; Uhv[j + 1u] = nhvx; Uhv[j + 2u] = nhvy;

    if (stage == 0u) {               // the predictor's clip is a half-step transient; count the corrector's
        return;
    }

    float th = (p.t0 + sf[S_T] + dt) / 3600.0f;
    float wcell = 0.0f;
    float vmagn = 0.0f;
    if (nh0 > MF_HVEL) {
        float un = mf_vel(nhu0, nh0);
        float vn = mf_vel(nhv0, nh0);
        float cc = sqrt(g * nh0);
        wcell = fmax(fabs(un) + cc, fabs(vn) + cc);
        vmagn = sqrt(un * un + vn * vn);
    }
    if (nh0 > p.dry) {
        if (nh0 > hmax[i]) {
            hmax[i] = nh0;
            tmax[i] = th;
        }
        if (nh0 > p.arr_depth && arr[i] == MF_NODATA) {
            arr[i] = th;
        }
    }
    if (vmagn > vmax[i]) {
        vmax[i] = vmagn;
    }

    bool wet = nh0 > 0.0f;
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
kernel void k_end_dg2(MF_ARGS_DG2) {
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
