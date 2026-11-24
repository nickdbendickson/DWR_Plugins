// The few Metal Shading Language names kernels.metal uses, for its CPU build (cpu_kernels.cpp).
// One "GPU thread" is one call of the kernel function, so a SIMD group is a single thread:
// simd_sum / simd_max / simd_min return their argument and simd_is_first() is true.
// Built with OpenMP (-fopenmp, backends.build_cpu_library), MF_PAR_FOR spreads the rows of one
// dispatch over the cores and the atomics are real relaxed atomics. Every reduction the kernels
// make is an integer add / max / min (fixed-point or float bits, see kernels.metal S_*), which
// gives the same bits in any order, so a multi-core run equals the one-core run bit for bit
// (test_metalflood.py metalflood_cpu_openmp_matches_one_core). Without OpenMP the dispatcher is
// the plain serial loop and the atomics are plain read-modify-writes, as before.
#ifndef MF_SHIM_H
#define MF_SHIM_H
#define MF_CPU 1
#include <cmath>
#include <cstdint>
#include <cstring>

typedef uint32_t uint;
typedef unsigned char uchar;
struct uint2 {
    uint x, y;
};

#define kernel
#define device
#define constant const
#define thread
#define B(i)
#define GID

using std::fabs;
using std::fmax;
using std::fmin;
using std::pow;
using std::sqrt;

typedef uint atomic_uint;
enum mf_memory_order { memory_order_relaxed };

#if defined(_OPENMP)
// the two OpenMP runtime calls used, declared here instead of #include <omp.h>: a newer omp.h
// (Homebrew's libomp) uses OpenMP 5.1 pragmas that Apple clang cannot parse, so with
// -Xpreprocessor -fopenmp the header itself failed to compile (Runner 3.61.0)
extern "C" {
void omp_set_num_threads(int);
int omp_get_max_threads(void);
}
#define MF_PRAGMA(x) _Pragma(#x)
// rows of one dispatch across the cores; small dispatches (k_begin / k_end, tiny windows) stay serial
#define MF_PAR_FOR_IF(cond) \
    MF_PRAGMA(omp parallel for schedule(static) if ((cond) && hgt > 1 && (size_t)w * hgt >= 4096))
#define MF_PAR_FOR MF_PAR_FOR_IF(true)
inline uint atomic_fetch_max_explicit(atomic_uint* a, uint v, mf_memory_order) {
    uint o = __atomic_load_n(a, __ATOMIC_RELAXED);
    while (v > o && !__atomic_compare_exchange_n(a, &o, v, true, __ATOMIC_RELAXED, __ATOMIC_RELAXED)) {
    }
    return o;
}
inline uint atomic_fetch_min_explicit(atomic_uint* a, uint v, mf_memory_order) {
    uint o = __atomic_load_n(a, __ATOMIC_RELAXED);
    while (v < o && !__atomic_compare_exchange_n(a, &o, v, true, __ATOMIC_RELAXED, __ATOMIC_RELAXED)) {
    }
    return o;
}
inline uint atomic_fetch_add_explicit(atomic_uint* a, uint v, mf_memory_order) {
    return __atomic_fetch_add(a, v, __ATOMIC_RELAXED);
}
// how many cores a dispatch uses; n > 0 sets it (DAMSAFETY_MF_THREADS), 0 only asks
extern "C" int mf_threads(int n) {
    if (n > 0) omp_set_num_threads(n);
    return omp_get_max_threads();
}
#else
#define MF_PAR_FOR_IF(cond)
#define MF_PAR_FOR
inline uint atomic_fetch_max_explicit(atomic_uint* a, uint v, mf_memory_order) {
    uint o = *a;
    if (v > o) *a = v;
    return o;
}
inline uint atomic_fetch_min_explicit(atomic_uint* a, uint v, mf_memory_order) {
    uint o = *a;
    if (v < o) *a = v;
    return o;
}
inline uint atomic_fetch_add_explicit(atomic_uint* a, uint v, mf_memory_order) {
    uint o = *a;
    *a = o + v;
    return o;
}
extern "C" int mf_threads(int) { return 1; }
#endif
template <typename T> inline T simd_sum(T v) { return v; }
template <typename T> inline T simd_max(T v) { return v; }
template <typename T> inline T simd_min(T v) { return v; }
inline bool simd_is_first() { return true; }
template <typename T, typename U> inline T as_type(U u) {
    static_assert(sizeof(T) == sizeof(U), "as_type: sizes differ");
    T t;
    std::memcpy(&t, &u, sizeof t);
    return t;
}
#endif
