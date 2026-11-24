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

// Priority-Flood+eps (Barnes, Lehman and Mulla 2014), the fill fill.fill_dem uses by default.
// It reproduces k_pd_relax's float32 arithmetic, so it settles on the relaxation's fixed point
// in one pass instead of thousands of sweeps:
//   z, w: float32 ny*nx row-major, the DATUM-SHIFTED ground (relief, as _fill_relax passes it)
//   ok, outlet: uint8 (1 = valid / seed cell; outlets are fixed at z like the relaxation's)
//   a reached cell j gets  w[j] = fmax(z[j], w[i] + eps)  with i its lowest popped neighbour,
//   the same float32 add and the same fmax as the kernel's  nv = fmax(zi, m + p.eps).
// Water levels pop in non-decreasing order and (w + eps) never decreases w, so the first
// neighbour to reach j is its lowest: the value is the relaxation's fixed point. The relaxation
// stops a lowering smaller than its tol (1e-6 m), so it can sit a float32 step or two above
// this; that is the whole difference. Ties do not change any value (equal levels give equal
// sums). Writes w on valid cells (NoData cells get z back). Returns the number of valid cells
// never reached (0 on any real DEM), or -1 on bad arguments.
long long bg_priority_flood(const float* z, const unsigned char* ok, const unsigned char* outlet,
                           float* w, int ny, int nx, int conn, float eps) {
    if (!z || !ok || !outlet || !w || ny < 1 || nx < 1 || (conn != 4 && conn != 8))
        return -1;
    const int64_t n = (int64_t)ny * (int64_t)nx;
    typedef std::pair<float, int64_t> E;
    std::vector<E> store;
    std::vector<uint8_t> closed((size_t)n, 0);
    for (int64_t i = 0; i < n; ++i) {
        w[i] = z[i];
        closed[i] = ok[i] ? 0 : 1;
    }
    for (int64_t i = 0; i < n; ++i) {
        if (ok[i] && outlet[i]) {
            closed[i] = 1;
            store.push_back(E(w[i], i));
        }
    }
    std::priority_queue<E, std::vector<E>, std::greater<E> > pq(std::greater<E>(), std::move(store));
    const int d4[4][2] = {{-1, 0}, {1, 0}, {0, -1}, {0, 1}};
    const int d8[8][2] = {{-1, -1}, {-1, 0}, {-1, 1}, {0, -1}, {0, 1}, {1, -1}, {1, 0}, {1, 1}};
    const int nd = conn == 8 ? 8 : 4;
    while (!pq.empty()) {
        const E t = pq.top();
        pq.pop();
        const int64_t i = t.second;
        const int y = (int)(i / nx);
        const int x = (int)(i - (int64_t)y * nx);
        const float up = w[i] + eps;       // one float32 add (-ffp-contract=off: no FMA)
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
            w[j] = fmax(z[j], up);         // the kernel's fmax(zi, m + eps), float overload
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
