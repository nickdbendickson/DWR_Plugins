// DamSafety flood engine kernels: the local-inertial scheme of LISFLOOD-FP's default
// "acceleration" solver (Bates, Horritt and Fewtrell 2010), one GPU thread per cell or face.
//
// This one source is compiled two ways:
//   - by Metal at run time on a Mac (backends.MetalBackend, Apple GPU, float32);
//   - as C++ with mf_shim.h (cpu_kernels.cpp, backends.CpuBackend), the CPU build used in the
//     cloud tests and on computers without Metal.
// So it keeps to what both accept: plain functions, B(i)/GID for the Metal attributes, the
// metal:: math names that <cmath> also has, and simd_* / atomic_* calls the shim maps to
// serial code. Every kernel has the same argument list (MF_ARGS) so one dispatcher serves all.
//
// Scheme (the same as core/builtin_lisflood.py, which runs it in numpy, float64):
//   q_face = (q - g h_f dt dS) / (1 + g dt n^2 |q| / h_f^(7/3)),  h_f = max(eta) - max(z)
//   no flow where h_f <= 1 mm; h_f capped at 10 m (LISFLOOD-FP's MaxHflow);
//   FREE edges: outward discharge from the edge depth and the slope to the inner cell;
//   donor-cell limiter: a cell never sends more water than it holds (depth >= 0, mass kept);
//   dt = min(initial_tstep, alpha dx / sqrt(g h_max)), computed on the GPU every step.
//
// Float32 care: z is stored relative to the lowest valid cell (engine.py), and the water-
// surface slope is (z_r - z_l) + (h_r - h_l), never eta_r - eta_l of two large numbers.

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

// words of the state buffer (uint; the float ones are read through a float pointer)
#define S_T 0          // float: time since the batch started (s)
#define S_DT 1         // float: this step's dt (0 = the batch already reached its end time)
#define S_HMAX 2       // uint (float bits): deepest water now, sets the next dt
#define S_HNEXT 3      // uint (float bits), atomic: deepest water after this step
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
#define S_RAIN 16      // float: rainfall rate this step (m/s), added to every valid cell
#define S_RAIND 17     // float: rainfall depth added in this batch (m; x valid cells x dx^2 = m3)
#define S_TILES 18     // uint: 0 = every cell runs; 1 = the host turned the skip on (this batch's first redraw scans every cell); 2 = k_tgrow has written ton (a later redraw may leave out a tile that was off)
#define S_QBIN 19      // uint, atomic: inflow through the water-level edges this step (MF_FIX m3/s)
#define S_WORDS 32
// The two sums over many threads (edge outflow, clipped volume) are added as fixed-point uints:
// not every Apple GPU has float atomics (the CI runner's paravirtual GPU has none). Each SIMD
// group's sum is rounded to 1e-4 (m3/s, m3) before it is added: unbiased, and 4.3e5 at most.
#define MF_FIX 1.0e-4f

// Dry-tile skip (Runner 3.90.0, Boann 5.5.0), the GPU twin of the CPU build's dry-cell skip
// (cpu_kernels.cpp mf_rows). The window is cut into MF_TILE x MF_TILE cell tiles. Every few steps
// (the engine's SKIP_CHUNK, 8) k_tclear / k_tseed / k_tgrow mark the tiles holding a seed - a wet
// cell, a cell beside a face whose discharge or recorded velocity is not zero, or a source cell -
// and the 8 tiles around each. Water moves at most one cell per step, so a cell in an unmarked
// tile is more than MF_TILE > SKIP_CHUNK + 1 cells from every seed: it stays dry for the chunk,
// the faces beside it keep exactly 0, and its maxima do not move, which is what the full window
// computes there. The grid kernels skip those cells (k_limit and k_cont still take part in their
// SIMD-group reductions with the values a dry cell gives, so the groups and sums are unchanged).
// Same numbers bit for bit, as test_boann_gpu_tiles checks. Rain wets every cell: no skip.
// The host sets S_TILES to 1 once per batch, so the first redraw reads every cell (the window
// may have moved, and ton is not cleared between batches). k_tgrow then sets S_TILES to 2 after
// it writes ton, and a later redraw in the same batch does not re-read a cell whose tile was
// off. The window does not move inside a batch, and an off tile stays dry for the chunk, so a
// new seed can appear only in a tile that was on. The next batch starts at 1 again (full scan).
// Thread (0, 0) still marks every source. k_tclear still runs.
#define MF_TILE 16u

struct Params {
    uint nx, ny;          // grid columns, rows
    uint r0, c0;          // window origin (row, column)
    uint wr, wc;          // window rows, columns
    uint free_mask;       // MF_N | MF_S | MF_E | MF_W
    uint n_src;           // source cells
    uint n_series;        // inflow series
    uint fric2d;          // 1: friction from the 2-D discharge vector (LISFLOOD-FP 8 default)
    uint clip_end;        // 1: the last step is cut to land on t_end (the end of the run only;
                          // like LISFLOOD-FP, output times are passed, not landed on)
    uint inflow_lf_order; // 1 (default): a point source's water is withheld from the step's
                          // donor-cell limiter, so the limiter caps outflow at the water the cell
                          // held at step start (LISFLOOD-FP's DryCheck-then-source order) and the
                          // breach-inflow cell matches LISFLOOD-FP to the mm. 0: the source counts
                          // as available water the same step (mass-conserving, a lower, CFL-
                          // independent breach-cell depth) - see k_scale.
    float dx;             // cell size (m)
    float dt0;            // initial_tstep: the longest step (s)
    float alpha;          // CFL factor (0.7)
    float g;              // 9.80665
    float dry;            // 1e-3 m: no flow on shallower faces
    float maxhflow;       // 10 m
    float velmin;         // unused (kept for the layout): velocities follow LISFLOOD-FP
    float arr_depth;      // depth that counts as arrival (inittm_depth)
    float t0;             // model time at the batch start (s)
    float t_end;          // batch end, relative to t0 (s)
    float theta;          // q-centred weighting (de Almeida et al. 2012); 1 = Bates et al. 2010
    uint rain;            // 1: series n_series is a rainfall rate (m/s, LISFLOOD-FP's "rainfall"
                          // .rain file), added to every valid cell each step (Runner 3.60.0)
    uint n_wl;            // water-level (HFIX / HVAR) edge faces, s_cell / s_series [n_src ..)
                          // (Boann 6.8.0; 0 = none, and nothing below reads them)
    uint src_cfl;         // 1: inflow-aware time step ("sourcecfl" in the .par; Boann 6.13.0):
                          // see mf_src_dt. 0 (default): the step is LISFLOOD-FP 8.3's
};
inline uint mf_cells(constant Params& p) { return p.nx * p.ny; }

#define MF_ARGS \
    device const float* z B(0), \
    device const float* n2 B(1), \
    device float* h B(2), \
    device float* qx B(3), \
    device float* qy B(4), \
    device float* src B(5), \
    device float* scl B(6), \
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
    device float* qxo B(19), \
    device float* qyo B(20), \
    device float* vxf B(21), \
    device float* vyf B(22), \
    device uint* tseed B(23), \
    device uint* ton B(24), \
    constant Params& p B(25), \
    uint2 gid GID

// -- water-level (stage) boundary, Boann 6.8.0 ----------------------------------------------
// A water-level face j (0 <= j < n_wl) is s_cell[n_src + j] = its edge position (W: row,
// E: ny + row, N: 2 ny + column, S: 2 ny + nx + column) and s_series[n_src + j] = its level
// series (relative to the bed reference). k_begin writes each face's level at the step start into
// src[cells + position]; a position that is not a water-level face holds MF_WL_NONE. A deck
// without water levels has n_wl = 0: src has no slots past the cells and none of this runs.
#define MF_WL_NONE (-1.0e30f)
#define MF_WL_CUT (-1.0e29f)        // a slot above this holds a level

// the edge cell of edge position pos
inline uint mf_wl_cell(uint pos, constant Params& p) {
    if (pos < p.ny) {
        return pos * p.nx;                                   // W
    }
    pos -= p.ny;
    if (pos < p.ny) {
        return pos * p.nx + p.nx - 1u;                       // E
    }
    pos -= p.ny;
    if (pos < p.nx) {
        return pos;                                          // N
    }
    return (p.ny - 1u) * p.nx + (pos - p.nx);                // S
}

// LISFLOOD-FP 8.3's acceleration HFIX / HVAR edge face (boundary.cpp BCs): the ghost surface
// stands at the level lev over the edge cell's bed zc; hflow = max(hc, lev - zc); no flow below
// the dry depth; the discharge from the surface step (lev - zc) - hc over dx, with the 1-D
// Manning term. sgn = +1 on the N / W edges (inflow is +), -1 on the E / S edges (inflow is -).
inline float mf_wl_q(float q0, float lev, float zc, float hc, float n2c, bool okc, float sgn,
                     float dt, constant Params& p) {
    float hflow = fmax(hc, lev - zc);
    if (!okc || !(hflow > p.dry)) {
        return 0.0f;
    }
    float dh = (lev - zc) - hc;
    return (q0 + sgn * p.g * dt * hflow * dh / p.dx)
        / (1.0f + p.g * dt * n2c * fabs(q0) / pow(hflow, 7.0f / 3.0f));
}

// -- helpers ------------------------------------------------------------------------------

// local-inertial update of one interior face between cells l and r (r is the + side), as
// LISFLOOD-FP 8.3's CalcFPQxAcc: q the face's last discharge (m2/s), qup / qdn the faces before
// and after it along the flow axis (q-centred scheme, used when theta < 1), qperp the mean of
// the four crossing faces around it (friction from the 2-D discharge vector when fric2d)
inline float mf_face(float q, float qup, float qdn, float qperp, float zl, float hl, float zr,
                     float hr, bool wet_ok, float n2f, float dt, constant Params& p) {
    float hf = fmax(zl + hl, zr + hr) - fmax(zl, zr);
    if (!wet_ok || !(hf > p.dry)) {
        return 0.0f;
    }
    hf = fmin(hf, p.maxhflow);
    float ds = ((zr - zl) + (hr - hl)) / p.dx;
    float qv = p.fric2d != 0u ? sqrt(q * q + qperp * qperp) : fabs(q);
    float den = 1.0f + p.g * dt * n2f * qv / pow(hf, 7.0f / 3.0f);
    float push = p.g * hf * dt * ds;
    if (p.theta < 1.0f) {
        float qc = (p.theta * q + 0.5f * (1.0f - p.theta) * (qup + qdn) - push) / den;
        // LISFLOOD-FP: where the centred value flows uphill, the semi-implicit one is used
        if (!(qc * ds > 0.0f)) {
            return qc;
        }
    }
    return (q - push) / den;
}

// the face velocity LISFLOOD-FP records for maxVc: the new discharge over the flow depth it was
// computed with (capped at maxhflow), before any limiting; 0 where there is no flow
inline float mf_vface(float q, float zl, float hl, float zr, float hr, constant Params& p) {
    if (q == 0.0f) {
        return 0.0f;
    }
    float hf = fmin(fmax(zl + hl, zr + hr) - fmax(zl, zr), p.maxhflow);
    return q / hf;
}

// LISFLOOD-FP's FREE edge with no slope given: magnitude of the outward discharge from the
// edge cell's depth hc and the water-surface slope to its inner neighbour
inline float mf_free(float q0, float hc, float etac, float etain, float n2c, bool okc,
                            float dt, constant Params& p) {
    if (!(okc && hc > p.dry)) {
        return 0.0f;
    }
    q0 = fabs(q0);
    float hs = fmin(hc, p.maxhflow);
    return (q0 + p.g * dt * hs * fabs(etac - etain) / p.dx)
        / (1.0f + p.g * dt * n2c * q0 / pow(hs, 7.0f / 3.0f));
}

// one inflow series at time tm, as LISFLOOD-FP's InterpolateTimeSeries: linear, the first value
// before the start and the last value after the end
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


// The inflow-aware time step ("sourcecfl" in the .par, Params.src_cfl; Boann 6.13.0). A point
// source of Q (m3/s) into a cell of width dx pushes water out of that cell at about q / h_s on top
// of the wave speed sqrt(g h_s), with q = Q / dx (m2/s) and h_s the cell's depth, taken at least
// at critical depth (q^2 / g)^(1/3) (an empty cell fills to it within a step or two). So the step
// is also held to cfl dx / (q / h_s + sqrt(g h_s)) at every source cell. The plain CFL, from the
// deepest water only, does not see this: on Lake Delhi at 10 m (9 breach cells, ~443 m3/s each at
// the peak) it let the breach cells overshoot and checkerboard 3.5 km of valley at CFL 0.7, while
// with this bound CFL 0.7 gives the CFL 0.35 flood to 1 mm in fewer steps than CFL 0.5. It binds
// while a source pours in faster than the deepest water's wave speed (a breach, a filling cell);
// it moves the source cell and its near field, and turns nothing on without "sourcecfl".
inline float mf_src_dt(float dt, float qs, float hs, float dx, float alpha, float g) {
    float q = fabs(qs) / dx;
    if (!(q > 0.0f)) {
        return dt;
    }
    float hc = pow(q * q / g, 1.0f / 3.0f);
    float h = fmax(hs, hc);
    return fmin(dt, alpha * dx / (q / h + sqrt(g * h)));
}

// -- kernels --------------------------------------------------------------------------------

// one thread: the step's dt (CFL from the deepest water; no step starts at or past the batch
// end, and with clip_end none ends past it) and the
// source rates at the start of the step (as LISFLOOD-FP)
kernel void k_begin(MF_ARGS) {
    device float* sf = (device float*)st;
    float t = sf[S_T];
    float rem = p.t_end - t;
    if (!(rem > MF_TEPS)) {
        sf[S_DT] = 0.0f;        // the batch has reached its end time: the rest are no-ops
        return;
    }
    float hm = as_type<float>(st[S_HMAX]);
    float tm = p.t0 + t;
    // water levels at the step start; the ghost depth counts in the CFL depth, as a cell's would
    for (uint j = 0; j < p.n_wl; ++j) {
        uint k = p.n_src + j;
        float lev = mf_series(s_series[k], tm, ser_off, ser_t, ser_q, cursor);
        src[mf_cells(p) + s_cell[k]] = lev;
        hm = fmax(hm, lev - z[mf_wl_cell(s_cell[k], p)]);
    }
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
    st[S_QBIN] = 0u;
    for (uint i = 0; i < p.n_src; ++i) {
        src[s_cell[i]] = 0.0f;
    }
    float qin = 0.0f;
    for (uint i = 0; i < p.n_src; ++i) {
        float q = mf_series(s_series[i], tm, ser_off, ser_t, ser_q, cursor);
        src[s_cell[i]] += q;
        qin += q;
    }
    if (p.src_cfl != 0u) {          // the inflow-aware step (mf_src_dt); the step's dt is only cut
        float dts = dt;
        for (uint i = 0; i < p.n_src; ++i) {
            dts = mf_src_dt(dts, src[s_cell[i]], h[s_cell[i]], p.dx, p.alpha, p.g);
        }
        if (dts < dt) {
            dt = dts;
            sf[S_DT] = dt;
        }
    }
    sf[S_QIN] = qin;
    sf[S_VIN] += qin * dt;
    // rainfall: one rate over the whole grid at the step start, as LISFLOOD-FP's Rainfall()
    float rr = 0.0f;
    if (p.rain != 0u) {
        rr = fmax(mf_series(p.n_series, tm, ser_off, ser_t, ser_q, cursor), 0.0f);
    }
    sf[S_RAIN] = rr;
    sf[S_RAIND] += rr * dt;
}

// every face of the window: thread (x, y) owns x-face (row r0+y, face c0+x) when y < wr and
// y-face (face row r0+y, column c0+x) when x < wc. Reads last step's discharges (qxo, qyo:
// neighbours too, for the q-centred scheme and the 2-D friction), writes the new ones (qx, qy)
kernel void k_faces(MF_ARGS) {
    device float* sf = (device float*)st;
    float dt = sf[S_DT];
    if (dt <= 0.0f || !mf_on(st, ton, gid.x, gid.y, p)) {
        return;
    }
    uint nx = p.nx;
    uint ny = p.ny;
    uint r = p.r0 + gid.y;
    uint c = p.c0 + gid.x;
    if (gid.y < p.wr) {
        uint f = r * (nx + 1u) + c;
        float q = 0.0f;
        float v = 0.0f;
        float lev = (c == 0u || c == nx) && p.n_wl != 0u
            ? src[mf_cells(p) + (c == 0u ? r : ny + r)] : MF_WL_NONE;
        if (lev > MF_WL_CUT) {          // water-level edge face (W or E)
            uint i = c == 0u ? r * nx : r * nx + nx - 1u;
            q = mf_wl_q(qxo[f], lev, z[i], h[i], n2[i], ok[i] != 0, c == 0u ? 1.0f : -1.0f, dt, p);
        } else if (c == 0u) {
            if ((p.free_mask & MF_W) != 0u && nx > 1u) {
                uint i = r * nx;
                q = -mf_free(qxo[f], h[i], z[i] + h[i], z[i + 1u] + h[i + 1u], n2[i], ok[i] != 0,
                             dt, p);
            }
        } else if (c == nx) {
            if ((p.free_mask & MF_E) != 0u && nx > 1u) {
                uint i = r * nx + nx - 1u;
                q = mf_free(qxo[f], h[i], z[i] + h[i], z[i - 1u] + h[i - 1u], n2[i], ok[i] != 0,
                            dt, p);
            }
        } else {
            uint l = r * nx + c - 1u;
            uint rr = l + 1u;
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
            q = mf_face(q0, qup, qdn, qperp, z[l], h[l], z[rr], h[rr],
                        ok[l] != 0 && ok[rr] != 0, 0.5f * (n2[l] + n2[rr]), dt, p);
            v = mf_vface(q, z[l], h[l], z[rr], h[rr], p);
        }
        qx[f] = q;
        vxf[f] = v;
    }
    if (gid.x < p.wc) {
        uint f = r * nx + c;
        float q = 0.0f;
        float v = 0.0f;
        float lev = (r == 0u || r == ny) && p.n_wl != 0u
            ? src[mf_cells(p) + (r == 0u ? 2u * ny + c : 2u * ny + nx + c)] : MF_WL_NONE;
        if (lev > MF_WL_CUT) {          // water-level edge face (N or S)
            uint i = r == 0u ? c : (ny - 1u) * nx + c;
            q = mf_wl_q(qyo[f], lev, z[i], h[i], n2[i], ok[i] != 0, r == 0u ? 1.0f : -1.0f, dt, p);
        } else if (r == 0u) {
            if ((p.free_mask & MF_N) != 0u && ny > 1u) {
                uint i = c;
                q = -mf_free(qyo[f], h[i], z[i] + h[i], z[i + nx] + h[i + nx], n2[i], ok[i] != 0,
                             dt, p);
            }
        } else if (r == ny) {
            if ((p.free_mask & MF_S) != 0u && ny > 1u) {
                uint i = (ny - 1u) * nx + c;
                q = mf_free(qyo[f], h[i], z[i] + h[i], z[i - nx] + h[i - nx], n2[i], ok[i] != 0,
                            dt, p);
            }
        } else {
            uint u = (r - 1u) * nx + c;
            uint d = u + nx;
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
            q = mf_face(q0, qup, qdn, qperp, z[u], h[u], z[d], h[d],
                        ok[u] != 0 && ok[d] != 0, 0.5f * (n2[u] + n2[d]), dt, p);
            v = mf_vface(q, z[u], h[u], z[d], h[d], p);
        }
        qy[f] = q;
        vyf[f] = v;
    }
}

// every cell of the window: the share of its outflows it can afford this step
kernel void k_scale(MF_ARGS) {
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
                 + fmax(qy[i + nx], 0.0f) + fmax(-qy[i], 0.0f)) * dt * p.dx;
    // LISFLOOD-FP order (inflow_lf_order=1): this step's inflow is NOT yet available to flow out,
    // so the limiter caps outflow at the water the cell held at step start (LISFLOOD-FP's
    // DryCheck); k_cont adds the source after. 0: count the inflow as available (mass-conserving).
    float avail = h[i] * p.dx * p.dx + (p.inflow_lf_order ? 0.0f : src[i] * dt);
    scl[i] = (out > avail && out > 0.0f) ? avail / out : 1.0f;
}

// every face of the window: scale each discharge by its donor cell's share, keep it as next
// step's old discharge, and add up what leaves through the free edges
kernel void k_limit(MF_ARGS) {
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
    float inq = 0.0f;               // water in through a water-level edge (walls and FREE edges
                                    // carry none: their discharge is 0 or outward)
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
        } else if (c == 0u && q > 0.0f) {
            inq += q;
        } else if (c == nx && q < 0.0f) {
            inq += -q;
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
        } else if (r == 0u && q > 0.0f) {
            inq += q;
        } else if (r == ny && q < 0.0f) {
            inq += -q;
        }
    }
    float tot = simd_sum(out);
    if (simd_is_first() && tot != 0.0f) {
        atomic_fetch_add_explicit((device atomic_uint*)(st + S_QOUT),
                                  uint(tot * p.dx / MF_FIX + 0.5f), memory_order_relaxed);
    }
    if (p.n_wl != 0u) {
        float tin = simd_sum(inq);
        if (simd_is_first() && tin != 0.0f) {
            atomic_fetch_add_explicit((device atomic_uint*)(st + S_QBIN),
                                      uint(tin * p.dx / MF_FIX + 0.5f), memory_order_relaxed);
        }
    }
}

// every cell of the window: continuity, the depth >= 0 clip, and the running maxima
kernel void k_cont(MF_ARGS) {
    device float* sf = (device float*)st;
    float dt = sf[S_DT];
    if (dt <= 0.0f) {
        return;
    }
    uint nx = p.nx;
    uint r = p.r0 + gid.y;
    uint c = p.c0 + gid.x;
    // a skipped cell is far from water: it stays dry (depth 0, nothing clipped, no maximum), and
    // takes part in the SIMD-group reductions below with exactly those values (see MF_TILE)
    float hn = 0.0f;
    float clip = 0.0f;
    if (mf_on(st, ton, gid.x, gid.y, p)) {
        uint i = r * nx + c;
        uint fx = r * (nx + 1u) + c;
        float dx = p.dx;
        hn = h[i] + dt / dx * (qx[fx] - qx[fx + 1u] + qy[i] - qy[i + nx])
            + src[i] * dt / (dx * dx) + sf[S_RAIN] * dt;
        if (hn < 0.0f) {
            clip = -hn * dx * dx;
            hn = 0.0f;
        }
        if (ok[i] == 0) {
            hn = 0.0f;
        }
        h[i] = hn;
        // maxima as LISFLOOD-FP keeps them: only water deeper than 1 mm counts, from the first
        // step on (a start-depth pond's first depth is not a maximum, its arrival is the first step)
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

// every cell of the window: LISFLOOD-FP's UpdateV, Vc = |(max |vx| of its two x faces, max |vy|
// of its two y faces)| from the face velocities k_faces recorded, into the running maximum.
// (The FREE edge faces carry no velocity in LISFLOOD-FP either.)
kernel void k_speed(MF_ARGS) {
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
    float hv = h[i];
    if (hv > p.dry) {
        float dv = hv * v;
        uint j = mf_cells(p) + i;
        if (dv > vmax[j]) {
            vmax[j] = dv;
        }
    }
}

// one thread: close the step
kernel void k_end(MF_ARGS) {
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
    if (st[S_QBIN] != 0u) {         // water-level inflow: booked as inflow (Qin, injected volume)
        float qb = float(st[S_QBIN]) * MF_FIX;
        sf[S_QIN] += qb;
        sf[S_VIN] += qb * dt;
    }
    st[S_HMAX] = st[S_HNEXT];
}

// -- dry-tile skip (see MF_TILE) ---------------------------------------------------------------
// The host runs these three, in this order, before the first step of every chunk of steps when it
// turns the skip on (st[S_TILES] = 1). Grid: k_tclear / k_tgrow one thread per tile of the window
// ((wc + MF_TILE - 1) / MF_TILE x (wr + MF_TILE - 1) / MF_TILE), k_tseed one per window cell.

kernel void k_tclear(MF_ARGS) {
    uint ntx = (p.wc + MF_TILE - 1u) / MF_TILE;
    uint nty = (p.wr + MF_TILE - 1u) / MF_TILE;
    if (gid.x < ntx && gid.y < nty) {
        tseed[gid.y * ntx + gid.x] = 0u;
    }
}

// a window cell is a seed when it holds water or a face beside it carried flow or recorded a
// velocity in the last step (as the CPU skip's mf_rows); thread (0, 0) also marks the sources.
// S_TILES == 1 (the host, once per batch) reads every cell: the window may have moved, and ton
// is not cleared between batches. S_TILES == 2 reads a cell only when its tile was on. ton still
// holds that map here, because only k_tgrow writes it and k_tgrow runs after this kernel.
kernel void k_tseed(MF_ARGS) {
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
        if (h[i] > 0.0f || qxo[fx] != 0.0f || vxf[fx] != 0.0f || qxo[fx + 1u] != 0.0f ||
            vxf[fx + 1u] != 0.0f || qyo[i] != 0.0f || vyf[i] != 0.0f || qyo[i + nx] != 0.0f ||
            vyf[i + nx] != 0.0f) {
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
        // a water-level edge cell is a seed like a source: water can come in there at any step
        for (uint j = 0; j < p.n_wl; ++j) {
            uint e = mf_wl_cell(s_cell[p.n_src + j], p);
            uint sr = e / nx;
            uint sc = e % nx;
            if (sr >= p.r0 && sr < p.r0 + p.wr && sc >= p.c0 && sc < p.c0 + p.wc) {
                atomic_fetch_max_explicit((device atomic_uint*)(tseed + ((sr - p.r0) / MF_TILE)
                                                                * ntx + (sc - p.c0) / MF_TILE),
                                          1u, memory_order_relaxed);
            }
        }
    }
}

// a tile runs when it or one of its 8 neighbours holds a seed (or it rains: every cell runs)
kernel void k_tgrow(MF_ARGS) {
    uint ntx = (p.wc + MF_TILE - 1u) / MF_TILE;
    uint nty = (p.wr + MF_TILE - 1u) / MF_TILE;
    if (gid.x >= ntx || gid.y >= nty) {
        return;
    }
    uint on = p.rain != 0u ? 1u : 0u;
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
