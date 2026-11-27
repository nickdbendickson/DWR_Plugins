// CPU build of kernels_fv.metal: the same shock-capturing kernel source, called once per "thread"
// in the order the GPU would run the dispatches of one step. backends.CpuBackend compiles this
// file with the system C++ compiler at first use and calls mf_run_batch through ctypes.
// the standard headers come before the shim: it defines kernel / device as empty macros
#include <algorithm>
#include "mf_shim.h"
#include "kernels_fv.metal"

// the kernel is a template argument, so the compiler inlines it into the loop.
// tseed / ton (uint) are the dry-tile skip, buffers 24 and 25; Params is bound at 26.
template <void (*K)(const float*, const float*, float*, float*, float*, float*, float*, float*,
                    float*, float*, const uchar*, const uint*, const uint*, const uint*,
                    const float*, const float*, uint*, uint*, float*, float*, float*, float*,
                    float*, float*, uint*, uint*, const Params&, uint2)>
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
              (float*)b[22], (float*)b[23], (uint*)b[24], (uint*)b[25], p, g);
        }
    }
}

extern "C" {

int mf_abi_version(void) { return 1; }

int mf_params_size(void) { return (int)sizeof(Params); }

// n steps on the window in p; the same dispatch order as the Metal backend's FV plan
int mf_run_batch(void** b, const Params* pp, int nsteps) {
    const Params& p = *pp;
    for (int s = 0; s < nsteps; ++s) {
        mf_dispatch<k_begin_fv>(b, p, 1, 1);
        mf_dispatch<k_faces_fv>(b, p, p.wc + 1, p.wr + 1);
        mf_dispatch<k_cont_fv>(b, p, p.wc, p.wr);
        mf_dispatch<k_end_fv>(b, p, 1, 1);
    }
    return 0;
}

// mf_run_batch with the GPU's dry-tile skip (kernels_fv.metal MF_TILE). The host sets st[S_TILES].
// ton is buffer 25. Returns the window cells the marked tiles held, summed over the steps.
long long mf_run_batch_tiles(void** b, const Params* pp, int nsteps, int chunk) {
    const Params& p = *pp;
    const uint ntx = (p.wc + MF_TILE - 1u) / MF_TILE, nty = (p.wr + MF_TILE - 1u) / MF_TILE;
    const uint* ton = (const uint*)b[25];
    long long run = 0;
    size_t cells = (size_t)p.wr * p.wc;
    if (chunk < 1) chunk = nsteps > 0 ? nsteps : 1;
    for (int s = 0; s < nsteps; ++s) {
        if (s % chunk == 0) {
            mf_dispatch<k_tclear_fv>(b, p, ntx, nty);
            mf_dispatch<k_tseed_fv>(b, p, p.wc, p.wr);
            mf_dispatch<k_tgrow_fv>(b, p, ntx, nty);
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
        mf_dispatch<k_begin_fv>(b, p, 1, 1);
        mf_dispatch<k_faces_fv>(b, p, p.wc + 1, p.wr + 1);
        mf_dispatch<k_cont_fv>(b, p, p.wc, p.wr);
        mf_dispatch<k_end_fv>(b, p, 1, 1);
        run += (long long)cells;
    }
    return run;
}
}
