// BoannGrid D8 flow directions + accumulation, on the depression-filled surface.
//
// The D8 (8-connected) sibling of kernels_flow.metal. Same three-kernel shape, same buffers, same
// double-buffered gather; the only difference is that each cell drains to the steepest of its
// EIGHT neighbours by DISTANCE-WEIGHTED drop (O'Callaghan & Mark; the WhiteboxTools / standard GIS
// D8), so a diagonal neighbour competes on slope (drop / sqrt(2)), not raw drop.
//
//   k_d8_dir   per cell, the single D8 steepest-descent direction on the filled surface w: the
//              neighbour maximising (w[i]-w[j]) * invdist over the strictly-lower neighbours, with
//              ties broken in the fixed order N,E,S,W,NE,SE,SW,NW so the result is deterministic.
//              A cell with no lower valid neighbour (a grid outlet, or draining to NoData) gets
//              NONE(8).
//   k8_accum_a a1[c] = 1 + sum of a0 over the (up to 8) neighbours that flow INTO c (gather).
//   k8_accum_b a0[c] = 1 + sum of a1 over the neighbours that flow into c; counts a0 changes in
//              st[0] so the host can stop when the accumulation has converged.
//
// D8 is here as an OPTION beside the 4-connected default: the flood engine routes D4, but channel
// extraction and the WhiteboxTools cross-check want the standard 8-connected drainage area. The
// filled surface drains everywhere and every flow edge is strictly downhill, so the D8 network is
// acyclic and the gather has the same unique fixed point = drainage area, identical on the CPU
// build and any Metal GPU.

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
#define DIR_NE 4u
#define DIR_SE 5u
#define DIR_SW 6u
#define DIR_NW 7u
#define DIR_NONE 8u

#define INV_DIAG 0.70710678f   // 1 / sqrt(2): diagonal slope weight

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
kernel void k_d8_dir(FF_ARGS) {
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
    float wi = w[i];
    float bests = 0.0f;                  // best (strictly positive) distance-weighted drop
    uint bestd = DIR_NONE;
    bool hasN = (y > 0u), hasS = (y + 1u < p.ny), hasW = (x > 0u), hasE = (x + 1u < p.nx);
    // cardinals first (weight 1), then diagonals (weight 1/sqrt2); fixed order N,E,S,W,NE,SE,SW,NW
    if (hasN) {
        uint j = (y - 1u) * p.nx + x;
        if (ok[j] != 0u) { float s = wi - w[j]; if (s > bests) { bests = s; bestd = DIR_N; } }
    }
    if (hasE) {
        uint j = y * p.nx + (x + 1u);
        if (ok[j] != 0u) { float s = wi - w[j]; if (s > bests) { bests = s; bestd = DIR_E; } }
    }
    if (hasS) {
        uint j = (y + 1u) * p.nx + x;
        if (ok[j] != 0u) { float s = wi - w[j]; if (s > bests) { bests = s; bestd = DIR_S; } }
    }
    if (hasW) {
        uint j = y * p.nx + (x - 1u);
        if (ok[j] != 0u) { float s = wi - w[j]; if (s > bests) { bests = s; bestd = DIR_W; } }
    }
    if (hasN && hasE) {
        uint j = (y - 1u) * p.nx + (x + 1u);
        if (ok[j] != 0u) { float s = (wi - w[j]) * INV_DIAG; if (s > bests) { bests = s; bestd = DIR_NE; } }
    }
    if (hasS && hasE) {
        uint j = (y + 1u) * p.nx + (x + 1u);
        if (ok[j] != 0u) { float s = (wi - w[j]) * INV_DIAG; if (s > bests) { bests = s; bestd = DIR_SE; } }
    }
    if (hasS && hasW) {
        uint j = (y + 1u) * p.nx + (x - 1u);
        if (ok[j] != 0u) { float s = (wi - w[j]) * INV_DIAG; if (s > bests) { bests = s; bestd = DIR_SW; } }
    }
    if (hasN && hasW) {
        uint j = (y - 1u) * p.nx + (x - 1u);
        if (ok[j] != 0u) { float s = (wi - w[j]) * INV_DIAG; if (s > bests) { bests = s; bestd = DIR_NW; } }
    }
    dir[i] = (uchar)bestd;
}

// sum of accumulation over the (up to 8) neighbours that flow into cell (x,y): each neighbour
// contributes if its own direction points back at the centre.
static inline uint gather_in8(device const uchar* ok, device const uchar* dir,
                              device const uint* a, uint x, uint y, constant TParams& p) {
    uint s = 0u;
    bool hasN = (y > 0u), hasS = (y + 1u < p.ny), hasW = (x > 0u), hasE = (x + 1u < p.nx);
    if (hasN) {                                   // N neighbour points S
        uint j = (y - 1u) * p.nx + x;
        if (ok[j] != 0u && dir[j] == DIR_S) { s += a[j]; }
    }
    if (hasE) {                                   // E neighbour points W
        uint j = y * p.nx + (x + 1u);
        if (ok[j] != 0u && dir[j] == DIR_W) { s += a[j]; }
    }
    if (hasS) {                                   // S neighbour points N
        uint j = (y + 1u) * p.nx + x;
        if (ok[j] != 0u && dir[j] == DIR_N) { s += a[j]; }
    }
    if (hasW) {                                   // W neighbour points E
        uint j = y * p.nx + (x - 1u);
        if (ok[j] != 0u && dir[j] == DIR_E) { s += a[j]; }
    }
    if (hasN && hasE) {                           // NE neighbour points SW
        uint j = (y - 1u) * p.nx + (x + 1u);
        if (ok[j] != 0u && dir[j] == DIR_SW) { s += a[j]; }
    }
    if (hasS && hasE) {                           // SE neighbour points NW
        uint j = (y + 1u) * p.nx + (x + 1u);
        if (ok[j] != 0u && dir[j] == DIR_NW) { s += a[j]; }
    }
    if (hasS && hasW) {                           // SW neighbour points NE
        uint j = (y + 1u) * p.nx + (x - 1u);
        if (ok[j] != 0u && dir[j] == DIR_NE) { s += a[j]; }
    }
    if (hasN && hasW) {                           // NW neighbour points SE
        uint j = (y - 1u) * p.nx + (x - 1u);
        if (ok[j] != 0u && dir[j] == DIR_SE) { s += a[j]; }
    }
    return s;
}

// --- accumulation, double-buffered (a0 -> a1) -------------------------------------------------
kernel void k8_accum_a(FF_ARGS) {
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
    a1[i] = 1u + gather_in8(ok, dir, a0, x, y, p);
}

// --- accumulation, double-buffered (a1 -> a0), counting changes --------------------------------
kernel void k8_accum_b(FF_ARGS) {
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
    uint nv = 1u + gather_in8(ok, dir, a1, x, y, p);
    if (nv != a0[i]) {
        atomic_fetch_add_explicit(st, 1u, memory_order_relaxed);
    }
    a0[i] = nv;
}
