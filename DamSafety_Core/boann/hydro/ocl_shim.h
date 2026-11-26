// The few Metal Shading Language names a Boann kernel file uses, for its OpenCL build
// (opencl_backend.py: a GPU that is not Apple's - Intel Arc, AMD, NVIDIA - on Windows or Linux).
// opencl_backend.translate() puts this file in front of the .metal source and rewrites the four
// things a macro cannot: `constant Params& p` (a struct by value), the kernel's thread position
// (get_global_id), `as_type<T>(x)` (as_T(x)) and C++ casts such as `uint(x)`.
//
// The kernels run as OpenCL C 1.2. Dispatches have no fixed work-group size, so a "SIMD group" is
// one work-item, as in the CPU build (mf_shim.h): simd_sum / simd_max / simd_min return their
// argument and every work-item adds its own value with a 32-bit integer atomic. The answers are
// the CPU build's, to float round-off; the totals are added as integers, so their order does
// not matter. No fast-math and no fused multiply-add (the CPU build's -ffp-contract=off).

// MF_CPU only keeps the .metal file from including <metal_stdlib>; nothing else reads it
#define MF_CPU 1
#define MF_OCL 1
#pragma OPENCL FP_CONTRACT OFF

#define kernel __kernel
#define device __global
#define thread
#define B(i)
#define GID

typedef uint atomic_uint;
#define memory_order_relaxed 0
#define atomic_fetch_add_explicit(a, v, o) atomic_add((volatile __global uint*)(a), (v))
#define atomic_fetch_max_explicit(a, v, o) atomic_max((volatile __global uint*)(a), (v))
#define atomic_fetch_min_explicit(a, v, o) atomic_min((volatile __global uint*)(a), (v))

#define simd_sum(v) (v)
#define simd_max(v) (v)
#define simd_min(v) (v)
#define simd_is_first() (true)
