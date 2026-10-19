#!/usr/bin/env python3
"""Regenerate tests/data/golden/builtin_engine.json from the current built-in engine.

Run this ONLY after a deliberate, vetted change to the engine or the read_results scalars; it
overwrites the frozen baseline the golden regression checks against. Pure numpy; no scipy.

    python3 tools/regen_golden_builtin.py
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from nd_breach_runner.tests.test_golden_baseline import run_fixed_case, GOLDEN  # noqa: E402


def main():
    scalars, raster = run_fixed_case()
    out = {
        "description": ("Frozen regulatory-facing outputs of the deterministic built-in flood "
                        "engine on a fixed synthetic breach (a 2 km channel, 20 m cells, a 30 cms "
                        "breach hydrograph for 600 s). Regenerate with: "
                        "python3 tools/regen_golden_builtin.py. A drift here means a coefficient, "
                        "units or sign change in the engine moved the flood numbers that feed "
                        "hazard classification."),
        "case": "channel_20m_30cms_1200s",
        "scalars": {k: scalars[k] for k in (
            "cells", "cell_size_m", "sim_time_hr", "peak_inflow_cfs", "injected_acft",
            "in_domain_acft", "flooded_acres", "max_depth_ft", "max_velocity_fps",
            "max_volume_error_pct")},
        "raster": {
            "wet_cells": raster["wet_cells"],
            "max_depth_m": round(raster["max_depth_m"], 3),
            "sum_depth_m": round(raster["sum_depth_m"], 3),
        },
    }
    with open(GOLDEN, "w", encoding="utf-8") as f:
        f.write(json.dumps(out, indent=1) + "\n")
    print("wrote", os.path.relpath(GOLDEN, ROOT))


if __name__ == "__main__":
    main()
