// DamSafety sub-grid local-inertial kernels: the local-inertial ("acceleration") scheme of
// kernels.metal run on a COARSE grid whose cells each keep their r x r fine (LiDAR) sub-cells as
// stage tables, after Nithila Devi and Kuiry (2024, Water Resources Research 60, e2023WR035334).
// The GPU/CPU mirror of subli_ref.py (the float64 oracle, which states the scheme and where it
// departs from the paper); on flat sub-cells it is kernels.metal's face update term for term.
//
// Numerics:
//   - per cell: the wetted VOLUME V under one free surface (the paper's Eq. 10). The cell's sorted
//     sub-elevations s (m = r*r, stored as heights above the cell's LOWEST sub-cell, so s[0] = 0)
//     give the depth above that lowest sub-cell, d, as the exact inverse of V; zb is the lowest
//     sub-cell's elevation above the run's datum, so the free surface is zb + d;
//   - per face: the COMMON EDGE of the two cells (each sub-segment at the higher of the two
//     sub-beds, the paper's Eq. 11), wetted at the higher of the two surfaces, gives the wet area A,
//     the wet width W and the compound Manning conveyance K = sum w h^(5/3) / n. The whole-face
//     discharge Q (m3/s) is
//         Q_new = (theta Q + (1 - theta) Q_nb - g A dt S) / (1 + g A dt Qv / K^2)
//     with S the surface slope and Qv = |Q| or the 2-D discharge vector (fric2d). The weighting
//     (Params.weighting) picks theta and the neighbour Q_nb:
//       0 none    theta = 1 (Bates et al. 2010): the plain engine's default step;
//       1 upwind  the paper's Eq. 7 and 8 (after Sridharan et al. 2020): Q_nb is the face upwind
//                 along the axis, theta = 1 - (dt / dx) min(|Q| / A, sqrt(g A / W)).
//     Params.friction 1 swaps K for the paper's one mean face depth, K = dX (A / dX)^(5/3) / n
//     (its Eq. 9): tests and comparisons only, it runs a narrow channel too deep in bank;
//   - FREE edges: outward discharge through the edge's own sub-bed profile from the slope to the
//     inner cell (kernels.metal's mf_free), blended with the next face inward under the upwind
//     weighting (subli_ref.py says why the edge needs it);
//   - the faces of a cell that a point source is feeding are never weighted (subli_ref.py);
//   - donor-cell limiter (a cell never sends more water than it holds), dt = min(initial_tstep,
//     alpha dX / sqrt(g d_max)), point sources, maxima, arrival and the wet window: as kernels.metal.
//
// Float32 care, as kernels.metal: elevations are relative (s to the cell's lowest sub-cell, zb and
// the face profiles to the lowest valid cell of the run), and the surface slope is
// (zb_r - zb_l) + (d_r - d_l), never the difference of two large surface elevations.
//
// Compiled two ways, exactly like kernels.metal: by Metal at run time on a Mac, and as C++ with
// mf_shim.h (cpu_kernels_subli.cpp) everywhere else. 30 buffers + Params at 30 = 31 slots, Metal's
// cap. Nothing else may be added to this engine without removing a buffer.

#ifndef MF_CPU
#include <metal_stdlib>
using namespace metal;
#define B(i) [[buffer(i)]]
#define GID [[thread_position_in_grid]]
#endif

#define MF_NODATA (-9999.0f)
#define MF_TEPS 1.0e-3f           // s: closer than this to the batch's end time = arrived

// edges in Params.free_mask
#define MF_N 1u
#define MF_S 2u
#define MF_E 4u
#define MF_W 8u

// Params.weighting
#define MF_W_NONE 0u
#define MF_W_UPWIND 1u

// words of the state buffer (the layout of kernels.metal)
#define S_T 0
#define S_DT 1
#define S_HMAX 2       // uint (float bits): deepest water now (above a cell's lowest sub-cell)
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
#define S_TILES 18     // uint: 0 = every cell runs; 1 = the host turned the skip on (this batch's first redraw scans every cell); 2 = k_tgrow_subli has written ton (a later redraw may leave out a tile that was off)
#define S_WORDS 32
#define MF_FIX 1.0e-4f

// Dry-tile skip on the coarse grid. The window is cut into MF_TILE x MF_TILE coarse cells. Every
// SKIP_CHUNK steps (8) three tile kernels mark a tile that holds a seed and the 8 tiles around it.
// A seed is a cell with V or d above 0, a cell beside a face whose discharge or recorded velocity
// is not 0, or a source cell. A face between two dry cells stays at exactly 0: each sub-segment of
// the common edge is at least either cell's lowest sub-cell, so a dry cell's surface (its lowest
// sub-cell) wets none of them. Water moves at most one coarse cell a step, so in one chunk it
// reaches at most 9 cells, counting the cell beside a face that still carries flow. A tile left
// off is at least one whole tile from every seed, further than that, so what the full window
// would compute there is already stored: volume 0, depth 0 (inverting a zero volume is exactly 0),
// faces 0, and the maxima do not move. A stale scale factor in an off tile is harmless, because
// the limiter multiplies by it only on a face whose discharge is not 0, and every face next to an
// off tile is 0. k_limit_subli and k_cont_subli do not return early on a skipped thread. They take
// part in the SIMD-group reductions with what a dry cell gives (outflow 0, clipped volume 0,
// depth 0, not wet), so every group adds the same numbers in the same order. This engine takes no
// rain. Tiles are numbered from the window origin and redrawn before step 0 of every batch.
// The host sets S_TILES to 1 for that first redraw, so every cell is read (the window may have
// moved, and ton is not cleared between batches). k_tgrow_subli then sets S_TILES to 2 after it
// writes ton. A later redraw in the same batch does not re-read a cell whose tile was off: an
// off tile stays dry for the chunk, so a new seed lies only in a tile that was on. Thread (0, 0)
// still marks every source. k_tclear_subli still runs. The next batch starts at 1 again.
#define MF_TILE 16u

struct Params {
    uint nx, ny;          // COARSE grid columns, rows
    uint r0, c0;          // window origin (row, column)
    uint wr, wc;          // window rows, columns
    uint free_mask;       // MF_N | MF_S | MF_E | MF_W
    uint n_src;           // source cells
    uint n_series;        // inflow series
    uint fric2d;          // 1: friction from the 2-D discharge vector (LISFLOOD-FP 8 default)
    uint clip_end;        // 1: the last step is cut to land on t_end
    uint inflow_lf_order; // 1: a point source's water is withheld from the step's limiter
    uint r;               // fine sub-cells per coarse cell per axis
    uint m;               // r*r, the stride of the cell curve buffer s
    uint weighting;       // MF_W_NONE / MF_W_UPWIND
    uint friction;        // 0: compound conveyance (default); 1: the paper's mean face depth
    float dx;             // COARSE cell size (m)
    float dt0;            // initial_tstep: the longest step (s)
    float alpha;          // CFL factor
    float g;
    float dry;            // 1e-3 m: no flow through a face whose deepest sub-segment is shallower
    float maxhflow;       // 10 m: cap on a sub-segment's flow depth
    float arr_depth;      // depth that counts as arrival (inittm_depth)
    float t0;             // model time at the batch start (s)
    float t_end;          // batch end, relative to t0 (s)
    float wsub;           // dx / r: the length of a face sub-segment
};

inline uint mf_cells(constant Params& p) { return p.nx * p.ny; }

#define MF_ARGS_SUBLI \
    device const float* s B(0), \
    device const float* zb B(1), \
    device float* V B(2), \
    device float* d B(3), \
    device float* qx B(4), \
    device float* qy B(5), \
    device float* src B(6), \
    device float* scl B(7), \
    device float* hmax B(8), \
    device float* tmax B(9), \
    device float* arr B(10), \
    device float* vmax B(11), \
    device const uchar* ok B(12), \
    device const uint* s_cell B(13), \
    device const uint* s_series B(14), \
    device const uint* ser_off B(15), \
    device const float* ser_t B(16), \
    device const float* ser_q B(17), \
    device uint* cursor B(18), \
    device uint* st B(19), \
    device float* qxo B(20), \
    device float* qyo B(21), \
    device float* vxf B(22), \
    device float* vyf B(23), \
    device const float* zfx B(24), \
    device const float* nf2x B(25), \
    device const float* zfy B(26), \
    device const float* nf2y B(27), \
    device uint* tseed B(28), \
    device uint* ton B(29), \
    constant Params& p B(30), \
    uint2 gid GID

// -- helpers ---------------------------------------------------------------------------------------

// depth above the cell's lowest sub-cell from its volume. V(d) is piecewise linear and increasing,
// with a breakpoint at each sorted sub-height s[k] (s[0] = 0); on the segment above s[k], k + 1
// sub-cells are wet and V = asub ((k + 1) d - sum_{j<=k} s[j])  ->  d = (V / asub + csum_k) / (k + 1).
inline float mf_invert_d(device const float* sc, uint m, float asub, float Vcell) {
    float V0 = fmax(Vcell, 0.0f);
    float csum = 0.0f;       // sum_{j<=k} s[j]
    float pref = 0.0f;       // sum_{j<k}  s[j]
    uint k = 0;
    for (uint j = 0; j < m; ++j) {
        float Vb = asub * (float(j) * sc[j] - pref);
        if (Vb <= V0) {
            k = j;
            csum = pref + sc[j];
        } else {
            break;
        }
        pref += sc[j];
    }
    return (V0 / asub + csum) / float(k + 1);
}

struct Face {
    float A;       // wet area (m2)
    float W;       // wet width (m)
    float K;       // Manning conveyance (m3/s)
    float hdeep;   // deepest sub-segment (m)
};

// the common edge zf[0..r-1] (n^2 nf2[0..r-1]) wetted at the surface etaf
inline Face mf_face_sub(device const float* zf, device const float* nf2, float etaf,
                        constant Params& p) {
    float a = 0.0f;
    float w = 0.0f;
    float kk = 0.0f;
    float n2s = 0.0f;
    float hd = 0.0f;
    for (uint k = 0; k < p.r; ++k) {
        float h = fmin(etaf - zf[k], p.maxhflow);
        n2s += nf2[k];
        if (h > 0.0f) {
            a += h;
            w += 1.0f;
            kk += pow(h, 5.0f / 3.0f) / sqrt(fmax(nf2[k], 1.0e-12f));
            hd = fmax(hd, h);
        }
    }
    Face f;
    f.A = p.wsub * a;
    f.W = p.wsub * w;
    f.hdeep = hd;
    if (p.friction != 0u) {
        float len = p.wsub * float(p.r);
        f.K = len * pow(f.A / len, 5.0f / 3.0f) / sqrt(fmax(n2s / float(p.r), 1.0e-12f));
    } else {
        f.K = p.wsub * kk;
    }
    return f;
}

// the adaptive weight of the paper's Eq. 8, with the hydraulic depth A / W for the wave speed
inline float mf_theta_up(float q, float A, float W, float dt, constant Params& p) {
    float u = fabs(q) / A;
    float c = sqrt(p.g * A / fmax(W, 1.0e-12f));
    return fmin(fmax(1.0f - dt / p.dx * fmin(u, c), 0.0f), 1.0f);
}

// local-inertial update of one interior face: q its last discharge (m3/s), qup / qdn the faces
// before and after it along the axis, qperp the mean of the four crossing faces around it, ds the
// surface slope across it, blend false on a face of a source cell (never weighted: the water there
// comes out of the source, not from the face upwind, and blending with that face holds it back)
inline float mf_q_sub(float q, float qup, float qdn, float qperp, float ds, Face f, float dt,
                      bool blend, constant Params& p) {
    float A = fmax(f.A, 1.0e-12f);
    float K2 = fmax(f.K * f.K, 1.0e-30f);
    float qv = p.fric2d != 0u ? sqrt(q * q + qperp * qperp) : fabs(q);
    float den = 1.0f + p.g * A * dt * qv / K2;
    float push = p.g * A * dt * ds;
    if (blend && p.weighting == MF_W_UPWIND) {
        float th = mf_theta_up(q, A, f.W, dt, p);
        float qn = q > 0.0f ? qup : (q < 0.0f ? qdn : q);
        float qc = (th * q + (1.0f - th) * qn - push) / den;
        // as LISFLOOD-FP's weighted form: where the blended value flows uphill, the plain one is used
        if (!(qc * ds > 0.0f)) {
            return qc;
        }
    }
    return (q - push) / den;
}

// FREE edge: magnitude of the outward discharge through the edge profile f at the edge cell's
// surface, from the slope to its inner neighbour; q0 the edge face's last discharge, qin the next
// face inward (+ toward the edge), its upwind neighbour
inline float mf_free_sub(float q0, float qin, float slope, Face f, float dt, bool blend,
                         constant Params& p) {
    float A = fmax(f.A, 1.0e-12f);
    float K2 = fmax(f.K * f.K, 1.0e-30f);
    q0 = fabs(q0);
    float qb = q0;
    if (blend && p.weighting == MF_W_UPWIND) {
        float th = mf_theta_up(q0, A, f.W, dt, p);
        qb = th * q0 + (1.0f - th) * fmax(qin, 0.0f);
    }
    return (qb + p.g * A * dt * fabs(slope)) / (1.0f + p.g * A * dt * q0 / K2);
}

inline float mf_series(uint k, float tm, device const uint* ser_off,
                       device const float* ser_t, device const float* ser_q, device uint* cursor) {
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
// dry-tile skip on for this batch; then only in a marked tile. A face thread goes with the cell
// east of its x-face / south of its y-face (the window's last cell for the closing faces).
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

// -- kernels ---------------------------------------------------------------------------------------

// one thread: the step's dt (CFL from the deepest water) and the source rates at the start of the
// step, as kernels.metal's k_begin
kernel void k_begin_subli(MF_ARGS_SUBLI) {
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

// every face of the window: thread (x, y) owns x-face (row r0+y, face c0+x) when y < wr and
// y-face (face row r0+y, column c0+x) when x < wc. Reads last step's discharges (qxo, qyo),
// writes the new ones (qx, qy) and the face velocities (Q / A)
kernel void k_faces_subli(MF_ARGS_SUBLI) {
    device float* sf = (device float*)st;
    float dt = sf[S_DT];
    if (dt <= 0.0f || !mf_on(st, ton, gid.x, gid.y, p)) {
        return;
    }
    uint nx = p.nx;
    uint ny = p.ny;
    uint R = p.r;
    uint r = p.r0 + gid.y;
    uint c = p.c0 + gid.x;
    if (gid.y < p.wr) {
        uint f = r * (nx + 1u) + c;
        device const float* zf = zfx + (size_t)f * R;
        device const float* nf = nf2x + (size_t)f * R;
        float q = 0.0f;
        float v = 0.0f;
        if (c == 0u) {
            if ((p.free_mask & MF_W) != 0u && nx > 1u) {
                uint i = r * nx;
                Face fc = mf_face_sub(zf, nf, zb[i] + d[i], p);
                if (ok[i] != 0 && fc.hdeep > p.dry) {
                    float sl = ((zb[i] - zb[i + 1u]) + (d[i] - d[i + 1u])) / p.dx;
                    q = -mf_free_sub(qxo[f], -qxo[f + 1u], sl, fc, dt, src[i] == 0.0f, p);
                }
            }
        } else if (c == nx) {
            if ((p.free_mask & MF_E) != 0u && nx > 1u) {
                uint i = r * nx + nx - 1u;
                Face fc = mf_face_sub(zf, nf, zb[i] + d[i], p);
                if (ok[i] != 0 && fc.hdeep > p.dry) {
                    float sl = ((zb[i] - zb[i - 1u]) + (d[i] - d[i - 1u])) / p.dx;
                    q = mf_free_sub(qxo[f], qxo[f - 1u], sl, fc, dt, src[i] == 0.0f, p);
                }
            }
        } else {
            uint l = r * nx + c - 1u;
            uint rr = l + 1u;
            if (ok[l] != 0 && ok[rr] != 0) {
                Face fc = mf_face_sub(zf, nf, fmax(zb[l] + d[l], zb[rr] + d[rr]), p);
                if (fc.hdeep > p.dry) {
                    float q0 = qxo[f];
                    float qup = qxo[f - 1u];
                    float qdn = qxo[f + 1u];
                    if (c == 1u && qup == 0.0f) {
                        qup = q0;
                    }
                    if (c + 1u == nx && qdn == 0.0f) {
                        qdn = q0;
                    }
                    float qperp = 0.25f * (qyo[l] + qyo[rr] + qyo[l + nx] + qyo[rr + nx]);
                    float ds = ((zb[rr] - zb[l]) + (d[rr] - d[l])) / p.dx;
                    q = mf_q_sub(q0, qup, qdn, qperp, ds, fc, dt,
                                 src[l] == 0.0f && src[rr] == 0.0f, p);
                    v = q != 0.0f ? q / fmax(fc.A, 1.0e-12f) : 0.0f;
                }
            }
        }
        qx[f] = q;
        vxf[f] = v;
    }
    if (gid.x < p.wc) {
        uint f = r * nx + c;
        device const float* zf = zfy + (size_t)f * R;
        device const float* nf = nf2y + (size_t)f * R;
        float q = 0.0f;
        float v = 0.0f;
        if (r == 0u) {
            if ((p.free_mask & MF_N) != 0u && ny > 1u) {
                uint i = c;
                Face fc = mf_face_sub(zf, nf, zb[i] + d[i], p);
                if (ok[i] != 0 && fc.hdeep > p.dry) {
                    float sl = ((zb[i] - zb[i + nx]) + (d[i] - d[i + nx])) / p.dx;
                    q = -mf_free_sub(qyo[f], -qyo[f + nx], sl, fc, dt, src[i] == 0.0f, p);
                }
            }
        } else if (r == ny) {
            if ((p.free_mask & MF_S) != 0u && ny > 1u) {
                uint i = (ny - 1u) * nx + c;
                Face fc = mf_face_sub(zf, nf, zb[i] + d[i], p);
                if (ok[i] != 0 && fc.hdeep > p.dry) {
                    float sl = ((zb[i] - zb[i - nx]) + (d[i] - d[i - nx])) / p.dx;
                    q = mf_free_sub(qyo[f], qyo[f - nx], sl, fc, dt, src[i] == 0.0f, p);
                }
            }
        } else {
            uint u = (r - 1u) * nx + c;
            uint dn = u + nx;
            if (ok[u] != 0 && ok[dn] != 0) {
                Face fc = mf_face_sub(zf, nf, fmax(zb[u] + d[u], zb[dn] + d[dn]), p);
                if (fc.hdeep > p.dry) {
                    float q0 = qyo[f];
                    float qup = qyo[f - nx];
                    float qdn = qyo[f + nx];
                    if (r == 1u && qup == 0.0f) {
                        qup = q0;
                    }
                    if (r + 1u == ny && qdn == 0.0f) {
                        qdn = q0;
                    }
                    uint fu = (r - 1u) * (nx + 1u) + c;
                    uint fd = r * (nx + 1u) + c;
                    float qperp = 0.25f * (qxo[fu] + qxo[fu + 1u] + qxo[fd] + qxo[fd + 1u]);
                    float ds = ((zb[dn] - zb[u]) + (d[dn] - d[u])) / p.dx;
                    q = mf_q_sub(q0, qup, qdn, qperp, ds, fc, dt,
                                 src[u] == 0.0f && src[dn] == 0.0f, p);
                    v = q != 0.0f ? q / fmax(fc.A, 1.0e-12f) : 0.0f;
                }
            }
        }
        qy[f] = q;
        vyf[f] = v;
    }
}

// every cell of the window: the share of its outflows it can afford this step
kernel void k_scale_subli(MF_ARGS_SUBLI) {
    device float* sf = (device float*)st;
    float dt = sf[S_DT];
    if (dt <= 0.0f || !mf_on(st, ton, gid.x, gid.y, p)) {
        return;
    }
    uint nx = p.nx;
    uint r = p.r0 + gid.y;
    uint c = p.c0 + gid.x;
    uint i = r * nx + c;
    uint fx = r * (nx + 1u) + c;
    float out = (fmax(qx[fx + 1u], 0.0f) + fmax(-qx[fx], 0.0f)
                 + fmax(qy[i + nx], 0.0f) + fmax(-qy[i], 0.0f)) * dt;
    // LISFLOOD-FP order (inflow_lf_order=1): this step's inflow is not yet available to flow out
    float avail = V[i] + (p.inflow_lf_order != 0u ? 0.0f : src[i] * dt);
    scl[i] = (out > avail && out > 0.0f) ? avail / out : 1.0f;
}

// every face of the window: scale each discharge by its donor cell's share, keep it as next
// step's old discharge, and add up what leaves through the free edges
kernel void k_limit_subli(MF_ARGS_SUBLI) {
    device float* sf = (device float*)st;
    float dt = sf[S_DT];
    if (dt <= 0.0f) {
        return;
    }
    uint nx = p.nx;
    uint ny = p.ny;
    uint r = p.r0 + gid.y;
    uint c = p.c0 + gid.x;
    float out = 0.0f;
    // a skipped face is far from water: it carries 0 and sends 0 out (see MF_TILE)
    bool on = mf_on(st, ton, gid.x, gid.y, p);
    if (on && gid.y < p.wr) {
        uint f = r * (nx + 1u) + c;
        float q = qx[f];
        if (q > 0.0f && c > 0u) {
            q *= scl[r * nx + c - 1u];
        } else if (q < 0.0f && c < nx) {
            q *= scl[r * nx + c];
        }
        qx[f] = q;
        qxo[f] = q;
        if (c == 0u && q < 0.0f) {
            out += -q;
        } else if (c == nx && q > 0.0f) {
            out += q;
        }
    }
    if (on && gid.x < p.wc) {
        uint f = r * nx + c;
        float q = qy[f];
        if (q > 0.0f && r > 0u) {
            q *= scl[(r - 1u) * nx + c];
        } else if (q < 0.0f && r < ny) {
            q *= scl[r * nx + c];
        }
        qy[f] = q;
        qyo[f] = q;
        if (r == 0u && q < 0.0f) {
            out += -q;
        } else if (r == ny && q > 0.0f) {
            out += q;
        }
    }
    float tot = simd_sum(out);
    if (simd_is_first() && tot != 0.0f) {
        atomic_fetch_add_explicit((device atomic_uint*)(st + S_QOUT),
                                  uint(tot / MF_FIX + 0.5f), memory_order_relaxed);
    }
}

// every cell of the window: continuity on the volume, the V >= 0 clip, the new depth from the
// cell's stage curve, and the running maxima
kernel void k_cont_subli(MF_ARGS_SUBLI) {
    device float* sf = (device float*)st;
    float dt = sf[S_DT];
    if (dt <= 0.0f) {
        return;
    }
    uint nx = p.nx;
    uint r = p.r0 + gid.y;
    uint c = p.c0 + gid.x;
    // a skipped cell is far from water: it stays dry (volume 0, nothing clipped, no maximum), and
    // takes part in the SIMD-group reductions below with exactly those values (see MF_TILE)
    float hn = 0.0f;
    float clip = 0.0f;
    if (mf_on(st, ton, gid.x, gid.y, p)) {
        uint i = r * nx + c;
        uint fx = r * (nx + 1u) + c;
        float Vn = V[i] + dt * (qx[fx] - qx[fx + 1u] + qy[i] - qy[i + nx]) + src[i] * dt;
        if (Vn < 0.0f) {
            clip = -Vn;
            Vn = 0.0f;
        }
        if (ok[i] == 0) {
            Vn = 0.0f;
        }
        V[i] = Vn;
        if (ok[i] != 0) {
            hn = mf_invert_d(s + (size_t)i * p.m, p.m, p.wsub * p.wsub, Vn);
        }
        d[i] = hn;
        float th = (p.t0 + sf[S_T] + dt) / 3600.0f;
        if (hn > p.dry) {
            if (hn > hmax[i]) {
                hmax[i] = hn;
                tmax[i] = th;
            }
            if (hn > p.arr_depth && arr[i] == MF_NODATA) {
                arr[i] = th;
            }
        }
    }
    bool wet = hn > 0.0f;
    float hm = simd_max(hn);
    uint rmin = simd_min(wet ? r : 0xffffffffu);
    uint rmax = simd_max(wet ? r : 0u);
    uint cmin = simd_min(wet ? c : 0xffffffffu);
    uint cmax = simd_max(wet ? c : 0u);
    float cl = simd_sum(clip);
    if (simd_is_first()) {
        if (hm > 0.0f) {
            atomic_fetch_max_explicit((device atomic_uint*)(st + S_HNEXT), as_type<uint>(hm),
                                      memory_order_relaxed);
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

// every cell of the window: the cell speed |(max |v| of its two x faces, of its two y faces)| from
// the face velocities k_faces_subli recorded, into the running maximum (as kernels.metal's k_speed;
// a face velocity here is the discharge over the WET area of the face, so a channel inside the cell
// reports the channel's speed)
kernel void k_speed_subli(MF_ARGS_SUBLI) {
    device float* sf = (device float*)st;
    float dt = sf[S_DT];
    if (dt <= 0.0f || !mf_on(st, ton, gid.x, gid.y, p)) {
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
    float hv = d[i];
    if (hv > p.dry) {
        float dv = hv * v;
        uint j = mf_cells(p) + i;
        if (dv > vmax[j]) {
            vmax[j] = dv;
        }
    }
}

// one thread: close the step
kernel void k_end_subli(MF_ARGS_SUBLI) {
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

// -- dry-tile skip (see MF_TILE) ---------------------------------------------------------------
// The host runs these three, in this order, before the first step of every chunk of steps when it
// turns the skip on (st[S_TILES] = 1). Grid: k_tclear_subli / k_tgrow_subli one thread per tile of
// the window, k_tseed_subli one per window cell.

kernel void k_tclear_subli(MF_ARGS_SUBLI) {
    uint ntx = (p.wc + MF_TILE - 1u) / MF_TILE;
    uint nty = (p.wr + MF_TILE - 1u) / MF_TILE;
    if (gid.x < ntx && gid.y < nty) {
        tseed[gid.y * ntx + gid.x] = 0u;
    }
}

// a window cell is a seed when it holds water (volume or depth) or a face beside it carried flow
// or recorded a velocity in the last step (as mf_rows); thread (0, 0) also marks the sources.
// S_TILES == 1 (the host, once per batch) reads every cell. S_TILES == 2 reads a cell only when
// its tile was on. ton still holds that map here, because only k_tgrow_subli writes it and that
// kernel runs after this one.
kernel void k_tseed_subli(MF_ARGS_SUBLI) {
    if (gid.x >= p.wc || gid.y >= p.wr) {
        return;
    }
    uint nx = p.nx;
    uint ntx = (p.wc + MF_TILE - 1u) / MF_TILE;
    uint tile = (gid.y / MF_TILE) * ntx + gid.x / MF_TILE;
    if (st[S_TILES] != 2u || ton[tile] != 0u) {
        uint r = p.r0 + gid.y;
        uint c = p.c0 + gid.x;
        uint i = r * nx + c;
        uint fx = r * (nx + 1u) + c;
        if (V[i] > 0.0f || d[i] > 0.0f || qxo[fx] != 0.0f || vxf[fx] != 0.0f ||
            qxo[fx + 1u] != 0.0f || vxf[fx + 1u] != 0.0f || qyo[i] != 0.0f || vyf[i] != 0.0f ||
            qyo[i + nx] != 0.0f || vyf[i + nx] != 0.0f) {
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

// a tile runs when it or one of its 8 neighbours holds a seed
kernel void k_tgrow_subli(MF_ARGS_SUBLI) {
    uint ntx = (p.wc + MF_TILE - 1u) / MF_TILE;
    uint nty = (p.wr + MF_TILE - 1u) / MF_TILE;
    if (gid.x >= ntx || gid.y >= nty) {
        return;
    }
    uint on = 0u;
    uint y0 = gid.y > 0u ? gid.y - 1u : 0u;
    uint y1 = gid.y + 1u < nty ? gid.y + 1u : nty - 1u;
    uint x0 = gid.x > 0u ? gid.x - 1u : 0u;
    uint x1 = gid.x + 1u < ntx ? gid.x + 1u : ntx - 1u;
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
