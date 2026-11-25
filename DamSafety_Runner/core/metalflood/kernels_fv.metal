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
//   - FREE domain edges are transmissive (zero-gradient ghost) for OUTFLOW only: an inward velocity
//     is cut to 0, so no water enters from outside the model (as ACC / LISFLOOD-FP FREE edges);
//     closed edges are reflective walls.
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
#define MF_SLOPE_EPS 1.0e-6f     // FREE-edge normal-depth floor deadband: a surface flat to
//                                 within this slope has no drainage drive (sqrt(slope) ill-
//                                 conditioned as slope -> 0); below it the transmissive flux stands.
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
#define S_RAIN 16      // float: rainfall rate this step (m/s), added to every valid cell (FV1)
#define S_RAIND 17     // float: rainfall depth added in this batch (m; x valid cells x dx^2 = m3)
#define S_TILES 18     // uint: 0 = every cell runs; 1 = the host turned the skip on (this batch's first redraw scans every cell); 2 = k_tgrow_fv has written ton (a later redraw may leave out a tile that was off)
#define S_WORDS 32
#define MF_FIX 1.0e-4f

// Dry-tile skip. The window is cut into MF_TILE x MF_TILE cell tiles. Every SKIP_CHUNK_FV steps
// (8, engine_fv.py) k_tclear_fv / k_tseed_fv / k_tgrow_fv mark the tiles that hold a seed and the
// MF_TILE_RING tiles around each. A seed is a cell whose (h, hu, hv) is not exactly 0, or a
// source cell. The skip is exact when all three of these hold, which is what
// tests/test_boann_fv_tiles.py checks bit for bit:
// 1. A cell that is exactly dry (h = hu = hv = 0) between dry cells stays exactly dry. A face
//    between two dry cells has HLLC states of zero depth and zero velocity, so its flux is 0,
//    and the bed source and friction are 0 at zero depth. The depth >= 0 clip stores +0, not
//    -0, so a cell the skip never writes (it stays +0) matches a cell the full window writes.
// 2. Water and numerical influence move at most one cell per step (one faces-and-cont sweep,
//    and a face reads only its two cells): R = 1. An unmarked tile is at least
//    MF_TILE * MF_TILE_RING + 1 = 17 cells from every seed, and 17 > 8 * 1.
// 3. Face fluxes are scratch. A face is computed when either cell beside it is on, so an on
//    cell's fluxes are rewritten by k_faces_fv before k_cont_fv reads them. An off cell is not
//    read.
// k_faces_fv and k_cont_fv still take part in every SIMD-group reduction with the value a dry
// cell gives (0 outflow, 0 wave speed, not wet, 0 clip), so the groups and the sums do not move.
// Rain wets every cell: the host does not turn the skip on, and k_tgrow_fv would mark every tile.
// The host sets S_TILES to 1 once per batch, so the first redraw reads every cell (the window
// may have moved, and ton is not cleared between batches). k_tgrow_fv then sets S_TILES to 2,
// and a later redraw of the same batch does not re-read a cell whose tile was off. Thread
// (0, 0) still marks every source. k_tclear_fv still runs.
#define MF_TILE 16u
#define MF_TILE_RING 1u

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
    uint rain;            // 1: series n_series is a rainfall rate (m/s), added to every valid cell
                          // each step (FV1 only; matches the ACC engine). Was pad4.
};

// Buffer layout for the FV scheme (its own order, 0..25; Params at 26). tseed and ton are the
// dry-tile skip (see MF_TILE): one uint per tile of the whole grid.
inline uint mf_cells(constant Params& p) { return p.nx * p.ny; }

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
    device uint* tseed B(24), \
    device uint* ton B(25), \
    constant Params& p B(26), \
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

// whether the grid kernels' thread (gx, gy) of the window runs: always unless the host turned the
// dry-tile skip on; then only in a marked tile. A coordinate past the window (the closing face)
// belongs to the window's last cell.
inline bool mf_on(device const uint* st, device const uint* ton, uint gx, uint gy,
                  constant Params& p) {
    if (st[S_TILES] == 0u) {
        return true;
    }
    uint tx = (gx < p.wc ? gx : p.wc - 1u) / MF_TILE;
    uint ty = (gy < p.wr ? gy : p.wr - 1u) / MF_TILE;
    uint ntx = (p.wc + MF_TILE - 1u) / MF_TILE;
    return ton[ty * ntx + tx] != 0u;
}

// an x-face / y-face runs when either cell beside it is on, so a face on the edge of an on tile
// is still computed and its flux is rewritten before the on cell reads it
inline bool mf_face_x(device const uint* st, device const uint* ton, uint gx, uint gy,
                      constant Params& p) {
    if (gy >= p.wr) {
        return false;
    }
    bool right = mf_on(st, ton, gx, gy, p);
    bool left = gx > 0u && mf_on(st, ton, gx - 1u, gy, p);
    return left || right;
}

inline bool mf_face_y(device const uint* st, device const uint* ton, uint gx, uint gy,
                      constant Params& p) {
    if (gx >= p.wc) {
        return false;
    }
    bool south = mf_on(st, ton, gx, gy, p);
    bool north = gy > 0u && mf_on(st, ton, gx, gy - 1u, p);
    return north || south;
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
    // rainfall (FV1 only): one rate over the whole grid at the step start, as LISFLOOD-FP's
    // Rainfall() and the ACC engine (kernels.metal). Added to every valid cell in k_cont_fv.
    float rr = 0.0f;
    if (p.rain != 0u) {
        rr = fmax(mf_series(p.n_series, tm, ser_off, ser_t, ser_q, cursor), 0.0f);
    }
    sf[S_RAIN] = rr;
    sf[S_RAIND] += rr * dt;
}

// FREE-edge outflow floor: the Manning normal-depth velocity that the water-surface slope to the
// inner cell drives, so a flooded basin that decelerates into a FREE boundary still drains instead
// of ponding. 0 when the surface does not slope outward, and at an established outflow the cell's
// own velocity already exceeds it, so the transmissive outflow is only ever raised, never cut.
inline float mf_free_und(float etac, float etain, float hc, float n2c, float dx) {
    float slope = (etain - etac) / dx;
    // deadband: a surface flat to within MF_SLOPE_EPS has no drainage drive, and sqrt(slope) is
    // ill-conditioned as slope -> 0, so below it the transmissive flux stands (keeps engines matched).
    if (slope <= MF_SLOPE_EPS || hc <= MF_HVEL) {
        return 0.0f;
    }
    return sqrt(slope) * pow(hc, 2.0f / 3.0f) / sqrt(fmax(n2c, 1e-12f));
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
    // an off face writes nothing: it is far from water, so the flux it would write is 0, which
    // is what the buffers already hold (see MF_TILE). The reduction below still sees 0.
    bool xon = mf_face_x(st, ton, gid.x, gid.y, p);
    bool yon = mf_face_y(st, ton, gid.x, gid.y, p);

    if (xon) {                        // x-face at (row r, face column c), 0..nx
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
            } else if (ok[l] != 0 && h[l] > MF_HVEL) {  // NoData on the right: a wall, as at the domain edge
                float wn = mf_vel(hu[l], h[l]);
                float wt = mf_vel(hv[l], h[l]);
                fl = mf_hllc(h[l], wn, wt, h[l], -wn, wt, g);
            } else if (ok[rr] != 0 && h[rr] > MF_HVEL) {  // NoData on the left: a wall
                float wn = mf_vel(hu[rr], h[rr]);
                float wt = mf_vel(hv[rr], h[rr]);
                fl = mf_hllc(h[rr], -wn, wt, h[rr], wn, wt, g);
            }
        } else if (c == 0u) {         // left domain edge, cell i is the + side
            uint i = r * nx;
            if (ok[i] != 0 && h[i] > MF_HVEL) {
                float ui = mf_vel(hu[i], h[i]);
                float vi = mf_vel(hv[i], h[i]);
                if ((p.free_mask & MF_W) != 0u) {
                    float uo = ui;
                    if (nx > 1u) {
                        float und = mf_free_und(z[i] + h[i], z[i + 1u] + h[i + 1u], h[i], n2[i], p.dx);
                        if (und > 0.0f) uo = fmin(ui, -und);          // at least und outward (-x)
                    }
                    uo = fmin(uo, 0.0f);                     // outflow only: nothing enters from outside
                    fl = mf_hllc(h[i], uo, vi, h[i], uo, vi, g);      // transmissive, normal-depth floor
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
                    float uo = ui;
                    if (nx > 1u) {
                        float und = mf_free_und(z[i] + h[i], z[i - 1u] + h[i - 1u], h[i], n2[i], p.dx);
                        if (und > 0.0f) uo = fmax(ui, und);           // at least und outward (+x)
                    }
                    uo = fmax(uo, 0.0f);                     // outflow only: nothing enters from outside
                    fl = mf_hllc(h[i], uo, vi, h[i], uo, vi, g);
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

    if (yon) {                        // y-face at (face row r, col c), 0..ny. Normal = y (south +).
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
            } else if (ok[u] != 0 && h[u] > MF_HVEL) {  // NoData on the south: a wall, as at the domain edge
                float wn = mf_vel(hv[u], h[u]);
                float wt = mf_vel(hu[u], h[u]);
                fl = mf_hllc(h[u], wn, wt, h[u], -wn, wt, g);
            } else if (ok[d] != 0 && h[d] > MF_HVEL) {  // NoData on the north: a wall
                float wn = mf_vel(hv[d], h[d]);
                float wt = mf_vel(hu[d], h[d]);
                fl = mf_hllc(h[d], -wn, wt, h[d], wn, wt, g);
            }
        } else if (r == 0u) {         // north domain edge, cell i is the + (south) side
            uint i = c;
            if (ok[i] != 0 && h[i] > MF_HVEL) {
                float ni = mf_vel(hv[i], h[i]);
                float ti = mf_vel(hu[i], h[i]);
                if ((p.free_mask & MF_N) != 0u) {
                    float no = ni;
                    if (ny > 1u) {
                        float und = mf_free_und(z[i] + h[i], z[i + nx] + h[i + nx], h[i], n2[i], p.dx);
                        if (und > 0.0f) no = fmin(ni, -und);          // at least und outward (-y)
                    }
                    no = fmin(no, 0.0f);                     // outflow only: nothing enters from outside
                    fl = mf_hllc(h[i], no, ti, h[i], no, ti, g);
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
                    float no = ni;
                    if (ny > 1u) {
                        float und = mf_free_und(z[i] + h[i], z[i - nx] + h[i - nx], h[i], n2[i], p.dx);
                        if (und > 0.0f) no = fmax(ni, und);           // at least und outward (+y)
                    }
                    no = fmax(no, 0.0f);                     // outflow only: nothing enters from outside
                    fl = mf_hllc(h[i], no, ti, h[i], no, ti, g);
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
    // a skipped cell is far from water: it stays exactly dry (depth 0, nothing clipped, no
    // maximum) and takes part in the SIMD-group reductions below with those zeros (see MF_TILE)
    float hn = 0.0f;
    float clip = 0.0f;
    float wcell = 0.0f;
    if (mf_on(st, ton, gid.x, gid.y, p)) {
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

    hn = h0 + dh + src[i] * dt / (dx * dx) + sf[S_RAIN] * dt;
    float hun = hu[i] + dhu;
    float hvn = hv[i] + dhv;

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
    if (hn > p.dry) {
        float dv = hn * vmagn;
        uint j = mf_cells(p) + i;
        if (dv > vmax[j]) {
            vmax[j] = dv;
        }
    }
    }   // mf_on

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

// -- dry-tile skip (see MF_TILE) ---------------------------------------------------------------
// The host runs these three, in this order, before the first step of every chunk when it turns
// the skip on (st[S_TILES] = 1). Grid: k_tclear_fv / k_tgrow_fv one thread per tile of the window,
// k_tseed_fv one per window cell.

kernel void k_tclear_fv(MF_ARGS_FV) {
    uint ntx = (p.wc + MF_TILE - 1u) / MF_TILE;
    uint nty = (p.wr + MF_TILE - 1u) / MF_TILE;
    if (gid.x < ntx && gid.y < nty) {
        tseed[gid.y * ntx + gid.x] = 0u;
    }
}

// a window cell is a seed when h, hu or hv is not exactly 0; thread (0, 0) also marks the sources.
// S_TILES == 1 (the host, once per batch) reads every cell. S_TILES == 2 reads a cell only when
// its tile was on. ton still holds that map here, because only k_tgrow_fv writes it and that
// kernel runs after this one.
kernel void k_tseed_fv(MF_ARGS_FV) {
    if (gid.x >= p.wc || gid.y >= p.wr) {
        return;
    }
    uint nx = p.nx;
    uint ntx = (p.wc + MF_TILE - 1u) / MF_TILE;
    uint tile = (gid.y / MF_TILE) * ntx + gid.x / MF_TILE;
    if (st[S_TILES] != 2u || ton[tile] != 0u) {
        uint i = (p.r0 + gid.y) * nx + (p.c0 + gid.x);
        if (h[i] != 0.0f || hu[i] != 0.0f || hv[i] != 0.0f) {
            atomic_fetch_max_explicit((device atomic_uint*)(tseed + tile),
                                      1u, memory_order_relaxed);
        }
    }
    if (gid.x == 0u && gid.y == 0u) {
        for (uint k = 0; k < p.n_src; ++k) {
            uint sr = s_cell[k] / nx;
            uint sc = s_cell[k] % nx;
            if (sr >= p.r0 && sr < p.r0 + p.wr && sc >= p.c0 && sc < p.c0 + p.wc) {
                atomic_fetch_max_explicit((device atomic_uint*)(tseed + ((sr - p.r0) / MF_TILE)
                                                                * ntx + (sc - p.c0) / MF_TILE),
                                          1u, memory_order_relaxed);
            }
        }
    }
}

// a tile runs when it, or a tile within MF_TILE_RING of it, holds a seed (or it rains)
kernel void k_tgrow_fv(MF_ARGS_FV) {
    uint ntx = (p.wc + MF_TILE - 1u) / MF_TILE;
    uint nty = (p.wr + MF_TILE - 1u) / MF_TILE;
    if (gid.x >= ntx || gid.y >= nty) {
        return;
    }
    uint on = p.rain != 0u ? 1u : 0u;
    uint y0 = gid.y > MF_TILE_RING ? gid.y - MF_TILE_RING : 0u;
    uint y1 = gid.y + MF_TILE_RING < nty ? gid.y + MF_TILE_RING : nty - 1u;
    uint x0 = gid.x > MF_TILE_RING ? gid.x - MF_TILE_RING : 0u;
    uint x1 = gid.x + MF_TILE_RING < ntx ? gid.x + MF_TILE_RING : ntx - 1u;
    for (uint y = y0; y <= y1; ++y) {
        for (uint x = x0; x <= x1; ++x) {
            if (tseed[y * ntx + x] != 0u) {
                on = 1u;
            }
        }
    }
    ton[gid.y * ntx + gid.x] = on;
    if (gid.x == 0u && gid.y == 0u) {
        st[S_TILES] = 2u;   // after ton is written; the next redraw of this batch may trust it
    }
}
