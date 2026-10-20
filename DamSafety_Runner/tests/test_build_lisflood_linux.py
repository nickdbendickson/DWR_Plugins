"""Audit 2026-10-19 R-H1: scripts/build_lisflood_linux.sh applies the arrival-depth patch
(scripts/patch_lisflood.py) before cmake, like build_lisflood_macos.sh, and refuses a build
without it. The script runs against a tiny source tree made of the exact text the patch
edits, with a stand-in cmake (no compiler, no model run)."""
import importlib.util
import os
import shutil
import subprocess
import tempfile

from nd_breach_runner.tests.registry import test

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(os.path.dirname(HERE), "scripts")
LINUX = os.path.join(SCRIPTS, "build_lisflood_linux.sh")

STUB_CMAKE = """#!/bin/bash
echo "cmake $*" >> "$PWD/cmake_calls.log"
if [ "$1" = "--build" ]; then
  mkdir -p "$2"
  { echo BINARY; grep -h inittm_depth pars.cpp 2>/dev/null; } > "$2/lisflood"
fi
"""


def _patch_module():
    spec = importlib.util.spec_from_file_location("patch_lisflood_under_test",
                                                  os.path.join(SCRIPTS, "patch_lisflood.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _mini_source(folder):
    """One file per EDITS entry, holding the text the patch looks for (CRLF, as 8.2 ships)."""
    for rel, old, _new, _marker in _patch_module().EDITS:
        old = old[0] if isinstance(old, tuple) else old
        path = os.path.join(folder, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "ab") as fh:
            fh.write(("// before\n" + old + "\n// after\n").replace("\n", "\r\n").encode())
    with open(os.path.join(folder, "config.default.cmake"), "w") as fh:
        fh.write("set(_NETCDF 1)\n")


def _run(src, out, stub):
    env = dict(os.environ, PATH=stub + os.pathsep + os.environ.get("PATH", ""))
    return subprocess.run(["bash", LINUX, src, out], env=env, capture_output=True, text=True,
                          timeout=120)


@test
def linux_build_script_applies_the_arrival_depth_patch():
    text = open(LINUX, encoding="utf-8").read()
    i_patch, i_cmake = text.find("patch_lisflood.py"), text.find("cmake -S")
    assert 0 < i_patch < i_cmake, "patch_lisflood.py must run before cmake"
    assert "grep -q inittm_depth" in text
    if shutil.which("bash") is None:
        return
    work = tempfile.mkdtemp(prefix="ndbreach_buildlinux_")
    try:
        src, out, stub = (os.path.join(work, n) for n in ("src", "out", "stub"))
        _mini_source(src)
        os.makedirs(stub)
        with open(os.path.join(stub, "cmake"), "w") as fh:
            fh.write(STUB_CMAKE)
        os.chmod(os.path.join(stub, "cmake"), 0o755)
        r = _run(src, out, stub)
        assert r.returncode == 0, r.stdout + r.stderr
        assert "patched  pars.cpp" in r.stdout and "Built: " in r.stdout, r.stdout
        assert "inittm_depth" in open(os.path.join(src, "pars.cpp"), encoding="utf-8").read()
        assert b"\r\n" in open(os.path.join(src, "pars.cpp"), "rb").read()   # endings kept
        calls = open(os.path.join(src, "cmake_calls.log")).read()
        assert "-D_CONFIG=nd-breach.cmake" in calls and "--target lisflood" in calls, calls
        assert "set(_NETCDF 0)" in open(os.path.join(src, "nd-breach.cmake")).read()
        assert os.path.isfile(os.path.join(out, "lisflood"))
        r2 = _run(src, out, stub)                          # safe to run twice
        assert r2.returncode == 0 and "already  pars.cpp" in r2.stdout, r2.stdout + r2.stderr
        # a build that does not carry the keyword is refused
        with open(os.path.join(stub, "cmake"), "w") as fh:
            fh.write('#!/bin/bash\n[ "$1" = "--build" ] && mkdir -p "$2" && echo B > "$2/lisflood"\n'
                     "exit 0\n")
        r3 = _run(src, out + "_bad", stub)
        assert r3.returncode != 0 and "patch is not in the build" in r3.stdout, r3.stdout
    finally:
        shutil.rmtree(work, ignore_errors=True)


@test
def rebuild_messages_point_to_the_patching_scripts():
    for rel in ("algorithms.py", os.path.join("core", "lisflood.py")):
        text = open(os.path.join(os.path.dirname(HERE), rel), encoding="utf-8").read()
        assert "on macOS on macOS" not in text, rel
        assert "or build with CMake on Linux" not in text, rel
