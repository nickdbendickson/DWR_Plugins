// CPU build of kernels_fill.metal: the same kernel source, called once per "thread" in the
// order the GPU would run one sweep. backends.CpuBackend compiles this file with the system C++
// compiler at first use and calls mf_run_batch through ctypes. It reuses core/metalflood's
// mf_shim.h (found via the -I that backends.build_cpu_library passes for the shim's folder).
// bg_priority_flood (below) is the fill fill.fill_dem uses by default; it is plain C++ and does
// not use the kernel.
#include <cstdint>
#include <queue>
#include <vector>

#include "mf_shim.h"
#include "kernels_fill.metal"

// the kernel is a template argument, so the compiler inlines it into the loop
template <void (*K)(const float*, const uchar*, float*, atomic_uint*, const TParams&, uint2)>
static void tf_dispatch(void** b, const TParams& p, uint w, uint hgt) {
    for (uint y = 0; y < hgt; ++y) {
        for (uint x = 0; x < w; ++x) {
            uint2 g = {x, y};
            K((const float*)b[0], (const uchar*)b[1], (float*)b[2], (atomic_uint*)b[3], p, g);
        }
    }
}

extern "C" {

int mf_abi_version(void) { return 1; }

int mf_params_size(void) { return (int)sizeof(TParams); }

// n relaxation sweeps over the window; the same kernel and order as backends.MetalBackend
int mf_run_batch(void** b, const TParams* pp, int nsteps) {
    const TParams& p = *pp;
    for (int s = 0; s < nsteps; ++s) {
        tf_dispatch<k_pd_relax>(b, p, p.wc, p.wr);
    }
    return 0;
}

// Priority-Flood+eps (Barnes et al. 2014), the fill fill.fill_dem uses by default (Runner 3.83.0).
// z, w: float64 ny*nx row-major; ok, outlet: uint8 (1 = valid / seed); conn 4 or 8.
// Writes w (valid cells; NoData cells get z back). Returns the number of valid cells never
// reached (0 on any real DEM), or -1 on bad arguments.
// Ties break on the smaller flat index, which is (row, col) order: the same order as
// core/terrain.priority_flood's (level, row, col) heap. No pit queue, no float32, no datum shift.
long long bg_priority_flood(const double* z, const unsigned char* ok, const unsigned char* outlet,
                           double* w, int ny, int nx, int conn, double eps) {
    if (!z || !ok || !outlet || !w || ny < 1 || nx < 1 || (conn != 4 && conn != 8))
        return -1;
    const int64_t n = (int64_t)ny * (int64_t)nx;
    typedef std::pair<double, int64_t> E;
    std::priority_queue<E, std::vector<E>, std::greater<E> > pq;
    std::vector<uint8_t> closed((size_t)n, 0);
    for (int64_t i = 0; i < n; ++i) {
        w[i] = z[i];
        closed[i] = ok[i] ? 0 : 1;
    }
    for (int64_t i = 0; i < n; ++i) {
        if (ok[i] && outlet[i]) {
            closed[i] = 1;
            pq.push(E(w[i], i));
        }
    }
    const int d4[4][2] = {{-1, 0}, {1, 0}, {0, -1}, {0, 1}};
    const int d8[8][2] = {{-1, -1}, {-1, 0}, {-1, 1}, {0, -1}, {0, 1}, {1, -1}, {1, 0}, {1, 1}};
    const int nd = conn == 8 ? 8 : 4;
    while (!pq.empty()) {
        E t = pq.top();
        pq.pop();
        const int64_t i = t.second;
        const int y = (int)(i / nx);
        const int x = (int)(i - (int64_t)y * nx);
        const double wi = w[i];
        for (int k = 0; k < nd; ++k) {
            const int dy = conn == 8 ? d8[k][0] : d4[k][0];
            const int dx = conn == 8 ? d8[k][1] : d4[k][1];
            const int yy = y + dy;
            const int xx = x + dx;
            if (yy < 0 || yy >= ny || xx < 0 || xx >= nx)
                continue;
            const int64_t j = (int64_t)yy * nx + xx;
            if (closed[j])
                continue;
            closed[j] = 1;
            if (w[j] <= wi)
                w[j] = wi + eps;          // one double add; -ffp-contract=off, so no FMA
            pq.push(E(w[j], j));
        }
    }
    long long unreached = 0;
    for (int64_t i = 0; i < n; ++i)
        if (ok[i] && !closed[i])
            ++unreached;
    return unreached;
}

}
