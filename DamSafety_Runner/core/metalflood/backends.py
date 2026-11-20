"""Where the kernels of kernels.metal run.

MetalBackend   the Apple GPU, through the Metal framework called with ctypes (no PyObjC, no
               Xcode, nothing to install: the shader source is compiled by Metal at run time).
               Apple silicon (no float atomics needed).
CpuBackend     the same kernel source built as C++ (mf_shim.h + cpu_kernels.cpp) with the
               computer's C++ compiler at first use, cached by the source's hash. One core. It is
               how the engine is tested where there is no Apple GPU (Linux CI, the cloud).

Both hold every grid as a numpy array the host reads and writes directly (Metal: shared
storage, the GPU's own memory on Apple silicon), and run n steps per call (run_batch)."""
import ctypes
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
KERNEL_SOURCE = os.path.join(HERE, "kernels.metal")
SHIM = os.path.join(HERE, "mf_shim.h")
CPU_SOURCE = os.path.join(HERE, "cpu_kernels.cpp")
FV_KERNEL_SOURCE = os.path.join(HERE, "kernels_fv.metal")
FV_CPU_SOURCE = os.path.join(HERE, "cpu_kernels_fv.cpp")

S_WORDS = 32
KERNELS = ("k_begin", "k_faces", "k_scale", "k_limit", "k_cont", "k_speed", "k_end")
FV_KERNELS = ("k_begin_fv", "k_faces_fv", "k_cont_fv", "k_end_fv")


class BackendError(RuntimeError):
    pass


class Params(ctypes.Structure):
    """struct Params of kernels.metal, field for field."""
    _fields_ = [(n, ctypes.c_uint32) for n in (
        "nx", "ny", "r0", "c0", "wr", "wc", "free_mask", "n_src", "n_series",
        "fric2d", "clip_end", "inflow_lf_order")] + [(n, ctypes.c_float) for n in (
            "dx", "dt0", "alpha", "g", "dry", "maxhflow", "velmin", "arr_depth", "t0", "t_end",
            "theta", "pad4")]


def buffer_layout(ny, nx, n_src, n_series, n_values):
    """[(name, dtype, count)] in the order of the kernels' buffer indices 0..22."""
    cells = ny * nx
    f, u = np.float32, np.uint32
    return [("z", f, cells), ("n2", f, cells), ("h", f, cells), ("qx", f, ny * (nx + 1)),
            ("qy", f, (ny + 1) * nx), ("src", f, cells), ("scl", f, cells), ("hmax", f, cells),
            ("tmax", f, cells), ("arr", f, cells), ("vmax", f, cells), ("ok", np.uint8, cells),
            ("s_cell", u, n_src), ("s_series", u, n_src), ("ser_off", u, n_series + 1),
            ("ser_t", f, n_values), ("ser_q", f, n_values), ("cursor", u, n_series),
            ("st", u, S_WORDS), ("qxo", f, ny * (nx + 1)), ("qyo", f, (ny + 1) * nx),
            ("vxf", f, ny * (nx + 1)), ("vyf", f, (ny + 1) * nx)]


def fv_buffer_layout(ny, nx, n_src, n_series, n_values):
    """[(name, dtype, count)] in the order of kernels_fv.metal's buffer indices 0..23."""
    cells = ny * nx
    xf, yf = ny * (nx + 1), (ny + 1) * nx
    f, u = np.float32, np.uint32
    return [("z", f, cells), ("n2", f, cells), ("h", f, cells), ("hu", f, cells),
            ("hv", f, cells), ("src", f, cells), ("hmax", f, cells), ("tmax", f, cells),
            ("arr", f, cells), ("vmax", f, cells), ("ok", np.uint8, cells),
            ("s_cell", u, n_src), ("s_series", u, n_src), ("ser_off", u, n_series + 1),
            ("ser_t", f, n_values), ("ser_q", f, n_values), ("cursor", u, n_series),
            ("st", u, S_WORDS), ("fxh", f, xf), ("fxhu", f, xf), ("fxhv", f, xf),
            ("fyh", f, yf), ("fyhu", f, yf), ("fyhv", f, yf)]


class Spec:
    """What tells the generic Metal/CPU backend which kernels to run: one engine's kernel
    source, its C++-build source and shim, the kernel names, the ctypes Params for that
    source, and a plan(params) -> [(kernel_name, (w, h, d))] giving the dispatch order and
    grid size of one step. core/metalterrain (BoannGrid) reuses this backend with its own
    Spec; the default is this flood engine, so existing callers are unchanged."""

    def __init__(self, name, kernel_source, shim, cpu_source, kernels, params_cls, plan):
        self.name = name
        self.kernel_source = kernel_source
        self.shim = shim
        self.cpu_source = cpu_source
        self.kernels = tuple(kernels)
        self.params_cls = params_cls
        self.plan = plan


def _flood_plan(p):
    one = (1, 1, 1)
    faces = (p.wc + 1, p.wr + 1, 1)
    cells = (p.wc, p.wr, 1)
    return [("k_begin", one), ("k_faces", faces), ("k_scale", cells), ("k_limit", faces),
            ("k_cont", cells), ("k_speed", cells), ("k_end", one)]


FLOOD_SPEC = Spec("mf", KERNEL_SOURCE, SHIM, CPU_SOURCE, KERNELS, Params, _flood_plan)


def _fv_plan(p):
    one = (1, 1, 1)
    faces = (p.wc + 1, p.wr + 1, 1)
    cells = (p.wc, p.wr, 1)
    return [("k_begin_fv", one), ("k_faces_fv", faces), ("k_cont_fv", cells), ("k_end_fv", one)]


FV_SPEC = Spec("fv", FV_KERNEL_SOURCE, SHIM, FV_CPU_SOURCE, FV_KERNELS, Params, _fv_plan)


def _source_hash(spec):
    h = hashlib.sha256()
    for p in (spec.kernel_source, spec.shim, spec.cpu_source):
        with open(p, "rb") as f:
            h.update(f.read())
    return h.hexdigest()[:16]


def _cache_dir():
    d = os.environ.get("DAMSAFETY_MF_CACHE")
    if not d:
        if sys.platform == "darwin":
            d = os.path.expanduser("~/Library/Caches/DamSafety/metalflood")
        else:
            d = os.path.join(os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"),
                             "DamSafety", "metalflood")
    os.makedirs(d, exist_ok=True)
    return d


# -- CPU ----------------------------------------------------------------------------------

def find_compiler():
    for c in (os.environ.get("CXX"), "c++", "clang++", "g++"):
        if c and shutil.which(c):
            return shutil.which(c)
    return None


def build_cpu_library(spec=FLOOD_SPEC, log=None):
    """The CPU build of the kernels (compiled once per source version), its path."""
    ext = ".dylib" if sys.platform == "darwin" else (".dll" if os.name == "nt" else ".so")
    out = os.path.join(_cache_dir(), "{}_cpu_{}{}".format(spec.name, _source_hash(spec), ext))
    if os.path.isfile(out):
        return out
    cxx = find_compiler()
    if not cxx:
        raise BackendError("no C++ compiler (c++, clang++ or g++) for the engine's CPU build")
    fd, tmp = tempfile.mkstemp(suffix=ext, dir=os.path.dirname(out))
    os.close(fd)
    inc = []
    for d in dict.fromkeys(os.path.dirname(p) for p in
                           (spec.cpu_source, spec.shim, spec.kernel_source)):
        inc += ["-I", d]
    cmd = [cxx, "-O3", "-std=c++14", "-fno-strict-aliasing", "-shared", "-fPIC",
           *inc, "-x", "c++", spec.cpu_source, "-o", tmp]
    if log:
        log("Building the engine's CPU kernels: " + " ".join(cmd))
    r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                       universal_newlines=True)
    if r.returncode != 0:
        os.remove(tmp)
        raise BackendError("the CPU kernels did not compile:\n" + r.stdout[-3000:])
    os.replace(tmp, out)
    return out


class CpuBackend:
    kind = "cpu"

    def __init__(self, spec=FLOOD_SPEC, log=None):
        self.spec = spec
        self.lib = ctypes.CDLL(build_cpu_library(spec, log))
        self.lib.mf_run_batch.argtypes = [ctypes.c_void_p, ctypes.POINTER(spec.params_cls),
                                          ctypes.c_int]
        self.lib.mf_run_batch.restype = ctypes.c_int
        if self.lib.mf_params_size() != ctypes.sizeof(spec.params_cls):
            raise BackendError("the CPU kernels' Params is {} bytes, Python's {}".format(
                self.lib.mf_params_size(), ctypes.sizeof(spec.params_cls)))
        self.device = "CPU (one core, C++ build of the Metal kernels)"
        self.arrays = {}

    def allocate(self, layout):
        self.arrays = {}
        ptrs = (ctypes.c_void_p * len(layout))()
        for k, (name, dtype, count) in enumerate(layout):
            a = np.zeros(max(int(count), 1), dtype=dtype)
            self.arrays[name] = a
            ptrs[k] = a.ctypes.data
        self._ptrs = ptrs
        return self.arrays

    def run_batch(self, params, nsteps):
        self.lib.mf_run_batch(self._ptrs, ctypes.byref(params), int(nsteps))

    def close(self):
        pass


# -- Metal --------------------------------------------------------------------------------

class _MTLSize(ctypes.Structure):
    _fields_ = [("width", ctypes.c_ulong), ("height", ctypes.c_ulong),
                ("depth", ctypes.c_ulong)]


_MTL_STORAGE_SHARED = 0
_MTL_CB_COMPLETED = 4
_MTL_CB_ERROR = 5


class _ObjC:
    """objc_msgSend with a ctypes prototype per signature (arm64 needs the exact one)."""

    def __init__(self):
        self.lib = ctypes.CDLL("/usr/lib/libobjc.A.dylib")
        ctypes.CDLL("/System/Library/Frameworks/Foundation.framework/Foundation")
        self.lib.objc_getClass.restype = ctypes.c_void_p
        self.lib.objc_getClass.argtypes = [ctypes.c_char_p]
        self.lib.sel_registerName.restype = ctypes.c_void_p
        self.lib.sel_registerName.argtypes = [ctypes.c_char_p]
        self.lib.objc_autoreleasePoolPush.restype = ctypes.c_void_p
        self.lib.objc_autoreleasePoolPop.argtypes = [ctypes.c_void_p]
        self._send = ctypes.cast(self.lib.objc_msgSend, ctypes.c_void_p).value
        self._protos = {}
        self._sels = {}

    def cls(self, name):
        c = self.lib.objc_getClass(name.encode())
        if not c:
            raise BackendError("no Objective-C class " + name)
        return c

    def sel(self, name):
        s = self._sels.get(name)
        if s is None:
            s = self._sels[name] = self.lib.sel_registerName(name.encode())
        return s

    def __call__(self, obj, sel, *args, restype=ctypes.c_void_p, argtypes=()):
        key = (restype, tuple(argtypes))
        f = self._protos.get(key)
        if f is None:
            f = self._protos[key] = ctypes.CFUNCTYPE(
                restype, ctypes.c_void_p, ctypes.c_void_p, *argtypes)(self._send)
        return f(obj, self.sel(sel), *args)

    def nsstring(self, text):
        return self(self.cls("NSString"), "stringWithUTF8String:", text.encode("utf-8"),
                    argtypes=[ctypes.c_char_p])

    def text(self, nsstr):
        if not nsstr:
            return ""
        b = self(nsstr, "UTF8String", restype=ctypes.c_char_p)
        return b.decode("utf-8", "replace") if b else ""

    def error_text(self, err):
        if not err:
            return "(no detail)"
        return self.text(self(err, "localizedDescription"))

    def release(self, obj):
        if obj:
            self(obj, "release", restype=None)


def metal_available():
    """(True, device name) when this Mac has a Metal GPU the engine can use, else
    (False, why not)."""
    if sys.platform != "darwin":
        return False, "not macOS"
    try:
        m = ctypes.CDLL("/System/Library/Frameworks/Metal.framework/Metal")
        m.MTLCreateSystemDefaultDevice.restype = ctypes.c_void_p
        objc = _ObjC()
        pool = objc.lib.objc_autoreleasePoolPush()
        try:
            dev = m.MTLCreateSystemDefaultDevice()
            if not dev:
                return False, "no Metal device"
            name = objc.text(objc(dev, "name"))
            objc.release(dev)
            return True, name
        finally:
            objc.lib.objc_autoreleasePoolPop(pool)
    except (OSError, AttributeError) as e:
        return False, "Metal could not be loaded ({})".format(e)


class MetalBackend:
    kind = "metal"

    def __init__(self, spec=FLOOD_SPEC, log=None):
        self.spec = spec
        if sys.platform != "darwin":
            raise BackendError("Metal needs macOS")
        try:
            self._mtl = ctypes.CDLL("/System/Library/Frameworks/Metal.framework/Metal")
        except OSError as e:
            raise BackendError("Metal could not be loaded ({})".format(e))
        self._mtl.MTLCreateSystemDefaultDevice.restype = ctypes.c_void_p
        o = self.o = _ObjC()
        pool = o.lib.objc_autoreleasePoolPush()
        try:
            self.dev = self._mtl.MTLCreateSystemDefaultDevice()
            if not self.dev:
                raise BackendError("this Mac reports no Metal GPU")
            self.device = o.text(o(self.dev, "name"))
            self.queue = o(self.dev, "newCommandQueue")
            with open(spec.kernel_source, encoding="utf-8") as f:
                src = f.read()
            opts = o(o(o.cls("MTLCompileOptions"), "alloc"), "init")
            # IEEE math (no fast-math), so the GPU follows the CPU build closely
            if o(opts, "respondsToSelector:", o.sel("setMathMode:"), restype=ctypes.c_bool,
                 argtypes=[ctypes.c_void_p]):
                o(opts, "setMathMode:", 0, restype=None, argtypes=[ctypes.c_long])
            else:
                o(opts, "setFastMathEnabled:", False, restype=None, argtypes=[ctypes.c_bool])
            err = ctypes.c_void_p()
            self.library = o(self.dev, "newLibraryWithSource:options:error:", o.nsstring(src),
                             opts, ctypes.byref(err),
                             argtypes=[ctypes.c_void_p, ctypes.c_void_p,
                                       ctypes.POINTER(ctypes.c_void_p)])
            o.release(opts)
            if not self.library:
                raise BackendError("Metal did not compile the kernels: " + o.error_text(err.value))
            self.pso = {}
            self.tg = {}
            for name in spec.kernels:
                fn = o(self.library, "newFunctionWithName:", o.nsstring(name),
                       argtypes=[ctypes.c_void_p])
                if not fn:
                    raise BackendError("no kernel {} in the Metal library".format(name))
                err = ctypes.c_void_p()
                pso = o(self.dev, "newComputePipelineStateWithFunction:error:", fn,
                        ctypes.byref(err),
                        argtypes=[ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)])
                o.release(fn)
                if not pso:
                    raise BackendError("Metal pipeline {}: {}".format(name,
                                                                       o.error_text(err.value)))
                w = o(pso, "threadExecutionWidth", restype=ctypes.c_ulong)
                mx = o(pso, "maxTotalThreadsPerThreadgroup", restype=ctypes.c_ulong)
                self.pso[name] = pso
                self.tg[name] = _MTLSize(w, max(1, min(8, mx // max(w, 1))), 1)
        finally:
            o.lib.objc_autoreleasePoolPop(pool)
        self.buffers = []
        self.arrays = {}
        if log:
            log("Metal GPU: {}".format(self.device))

    def allocate(self, layout):
        o = self.o
        for b in self.buffers:
            o.release(b)
        self.buffers = []
        self.arrays = {}
        for name, dtype, count in layout:
            nbytes = max(int(count), 1) * np.dtype(dtype).itemsize
            buf = o(self.dev, "newBufferWithLength:options:", nbytes, _MTL_STORAGE_SHARED,
                    argtypes=[ctypes.c_ulong, ctypes.c_ulong])
            if not buf:
                raise BackendError("the GPU could not allocate {} ({:,} bytes)".format(
                    name, nbytes))
            ptr = o(buf, "contents")
            raw = (ctypes.c_char * nbytes).from_address(ptr)
            a = np.frombuffer(raw, dtype=dtype)
            a[:] = 0
            self.buffers.append(buf)
            self.arrays[name] = a
        return self.arrays

    def run_batch(self, params, nsteps):
        o = self.o
        pool = o.lib.objc_autoreleasePoolPush()
        try:
            cb = o(self.queue, "commandBuffer")
            enc = o(cb, "computeCommandEncoder")
            for k, buf in enumerate(self.buffers):
                o(enc, "setBuffer:offset:atIndex:", buf, 0, k, restype=None,
                  argtypes=[ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong])
            o(enc, "setBytes:length:atIndex:", ctypes.addressof(params), ctypes.sizeof(params),
              len(self.buffers), restype=None,
              argtypes=[ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong])
            one = _MTLSize(1, 1, 1)
            plan = [(name, _MTLSize(w, h, d)) for name, (w, h, d) in self.spec.plan(params)]
            set_pso = ("setComputePipelineState:", [ctypes.c_void_p])
            disp_args = [_MTLSize, _MTLSize]
            for _ in range(int(nsteps)):
                for name, grid in plan:
                    o(enc, set_pso[0], self.pso[name], restype=None, argtypes=set_pso[1])
                    tg = self.tg[name] if (grid.width * grid.height * grid.depth) > 1 else one
                    o(enc, "dispatchThreads:threadsPerThreadgroup:", grid, tg, restype=None,
                      argtypes=disp_args)
            o(enc, "endEncoding", restype=None)
            o(cb, "commit", restype=None)
            o(cb, "waitUntilCompleted", restype=None)
            status = o(cb, "status", restype=ctypes.c_ulong)
            if status != _MTL_CB_COMPLETED:
                raise BackendError("the GPU stopped with an error: " + o.error_text(o(cb, "error")))
        finally:
            o.lib.objc_autoreleasePoolPop(pool)

    def close(self):
        o = self.o
        for b in self.buffers:
            o.release(b)
        self.buffers = []
        for p in self.pso.values():
            o.release(p)
        self.pso = {}
        for obj in ("library", "queue", "dev"):
            o.release(getattr(self, obj, None))
            setattr(self, obj, None)


def make_backend(which="auto", log=None, spec=FLOOD_SPEC):
    """'metal', 'cpu' or 'auto' (Metal when this Mac has it, else the CPU build). The
    DAMSAFETY_MF_BACKEND environment variable overrides 'auto'."""
    which = (which or "auto").lower()
    if which == "auto":
        which = os.environ.get("DAMSAFETY_MF_BACKEND", "auto").lower()
    if which == "metal":
        return MetalBackend(spec, log)
    if which == "cpu":
        return CpuBackend(spec, log)
    if sys.platform == "darwin":
        try:
            return MetalBackend(spec, log)
        except BackendError as e:
            if log:
                log("Metal not used ({}); the CPU build runs instead.".format(e))
    return CpuBackend(spec, log)
