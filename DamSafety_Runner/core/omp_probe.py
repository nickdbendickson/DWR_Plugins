"""The OpenMP runtime's own thread count in this process (3.10.50).

anuga.get_omp_num_threads() only returns a Python variable that set_omp_num_threads stores
(ANUGA 4.0.1 shallow_water_domain.py), so it always equals the count that was asked for.
omp_runtime_threads() asks the OpenMP runtime ANUGA loaded (libgomp on Linux and conda-forge
Windows, libomp on macOS; libiomp5 / vcomp are also tried) via ctypes omp_get_max_threads.
It only looks at runtimes that are ALREADY loaded (it never loads one itself, which would
report that fresh runtime's default instead), and it never raises: (None, reason) when no
runtime is found. Standard library only, so the ANUGA worker can import it."""
import ctypes
import os
import re
import sys

_LIB_RE = re.compile(r"(^|[\\/])(lib)?(gomp|omp|iomp5?|vcomp)[^\\/]*\.(so[.\d]*|dylib|dll)$",
                     re.I)
WINDOWS_NAMES = ("libgomp-1.dll", "libomp.dll", "libiomp5md.dll", "vcomp140.dll")


def _loaded_linux():
    try:
        with open("/proc/self/maps", encoding="utf-8", errors="replace") as f:
            paths = [ln.split(None, 5)[-1].strip() for ln in f if "/" in ln]
    except OSError:
        return []
    seen, out = set(), []
    for p in paths:
        if p not in seen and _LIB_RE.search(p):
            seen.add(p)
            out.append(p)
    return out


def _loaded_darwin():
    try:
        libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
        libc._dyld_image_count.restype = ctypes.c_uint32
        libc._dyld_get_image_name.restype = ctypes.c_char_p
        libc._dyld_get_image_name.argtypes = [ctypes.c_uint32]
        names = [libc._dyld_get_image_name(i) for i in range(libc._dyld_image_count())]
    except (OSError, AttributeError):
        return []
    return [n.decode("utf-8", "replace") for n in names
            if n and _LIB_RE.search(n.decode("utf-8", "replace"))]


def _loaded_windows():
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.GetModuleHandleW.restype = ctypes.c_void_p
        k32.GetModuleHandleW.argtypes = [ctypes.c_wchar_p]
    except (OSError, AttributeError):
        return []
    return [n for n in WINDOWS_NAMES if k32.GetModuleHandleW(n)]


def loaded_runtimes(platform=None):
    """Paths (names on Windows) of the OpenMP runtimes loaded in this process."""
    plat = platform or sys.platform
    try:
        if plat.startswith("linux"):
            return _loaded_linux()
        if plat == "darwin":
            return _loaded_darwin()
        if plat in ("win32", "cygwin"):
            return _loaded_windows()
    except Exception:        # noqa: BLE001 - a probe, never an error
        pass
    return []


def omp_runtime_threads(libs=None):
    """(n, runtime name) from the first loaded OpenMP runtime that has omp_get_max_threads,
    else (None, reason). `libs`: paths to try instead of the loaded ones (tests)."""
    libs = loaded_runtimes() if libs is None else list(libs)
    if not libs:
        return None, "no OpenMP runtime loaded in this process"
    mode = getattr(os, "RTLD_NOLOAD", 0) | getattr(ctypes, "RTLD_GLOBAL", 0)
    for path in libs:
        try:
            lib = ctypes.CDLL(path, mode=mode) if os.name != "nt" else ctypes.CDLL(path)
            fn = lib.omp_get_max_threads
            fn.restype = ctypes.c_int
            fn.argtypes = []
            return int(fn()), os.path.basename(path)
        except Exception:    # noqa: BLE001 - not loaded / no such symbol: try the next
            continue
    return None, "OpenMP runtime without omp_get_max_threads ({})".format(
        ", ".join(os.path.basename(p) for p in libs)[:200])
