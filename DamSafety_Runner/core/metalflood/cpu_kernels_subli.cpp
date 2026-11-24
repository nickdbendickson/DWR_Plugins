// CPU build of kernels_subli.metal (the sub-grid local-inertial engine): the same kernel source,
// called once per "thread" in the order the GPU would run the dispatches of one step.
// backends.CpuBackend compiles this with the system C++ compiler at first use and calls mf_run_batch
// through ctypes.
#include "mf_shim.h"
#include "kernels_subli.metal"

// the MF_ARGS_SUBLI signature: 28 buffers (0..27) then const Params& and uint2
template <void (*K)(const float*, const float*, float*, float*, float*, float*, float*, float*,
                    float*, float*, float*, float*, const uchar*, const uint*, const uint*,
                    const uint*, const float*, const float*, uint*, uint*, float*, float*, float*,
                    float*, const float*, const float*, const float*, const float*, const Params&,
                    uint2)>
static void mf_dispatch(void** b, const Params& p, uint w, uint hgt) {
    MF_PAR_FOR
    for (uint y = 0; y < hgt; ++y) {
        for (uint x = 0; x < w; ++x) {
            uint2 g = {x, y};
            K((const float*)b[0], (const float*)b[1], (float*)b[2], (float*)b[3], (float*)b[4],
              (float*)b[5], (float*)b[6], (float*)b[7], (float*)b[8], (float*)b[9], (float*)b[10],
              (float*)b[11], (const uchar*)b[12], (const uint*)b[13], (const uint*)b[14],
              (const uint*)b[15], (const float*)b[16], (const float*)b[17], (uint*)b[18],
              (uint*)b[19], (float*)b[20], (float*)b[21], (float*)b[22], (float*)b[23],
              (const float*)b[24], (const float*)b[25], (const float*)b[26], (const float*)b[27],
              p, g);
        }
    }
}

extern "C" {

int mf_abi_version(void) { return 1; }

int mf_params_size(void) { return (int)sizeof(Params); }

// n steps on the window in p; the same dispatch order as subli_backend._subli_plan
int mf_run_batch(void** b, const Params* pp, int nsteps) {
    const Params& p = *pp;
    for (int s = 0; s < nsteps; ++s) {
        mf_dispatch<k_begin_subli>(b, p, 1, 1);
        mf_dispatch<k_faces_subli>(b, p, p.wc + 1, p.wr + 1);
        mf_dispatch<k_scale_subli>(b, p, p.wc, p.wr);
        mf_dispatch<k_limit_subli>(b, p, p.wc + 1, p.wr + 1);
        mf_dispatch<k_cont_subli>(b, p, p.wc, p.wr);
        mf_dispatch<k_speed_subli>(b, p, p.wc, p.wr);
        mf_dispatch<k_end_subli>(b, p, 1, 1);
    }
    return 0;
}
}
