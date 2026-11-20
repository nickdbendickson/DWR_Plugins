// The few Metal Shading Language names kernels.metal uses, for its CPU build (cpu_kernels.cpp).
// One "GPU thread" is one call of the kernel function, and the dispatcher calls them one after
// another, so a SIMD group is a single thread: simd_sum / simd_max / simd_min return their
// argument, simd_is_first() is true, and the atomics are plain read-modify-writes.
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
#define B(i)
#define GID

using std::fabs;
using std::fmax;
using std::fmin;
using std::pow;
using std::sqrt;

typedef uint atomic_uint;
enum mf_memory_order { memory_order_relaxed };

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
