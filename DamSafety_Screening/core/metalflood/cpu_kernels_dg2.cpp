// CPU build of kernels_dg2.metal: the same second-order RKDG2 kernel source, called once per
// "thread" in the order the GPU would run the dispatches of one SSP-RK2 step. backends.CpuBackend
// compiles this file with the system C++ compiler at first use and calls mf_run_batch through
// ctypes. The dispatch order here MUST match _dg2_plan in backends.py exactly.
#include <algorithm>
#include "mf_shim.h"
#include "kernels_dg2.metal"

// the kernel is a template argument, so the compiler inlines it into the loop. The DG2 argument list
// is the 29-buffer MF_ARGS_DG2 (the previous 27 plus tseed and ton). Params is bound at 29.
template <void (*K)(const float*, const float*, float*, float*, float*, float*, float*, float*,
                    float*, float*, const uchar*, const uint*, const uint*, const uint*,
                    const float*, const float*, uint*, uint*, float*, float*, float*, float*,
                    float*, float*, float*, float*, float*, uint*, uint*,
                    const Params&, uint2)>
static void mf_dispatch(void** b, const Params& p, uint w, uint hgt) {
    MF_PAR_FOR
    for (uint y = 0; y < hgt; ++y) {
        for (uint x = 0; x < w; ++x) {
            uint2 g = {x, y};
            K((const float*)b[0], (const float*)b[1], (float*)b[2], (float*)b[3],
              (float*)b[4], (float*)b[5], (float*)b[6], (float*)b[7], (float*)b[8],
              (float*)b[9], (const uchar*)b[10], (const uint*)b[11], (const uint*)b[12],
              (const uint*)b[13], (const float*)b[14], (const float*)b[15], (uint*)b[16],
              (uint*)b[17], (float*)b[18], (float*)b[19], (float*)b[20], (float*)b[21],
              (float*)b[22], (float*)b[23], (float*)b[24], (float*)b[25], (float*)b[26],
              (uint*)b[27], (uint*)b[28], p, g);
        }
    }
}

extern "C" {

int mf_abi_version(void) { return 1; }

int mf_params_size(void) { return (int)sizeof(Params); }

// n SSP-RK2 steps on the window in p; the same dispatch order as the Metal backend's DG2 plan:
// predictor (stage 0) then corrector (stage 1), each a begin -> save -> limit -> faces -> cont
// sweep. k_save_dg2 copies U^n once at stage 0; k_limit_dg2 is a no-op when the limiter is off.
int mf_run_batch(void** b, const Params* pp, int nsteps) {
    const Params& p = *pp;
    for (int s = 0; s < nsteps; ++s) {
        mf_dispatch<k_stage_p_dg2>(b, p, 1, 1);
        mf_dispatch<k_begin_dg2>(b, p, 1, 1);
        mf_dispatch<k_save_dg2>(b, p, p.wc, p.wr);
        mf_dispatch<k_limit_dg2>(b, p, p.wc, p.wr);
        mf_dispatch<k_faces_dg2>(b, p, p.wc + 1, p.wr + 1);
        mf_dispatch<k_cont_dg2>(b, p, p.wc, p.wr);
        mf_dispatch<k_stage_c_dg2>(b, p, 1, 1);
        mf_dispatch<k_begin_dg2>(b, p, 1, 1);
        mf_dispatch<k_save_dg2>(b, p, p.wc, p.wr);
        mf_dispatch<k_limit_dg2>(b, p, p.wc, p.wr);
        mf_dispatch<k_faces_dg2>(b, p, p.wc + 1, p.wr + 1);
        mf_dispatch<k_cont_dg2>(b, p, p.wc, p.wr);
        mf_dispatch<k_end_dg2>(b, p, 1, 1);
    }
    return 0;
}

// The GPU's dry-tile skip (kernels_dg2.metal MF_TILE). ton is buffer 28. The host sets st[S_TILES].
long long mf_run_batch_tiles(void** b, const Params* pp, int nsteps, int chunk) {
    const Params& p = *pp;
    const uint ntx = (p.wc + MF_TILE - 1u) / MF_TILE, nty = (p.wr + MF_TILE - 1u) / MF_TILE;
    const uint* ton = (const uint*)b[28];
    long long run = 0;
    size_t cells = (size_t)p.wr * p.wc;
    if (chunk < 1) chunk = nsteps > 0 ? nsteps : 1;
    for (int s = 0; s < nsteps; ++s) {
        if (s % chunk == 0) {
            mf_dispatch<k_tclear_dg2>(b, p, ntx, nty);
            mf_dispatch<k_tseed_dg2>(b, p, p.wc, p.wr);
            mf_dispatch<k_tgrow_dg2>(b, p, ntx, nty);
            cells = 0;
            for (uint ty = 0; ty < nty; ++ty) {
                for (uint tx = 0; tx < ntx; ++tx) {
                    if (ton[ty * ntx + tx] != 0u) {
                        uint w = std::min(MF_TILE, p.wc - tx * MF_TILE);
                        uint hh = std::min(MF_TILE, p.wr - ty * MF_TILE);
                        cells += (size_t)w * hh;
                    }
                }
            }
        }
        mf_dispatch<k_stage_p_dg2>(b, p, 1, 1);
        mf_dispatch<k_begin_dg2>(b, p, 1, 1);
        mf_dispatch<k_save_dg2>(b, p, p.wc, p.wr);
        mf_dispatch<k_limit_dg2>(b, p, p.wc, p.wr);
        mf_dispatch<k_faces_dg2>(b, p, p.wc + 1, p.wr + 1);
        mf_dispatch<k_cont_dg2>(b, p, p.wc, p.wr);
        mf_dispatch<k_stage_c_dg2>(b, p, 1, 1);
        mf_dispatch<k_begin_dg2>(b, p, 1, 1);
        mf_dispatch<k_save_dg2>(b, p, p.wc, p.wr);
        mf_dispatch<k_limit_dg2>(b, p, p.wc, p.wr);
        mf_dispatch<k_faces_dg2>(b, p, p.wc + 1, p.wr + 1);
        mf_dispatch<k_cont_dg2>(b, p, p.wc, p.wr);
        mf_dispatch<k_end_dg2>(b, p, 1, 1);
        run += (long long)cells;
    }
    return run;
}
}
