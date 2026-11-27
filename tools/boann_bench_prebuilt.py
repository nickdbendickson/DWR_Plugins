#!/usr/bin/env python3
"""Boann's speed test (DamSafety_Core/boann/hydro/bench.py) on a computer that has no C++ compiler
but does have Boann's prebuilt CPU libraries under DamSafety_Core/boann/hydro/prebuilt/<platform>/
(a Windows computer with a release zip, or .github/workflows/boann-windows-speed.yml).

bench.py runs the CPU build only when it finds a compiler, even where a prebuilt library would be
used; here the CPU build runs whenever that folder exists. Same arguments as bench.py.

    python tools/boann_bench_prebuilt.py --size medium --lisflood lisflood.exe"""
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
from DamSafety_Core.boann.hydro import backends as be   # noqa: E402
from DamSafety_Core.boann.hydro import bench            # noqa: E402

if os.path.isdir(os.path.join(be.PREBUILT_DIR, be.platform_tag())):
    _find = be.find_compiler
    be.find_compiler = lambda: _find() or "prebuilt"
bench.main()
