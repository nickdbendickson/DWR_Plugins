// DamSafety sub-grid finite-volume kernels: the shock-capturing HLLC scheme of kernels_fv.metal,
// but each coarse cell keeps its r x r fine (LiDAR) sub-cells as stage tables, so a channel the
// flat cell loses is retained in the cell storage and the face conveyance. The GPU/CPU mirror of
// subfv_hllc_ref.py (the float64 oracle); it reduces to kernels_fv.metal exactly on a flat fine DEM.
//
// Numerics (see subfv_hllc_ref.py for the derivation):
//   - conserved per cell: water VOLUME V and the two total momenta Px, Py (so u = Px / V). The
//     water-surface elevation eta is V inverted through the cell hypsometric curve (the sorted
//     sub-elevations s, m = r*r per cell), recomputed each step into the eta buffer;
//   - per face: Audusse reconstruction on the shared sub-bed sill gives each side's wet area A,
//     wet top width W and first moment I1; the HLLC Riemann flux is in cross-sectional (A, Q)
//     space, hydraulic depth A/W for the wave speeds and g*I1 for pressure, so a flux is the whole
//     face discharge / momentum;
//   - well-balanced Audusse bed source g*(I1 at the +face - I1 at the -face), recomputed in k_cont
//     from the cell's own eta; keeps a lake at rest over any sub-grid topography;
//   - semi-implicit Manning friction on the updated momentum, cell hydraulic depth V/P and the
//     per-cell composite roughness nc2.
//
// Compiled two ways, exactly like kernels_fv.metal: by Metal at run time on a Mac, and as C++ with
// mf_shim.h (cpu_kernels_subfv.cpp) everywhere else. Buffers: the all-x-faces profiles (0..nx)
// carry the FREE-edge sub-bed in their boundary slots, so no separate edge buffers; csum, zbed and
// the face first moments are recomputed inline. 29 buffers + Params at 29 = 30 slots (<= Metal's 31).

#ifndef MF_CPU
#include <metal_stdlib>
using namespace metal;
#define B(i) [[buffer(i)]]
#define GID [[thread_position_in_grid]]
#endif

#define MF_NODATA (-9999.0f)
#define MF_CFL_CAP 0.45f
#define MF_HVEL 1.0e-6f          // depth floor for velocity desingularisation
#define MF_SLOPE_EPS 1.0e-6f     // FREE-edge normal-depth floor deadband: a surface flat to
//                                 within this slope has no drainage drive (sqrt(slope) ill-
//                                 conditioned as slope -> 0); below it the transmissive flux stands.
#define MF_DRY 1.0e-3f
#define MF_MSUB 64               // max r*r supported (r <= 8); local scratch for the curve loops

#define MF_N 1u
#define MF_S 2u
#define MF_E 4u
#define MF_W 8u

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
#define S_WORDS 32
#define MF_FIX 1.0e-4f

struct Params {
    uint nx, ny;
    uint r0, c0;
    uint wr, wc;
    uint free_mask;
    uint n_src;
    uint n_series;
    uint r;               // fine sub-cells per coarse cell per axis
    uint m;               // r*r, the stride of the cell curve buffer s
    uint clip_end;
    float dx;
    float dt0;
    float alpha;          // CFL factor (capped at MF_CFL_CAP)
    float g;
    float dry;            // 1e-3 m
    float arr_depth;
    float t0;
    float t_end;
    float wsub;           // dx / r
    float pad;
};

// Buffer layout (0..28; Params at 29):
#define MF_ARGS_SUBFV \
    device const float* s B(0), \
    device const float* nc2 B(1), \
    device float* V B(2), \
    device float* Px B(3), \
    device float* Py B(4), \
    device float* eta B(5), \
    device float* src B(6), \
    device float* hmax B(7), \
    device float* tmax B(8), \
    device float* arr B(9), \
    device float* vmax B(10), \
    device const uchar* ok B(11), \
    device const uint* s_cell B(12), \
    device const uint* s_series B(13), \
    device const uint* ser_off B(14), \
    device const float* ser_t B(15), \
    device const float* ser_q B(16), \
    device uint* cursor B(17), \
    device uint* st B(18), \
    device float* fxA B(19), \
    device float* fxN B(20), \
    device float* fxT B(21), \
    device float* fyA B(22), \
    device float* fyN B(23), \
    device float* fyT B(24), \
    device const float* zfx B(25), \
    device const float* nf2x B(26), \
    device const float* zfy B(27), \
    device const float* nf2y B(28), \
    constant Params& p B(29), \
    uint2 gid GID

// -- cell stage curves (sorted sub-elevations s[0..m-1] for one cell) ------------------------------

// volume V(eta) = asub * sum_j max(eta - s[j], 0); asub = wsub*wsub
inline float mf_cell_volume(device const float* sc, uint m, float asub, float eta) {
    float acc = 0.0f;
    for (uint j = 0; j < m; ++j) {
        float d = eta - sc[j];
        if (d > 0.0f) acc += d;
    }
    return asub * acc;
}

// eta from V: V is piecewise linear, increasing, breakpoints at s[k]. On the segment above s[k],
// k+1 sub-cells wet and V = asub*((k+1) eta - sum_{j<=k} s[j]) -> eta = (V/asub + csum_k)/(k+1).
inline float mf_invert_eta(device const float* sc, uint m, float asub, float Vcell) {
    float V0 = fmax(Vcell, 0.0f);
    float csum = 0.0f;       // sum_{j<=k} s[j]
    float pref = 0.0f;       // sum_{j<k}  s[j]
    uint k = 0;
    // find the largest k with V at breakpoint eta=s[k] (= asub*(k*s[k] - pref)) <= V0
    for (uint j = 0; j < m; ++j) {
        float Vb = asub * (float(j) * sc[j] - pref);
        if (Vb <= V0) { k = j; csum = pref + sc[j]; }
        else break;
        pref += sc[j];
    }
    return (V0 / asub + csum) / float(k + 1);
}

inline float mf_cell_area(device const float* sc, uint m, float asub, float eta) {
    float c = 0.0f;
    for (uint j = 0; j < m; ++j) if (sc[j] < eta) c += 1.0f;
    return asub * c;
}

// face reconstruction at level eta over a sub-bed profile zf[0..r-1]: wet area A, top width W,
// first moment I1 = sum wsub * 0.5 * h^2.
inline void mf_face_recon(device const float* zf, uint r, float wsub, float eta,
                          thread float& A, thread float& W, thread float& I1) {
    float a = 0.0f, w = 0.0f, i1 = 0.0f;
    for (uint k = 0; k < r; ++k) {
        float h = eta - zf[k];
        if (h > 0.0f) { a += h; w += 1.0f; i1 += 0.5f * h * h; }
    }
    A = wsub * a; W = wsub * w; I1 = wsub * i1;
}

struct F3 { float A, Fn, Ft, smax; };   // whole-face fluxes: mass, normal mom, tang mom, |wave|

// cross-sectional HLLC in (A, Q) space (mirrors mf_hllc of kernels_fv.metal, scaled by width)
inline F3 mf_hllc_sub(float AL, float WL, float I1L, float uL, float vL,
                      float AR, float WR, float I1R, float uR, float vR, float g) {
    F3 f; f.A = 0.0f; f.Fn = 0.0f; f.Ft = 0.0f; f.smax = 0.0f;
    bool dryL = AL <= 0.0f;
    bool dryR = AR <= 0.0f;
    if (dryL && dryR) return f;
    float hcL = AL / fmax(WL, 1.0e-12f);
    float hcR = AR / fmax(WR, 1.0e-12f);
    float cL = sqrt(g * fmax(hcL, 0.0f));
    float cR = sqrt(g * fmax(hcR, 0.0f));
    float SL, SR;
    if (dryL) { SL = uR - 2.0f * cR; SR = uR + cR; }
    else if (dryR) { SL = uL - cL; SR = uL + 2.0f * cL; }
    else {
        float ustar = 0.5f * (uL + uR) + cL - cR;
        float cstar = 0.5f * (cL + cR) + 0.25f * (uL - uR);
        SL = fmin(uL - cL, ustar - cstar);
        SR = fmax(uR + cR, ustar + cstar);
    }
    float FAL = AL * uL;
    float FAR = AR * uR;
    float FNL = AL * uL * uL + g * I1L;
    float FNR = AR * uR * uR + g * I1R;
    if (SL >= 0.0f) { f.A = FAL; f.Fn = FNL; f.Ft = FAL * vL; }
    else if (SR <= 0.0f) { f.A = FAR; f.Fn = FNR; f.Ft = FAR * vR; }
    else {
        float inv = 1.0f / (SR - SL);
        float FA = (SR * FAL - SL * FAR + SL * SR * (AR - AL)) * inv;
        float FN = (SR * FNL - SL * FNR + SL * SR * (AR * uR - AL * uL)) * inv;
        float num = SL * AR * (uR - SR) - SR * AL * (uL - SL);
        float den = AR * (uR - SR) - AL * (uL - SL);
        float Sstar = (fabs(den) > 1.0e-12f) ? num / den : 0.5f * (SL + SR);
        f.A = FA; f.Fn = FN; f.Ft = FA * (Sstar >= 0.0f ? vL : vR);
    }
    f.smax = fmax(fabs(SL), fabs(SR));
    return f;
}

inline float mf_vel(float mom, float Vol, float vfloor) {
    return (Vol > vfloor) ? mom / Vol : 0.0f;
}

inline float mf_series(uint k, float tm, device const uint* ser_off,
                       device const float* ser_t, device const float* ser_q, device uint* cursor) {
    uint a = ser_off[k];
    uint b = ser_off[k + 1];
    if (b <= a) return 0.0f;
    if (tm >= ser_t[b - 1]) return ser_q[b - 1];
    if (tm <= ser_t[a]) return ser_q[a];
    uint j = cursor[k];
    if (j < a || j + 1 >= b || ser_t[j] > tm) j = a;
    while (j + 2 < b && ser_t[j + 1] < tm) j += 1u;
    cursor[k] = j;
    float span = ser_t[j + 1] - ser_t[j];
    float ff = span > 0.0f ? (tm - ser_t[j]) / span : 1.0f;
    return ser_q[j] + ff * (ser_q[j + 1] - ser_q[j]);
}

// -- kernels ---------------------------------------------------------------------------------------

// one thread: this step's dt from the max wave speed, and the source rates
kernel void k_begin_subfv(MF_ARGS_SUBFV) {
    device float* sf = (device float*)st;
    float wmax = as_type<float>(st[S_WMAX]);
    float cfl = fmin(p.alpha, MF_CFL_CAP);
    float dt = (wmax > 0.0f) ? cfl * p.dx / wmax : p.dt0;
    if (p.dt0 > 0.0f) dt = fmin(dt, p.dt0);
    float remain = p.t_end - sf[S_T];
    if (dt > remain) dt = remain;
    sf[S_DT] = (remain > 0.0f) ? dt : 0.0f;
    st[S_WNEXT] = 0u;
    float qin = 0.0f;
    for (uint i = 0; i < p.n_src; ++i) src[s_cell[i]] = 0.0f;
    for (uint i = 0; i < p.n_src; ++i) {
        float q = mf_series(s_series[i], p.t0 + sf[S_T] + dt, ser_off, ser_t, ser_q, cursor);
        src[s_cell[i]] += q;
        qin += q;
    }
    sf[S_QIN] = qin;
    st[S_QOUT] = 0u;
}

// every cell: water-surface elevation from the current volume
kernel void k_eta_subfv(MF_ARGS_SUBFV) {
    device float* sf = (device float*)st;
    if (sf[S_DT] <= 0.0f) return;
    uint nx = p.nx;
    uint r = p.r0 + gid.y;
    uint c = p.c0 + gid.x;
    uint i = r * nx + c;
    float asub = p.wsub * p.wsub;
    device const float* sc = s + (size_t)i * p.m;
    eta[i] = (ok[i] != 0) ? mf_invert_eta(sc, p.m, asub, V[i]) : sc[0];
}

// FREE-edge outflow floor: the Manning normal-depth velocity that the water-surface slope to the
// inner cell drives through this face's sub-grid conveyance (area A, top width W, roughness nc2).
// At a coarse cell a narrow sub-grid outlet carries the flow at a far higher velocity than the
// cell's bulk u, so the pure transmissive flux (A * bulk u) under-drains and the valley ponds; this
// floor restores the drainage. It is 0 when the surface does not slope outward (slope <= 0), so a
// lake at rest at a FREE edge stays at rest, and at native resolution the bulk u already exceeds it.
inline float mf_subfv_und(float etac, float etain, float A, float W, float nc2c, float dx) {
    if (A <= 0.0f) return 0.0f;
    float slope = (etain - etac) / dx;
    if (slope <= MF_SLOPE_EPS) return 0.0f;   // deadband: no drainage drive below this surface slope
    float Rh = A / fmax(W, 1e-9f);
    return sqrt(slope) * pow(Rh, 2.0f / 3.0f) / sqrt(fmax(nc2c, 1e-12f));
}

// every face: HLLC flux of the Audusse-reconstructed sub-grid states
kernel void k_faces_subfv(MF_ARGS_SUBFV) {
    device float* sf = (device float*)st;
    if (sf[S_DT] <= 0.0f) return;
    uint nx = p.nx, ny = p.ny, r = p.r, R = r;
    uint row = p.r0 + gid.y;
    uint c = p.c0 + gid.x;
    float g = p.g, wsub = p.wsub, dx = p.dx;
    float vfloor = MF_HVEL * dx * dx;
    float outflow = 0.0f;
    uint fm = p.free_mask;

    if (gid.y < p.wr) {               // x-face at (row, face col c), 0..nx
        uint fcol = c;
        device const float* zf = zfx + ((size_t)row * (nx + 1) + fcol) * R;
        F3 f; f.A = 0.0f; f.Fn = 0.0f; f.Ft = 0.0f; f.smax = 0.0f;
        if (fcol > 0u && fcol < nx) {
            uint l = row * nx + fcol - 1u;
            uint rr = l + 1u;
            if (ok[l] != 0 && ok[rr] != 0) {
                float AL, WL, I1L, AR, WR, I1R;
                mf_face_recon(zf, R, wsub, eta[l], AL, WL, I1L);
                mf_face_recon(zf, R, wsub, eta[rr], AR, WR, I1R);
                float uL = mf_vel(Px[l], V[l], vfloor), vL = mf_vel(Py[l], V[l], vfloor);
                float uR = mf_vel(Px[rr], V[rr], vfloor), vR = mf_vel(Py[rr], V[rr], vfloor);
                f = mf_hllc_sub(AL, WL, I1L, uL, vL, AR, WR, I1R, uR, vR, g);
            }
        } else if (fcol == 0u) {      // west edge, cell is the + side
            uint i = row * nx;
            if (ok[i] != 0) {
                float A, W, I1; mf_face_recon(zf, R, wsub, eta[i], A, W, I1);
                float u = mf_vel(Px[i], V[i], vfloor), v = mf_vel(Py[i], V[i], vfloor);
                if (fm & MF_W) {
                    float und = (nx > 1u) ? mf_subfv_und(eta[i], eta[i + 1u], A, W, nc2[i], dx) : 0.0f;
                    if (und > 0.0f) u = fmin(u, -und);     // at least und outward (-x)
                    u = fmin(u, 0.0f);                       // outflow only: nothing enters from outside
                    f.A = A * u; f.Fn = A * u * u + g * I1; f.Ft = A * u * v;
                    outflow += fmax(-f.A, 0.0f);
                }
                else { f.Fn = g * I1; }
            }
        } else {                      // east edge, cell is the - side
            uint i = row * nx + nx - 1u;
            if (ok[i] != 0) {
                float A, W, I1; mf_face_recon(zf, R, wsub, eta[i], A, W, I1);
                float u = mf_vel(Px[i], V[i], vfloor), v = mf_vel(Py[i], V[i], vfloor);
                if (fm & MF_E) {
                    float und = (nx > 1u) ? mf_subfv_und(eta[i], eta[i - 1u], A, W, nc2[i], dx) : 0.0f;
                    if (und > 0.0f) u = fmax(u, und);      // at least und outward (+x)
                    u = fmax(u, 0.0f);                       // outflow only: nothing enters from outside
                    f.A = A * u; f.Fn = A * u * u + g * I1; f.Ft = A * u * v;
                    outflow += fmax(f.A, 0.0f);
                }
                else { f.Fn = g * I1; }
            }
        }
        uint fi = row * (nx + 1u) + fcol;
        fxA[fi] = f.A; fxN[fi] = f.Fn; fxT[fi] = f.Ft;
        if (f.smax > as_type<float>(st[S_WNEXT]))
            atomic_fetch_max_explicit((device atomic_uint*)(st + S_WNEXT), as_type<uint>(f.smax),
                                      memory_order_relaxed);
    }

    if (gid.x < p.wc) {               // y-face at (face row, col c), 0..ny. Normal +y = south.
        uint frow = row;
        device const float* zf = zfy + ((size_t)frow * nx + c) * R;
        F3 f; f.A = 0.0f; f.Fn = 0.0f; f.Ft = 0.0f; f.smax = 0.0f;
        if (frow > 0u && frow < ny) {
            uint u = (frow - 1u) * nx + c;
            uint d = u + nx;
            if (ok[u] != 0 && ok[d] != 0) {
                float AU, WU, I1U, AD, WD, I1D;
                mf_face_recon(zf, R, wsub, eta[u], AU, WU, I1U);
                mf_face_recon(zf, R, wsub, eta[d], AD, WD, I1D);
                // normal is +y (v-component rides normal), tangential is +x (u)
                float nU = mf_vel(Py[u], V[u], vfloor), tU = mf_vel(Px[u], V[u], vfloor);
                float nD = mf_vel(Py[d], V[d], vfloor), tD = mf_vel(Px[d], V[d], vfloor);
                f = mf_hllc_sub(AU, WU, I1U, nU, tU, AD, WD, I1D, nD, tD, g);
            }
        } else if (frow == 0u) {      // north edge, cell is the + (south) side
            uint i = c;
            if (ok[i] != 0) {
                float A, W, I1; mf_face_recon(zf, R, wsub, eta[i], A, W, I1);
                float nv = mf_vel(Py[i], V[i], vfloor), tu = mf_vel(Px[i], V[i], vfloor);
                if (fm & MF_N) {
                    float und = (ny > 1u) ? mf_subfv_und(eta[i], eta[i + nx], A, W, nc2[i], dx) : 0.0f;
                    if (und > 0.0f) nv = fmin(nv, -und);   // at least und outward (-y)
                    nv = fmin(nv, 0.0f);                     // outflow only: nothing enters from outside
                    f.A = A * nv; f.Fn = A * nv * nv + g * I1; f.Ft = A * nv * tu;
                    outflow += fmax(-f.A, 0.0f);
                }
                else { f.Fn = g * I1; }
            }
        } else {                      // south edge, cell is the - side
            uint i = (ny - 1u) * nx + c;
            if (ok[i] != 0) {
                float A, W, I1; mf_face_recon(zf, R, wsub, eta[i], A, W, I1);
                float nv = mf_vel(Py[i], V[i], vfloor), tu = mf_vel(Px[i], V[i], vfloor);
                if (fm & MF_S) {
                    float und = (ny > 1u) ? mf_subfv_und(eta[i], eta[i - nx], A, W, nc2[i], dx) : 0.0f;
                    if (und > 0.0f) nv = fmax(nv, und);    // at least und outward (+y)
                    nv = fmax(nv, 0.0f);                     // outflow only: nothing enters from outside
                    f.A = A * nv; f.Fn = A * nv * nv + g * I1; f.Ft = A * nv * tu;
                    outflow += fmax(f.A, 0.0f);
                }
                else { f.Fn = g * I1; }
            }
        }
        uint fi = frow * nx + c;
        fyA[fi] = f.A; fyN[fi] = f.Fn; fyT[fi] = f.Ft;
        if (f.smax > as_type<float>(st[S_WNEXT]))
            atomic_fetch_max_explicit((device atomic_uint*)(st + S_WNEXT), as_type<uint>(f.smax),
                                      memory_order_relaxed);
    }

    float tot = simd_sum(outflow);
    if (simd_is_first() && tot != 0.0f)
        atomic_fetch_add_explicit((device atomic_uint*)(st + S_QOUT),
                                  uint(tot / MF_FIX + 0.5f), memory_order_relaxed);
}

// every cell: finite-volume update with the well-balanced bed source, friction, V>=0 clip, maxima
kernel void k_cont_subfv(MF_ARGS_SUBFV) {
    device float* sf = (device float*)st;
    float dt = sf[S_DT];
    if (dt <= 0.0f) return;
    uint nx = p.nx, ny = p.ny, R = p.r;
    uint row = p.r0 + gid.y;
    uint c = p.c0 + gid.x;
    uint i = row * nx + c;
    float g = p.g, wsub = p.wsub, dx = p.dx;
    float asub = wsub * wsub;
    uint fx = row * (nx + 1u) + c;       // left x-face; right is fx+1
    device const float* sc = s + (size_t)i * p.m;

    // bed source: this cell's first moment at its four faces, from its own eta
    float etaI = eta[i];
    device const float* zfxR = zfx + ((size_t)row * (nx + 1) + c + 1) * R;
    device const float* zfxL = zfx + ((size_t)row * (nx + 1) + c) * R;
    device const float* zfyD = zfy + ((size_t)(row + 1) * nx + c) * R;
    device const float* zfyU = zfy + ((size_t)row * nx + c) * R;
    float A_, W_, I1xR, I1xL, I1yD, I1yU;
    mf_face_recon(zfxR, R, wsub, etaI, A_, W_, I1xR);
    mf_face_recon(zfxL, R, wsub, etaI, A_, W_, I1xL);
    mf_face_recon(zfyD, R, wsub, etaI, A_, W_, I1yD);
    mf_face_recon(zfyU, R, wsub, etaI, A_, W_, I1yU);
    float bedx = g * (I1xR - I1xL);
    float bedy = g * (I1yD - I1yU);

    float dV = -dt * ((fxA[fx + 1u] - fxA[fx]) + (fyA[i + nx] - fyA[i])) + src[i] * dt;
    float dPx = -dt * ((fxN[fx + 1u] - fxN[fx]) + (fyT[i + nx] - fyT[i])) + dt * bedx;
    float dPy = -dt * ((fxT[fx + 1u] - fxT[fx]) + (fyN[i + nx] - fyN[i])) + dt * bedy;

    float Vn = V[i] + dV;
    float Pxn = Px[i] + dPx;
    float Pyn = Py[i] + dPy;

    float clip = 0.0f;
    if (Vn < 0.0f) { clip = -Vn; Vn = 0.0f; Pxn = 0.0f; Pyn = 0.0f; }

    // invert to the new eta, friction with the cell hydraulic depth V/P and composite roughness
    float etaN = mf_invert_eta(sc, p.m, asub, Vn);
    float P = mf_cell_area(sc, p.m, asub, etaN);
    float vfloor = MF_HVEL * dx * dx;
    float un = mf_vel(Pxn, Vn, vfloor), vn = mf_vel(Pyn, Vn, vfloor);
    float hR = (P > 0.0f) ? Vn / fmax(P, 1.0e-12f) : 0.0f;
    float spd = sqrt(un * un + vn * vn);
    if (spd > 0.0f && hR > MF_HVEL) {
        float cf = 1.0f + g * nc2[i] * dt * spd / pow(fmax(hR, MF_HVEL), 4.0f / 3.0f);
        Pxn /= cf; Pyn /= cf;
    }
    if (Vn <= vfloor) { Pxn = 0.0f; Pyn = 0.0f; }   // near-dry cell carries no momentum (matches ref)
    if (ok[i] == 0) { Vn = 0.0f; Pxn = 0.0f; Pyn = 0.0f; }
    V[i] = Vn; Px[i] = Pxn; Py[i] = Pyn;
    etaN = (ok[i] != 0) ? mf_invert_eta(sc, p.m, asub, Vn) : sc[0];
    eta[i] = etaN;

    float zbed = sc[0];
    float h = etaN - zbed;
    float th = (p.t0 + sf[S_T] + dt) / 3600.0f;
    float wcell = 0.0f, vmagn = 0.0f;
    if (h > MF_HVEL) {
        // the CFL limit keeps the speed BEFORE friction (un, vn): it is the larger of the two, and
        // the shorter step it gives is what holds the V >= 0 clip down on coarse cells (Lake Delhi
        // at r = 8: 1.7 % of the water is clip with it, 6.8 % with the speed after friction).
        float cc = sqrt(g * fmax(hR, 0.0f));
        wcell = fmax(fabs(un), fabs(vn)) + cc;
        // the peak-velocity grid takes the speed the cell CARRIES into the next step, after
        // friction and the near-dry reset (as kernels_fv.metal). The speed before friction is an
        // intermediate: in a film of water it is momentum over almost no volume, thousands of m/s
        // for one step.
        float uc = mf_vel(Pxn, Vn, vfloor), vc = mf_vel(Pyn, Vn, vfloor);
        vmagn = sqrt(uc * uc + vc * vc);
    }
    if (h > p.dry) {
        if (h > hmax[i]) { hmax[i] = h; tmax[i] = th; }
        if (h > p.arr_depth && arr[i] == MF_NODATA) arr[i] = th;
    }
    if (vmagn > vmax[i]) vmax[i] = vmagn;

    float cl = simd_sum(clip);
    float wm = simd_max(wcell);
    if (simd_is_first()) {
        if (wm > 0.0f)
            atomic_fetch_max_explicit((device atomic_uint*)(st + S_WNEXT), as_type<uint>(wm),
                                      memory_order_relaxed);
        if (cl > 0.0f)
            atomic_fetch_add_explicit((device atomic_uint*)(st + S_CLIP),
                                      uint(cl / MF_FIX + 0.5f), memory_order_relaxed);
    }
}

// one thread: close the step
kernel void k_end_subfv(MF_ARGS_SUBFV) {
    device float* sf = (device float*)st;
    float dt = sf[S_DT];
    if (dt <= 0.0f) return;
    sf[S_T] = sf[S_T] + dt;
    st[S_STEPS] = st[S_STEPS] + 1u;
    sf[S_MINDT] = fmin(sf[S_MINDT], dt);
    sf[S_LASTDT] = dt;
    sf[S_VOUT] += float(st[S_QOUT]) * MF_FIX * dt;
    sf[S_VIN] += sf[S_QIN] * dt;
    st[S_WMAX] = st[S_WNEXT];
}
