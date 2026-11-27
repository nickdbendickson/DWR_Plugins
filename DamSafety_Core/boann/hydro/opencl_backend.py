"""Boann on a GPU that is not Apple's, through OpenCL: Intel Arc, AMD and NVIDIA on Windows and
Linux. No package to install: the GPU driver ships the OpenCL runtime (OpenCL.dll on Windows,
libOpenCL.so.1 on Linux), called here with ctypes, as MetalBackend calls Metal.

The kernels are the .metal files themselves. translate() puts ocl_shim.h in front of one and
rewrites the four Metal (C++) forms a macro cannot reach, so Metal, the CPU build and OpenCL
all compile the same source. The OpenCL driver compiles the result at run time (OpenCL C 1.2,
IEEE division and square root, no fused multiply-add).

Each "SIMD group" is one work-item (see ocl_shim.h), so the answers follow the CPU build to
float round-off: the same tolerance the Metal backend is held to (test_boann_opencl.py).

The host's numpy arrays stay the engine's view of the grids, so an engine reads and writes them
between batches exactly as it does with the other backends. How they reach the GPU:

- zero-copy (a GPU that shares the computer's memory: Intel Arc/Iris integrated graphics, AMD
  APUs, a CPU OpenCL such as PoCL; CL_DEVICE_HOST_UNIFIED_MEMORY). Each grid is one block of
  page-aligned host memory the buffer is made over (CL_MEM_USE_HOST_PTR), mapped while the host
  holds it and unmapped for the batch, as Metal's shared storage on Apple silicon: one copy of
  each grid, nothing copied.
- copies on demand (a discrete GPU with its own memory). After a batch only the state words
  (st) and the tile flags come back. Every other grid a kernel writes is read back the first
  time the engine looks it up (backend.arrays[name] / .get(name)) and not before; before the
  next batch only the grids the engine looked up since are written to the GPU (all of them
  the first time). An engine reads its grids through that lookup each time (the mass row, a
  snapshot, the gauge, the breach notch, the results), so it sees the numbers the GPU holds.

DAMSAFETY_MF_CL_ZEROCOPY=0 / 1 turns zero-copy off / insists on it (default: when the device
shares host memory). DAMSAFETY_MF_CL_SYNC=full reads every grid back after every batch (the
copies-on-demand path, as before; for checking). Neither changes a result.

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
CL_DEVICE_EXTENSIONS = 0x1030
CL_DEVICE_OPENCL_C_VERSION = 0x103D
CL_DEVICE_TYPE = 0x1000
CL_DEVICE_GLOBAL_MEM_SIZE = 0x101F
CL_DEVICE_MAX_COMPUTE_UNITS = 0x1002
CL_PROGRAM_BUILD_LOG = 0x1183
CL_DEVICE_HOST_UNIFIED_MEMORY = 0x1035
CL_MEM_READ_WRITE = 1 << 0
CL_MEM_USE_HOST_PTR = 1 << 3
CL_MAP_READ = 1 << 0
CL_MAP_WRITE = 1 << 1
CL_TRUE = 1
CL_FALSE = 0
# zero-copy wants the host block page-aligned and its size a whole number of cache lines
# (Intel's rule; it does no harm elsewhere)
ZC_ALIGN = 4096
ZC_SIZE_ALIGN = 64
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
        "clEnqueueMapBuffer": (_vp, [_vp, _vp, _u32, _c.c_uint64, _sz, _sz, _u32, _vp, _vp,
                                     _c.POINTER(_c.c_int)]),
        "clEnqueueUnmapMemObject": (_c.c_int, [_vp, _vp, _vp, _u32, _vp, _vp]),
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


def _subgroups_wanted(cl, dev):
    """The -cl-std the subgroup build uses ("CL2.0" / "CL3.0"), or "" for the one-work-item
    build: the device's OpenCL C is 2.0 or newer and it has cl_khr_subgroups or
    cl_intel_subgroups. DAMSAFETY_MF_CL_SUBGROUPS=0 / 1 turns it off / insists on it."""
    want = os.environ.get("DAMSAFETY_MF_CL_SUBGROUPS", "auto").strip().lower()
    if want in ("0", "no", "off", "false"):
        return ""
    ext = _info(cl, cl.clGetDeviceInfo, dev, CL_DEVICE_EXTENSIONS).value.decode(errors="replace")
    cver = _info(cl, cl.clGetDeviceInfo, dev, CL_DEVICE_OPENCL_C_VERSION).value.decode(
        errors="replace")
    m = re.search(r"OpenCL C (\d+)\.(\d+)", cver)
    major = int(m.group(1)) if m else 1
    has = "cl_khr_subgroups" in ext.split() or "cl_intel_subgroups" in ext.split()
    if major >= 2 and has:
        return "CL3.0" if major >= 3 else "CL2.0"
    if want in ("1", "yes", "on", "true"):
        raise be.BackendError("DAMSAFETY_MF_CL_SUBGROUPS=1 but this device has no subgroups "
                              "({}; {})".format(cver.strip(), "no subgroup extension"))
    return ""


def _zero_copy_wanted(cl, dev):
    """True when the grids should be zero-copy buffers over host memory: the device shares the
    host's memory (CL_DEVICE_HOST_UNIFIED_MEMORY: integrated graphics, a CPU OpenCL), or
    DAMSAFETY_MF_CL_ZEROCOPY=1. DAMSAFETY_MF_CL_ZEROCOPY=0 turns it off."""
    want = os.environ.get("DAMSAFETY_MF_CL_ZEROCOPY", "auto").strip().lower()
    if want in ("0", "no", "off", "false"):
        return False
    if want in ("1", "yes", "on", "true"):
        return True
    raw = _info(cl, cl.clGetDeviceInfo, dev, CL_DEVICE_HOST_UNIFIED_MEMORY).raw
    return len(raw) >= 4 and _c.c_uint32.from_buffer_copy(raw[:4]).value != 0


def _sync_full():
    return os.environ.get("DAMSAFETY_MF_CL_SYNC", "").strip().lower() == "full"


class HostArrays(dict):
    """The grids by name, as the engine sees them (OpenCLBackend.arrays). With copies on demand,
    looking a grid up (a[name], a.get(name)) first reads it back from the GPU when the GPU has
    written it since, and marks it to be written to the GPU before the next batch."""

    def __init__(self, backend, arrays):
        super().__init__(arrays)
        self._bk = backend

    def __getitem__(self, name):
        self._bk._host_wants(name)
        return dict.__getitem__(self, name)

    def get(self, name, default=None):
        if name not in self:
            return default
        return self[name]


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
            metal = f.read()
        # the buffers no kernel writes (device const in the argument list): never read back
        self.read_only = {int(i) for i in
                          re.findall(r"device const \w+\* *\w+ B\((\d+)\)", metal)}
        src = translate(metal).encode("utf-8")
        srcs = (_c.c_char_p * 1)(src)
        lens = (_sz * 1)(len(src))
        self.program = cl.clCreateProgramWithSource(self.ctx, 1, srcs, lens, _c.byref(err))
        _check(err.value, "clCreateProgramWithSource")
        # DAMSAFETY_MF_CL_OPTIONS adds build options (CI: -cl-opt-disable for PoCL, whose LLVM
        # aborts optimising the DG2 kernels; it changes no arithmetic: no fast-math either way)
        opts = BUILD_OPTIONS
        self.subgroups = _subgroups_wanted(cl, dev)
        if self.subgroups:
            opts = opts.replace(b"-cl-std=CL1.2", b"-cl-std=" + self.subgroups.encode()) \
                + b" -D MF_CL_SUBGROUPS=1"
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
        self.copied_bytes = 0
        self.threads = 1
        self.zero_copy = _zero_copy_wanted(cl, dev)
        self._zc_insist = os.environ.get("DAMSAFETY_MF_CL_ZEROCOPY", "").strip().lower() in (
            "1", "yes", "on", "true")
        self._host = {}             # name -> the numpy array (the backend's own view)
        self._blocks = []           # zero-copy: (host block, its address, its size) per grid
        self._log = log
        self._mapped = False        # zero-copy: the host holds the buffers (mapped)
        self._stale = set()         # copies: grids the GPU wrote that the host has not read back
        self._touched = set()       # copies: grids the host looked up since the last upload
        self.prof = be.Profile(self)
        if self.subgroups:
            self.device += ", subgroups"
        if log:
            log("OpenCL GPU: {}".format(self.device))

    def profile(self):
        return self.prof.report()

    def _release_buffers(self):
        cl = self.cl
        if self._mapped:
            self._unmap_all()
            _check(cl.clFinish(self.queue), "clFinish")
        for b in self.buffers:
            cl.clReleaseMemObject(b)
        self.buffers = []
        self._blocks = []

    def allocate(self, layout):
        self._release_buffers()
        self._names = [n for n, _d, _c2 in layout]
        self._layout = list(layout)
        if self.zero_copy:
            try:
                self._allocate_zero_copy(layout)
            except be.BackendError as e:
                if self._zc_insist:
                    raise
                self._release_buffers()
                self.zero_copy = False
                self._log_mode("zero-copy not used ({}); copies on demand instead".format(e))
                self._allocate_copies(layout)
        else:
            self._allocate_copies(layout)
        cl = self.cl
        for kh in self.kernels.values():
            for i, b in enumerate(self.buffers):
                _check(cl.clSetKernelArg(kh, i, _c.sizeof(_vp), _c.byref(b)), "clSetKernelArg")
        return self.arrays

    def _log_mode(self, msg):
        if self._log:
            self._log("OpenCL: " + msg)

    def _allocate_copies(self, layout):
        """A device buffer per grid and a host numpy array beside it (copies on demand)."""
        cl = self.cl
        err = _c.c_int(0)
        self._host = {}
        for name, dtype, count in layout:
            a = np.zeros(max(int(count), 1), dtype=dtype)
            buf = cl.clCreateBuffer(self.ctx, CL_MEM_READ_WRITE, a.nbytes, None, _c.byref(err))
            if err.value != CL_SUCCESS or not buf:
                raise be.BackendError("the GPU could not allocate {} ({:,} bytes, error {})".format(
                    name, a.nbytes, err.value))
            self.buffers.append(_vp(buf))
            self._host[name] = a
        idx = {n: k for k, n in enumerate(self._names)}
        self._writable = {n for n in self._names if idx[n] not in self.read_only}
        # read back after every batch: the state words the engine reads each batch and the tile
        # flags _cells_marked counts (both small)
        self._always = {n for n in ("st", "ton") if n in self._host}
        self._stale = set()
        self._touched = set(self._names)        # the first batch writes every grid
        self.arrays = HostArrays(self, self._host)

    def _allocate_zero_copy(self, layout):
        """Each grid one page-aligned host block, the buffer made over it (CL_MEM_USE_HOST_PTR);
        mapped now, so the host holds it until the first batch."""
        cl = self.cl
        err = _c.c_int(0)
        self._host = {}
        self._blocks = []
        for name, dtype, count in layout:
            n = max(int(count), 1)
            nbytes = n * np.dtype(dtype).itemsize
            size = -(-nbytes // ZC_SIZE_ALIGN) * ZC_SIZE_ALIGN
            raw = np.zeros(size + ZC_ALIGN, dtype=np.uint8)
            off = (-raw.ctypes.data) % ZC_ALIGN
            block = raw[off:off + size]
            ptr = block.ctypes.data
            buf = cl.clCreateBuffer(self.ctx, CL_MEM_READ_WRITE | CL_MEM_USE_HOST_PTR, size,
                                    _vp(ptr), _c.byref(err))
            if err.value != CL_SUCCESS or not buf:
                raise be.BackendError("the GPU could not make {} over host memory ({:,} bytes, "
                                      "error {})".format(name, size, err.value))
            self.buffers.append(_vp(buf))
            self._blocks.append((raw, ptr, size))
            self._host[name] = block[:nbytes].view(dtype)
        self._map_all(check=True)
        self.arrays = self._host
        if ", zero-copy" not in self.device:
            self.device += ", zero-copy"

    def _map_all(self, check=False):
        cl = self.cl
        err = _c.c_int(0)
        last = len(self.buffers) - 1
        for k, (b, (_raw, ptr, size)) in enumerate(zip(self.buffers, self._blocks)):
            got = cl.clEnqueueMapBuffer(self.queue, b, CL_TRUE if k == last else CL_FALSE,
                                        CL_MAP_READ | CL_MAP_WRITE, 0, size, 0, None, None,
                                        _c.byref(err))
            _check(err.value, "map " + self._names[k])
            if (got or 0) != ptr:
                # a USE_HOST_PTR buffer maps at its host block (OpenCL 1.2, 5.4.2); a driver that
                # answers elsewhere is copying, and the engine's arrays would not be its data
                if check:
                    cl.clEnqueueUnmapMemObject(self.queue, b, _vp(got), 0, None, None)
                    cl.clFinish(self.queue)
                    raise be.BackendError("the driver mapped {} away from its host memory".format(
                        self._names[k]))
                raise be.BackendError("OpenCL map of {} moved (zero-copy broken)".format(
                    self._names[k]))
        self._mapped = True

    def _unmap_all(self):
        cl = self.cl
        for k, (b, (_raw, ptr, _size)) in enumerate(zip(self.buffers, self._blocks)):
            _check(cl.clEnqueueUnmapMemObject(self.queue, b, _vp(ptr), 0, None, None),
                   "unmap " + self._names[k])
        self._mapped = False

    def _host_wants(self, name):
        """Copies on demand: the engine looks grid `name` up. Read it back if the GPU wrote it
        since the host last held it; it goes to the GPU before the next batch."""
        if self.zero_copy:
            return
        if name in self._stale:
            k = self._names.index(name)
            a = self._host[name]
            _check(self.cl.clEnqueueReadBuffer(self.queue, self.buffers[k], CL_TRUE, 0, a.nbytes,
                                               a.ctypes.data, 0, None, None), "copy " + name)
            self.copied_bytes += a.nbytes
            self._stale.discard(name)
        self._touched.add(name)

    def _upload(self):
        """Copies: write the grids the host looked up since the last batch, and the state words."""
        cl = self.cl
        n = 0
        for k, name in enumerate(self._names):
            if name not in self._touched and name not in self._always:
                continue
            a = self._host[name]
            _check(cl.clEnqueueWriteBuffer(self.queue, self.buffers[k], CL_FALSE, 0, a.nbytes,
                                           a.ctypes.data, 0, None, None), "copy " + name)
            n += a.nbytes
        self._touched = set()
        self.copied_bytes += n

    def _download(self):
        """Copies: read back the state words and tile flags; every other grid a kernel writes is
        now newer on the GPU (read back when the engine looks it up). DAMSAFETY_MF_CL_SYNC=full
        reads them all back now."""
        cl = self.cl
        full = _sync_full()
        n = 0
        stale = set()
        for k, name in enumerate(self._names):
            if name not in self._writable:
                continue
            if name in self._always or full:
                a = self._host[name]
                _check(cl.clEnqueueReadBuffer(self.queue, self.buffers[k], CL_FALSE, 0, a.nbytes,
                                              a.ctypes.data, 0, None, None), "copy " + name)
                n += a.nbytes
            else:
                stale.add(name)
        self._stale = stale
        self.copied_bytes += n

    def run_batch(self, params, nsteps, chunk=0):
        cl = self.cl
        prof = self.prof
        nsteps = int(nsteps)
        tiles = be._tiles_on(self.spec, params, chunk)
        st = self._host.get("st")
        if st is not None and self.spec.tile_plan is not None:
            st[be.S_TILES] = 1 if tiles else 0
        t0 = time.perf_counter()
        if self.zero_copy:
            self._unmap_all()
        else:
            self._upload()
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
        if self.zero_copy:
            self._map_all()
        else:
            self._download()
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
        ton = self._host.get("ton")
        if not tiles or ton is None:
            return wr * wc
        ntx, nty = -(-wc // be.TILE), -(-wr // be.TILE)
        on = ton[:ntx * nty].reshape(nty, ntx) != 0
        w = np.minimum(be.TILE, wc - be.TILE * np.arange(ntx))
        h = np.minimum(be.TILE, wr - be.TILE * np.arange(nty))
        return int((on * np.outer(h, w)).sum())

    def close(self):
        cl = self.cl
        if self.queue:
            self._release_buffers()     # the host keeps its arrays (zero-copy: the host blocks)
        for kh in self.kernels.values():
            cl.clReleaseKernel(kh)
        self.kernels = {}
        for attr, fn in (("program", cl.clReleaseProgram), ("queue", cl.clReleaseCommandQueue),
                         ("ctx", cl.clReleaseContext)):
            h = getattr(self, attr, None)
            if h:
                fn(h)
            setattr(self, attr, None)
