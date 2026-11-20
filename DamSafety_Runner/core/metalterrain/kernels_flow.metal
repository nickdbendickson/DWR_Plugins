// BoannGrid D4 flow directions + accumulation, on the depression-filled surface.
//
// Three GPU kernels, one thread per cell, in the metalterrain pattern (compiled by Metal at run
// time on a Mac, and as C++ via core/metalflood/mf_shim.h for the cloud CPU build):
//
//   k_d4_dir   per cell, the single D4 (4-connected) steepest-descent direction on the filled
//              surface w: the lowest of the N/E/S/W neighbours that is strictly lower, with ties
//              broken in the fixed order N,E,S,W so the result is deterministic. A cell with no
//              lower valid neighbour (a grid outlet, or a cell draining to NoData) gets NONE(4).
//   k_accum_a  a1[c] = 1 + sum of a0 over the neighbours that flow INTO c (gather, double-buffer)
//   k_accum_b  a0[c] = 1 + sum of a1 over the neighbours that flow into c; counts a0 changes in
//              st[0] so the host can stop when the accumulation has converged.
//
// D4 (not D8) because the local-inertial flood engine is 4-connected: the conditioner's routing
// has to match. The filled surface has no interior pits and drains everywhere (the eps ramp
// gives flats a strict order), so the D4 network is acyclic and the gather iteration has a unique
// fixed point = the drainage area (upstream cell count + 1), reached from any thread order. That
// is what makes the result identical on the CPU build and any Metal GPU.

#ifndef MF_CPU
#include <metal_stdlib>
using namespace metal;
#define B(i) [[buffer(i)]]
#define GID [[thread_position_in_grid]]
#endif

struct TParams {
    uint nx, ny;      // grid columns, rows
    uint r0, c0;      // window origin (row, column); flow uses the whole grid
    uint wr, wc;      // window rows, columns
    uint pad, pad2;
};

// direction codes: the single downstream neighbour, or NONE for an outlet / sink
#define DIR_N 0u
#define DIR_E 1u
#define DIR_S 2u
#define DIR_W 3u
#define DIR_NONE 4u

#define FF_ARGS \
    device const float* w B(0), \
    device const uchar* ok B(1), \
    device uchar* dir B(2), \
    device uint* a0 B(3), \
    device uint* a1 B(4), \
    device atomic_uint* st B(5), \
    constant TParams& p B(6), \
    uint2 gid GID

// --- flow direction ---------------------------------------------------------------------------
kernel void k_d4_dir(FF_ARGS) {
    (void)a0; (void)a1; (void)st;
    if (gid.x >= p.wc || gid.y >= p.wr) {
        return;
    }
    uint x = p.c0 + gid.x;
    uint y = p.r0 + gid.y;
    uint i = y * p.nx + x;
    if (ok[i] == 0u) {
        dir[i] = DIR_NONE;
        return;
    }
    float bestw = w[i];
    uint bestd = DIR_NONE;
    // N, E, S, W in that fixed order; strictly-lower wins, so the first of equal-lowest is kept
    if (y > 0u) {
        uint j = (y - 1u) * p.nx + x;
        if (ok[j] != 0u && w[j] < bestw) { bestw = w[j]; bestd = DIR_N; }
    }
    if (x + 1u < p.nx) {
        uint j = y * p.nx + (x + 1u);
        if (ok[j] != 0u && w[j] < bestw) { bestw = w[j]; bestd = DIR_E; }
    }
    if (y + 1u < p.ny) {
        uint j = (y + 1u) * p.nx + x;
        if (ok[j] != 0u && w[j] < bestw) { bestw = w[j]; bestd = DIR_S; }
    }
    if (x > 0u) {
        uint j = y * p.nx + (x - 1u);
        if (ok[j] != 0u && w[j] < bestw) { bestw = w[j]; bestd = DIR_W; }
    }
    dir[i] = (uchar)bestd;
}

// sum of accumulation over the (up to 4) neighbours that flow into cell (x,y)
static inline uint gather_in(device const uchar* ok, device const uchar* dir,
                             device const uint* a, uint x, uint y, constant TParams& p) {
    uint s = 0u;
    if (y > 0u) {                       // N neighbour flows in if it points S
        uint j = (y - 1u) * p.nx + x;
        if (ok[j] != 0u && dir[j] == DIR_S) { s += a[j]; }
    }
    if (x + 1u < p.nx) {                 // E neighbour flows in if it points W
        uint j = y * p.nx + (x + 1u);
        if (ok[j] != 0u && dir[j] == DIR_W) { s += a[j]; }
    }
    if (y + 1u < p.ny) {                 // S neighbour flows in if it points N
        uint j = (y + 1u) * p.nx + x;
        if (ok[j] != 0u && dir[j] == DIR_N) { s += a[j]; }
    }
    if (x > 0u) {                        // W neighbour flows in if it points E
        uint j = y * p.nx + (x - 1u);
        if (ok[j] != 0u && dir[j] == DIR_E) { s += a[j]; }
    }
    return s;
}

// --- accumulation, double-buffered (a0 -> a1) -------------------------------------------------
kernel void k_accum_a(FF_ARGS) {
    (void)w; (void)st;
    if (gid.x >= p.wc || gid.y >= p.wr) {
        return;
    }
    uint x = p.c0 + gid.x;
    uint y = p.r0 + gid.y;
    uint i = y * p.nx + x;
    if (ok[i] == 0u) {
        a1[i] = 0u;
        return;
    }
    a1[i] = 1u + gather_in(ok, dir, a0, x, y, p);
}

// --- accumulation, double-buffered (a1 -> a0), counting changes --------------------------------
kernel void k_accum_b(FF_ARGS) {
    (void)w;
    if (gid.x >= p.wc || gid.y >= p.wr) {
        return;
    }
    uint x = p.c0 + gid.x;
    uint y = p.r0 + gid.y;
    uint i = y * p.nx + x;
    if (ok[i] == 0u) {
        return;                         // a0 for NoData stays 0 (host seeded it)
    }
    uint nv = 1u + gather_in(ok, dir, a1, x, y, p);
    if (nv != a0[i]) {
        atomic_fetch_add_explicit(st, 1u, memory_order_relaxed);
    }
    a0[i] = nv;
}
