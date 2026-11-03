"""Hazard 2.22.4: rasters named with units after the quantity ("Depth_ft.tif",
"Velocity_ftps.tif", "Depth_m.asc", "Velocity_ft_per_s.tif") are found in the dam folder
(they were taken for time steps and skipped) and their units come from the name.

    python3 DamSafety_Hazard/tests/test_dsswise_unit_names.py
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))

from DamSafety_Hazard import autodetect as A  # noqa: E402
from DamSafety_Hazard import dsswise as D  # noqa: E402

fails = []


def check(cond, msg):
    print(("ok   " if cond else "FAIL ") + msg)
    if not cond:
        fails.append(msg)


UNITS = [
    ("Depth_ft.tif", D.R_DEPTH, "ft"), ("depth_FT.asc", D.R_DEPTH, "ft"),
    ("DEPTH-feet.tif", D.R_DEPTH, "ft"), ("Depth_m.tif", D.R_DEPTH, "m"),
    ("Velocity_ftps.tif", D.R_VEL, "ft/s"), ("velocity_FPS.asc", D.R_VEL, "ft/s"),
    ("Velocity_ft_s.tif", D.R_VEL, "ft/s"), ("Velocity_ft_per_s.tif", D.R_VEL, "ft/s"),
    ("Velocity_mps.tif", D.R_VEL, "m/s"), ("Velocity_m_s.tif", D.R_VEL, "m/s"),
    # existing DSS-WISE Lite names keep working
    ("MaxDepth_in_Ft.tif", D.R_DEPTH, "ft"), ("MaxVelocity_in_M_per_Sec.tif", D.R_VEL, "m/s"),
    ("MaxVelocity_in_Ft_per_Sec.tif", D.R_VEL, "ft/s"),
]
for name, role, units in UNITS:
    r = D.detect_role(name)
    u, basis = D.detect_units(name, r)
    check(r == role and u == units and basis == "file name",
          "{}: {} {} ({})".format(name, r, u, basis))
    check(not A._is_time_step(name), name + " is not a time step")

for name in ("Depth_0100.tif", "Velocity (01JAN2020 12:00:00).tif", "Depth_m_0100.tif"):
    check(A._is_time_step(name), name + " still a time step")
u, basis = D.detect_units("Depth (Max).tif", D.R_DEPTH)
check(u == "ft" and basis.startswith("assumed"), "Depth (Max): assumed ft")


def touch(p):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    open(p, "wb").close()


with tempfile.TemporaryDirectory() as td:
    dam = os.path.join(td, "DWR-9999")
    touch(os.path.join(dam, "DSSWISE", "Results", "Depth_ft.tif"))
    touch(os.path.join(dam, "DSSWISE", "Results", "Velocity_ftps.tif"))
    f = A.find_rasters(dam)
    check(f["depth"] and f["depth"].endswith("Depth_ft.tif"), "nested Depth_ft.tif found")
    check(f["vel"] and f["vel"].endswith("Velocity_ftps.tif"), "nested Velocity_ftps.tif found")
    check(not any("WARNING" in n for n in f["notes"]), "no unit warning for feet")

with tempfile.TemporaryDirectory() as td:
    dam = os.path.join(td, "DWR-9998")
    touch(os.path.join(dam, "Depth_m.asc"))
    touch(os.path.join(dam, "Velocity_m_s.asc"))
    f = A.find_rasters(dam)
    check(f["depth"] and f["vel"], "metric rasters found")
    check(sum("WARNING" in n and "named in m" in n for n in f["notes"]) == 2,
          "metric names flagged: {}".format(f["notes"]))

print("FAILED: {}".format(len(fails)) if fails else "all passed")
sys.exit(1 if fails else 0)
