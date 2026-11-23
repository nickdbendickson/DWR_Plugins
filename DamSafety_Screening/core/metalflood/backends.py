"""Where the kernels of kernels.metal run.

MetalBackend   the Apple GPU, through the Metal framework called with ctypes (no PyObjC, no
               Xcode, nothing to install: the shader source is compiled by Metal at run time).
               Apple silicon (no float atomics needed).
CpuBackend     the same kernel source built as C++ (mf_shim.h + cpu_kernels.cpp): a library
               shipped with the plugin (prebuilt/<platform>/, made by the release workflow with
               tools/build_boann_cpu.py) when one matches the source's hash, else compiled with
               the computer's C++ compiler at first use and cached by that hash. Built with
               OpenMP when the compiler has it (every core, bit-identical to one core; set
               DAMSAFETY_MF_THREADS to cap it, DAMSAFETY_MF_OPENMP=0 for the one-core build),
               else one core. It is how Boann runs on Windows and Linux, and how the engine is
               tested where there is no Apple GPU (Linux CI, the cloud).

Both hold every grid as a numpy array the host reads and writes directly (Metal: shared
storage, the GPU's own memory on Apple silicon), and run n steps per call (run_batch)."""
import ctypes
import hashlib
import os
import platform
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
FV2_KERNEL_SOURCE = os.path.join(HERE, "kernels_fv2.metal")
FV2_CPU_SOURCE = os.path.join(HERE, "cpu_kernels_fv2.cpp")
DG2_KERNEL_SOURCE = os.path.join(HERE, "kernels_dg2.metal")
DG2_CPU_SOURCE = os.path.join(HERE, "cpu_kernels_dg2.cpp")
SGC_KERNEL_SOURCE = os.path.join(HERE, "kernels_sgc.metal")
SGC_CPU_SOURCE = os.path.join(HERE, "cpu_kernels_sgc.cpp")

S_WORDS = 32
KERNELS = ("k_begin", "k_faces", "k_scale", "k_limit", "k_cont", "k_speed", "k_end")
FV_KERNELS = ("k_begin_fv", "k_faces_fv", "k_cont_fv", "k_end_fv")
FV2_KERNELS = ("k_stage_p_fv2", "k_begin_fv2", "k_save_fv2", "k_faces_fv2", "k_cont_fv2",
               "k_stage_c_fv2", "k_end_fv2")
DG2_KERNELS = ("k_stage_p_dg2", "k_begin_dg2", "k_save_dg2", "k_limit_dg2", "k_faces_dg2",
               "k_cont_dg2", "k_stage_c_dg2", "k_end_dg2")
SGC_KERNELS = ("k_begin_sgc", "k_faces_sgc", "k_scale_sgc", "k_limit_sgc", "k_cont_sgc",
               "k_speed_sgc", "k_end_sgc")


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


def fv2_buffer_layout(ny, nx, n_src, n_series, n_values):
    """[(name, dtype, count)] in the order of kernels_fv2.metal's buffer indices 0..27: the FV1
    layout (0..23), the previous-step state h0/hu0/hv0 for the RK2 blend, and hs, the stage-start
    depth k_cont_fv2 reads for its neighbours' slopes (so the in-place update has no read-after-write
    race). Params is bound at 28. The minmod slopes are not stored (Metal caps the buffer table at
    31 slots); they are recomputed inline in the kernels."""
    cells = ny * nx
    f = np.float32
    base = fv_buffer_layout(ny, nx, n_src, n_series, n_values)
    return base + [("h0", f, cells), ("hu0", f, cells), ("hv0", f, cells), ("hs", f, cells)]


def dg2_buffer_layout(ny, nx, n_src, n_series, n_values):
    """[(name, dtype, count)] in the order of kernels_dg2.metal's buffer indices 0..26. The three
    conserved variables (Uh, Uhu, Uhv) and their previous-step RK2 copy (Uh0, Uhu0, Uhv0) are stored
    packed, three modal coefficients per cell (3*cells floats): [3 i + 0] element average, [3 i + 1]
    x-slope, [3 i + 2] y-slope. The bed z is piecewise constant (one float per cell). 27 buffers keep
    DG2 inside Metal's 31-slot argument-table cap (slopes are modal unknowns, not separate buffers)."""
    cells = ny * nx
    xf, yf = ny * (nx + 1), (ny + 1) * nx
    f, u = np.float32, np.uint32
    return [("z", f, cells), ("n2", f, cells), ("Uh", f, 3 * cells), ("Uhu", f, 3 * cells),
            ("Uhv", f, 3 * cells), ("src", f, cells), ("hmax", f, cells), ("tmax", f, cells),
            ("arr", f, cells), ("vmax", f, cells), ("ok", np.uint8, cells),
            ("s_cell", u, n_src), ("s_series", u, n_src), ("ser_off", u, n_series + 1),
            ("ser_t", f, n_values), ("ser_q", f, n_values), ("cursor", u, n_series),
            ("st", u, S_WORDS), ("fxh", f, xf), ("fxhu", f, xf), ("fxhv", f, xf),
            ("fyh", f, yf), ("fyhu", f, yf), ("fyhv", f, yf),
            ("Uh0", f, 3 * cells), ("Uhu0", f, 3 * cells), ("Uhv0", f, 3 * cells)]


def sgc_buffer_layout(ny, nx, n_src, n_series, n_values):
    """[(name, dtype, count)] in the order of kernels_sgc.metal's buffer indices 0..29. The ACC
    buffers plus the channel/floodplain discharge split (qxc/qxf and their old qxco/qxfo, likewise
    y) and the per-cell SGC geometry (width w, bankfull depth Dd so bed = z - Dd, channel n^2 nc2).
    30 buffers + Params at index 30 = 31 slots, the Metal argument-table cap."""
    cells = ny * nx
    xf, yf = ny * (nx + 1), (ny + 1) * nx
    f, u = np.float32, np.uint32
    return [("z", f, cells), ("n2", f, cells), ("h", f, cells),
            ("qxc", f, xf), ("qxf", f, xf), ("qyc", f, yf), ("qyf", f, yf),
            ("src", f, cells), ("scl", f, cells), ("hmax", f, cells), ("tmax", f, cells),
            ("arr", f, cells), ("vmax", f, cells), ("ok", np.uint8, cells),
            ("s_cell", u, n_src), ("s_series", u, n_src), ("ser_off", u, n_series + 1),
            ("ser_t", f, n_values), ("ser_q", f, n_values), ("cursor", u, n_series),
            ("st", u, S_WORDS), ("qxco", f, xf), ("qxfo", f, xf), ("qyco", f, yf), ("qyfo", f, yf),
            ("vxf", f, xf), ("vyf", f, yf), ("w", f, cells), ("Dd", f, cells), ("nc2", f, cells)]


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


def _fv2_plan(p):
    one = (1, 1, 1)
    faces = (p.wc + 1, p.wr + 1, 1)
    cells = (p.wc, p.wr, 1)
    # one SSP-RK2 step: predictor sweep (stage 0), then corrector sweep (stage 1), then close.
    # k_save_fv2 saves the stage-start depth hs at both stages and U^n once at stage 0; the slopes are
    # recomputed inline in faces/cont (cont reads its neighbours from hs, not the h it overwrites).
    return [("k_stage_p_fv2", one), ("k_begin_fv2", one), ("k_save_fv2", cells),
            ("k_faces_fv2", faces), ("k_cont_fv2", cells),
            ("k_stage_c_fv2", one), ("k_begin_fv2", one), ("k_save_fv2", cells),
            ("k_faces_fv2", faces), ("k_cont_fv2", cells), ("k_end_fv2", one)]


FV2_SPEC = Spec("fv2", FV2_KERNEL_SOURCE, SHIM, FV2_CPU_SOURCE, FV2_KERNELS, Params, _fv2_plan)


def _dg2_plan(p):
    one = (1, 1, 1)
    faces = (p.wc + 1, p.wr + 1, 1)
    cells = (p.wc, p.wr, 1)
    # one SSP-RK2 step: predictor sweep (stage 0), then corrector sweep (stage 1), then close.
    # k_save_dg2 copies U^n once at stage 0; k_limit_dg2 is a no-op when Params.limiter is 0.
    return [("k_stage_p_dg2", one), ("k_begin_dg2", one), ("k_save_dg2", cells),
            ("k_limit_dg2", cells), ("k_faces_dg2", faces), ("k_cont_dg2", cells),
            ("k_stage_c_dg2", one), ("k_begin_dg2", one), ("k_save_dg2", cells),
            ("k_limit_dg2", cells), ("k_faces_dg2", faces), ("k_cont_dg2", cells),
            ("k_end_dg2", one)]


DG2_SPEC = Spec("dg2", DG2_KERNEL_SOURCE, SHIM, DG2_CPU_SOURCE, DG2_KERNELS, Params, _dg2_plan)


def _sgc_plan(p):
    one = (1, 1, 1)
    faces = (p.wc + 1, p.wr + 1, 1)
    cells = (p.wc, p.wr, 1)
    return [("k_begin_sgc", one), ("k_faces_sgc", faces), ("k_scale_sgc", cells),
            ("k_limit_sgc", faces), ("k_cont_sgc", cells), ("k_speed_sgc", cells),
            ("k_end_sgc", one)]


SGC_SPEC = Spec("sgc", SGC_KERNEL_SOURCE, SHIM, SGC_CPU_SOURCE, SGC_KERNELS, Params, _sgc_plan)


# The C++ flags of every CPU build. -ffp-contract=off: never fuse a * b + c into one FMA. A compiler
# that targets a CPU with FMA (Ubuntu 26.04's g++, every Apple Silicon clang) otherwise fuses some
# sums and not others depending on how it vectorises a loop, and the one-core and multi-core builds
# vectorise differently, so their answers drifted apart in the last bit (DG2 on CI, 2026-11-22).
# The flags are part of the library's name (_source_hash), so a build made with other flags is
# never reused.
CPU_FLAGS = ("-O3", "-std=c++14", "-fno-strict-aliasing", "-ffp-contract=off")


def _source_hash(spec):
    h = hashlib.sha256()
    for p in (spec.kernel_source, spec.shim, spec.cpu_source):
        with open(p, "rb") as f:
            h.update(f.read())
    h.update(" ".join(CPU_FLAGS).encode())
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


PREBUILT_DIR = os.path.join(HERE, "prebuilt")


def platform_tag():
    """linux-x64, windows-x64, macos-arm64, ... : the prebuilt/ folder for this computer."""
    m = platform.machine().lower()
    arch = "arm64" if m in ("arm64", "aarch64") else ("x64" if m in ("x86_64", "amd64") else m)
    osname = "macos" if sys.platform == "darwin" else ("windows" if os.name == "nt" else "linux")
    return "{}-{}".format(osname, arch)


def library_ext(osname=None):
    osname = osname or platform_tag().split("-")[0]
    return {"macos": ".dylib", "windows": ".dll"}.get(osname, ".so")


def cpu_library_name(spec, ext, openmp):
    """<spec>_cpu_<source hash>[_omp]<ext>, the name of a CPU build of these exact sources."""
    return "{}_cpu_{}{}{}".format(spec.name, _source_hash(spec), "_omp" if openmp else "", ext)


def cpu_build_command(spec, cxx, out, openmp=True, extra=()):
    """The compiler command for the CPU build of spec (tools/build_boann_cpu.py uses it too)."""
    inc = []
    for d in dict.fromkeys(os.path.dirname(p) for p in
                           (spec.cpu_source, spec.shim, spec.kernel_source)):
        inc += ["-I", d]
    cmd = [cxx] + list(CPU_FLAGS) + ["-shared", "-fPIC"]
    if openmp:
        cmd.append("-fopenmp")
    return cmd + inc + ["-x", "c++", spec.cpu_source] + list(extra) + ["-o", out]


def prebuilt_available(spec=FLOOD_SPEC):
    """True when this plugin ships a CPU build of spec's current sources for this computer."""
    d = os.path.join(PREBUILT_DIR, platform_tag())
    return any(os.path.isfile(os.path.join(d, cpu_library_name(spec, library_ext(), omp)))
               for omp in (True, False))


def _loads(path, log=None):
    """True when the library opens here (a prebuilt OpenMP build needs the OpenMP runtime, which a
    computer may lack: the one-core build is the fallback)."""
    try:
        ctypes.CDLL(path)
        return True
    except OSError as e:
        if log:
            log("Not using {} ({}).".format(os.path.basename(path), e))
        return False


def _openmp_wanted():
    return os.environ.get("DAMSAFETY_MF_OPENMP", "1").strip().lower() not in ("0", "no", "false", "off")


def build_cpu_library(spec=FLOOD_SPEC, log=None):
    """The CPU build of the kernels, its path: a prebuilt library shipped with the plugin for
    these exact sources, else one compiled here once per source version (OpenMP first, the
    one-core build if the compiler has no OpenMP)."""
    ext = library_ext()
    kinds = [True, False] if _openmp_wanted() else [False]
    for omp in kinds:
        p = os.path.join(PREBUILT_DIR, platform_tag(), cpu_library_name(spec, ext, omp))
        if os.path.isfile(p) and _loads(p, log):
            return p
    cxx = find_compiler()
    err = ""
    for omp in kinds:
        out = os.path.join(_cache_dir(), cpu_library_name(spec, ext, omp))
        if os.path.isfile(out):
            return out
        failed = out + ".failed"                # this compiler has no OpenMP: do not retry each run
        if not cxx or (omp and os.path.isfile(failed)):
            continue
        fd, tmp = tempfile.mkstemp(suffix=ext, dir=os.path.dirname(out))
        os.close(fd)
        cmd = cpu_build_command(spec, cxx, tmp, openmp=omp)
        if log:
            log("Building the engine's CPU kernels: " + " ".join(cmd))
        r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           universal_newlines=True)
        if r.returncode == 0:
            os.replace(tmp, out)
            return out
        os.remove(tmp)
        err = r.stdout[-3000:]
        if omp:
            try:
                with open(failed, "w") as f:
                    f.write(err)
            except OSError:
                pass
            if log:
                log("This compiler has no OpenMP; building the one-core CPU kernels instead.")
    if not cxx:
        raise BackendError("no C++ compiler (c++, clang++ or g++) for the engine's CPU build, and no "
                           "prebuilt library for {} in this plugin (a release zip ships them)".format(
                               platform_tag()))
    raise BackendError("the CPU kernels did not compile:\n" + err)


class CpuBackend:
    kind = "cpu"

    def __init__(self, spec=FLOOD_SPEC, log=None):
        self.spec = spec
        self.path = build_cpu_library(spec, log)
        self.lib = ctypes.CDLL(self.path)
        self.lib.mf_run_batch.argtypes = [ctypes.c_void_p, ctypes.POINTER(spec.params_cls),
                                          ctypes.c_int]
        self.lib.mf_run_batch.restype = ctypes.c_int
        if self.lib.mf_params_size() != ctypes.sizeof(spec.params_cls):
            raise BackendError("the CPU kernels' Params is {} bytes, Python's {}".format(
                self.lib.mf_params_size(), ctypes.sizeof(spec.params_cls)))
        self.threads = 1
        if hasattr(self.lib, "mf_threads"):
            self.lib.mf_threads.argtypes = [ctypes.c_int]
            self.lib.mf_threads.restype = ctypes.c_int
            try:
                want = int(os.environ.get("DAMSAFETY_MF_THREADS", "0") or 0)
            except ValueError:
                want = 0
            self.threads = max(1, int(self.lib.mf_threads(max(0, want))))
        self.device = ("CPU ({} cores, C++ build of the Metal kernels)".format(self.threads)
                       if self.threads > 1 else "CPU (one core, C++ build of the Metal kernels)")
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
