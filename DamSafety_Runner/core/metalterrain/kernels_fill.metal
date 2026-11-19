// BoannGrid depression-fill kernel: Planchon & Darboux (2002) relaxation, one GPU thread per
// cell. It computes the depression-filled surface w (every cell drains to an outlet), the same
// surface core/terrain.priority_flood (Barnes et al.) computes serially, but as a monotone
// iterate-to-convergence relaxation that fits the Apple GPU.
//
// This one source is compiled two ways, like core/metalflood/kernels.metal:
//   - by Metal at run time on a Mac (backends.MetalBackend, Apple GPU, float32);
//   - as C++ with core/metalflood/mf_shim.h (cpu_kernels_fill.cpp, backends.CpuBackend), the
//     CPU build used in the cloud tests and on computers without a Metal GPU.
// It reuses metalflood's backend and shim; only the kernel, its Params and its one-kernel
// dispatch plan differ.
//
// Water levels only ever decrease (from a large start toward the solution), so the fixed point
// is unique and reached from any thread order: the converged surface is identical on the CPU
// build and on any Metal GPU, which is what makes the determinism/byte-parity test hold. Outlet
// cells (grid border and cells next to NoData) are seeded to the ground by the host and never
// rise; NoData cells are skipped (they act as outlets for their valid neighbours), so a basin
// enclosed by NoData drains to that NoData rather than filling without bound.

#ifndef MF_CPU
#include <metal_stdlib>
using namespace metal;
#define B(i) [[buffer(i)]]
#define GID [[thread_position_in_grid]]
#endif

struct TParams {
    uint nx, ny;      // grid columns, rows
    uint r0, c0;      // window origin (row, column); the fill uses the whole grid
    uint wr, wc;      // window rows, columns
    uint pad;
    float eps;        // minimal drainage increment so flats drain (m); matches the oracle's 1e-5
    float tol;        // a lowering smaller than this does not count as a change (m)
    float pad2;
};

// one counter in st[0] (atomic uint): cells lowered this sweep; 0 over a batch = converged
#define TF_ARGS \
    device const float* z B(0), \
    device const uchar* ok B(1), \
    device float* w B(2), \
    device atomic_uint* st B(3), \
    constant TParams& p B(4), \
    uint2 gid GID

kernel void k_pd_relax(TF_ARGS) {
    if (gid.x >= p.wc || gid.y >= p.wr) {
        return;
    }
    uint x = p.c0 + gid.x;
    uint y = p.r0 + gid.y;
    uint i = y * p.nx + x;
    if (ok[i] == 0u) {
        return;                       // NoData: left as-is; an outlet for its valid neighbours
    }
    float wi = w[i];
    float zi = z[i];
    if (!(wi > zi)) {
        return;                       // already at the ground: a fixed outlet, or fully drained
    }
    float m = wi;                     // lowest neighbour water level over the 8 valid neighbours
    for (int dy = -1; dy <= 1; ++dy) {
        for (int dx = -1; dx <= 1; ++dx) {
            if (dx == 0 && dy == 0) {
                continue;
            }
            int xx = int(x) + dx;
            int yy = int(y) + dy;
            if (xx < 0 || yy < 0 || xx >= int(p.nx) || yy >= int(p.ny)) {
                continue;
            }
            uint j = uint(yy) * p.nx + uint(xx);
            if (ok[j] == 0u) {
                continue;
            }
            float wj = w[j];
            if (wj < m) {
                m = wj;
            }
        }
    }
    float nv = fmax(zi, m + p.eps);
    if (nv < wi - p.tol) {
        w[i] = nv;
        atomic_fetch_add_explicit(st, 1u, memory_order_relaxed);
    }
}
