// CPU build of kernels_fv2.metal: the same 2nd-order shock-capturing kernel source, called once per
// "thread" in the order the GPU would run the dispatches of one SSP-RK2 step. backends.CpuBackend
// compiles this file with the system C++ compiler at first use and calls mf_run_batch through
// ctypes. The dispatch order here MUST match _fv2_plan in backends.py exactly.
#include "mf_shim.h"
#include "kernels_fv2.metal"

// the kernel is a template argument, so the compiler inlines it into the loop. The FV2 argument
// list is the 33-buffer MF_ARGS_FV2 (FV1's 24 plus h0/hu0/hv0 and the six slope buffers).
template <void (*K)(const float*, const float*, float*, float*, float*, float*, float*, float*,
                    float*, float*, const uchar*, const uint*, const uint*, const uint*,
                    const float*, const float*, uint*, uint*, float*, float*, float*, float*,
                    float*, float*, float*, float*, float*, float*, float*, float*, float*,
                    float*, float*, const Params&, uint2)>
static void mf_dispatch(void** b, const Params& p, uint w, uint hgt) {
    for (uint y = 0; y < hgt; ++y) {
        for (uint x = 0; x < w; ++x) {
            uint2 g = {x, y};
            K((const float*)b[0], (const float*)b[1], (float*)b[2], (float*)b[3],
              (float*)b[4], (float*)b[5], (float*)b[6], (float*)b[7], (float*)b[8],
              (float*)b[9], (const uchar*)b[10], (const uint*)b[11], (const uint*)b[12],
              (const uint*)b[13], (const float*)b[14], (const float*)b[15], (uint*)b[16],
              (uint*)b[17], (float*)b[18], (float*)b[19], (float*)b[20], (float*)b[21],
              (float*)b[22], (float*)b[23], (float*)b[24], (float*)b[25], (float*)b[26],
              (float*)b[27], (float*)b[28], (float*)b[29], (float*)b[30], (float*)b[31],
              (float*)b[32], p, g);
        }
    }
}

extern "C" {

int mf_abi_version(void) { return 1; }

int mf_params_size(void) { return (int)sizeof(Params); }

// n SSP-RK2 steps on the window in p; the same dispatch order as the Metal backend's FV2 plan:
// predictor (stage 0) then corrector (stage 1), each a begin -> slopes -> faces -> cont sweep.
int mf_run_batch(void** b, const Params* pp, int nsteps) {
    const Params& p = *pp;
    for (int s = 0; s < nsteps; ++s) {
        mf_dispatch<k_stage_p_fv2>(b, p, 1, 1);
        mf_dispatch<k_begin_fv2>(b, p, 1, 1);
        mf_dispatch<k_slopes_fv2>(b, p, p.wc, p.wr);
        mf_dispatch<k_faces_fv2>(b, p, p.wc + 1, p.wr + 1);
        mf_dispatch<k_cont_fv2>(b, p, p.wc, p.wr);
        mf_dispatch<k_stage_c_fv2>(b, p, 1, 1);
        mf_dispatch<k_begin_fv2>(b, p, 1, 1);
        mf_dispatch<k_slopes_fv2>(b, p, p.wc, p.wr);
        mf_dispatch<k_faces_fv2>(b, p, p.wc + 1, p.wr + 1);
        mf_dispatch<k_cont_fv2>(b, p, p.wc, p.wr);
        mf_dispatch<k_end_fv2>(b, p, 1, 1);
    }
    return 0;
}
}
