// CPU build of kernels_fill.metal: the same kernel source, called once per "thread" in the
// order the GPU would run one sweep. backends.CpuBackend compiles this file with the system C++
// compiler at first use and calls mf_run_batch through ctypes. It reuses core/metalflood's
// mf_shim.h (found via the -I that backends.build_cpu_library passes for the shim's folder).
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
}
