"""Boann on a GPU that is not Apple's, through OpenCL: Intel Arc, AMD and NVIDIA on Windows and
Linux. No package to install: the GPU driver ships the OpenCL runtime (OpenCL.dll on Windows,
libOpenCL.so.1 on Linux), called here with ctypes, as MetalBackend calls Metal.

The kernels are the .metal files themselves. translate() puts ocl_shim.h in front of one and
rewrites the four Metal (C++) forms a macro cannot reach, so Metal, the CPU build and OpenCL
all compile the same source. The OpenCL driver compiles the result at run time (OpenCL C 1.2,
IEEE division and square root, no fused multiply-add).

Each "SIMD group" is one work-item (see ocl_shim.h), so the answers follow the CPU build to
float round-off: the same tolerance the Metal backend is held to (test_boann_opencl.py).

Unlike Apple silicon, a discrete GPU has its own memory. The host's numpy arrays stay the
engine's view of the grids: run_batch writes every array to the GPU before the batch and reads
every one back after it, so an engine reads and writes them between batches exactly as it does
with the other backends.

DAMSAFETY_MF_CL_DEVICE=N picks the N-th OpenCL device (as `boann.py devices` lists them);
by default the first GPU, else the first device of any kind."""
import ctypes
import os
import re
import sys
import time

import numpy as np

from . import backends as be

HERE = os.path.dirname(os.path.abspath(__file__))
OCL_SHIM = os.path.join(HERE, "ocl_shim.h")
BUILD_OPTIONS = b"-cl-std=CL1.2 -cl-fp32-correctly-rounded-divide-sqrt"

# -- translation of a .metal file to OpenCL C ------------------------------------------------

_GID = "const uint2 gid = (uint2)((uint)get_global_id(0), (uint)get_global_id(1)); (void)gid;"


def translate(metal_text, shim_text=None):
    """OpenCL C for one Boann .metal kernel file: ocl_shim.h, then the source with
    `constant Params& p` passed by value, the thread position from get_global_id,
    as_type<T>(x) as as_T(x), C++ casts T(x) as (T)(x), the Params struct typedef'd, and every
    helper `static inline`."""
    if shim_text is None:
        with open(OCL_SHIM, encoding="utf-8") as f:
            shim_text = f.read()
    s = metal_text
    # the thread position: out of the kernels' argument-list macros, into the top of each body
    s, n = re.subn(r",\s*\\\s*\n\s*uint2 gid GID\s*\n", "\n", s)
    if n < 1:
        raise be.BackendError("OpenCL translation: no `uint2 gid GID` in the kernel arguments")
    s = re.sub(r"(kernel void \w+\(\w+\)\s*\{)", r"\1 " + _GID, s)
    s = s.replace("constant Params& p", "const Params p")
    s = re.sub(r"as_type<(\w+)>\(", r"as_\1(", s)
    s = re.sub(r"(?<![\w.])(uint|int|float)\(", r"(\1)(", s)
    s = re.sub(r"(?m)^inline ", "static inline ", s)
    # struct T {...}; -> typedef struct T {...} T; (C++ names a struct without the tag)
    out, pos = [], 0
    for m in re.finditer(r"(?m)^struct (\w+) \{", s):
        if m.start() < pos:
            continue
        depth, end = 1, m.end()
        while depth:
            depth += {"{": 1, "}": -1}.get(s[end], 0)
            end += 1
        # s[end - 1] is the closing brace; the struct's ';' follows it
        semi = s.index(";", end)
        out.append(s[pos:m.start()] + "typedef " + s[m.start():end] + " " + m.group(1) + ";")
        pos = semi + 1
    s = "".join(out) + s[pos:]
    return shim_text + "\n#line 1\n" + s


# -- the OpenCL runtime through ctypes --------------------------------------------------------

CL_SUCCESS = 0
CL_DEVICE_TYPE_GPU = 1 << 2
CL_DEVICE_TYPE_ALL = 0xFFFFFFFF
CL_PLATFORM_NAME = 0x0902
CL_DEVICE_NAME = 0x102B
CL_DEVICE_VERSION = 0x102F
CL_DEVICE_TYPE = 0x1000
CL_DEVICE_GLOBAL_MEM_SIZE = 0x101F
CL_DEVICE_MAX_COMPUTE_UNITS = 0x1002
CL_PROGRAM_BUILD_LOG = 0x1183
CL_MEM_READ_WRITE = 1 << 0
CL_TRUE = 1
CL_QUEUE_PROFILING_ENABLE = 1 << 1

_c = ctypes
_vp = _c.c_void_p
_u32 = _c.c_uint32
_sz = _c.c_size_t


def _load():
    names = (["OpenCL.dll"] if os.name == "nt" else
             ["/System/Library/Frameworks/OpenCL.framework/OpenCL"] if sys.platform == "darwin"
             else ["libOpenCL.so.1", "libOpenCL.so"])
    env = os.environ.get("DAMSAFETY_MF_OPENCL_LIB")
    if env:
        names.insert(0, env)
    err = None
    for n in names:
        try:
            lib = _c.CDLL(n)
            break
        except OSError as e:
            err = e
    else:
        raise be.BackendError("no OpenCL runtime here ({}); a GPU driver installs it".format(err))
    sig = {
        "clGetPlatformIDs": (_c.c_int, [_u32, _c.POINTER(_vp), _c.POINTER(_u32)]),
        "clGetPlatformInfo": (_c.c_int, [_vp, _u32, _sz, _vp, _c.POINTER(_sz)]),
        "clGetDeviceIDs": (_c.c_int, [_vp, _c.c_uint64, _u32, _c.POINTER(_vp), _c.POINTER(_u32)]),
        "clGetDeviceInfo": (_c.c_int, [_vp, _u32, _sz, _vp, _c.POINTER(_sz)]),
        "clCreateContext": (_vp, [_vp, _u32, _c.POINTER(_vp), _vp, _vp, _c.POINTER(_c.c_int)]),
        "clCreateCommandQueue": (_vp, [_vp, _vp, _c.c_uint64, _c.POINTER(_c.c_int)]),
        "clCreateProgramWithSource": (_vp, [_vp, _u32, _c.POINTER(_c.c_char_p),
                                            _c.POINTER(_sz), _c.POINTER(_c.c_int)]),
        "clBuildProgram": (_c.c_int, [_vp, _u32, _c.POINTER(_vp), _c.c_char_p, _vp, _vp]),
        "clGetProgramBuildInfo": (_c.c_int, [_vp, _vp, _u32, _sz, _vp, _c.POINTER(_sz)]),
        "clCreateKernel": (_vp, [_vp, _c.c_char_p, _c.POINTER(_c.c_int)]),
        "clSetKernelArg": (_c.c_int, [_vp, _u32, _sz, _vp]),
        "clCreateBuffer": (_vp, [_vp, _c.c_uint64, _sz, _vp, _c.POINTER(_c.c_int)]),
        "clEnqueueWriteBuffer": (_c.c_int, [_vp, _vp, _u32, _sz, _sz, _vp, _u32, _vp, _vp]),
        "clEnqueueReadBuffer": (_c.c_int, [_vp, _vp, _u32, _sz, _sz, _vp, _u32, _vp, _vp]),
        "clEnqueueNDRangeKernel": (_c.c_int, [_vp, _vp, _u32, _c.POINTER(_sz), _c.POINTER(_sz),
                                              _c.POINTER(_sz), _u32, _vp, _vp]),
        "clFinish": (_c.c_int, [_vp]),
        "clReleaseMemObject": (_c.c_int, [_vp]),
        "clReleaseKernel": (_c.c_int, [_vp]),
        "clReleaseProgram": (_c.c_int, [_vp]),
        "clReleaseCommandQueue": (_c.c_int, [_vp]),
        "clReleaseContext": (_c.c_int, [_vp]),
    }
    for name, (res, args) in sig.items():
        fn = getattr(lib, name)
        fn.restype = res
        fn.argtypes = args
    return lib


def _check(code, what):
    if code != CL_SUCCESS:
        raise be.BackendError("OpenCL {} failed (error {})".format(what, code))


def _info(cl, fn, obj, key):
    n = _sz(0)
    fn(obj, key, 0, None, _c.byref(n))
    buf = _c.create_string_buffer(max(n.value, 1))
    fn(obj, key, n.value, buf, None)
    return buf


def devices(cl=None):
    """Every OpenCL device here: [(platform name, device handle, device name, version,
    is_gpu, memory bytes, compute units)], in the order DAMSAFETY_MF_CL_DEVICE counts them."""
    cl = cl or _load()
    n = _u32(0)
    if cl.clGetPlatformIDs(0, None, _c.byref(n)) != CL_SUCCESS or n.value == 0:
        return []
    plats = (_vp * n.value)()
    cl.clGetPlatformIDs(n.value, plats, None)
    out = []
    for p in plats:
        pname = _info(cl, cl.clGetPlatformInfo, p, CL_PLATFORM_NAME).value.decode(errors="replace")
        nd = _u32(0)
        if cl.clGetDeviceIDs(p, CL_DEVICE_TYPE_ALL, 0, None, _c.byref(nd)) != CL_SUCCESS:
            continue
        devs = (_vp * nd.value)()
        cl.clGetDeviceIDs(p, CL_DEVICE_TYPE_ALL, nd.value, devs, None)
        for d in devs:
            name = _info(cl, cl.clGetDeviceInfo, d, CL_DEVICE_NAME).value.decode(errors="replace")
            ver = _info(cl, cl.clGetDeviceInfo, d, CL_DEVICE_VERSION).value.decode(errors="replace")
            typ = _c.c_uint64.from_buffer_copy(
                _info(cl, cl.clGetDeviceInfo, d, CL_DEVICE_TYPE).raw[:8]).value
            mem = _c.c_uint64.from_buffer_copy(
                _info(cl, cl.clGetDeviceInfo, d, CL_DEVICE_GLOBAL_MEM_SIZE).raw[:8]).value
            cu = _c.c_uint32.from_buffer_copy(
                _info(cl, cl.clGetDeviceInfo, d, CL_DEVICE_MAX_COMPUTE_UNITS).raw[:4]).value
            out.append((pname.strip(), d, name.strip(), ver.strip(),
                        bool(typ & CL_DEVICE_TYPE_GPU), int(mem), int(cu)))
    return out


def opencl_available():
    """(True, device name) when an OpenCL device is here, else (False, why)."""
    try:
        ds = devices()
    except be.BackendError as e:
        return False, str(e)
    if not ds:
        return False, "the OpenCL runtime lists no device"
    return True, _pick(ds)[2]


def _pick(ds):
    v = os.environ.get("DAMSAFETY_MF_CL_DEVICE", "").strip()
    if v:
        try:
            return ds[int(v)]
        except (ValueError, IndexError):
            raise be.BackendError("DAMSAFETY_MF_CL_DEVICE={} but {} OpenCL device(s) here".format(
                v, len(ds)))
    gpus = [d for d in ds if d[4]]
    return (gpus or ds)[0]


# -- the backend ------------------------------------------------------------------------------

class OpenCLBackend:
    """The same interface as backends.CpuBackend / MetalBackend: allocate(layout) -> numpy arrays,
    run_batch(params, nsteps, chunk), profile(), close()."""
    kind = "opencl"

    def __init__(self, spec=be.FLOOD_SPEC, log=None):
        self.spec = spec
        cl = self.cl = _load()
        ds = devices(cl)
        if not ds:
            raise be.BackendError("the OpenCL runtime lists no device")
        pname, dev, name, ver, _gpu, mem, cu = _pick(ds)
        self.dev = _vp(dev)
        self.device = "{} (OpenCL, {}, {} compute units, {:.1f} GB)".format(
            name, pname, cu, mem / 2.0 ** 30)
        err = _c.c_int(0)
        devs = (_vp * 1)(dev)
        self.ctx = cl.clCreateContext(None, 1, devs, None, None, _c.byref(err))
        _check(err.value, "clCreateContext")
        self.queue = cl.clCreateCommandQueue(self.ctx, dev, 0, _c.byref(err))
        _check(err.value, "clCreateCommandQueue")
        with open(spec.kernel_source, encoding="utf-8") as f:
            src = translate(f.read()).encode("utf-8")
        srcs = (_c.c_char_p * 1)(src)
        lens = (_sz * 1)(len(src))
        self.program = cl.clCreateProgramWithSource(self.ctx, 1, srcs, lens, _c.byref(err))
        _check(err.value, "clCreateProgramWithSource")
        # DAMSAFETY_MF_CL_OPTIONS adds build options (CI: -cl-opt-disable for PoCL, whose LLVM
        # aborts optimising the DG2 kernels; it changes no arithmetic: no fast-math either way)
        opts = BUILD_OPTIONS
        extra = os.environ.get("DAMSAFETY_MF_CL_OPTIONS", "").strip()
        if extra:
            opts = opts + b" " + extra.encode()
        code = cl.clBuildProgram(self.program, 1, devs, opts, None, None)
        if code != CL_SUCCESS:
            n = _sz(0)
            cl.clGetProgramBuildInfo(self.program, dev, CL_PROGRAM_BUILD_LOG, 0, None, _c.byref(n))
            buf = _c.create_string_buffer(max(n.value, 1))
            cl.clGetProgramBuildInfo(self.program, dev, CL_PROGRAM_BUILD_LOG, n.value, buf, None)
            raise be.BackendError("OpenCL did not compile the kernels (error {}):\n{}".format(
                code, buf.value.decode(errors="replace")[-4000:]))
        self.kernels = {}
        for k in spec.kernels:
            kh = cl.clCreateKernel(self.program, k.encode(), _c.byref(err))
            _check(err.value, "clCreateKernel " + k)
            self.kernels[k] = kh
        self.buffers = []
        self.arrays = {}
        self.cells_run = 0
        self.threads = 1
        self.prof = be.Profile(self)
        if log:
            log("OpenCL GPU: {}".format(self.device))

    def profile(self):
        return self.prof.report()

    def allocate(self, layout):
        cl = self.cl
        for b in self.buffers:
            cl.clReleaseMemObject(b)
        self.buffers = []
        self.arrays = {}
        err = _c.c_int(0)
        for name, dtype, count in layout:
            a = np.zeros(max(int(count), 1), dtype=dtype)
            buf = cl.clCreateBuffer(self.ctx, CL_MEM_READ_WRITE, a.nbytes, None, _c.byref(err))
            if err.value != CL_SUCCESS or not buf:
                raise be.BackendError("the GPU could not allocate {} ({:,} bytes, error {})".format(
                    name, a.nbytes, err.value))
            self.buffers.append(_vp(buf))
            self.arrays[name] = a
        self._names = [n for n, _d, _c2 in layout]
        for kh in self.kernels.values():
            for i, b in enumerate(self.buffers):
                _check(cl.clSetKernelArg(kh, i, _c.sizeof(_vp), _c.byref(b)), "clSetKernelArg")
        return self.arrays

    def _copy(self, to_gpu):
        cl = self.cl
        fn = cl.clEnqueueWriteBuffer if to_gpu else cl.clEnqueueReadBuffer
        for name, b in zip(self._names, self.buffers):
            a = self.arrays[name]
            _check(fn(self.queue, b, CL_TRUE if not to_gpu else 0, 0, a.nbytes,
                      a.ctypes.data, 0, None, None), "copy " + name)

    def run_batch(self, params, nsteps, chunk=0):
        cl = self.cl
        prof = self.prof
        nsteps = int(nsteps)
        tiles = be._tiles_on(self.spec, params, chunk)
        st = self.arrays.get("st")
        if st is not None and self.spec.tile_plan is not None:
            st[be.S_TILES] = 1 if tiles else 0
        t0 = time.perf_counter()
        self._copy(True)
        nb = len(self.buffers)
        for kh in self.kernels.values():
            _check(cl.clSetKernelArg(kh, nb, _c.sizeof(params), _c.byref(params)),
                   "clSetKernelArg Params")
        plan = self.spec.plan(params)
        tplan = self.spec.tile_plan(params) if tiles else []
        gsz = (_sz * 2)()
        for s in range(nsteps):
            steps = (tplan if tiles and s % int(chunk) == 0 else []) + plan
            for name, (w, h, _d) in steps:
                if int(w) <= 0 or int(h) <= 0:
                    continue
                gsz[0], gsz[1] = int(w), int(h)
                _check(cl.clEnqueueNDRangeKernel(self.queue, self.kernels[name], 2, None, gsz,
                                                 None, 0, None, None), "run " + name)
        t1 = time.perf_counter()
        self._copy(False)
        _check(cl.clFinish(self.queue), "clFinish")
        t2 = time.perf_counter()
        prof.encode_s += t1 - t0
        prof.encode_steps += nsteps
        prof.wait_s += t2 - t1
        self.cells_run += self._cells_marked(params, tiles) * nsteps
        prof.add_batch(params, nsteps)
        prof.look(params, nsteps)

    def _cells_marked(self, params, tiles):
        wr, wc = int(getattr(params, "wr", 0)), int(getattr(params, "wc", 0))
        ton = self.arrays.get("ton")
        if not tiles or ton is None:
            return wr * wc
        ntx, nty = -(-wc // be.TILE), -(-wr // be.TILE)
        on = ton[:ntx * nty].reshape(nty, ntx) != 0
        w = np.minimum(be.TILE, wc - be.TILE * np.arange(ntx))
        h = np.minimum(be.TILE, wr - be.TILE * np.arange(nty))
        return int((on * np.outer(h, w)).sum())

    def close(self):
        cl = self.cl
        for b in self.buffers:
            cl.clReleaseMemObject(b)
        self.buffers = []
        for kh in self.kernels.values():
            cl.clReleaseKernel(kh)
        self.kernels = {}
        for attr, fn in (("program", cl.clReleaseProgram), ("queue", cl.clReleaseCommandQueue),
                         ("ctx", cl.clReleaseContext)):
            h = getattr(self, attr, None)
            if h:
                fn(h)
            setattr(self, attr, None)
