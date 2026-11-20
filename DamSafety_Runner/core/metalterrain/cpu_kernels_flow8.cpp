// CPU build of kernels_flow8.metal: the D8 sibling of cpu_kernels_flow.cpp. Same dispatch shape,
// the same mf_shim.h, one "thread" per cell per sweep in GPU order. One sweep = D8 flow direction,
// then the two accumulation half-steps (a0 -> a1 -> a0); the host runs sweeps until st[0] stays 0.
#include "mf_shim.h"
#include "kernels_flow8.metal"

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

int mf_run_batch(void** b, const TParams* pp, int nsteps) {
    const TParams& p = *pp;
    for (int s = 0; s < nsteps; ++s) {
        ff_dispatch<k_d8_dir>(b, p, p.wc, p.wr);
        ff_dispatch<k8_accum_a>(b, p, p.wc, p.wr);
        ff_dispatch<k8_accum_b>(b, p, p.wc, p.wr);
    }
    return 0;
}
}
