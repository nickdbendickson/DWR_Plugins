// CPU build of kernels_flow.metal: the same kernel source, called once per "thread" per sweep in
// the order the GPU would run it. backends.CpuBackend compiles this with the system C++ compiler
// and calls mf_run_batch through ctypes. It reuses core/metalflood's mf_shim.h (found via the -I
// backends.build_cpu_library passes). One sweep = flow direction, then the two accumulation
// half-steps (a0 -> a1 -> a0); the host runs sweeps until the change counter st[0] stays 0.
#include "mf_shim.h"
#include "kernels_flow.metal"

// the flow kernels' shared buffer signature (device/B(i)/GID expand away under the shim)
template <void (*K)(const float*, const uchar*, uchar*, uint*, uint*, atomic_uint*,
                    const TParams&, uint2)>
static void ff_dispatch(void** b, const TParams& p, uint w, uint hgt) {
    for (uint y = 0; y < hgt; ++y) {
        for (uint x = 0; x < w; ++x) {
            uint2 g = {x, y};
            K((const float*)b[0], (const uchar*)b[1], (uchar*)b[2], (uint*)b[3], (uint*)b[4],
              (atomic_uint*)b[5], p, g);
        }
    }
}

extern "C" {

int mf_abi_version(void) { return 1; }

int mf_params_size(void) { return (int)sizeof(TParams); }

// n sweeps over the window; the same kernels and order as backends.MetalBackend's dispatch plan
int mf_run_batch(void** b, const TParams* pp, int nsteps) {
    const TParams& p = *pp;
    for (int s = 0; s < nsteps; ++s) {
        ff_dispatch<k_d4_dir>(b, p, p.wc, p.wr);
        ff_dispatch<k_accum_a>(b, p, p.wc, p.wr);
        ff_dispatch<k_accum_b>(b, p, p.wc, p.wr);
    }
    return 0;
}
}
