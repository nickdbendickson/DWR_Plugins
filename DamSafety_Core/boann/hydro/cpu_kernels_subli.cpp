// CPU build of kernels_subli.metal (the sub-grid local-inertial engine): the same kernel source,
// called once per "thread" in the order the GPU would run the dispatches of one step.
// backends.CpuBackend compiles this with the system C++ compiler at first use and calls mf_run_batch
// through ctypes (mf_run_batch, or mf_run_batch_skip: the dry-cell skip of cpu_kernels.cpp).
// the standard headers come before the shim: it defines kernel / device / constant / thread
// as empty macros for the Metal source, which would break libc++'s own headers (macOS)
#include <algorithm>
#include <vector>

#include "mf_shim.h"
#include "kernels_subli.metal"

// the MF_ARGS_SUBLI signature: 30 buffers (0..29) then const Params& and uint2
template <void (*K)(const float*, const float*, float*, float*, float*, float*, float*, float*,
                    float*, float*, float*, float*, const uchar*, const uint*, const uint*,
                    const uint*, const float*, const float*, uint*, uint*, float*, float*, float*,
                    float*, const float*, const float*, const float*, const float*, uint*,
                    uint*, const Params&,
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
              (uint*)b[28], (uint*)b[29], p, g);
        }
    }
}

// the same, over one column range per row instead of the whole window: rows y in [0, nrows),
// columns [lo[y], hi[y]); every kernel call is the one the full dispatch would make there
template <void (*K)(const float*, const float*, float*, float*, float*, float*, float*, float*,
                    float*, float*, float*, float*, const uchar*, const uint*, const uint*,
                    const uint*, const float*, const float*, uint*, uint*, float*, float*, float*,
                    float*, const float*, const float*, const float*, const float*, uint*,
                    uint*, const Params&,
                    uint2)>
static void mf_dispatch_rows(void** b, const Params& p, uint nrows, const uint* lo, const uint* hi,
                             size_t work) {
#if defined(_OPENMP)
#pragma omp parallel for schedule(dynamic, 8) if (nrows > 1 && work >= 4096)
#endif
    for (uint y = 0; y < nrows; ++y) {
        for (uint x = lo[y]; x < hi[y]; ++x) {
            uint2 g = {x, y};
            K((const float*)b[0], (const float*)b[1], (float*)b[2], (float*)b[3], (float*)b[4],
              (float*)b[5], (float*)b[6], (float*)b[7], (float*)b[8], (float*)b[9], (float*)b[10],
              (float*)b[11], (const uchar*)b[12], (const uint*)b[13], (const uint*)b[14],
              (const uint*)b[15], (const float*)b[16], (const float*)b[17], (uint*)b[18],
              (uint*)b[19], (float*)b[20], (float*)b[21], (float*)b[22], (float*)b[23],
              (const float*)b[24], (const float*)b[25], (const float*)b[26], (const float*)b[27],
              (uint*)b[28], (uint*)b[29], p, g);
        }
    }
}

// Dry-cell skip (Runner 3.92.0), cpu_kernels.cpp's mf_rows on this engine's buffers. A face of
// this engine carries flow only when the higher of its two cells' surfaces stands above some
// sub-segment of its common edge; each sub-segment of that edge is at least the lowest sub-cell
// of either cell, so between two cells with no water (V = 0, d = 0, surface at the lowest
// sub-cell) every face keeps exactly 0, as in the ACC engine, and water moves at most one cell
// per step. The seeds are the cells holding water (V or d above 0), the cells beside a face
// whose discharge or recorded velocity is not zero, and the source cells; each row's range
// [lo, hi) is the hull of the seeds within k + 1 rows, widened by k + 1 columns.
static void mf_rows(void** b, const Params& p, int k, std::vector<uint>& lo,
                    std::vector<uint>& hi) {
    const uint wr = p.wr, nx = p.nx;
    const float* V = (const float*)b[2];
    const float* d = (const float*)b[3];
    const uint* s_cell = (const uint*)b[13];
    const float* qxo = (const float*)b[20];
    const float* qyo = (const float*)b[21];
    const float* vxf = (const float*)b[22];
    const float* vyf = (const float*)b[23];
    const uint NONE = 0xffffffffu;
    std::vector<uint> smin(wr, NONE), smax(wr, 0u);
    size_t work = 0;
    for (uint y = 0; y < wr; ++y) work += hi[y] - lo[y];
#if defined(_OPENMP)
#pragma omp parallel for schedule(dynamic, 16) if (wr > 1 && work >= 65536)
#endif
    for (uint y = 0; y < wr; ++y) {
        size_t r = p.r0 + y;
        const float* Vr = V + r * nx + p.c0;
        const float* dr = d + r * nx + p.c0;
        const float* qa = qxo + r * (nx + 1u) + p.c0;     // x faces c0 .. c0 + wc of row r
        const float* va = vxf + r * (nx + 1u) + p.c0;
        const float* qu = qyo + r * nx + p.c0;            // y faces above row r ...
        const float* vu = vyf + r * nx + p.c0;
        const float* qd = qu + nx;                         // ... and below it
        const float* vd = vu + nx;
        uint a = NONE, z = 0u;
        for (uint x = lo[y]; x < hi[y]; ++x) {
            if (Vr[x] > 0.0f || dr[x] > 0.0f || qa[x] != 0.0f || va[x] != 0.0f ||
                qa[x + 1u] != 0.0f || va[x + 1u] != 0.0f || qu[x] != 0.0f || vu[x] != 0.0f ||
                qd[x] != 0.0f || vd[x] != 0.0f) {
                if (a == NONE) a = x;
                z = x;
            }
        }
        smin[y] = a;
        smax[y] = z;
    }
    for (uint i = 0; i < p.n_src; ++i) {
        uint r = s_cell[i] / nx, c = s_cell[i] % nx;
        if (r < p.r0 || r >= p.r0 + wr || c < p.c0 || c >= p.c0 + p.wc) continue;
        uint y = r - p.r0, x = c - p.c0;
        if (smin[y] == NONE) {
            smin[y] = smax[y] = x;
        } else {
            smin[y] = std::min(smin[y], x);
            smax[y] = std::max(smax[y], x);
        }
    }
    const int m = k + 1;
    for (uint y = 0; y < wr; ++y) {
        uint a = NONE, z = 0u;
        int y0 = std::max(0, (int)y - m), y1 = std::min((int)wr - 1, (int)y + m);
        for (int j = y0; j <= y1; ++j) {
            if (smin[j] != NONE) {
                a = std::min(a, smin[j]);
                z = std::max(z, smax[j]);
            }
        }
        if (a == NONE) {
            lo[y] = hi[y] = 0u;
        } else {
            lo[y] = a > (uint)m ? a - (uint)m : 0u;
            hi[y] = std::min(p.wc, z + (uint)m + 1u);
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

// mf_run_batch leaving out the cells that stay dry and the faces between them (mf_rows), with
// the ranges redrawn every `chunk` steps: the same numbers, bit for bit (this engine takes no
// rain). Returns the cell updates it ran (cells x steps).
long long mf_run_batch_skip(void** b, const Params* pp, int nsteps, int chunk) {
    const Params& p = *pp;
    const uint wr = p.wr, wc = p.wc;
    if (chunk < 1 || wr == 0u || wc == 0u) {
        mf_run_batch(b, pp, nsteps);
        return (long long)wr * wc * nsteps;
    }
    std::vector<uint> lo(wr, 0u), hi(wr, wc), flo(wr + 1u), fhi(wr + 1u);
    long long run = 0;
    for (int s0 = 0; s0 < nsteps; s0 += chunk) {
        const int k = std::min(chunk, nsteps - s0);
        mf_rows(b, p, k, lo, hi);
        // thread (x, y) of the face kernels owns x-face x of row y and y-face row y of column x:
        // face row y serves cell rows y - 1 and y, and x-face hi is a row's last east face
        size_t cells = 0, faces = 0;
        for (uint y = 0; y <= wr; ++y) {
            uint a = 0xffffffffu, z = 0u;
            for (uint j = (y > 0u ? y - 1u : 0u); j <= std::min(y, wr - 1u); ++j) {
                if (hi[j] > lo[j]) {
                    a = std::min(a, lo[j]);
                    z = std::max(z, hi[j]);
                }
            }
            flo[y] = fhi[y] = 0u;
            if (a != 0xffffffffu) {
                flo[y] = a;
                fhi[y] = std::min(wc + 1u, z + 1u);
                faces += fhi[y] - flo[y];
            }
            if (y < wr) cells += hi[y] - lo[y];
        }
        for (int s = 0; s < k; ++s) {
            mf_dispatch<k_begin_subli>(b, p, 1, 1);
            mf_dispatch_rows<k_faces_subli>(b, p, wr + 1u, flo.data(), fhi.data(), faces);
            mf_dispatch_rows<k_scale_subli>(b, p, wr, lo.data(), hi.data(), cells);
            mf_dispatch_rows<k_limit_subli>(b, p, wr + 1u, flo.data(), fhi.data(), faces);
            mf_dispatch_rows<k_cont_subli>(b, p, wr, lo.data(), hi.data(), cells);
            mf_dispatch_rows<k_speed_subli>(b, p, wr, lo.data(), hi.data(), cells);
            mf_dispatch<k_end_subli>(b, p, 1, 1);
        }
        run += (long long)cells * k;
    }
    return run;
}

// mf_run_batch with the GPU's dry-tile skip (kernels_subli.metal MF_TILE): the tiles are redrawn
// before every `chunk` steps and the grid kernels leave out the unmarked ones, exactly as the
// Metal backend dispatches them. The CPU build's own skip is mf_run_batch_skip; this one exists
// so the cloud tests can check the GPU's skip on the same kernel source. The host sets
// st[S_TILES]. Returns the window cells the marked tiles held, summed over the steps.
long long mf_run_batch_tiles(void** b, const Params* pp, int nsteps, int chunk) {
    const Params& p = *pp;
    const uint ntx = (p.wc + MF_TILE - 1u) / MF_TILE, nty = (p.wr + MF_TILE - 1u) / MF_TILE;
    const uint* ton = (const uint*)b[29];
    long long run = 0;
    size_t cells = (size_t)p.wr * p.wc;
    if (chunk < 1) chunk = nsteps > 0 ? nsteps : 1;
    for (int s = 0; s < nsteps; ++s) {
        if (s % chunk == 0) {
            mf_dispatch<k_tclear_subli>(b, p, ntx, nty);
            mf_dispatch<k_tseed_subli>(b, p, p.wc, p.wr);
            mf_dispatch<k_tgrow_subli>(b, p, ntx, nty);
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
        mf_dispatch<k_begin_subli>(b, p, 1, 1);
        mf_dispatch<k_faces_subli>(b, p, p.wc + 1, p.wr + 1);
        mf_dispatch<k_scale_subli>(b, p, p.wc, p.wr);
        mf_dispatch<k_limit_subli>(b, p, p.wc + 1, p.wr + 1);
        mf_dispatch<k_cont_subli>(b, p, p.wc, p.wr);
        mf_dispatch<k_speed_subli>(b, p, p.wc, p.wr);
        mf_dispatch<k_end_subli>(b, p, 1, 1);
        run += (long long)cells;
    }
    return run;
}
}
