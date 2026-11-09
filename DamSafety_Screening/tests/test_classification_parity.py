"""Screening leg of the three-way classification parity (audit P1, 2026-11-08).

The fixture and the Hazard / Runner legs live in the Runner's
tests/test_classification_parity.py (one synthetic flood, one structure set: outlines, points,
a structure 3 ft outside the wet edge, one 0.05 ft below a zone boundary). This script runs the
Screening's own leg (core/hazard.Grid.sample(structure=True) + classify_nd) against the
Hazard's array-level leg, so a Screening-only change that breaks the parity fails HERE, in the
Screening suite, and not only in the Runner's. Mock QGIS; numpy only (no GDAL).

    python3 DamSafety_Screening/tests/test_classification_parity.py
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(HERE, "qgis_mock"))
sys.path.insert(0, os.path.dirname(PLUGIN))
sys.path.insert(0, HERE)
import _runner  # noqa: E402  (the Runner in this repository; a FAIL in CI when missing)

root, folder = _runner.runner_folder(os.path.join("tests", "test_classification_parity.py"))
if root is None:
    _runner.missing("the three-way classification parity fixture")
    sys.exit(0)
if root not in sys.path:
    sys.path.insert(0, root)

from nd_breach_runner.tests import test_classification_parity as P  # noqa: E402
from nd_dam_screening.core import hazard as hz, nd_hazard as nh  # noqa: E402

d, v = P.rasters()
gt = (0.0, 1.0, 0.0, 60.0, 0.0, -1.0)
structs = P.structures()
want = P.hazard_leg(d, v, gt, structs)
got = P.screening_leg(d, v, gt, structs)
assert hz.nh.MODULE_VERSION == nh.MODULE_VERSION == "1.4.0"
problems = []
for sid, _k, _g, _m, note in structs:
    w, g = want[sid], got[sid]
    same = w[0] == g[0] and all((a is None and b is None) or (a is not None and b is not None
                                                             and abs(a - b) <= 1e-6)
                                for a, b in zip(w[1:], g[1:]))
    print("{:4} {:12s} Hazard {!s:12} {} ft {} ft/s | Screening {!s:12} {} ft {} ft/s".format(
        "ok" if same else "FAIL", sid, w[0], w[1], w[2], g[0], g[1], g[2]))
    if not same:
        problems.append("{} ({})".format(sid, note))
# the former differences, named: the ring / disc make the two outside structures wet; the
# table (not the polygon tolerance band) puts the 0.05 ft-below point in the judgment zone
assert got["outside_3ft"][0] == nh.ZONE_LOW and got["point_disc"][0] == nh.ZONE_LOW
assert got["near_high"][0] == nh.ZONE_JUDGMENT and got["far"][0] is None
grid = hz.Grid(d * hz.M_PER_FT, v * hz.M_PER_FT, gt)
assert grid.sample("polygon", [list(structs[4][2])], 0.1)["depth_m"] == 0.0     # bare footprint
assert grid.sample("point", [structs[6][2]], 0.1)["depth_m"] == 0.0            # own cell only
assert problems == [], problems
print("OK (Screening leg equals the Hazard leg on every structure of the parity fixture)")
