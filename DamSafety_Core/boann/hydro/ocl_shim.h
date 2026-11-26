// The few Metal Shading Language names a Boann kernel file uses, for its OpenCL build
// (opencl_backend.py: a GPU that is not Apple's - Intel Arc, AMD, NVIDIA - on Windows or Linux).
// opencl_backend.translate() puts this file in front of the .metal source and rewrites the four
// things a macro cannot: `constant Params& p` (a struct by value), the kernel's thread position
// (get_global_id), `as_type<T>(x)` (as_T(x)) and C++ casts such as `uint(x)`.
//
// The kernels run as OpenCL C 1.2. Dispatches have no fixed work-group size, so a "SIMD group" is
// one work-item, as in the CPU build (mf_shim.h), unless the device has subgroups (MF_CL_SUBGROUPS
// below): simd_sum / simd_max / simd_min return their
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

// a macro, not a typedef: OpenCL C 2.0+ has a type of that name (the subgroup build below)
#define atomic_uint uint
#define memory_order_relaxed 0
#define atomic_fetch_add_explicit(a, v, o) atomic_add((volatile __global uint*)(a), (v))
#define atomic_fetch_max_explicit(a, v, o) atomic_max((volatile __global uint*)(a), (v))
#define atomic_fetch_min_explicit(a, v, o) atomic_min((volatile __global uint*)(a), (v))

#if defined(MF_CL_SUBGROUPS)
// A device with subgroups (Intel Arc and most GPUs; opencl_backend picks it, OpenCL C 2.0+ and no
// fixed work-group size, so no work-item lies outside the grid): a "SIMD group" is a subgroup, as
// on Metal, and only its first work-item does the atomic. Far fewer atomics on one address; the
// totals are rounded per subgroup as Metal rounds them per SIMD group.
#if defined(cl_khr_subgroups)
#pragma OPENCL EXTENSION cl_khr_subgroups : enable
#endif
#define simd_sum(v) sub_group_reduce_add(v)
#define simd_max(v) sub_group_reduce_max(v)
#define simd_min(v) sub_group_reduce_min(v)
#define simd_is_first() (get_sub_group_local_id() == 0u)
#else
#define simd_sum(v) (v)
#define simd_max(v) (v)
#define simd_min(v) (v)
#define simd_is_first() (true)
#endif
