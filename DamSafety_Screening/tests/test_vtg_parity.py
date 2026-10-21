"""0.17.33: Von Thun & Gillette (1990) in Screening (core/breach_hydro.von_thun_gillette) gives
the Runner's numbers (core/breach.vtg_1990), also at the reservoir-size limits of Cb.

Cb is 6.1 / 18.3 / 42.7 / 54.9 m below 1,000 / 5,000 / 10,000 ac-ft / above. Screening used
rounded m3 limits (1.23e6 / 6.17e6 / 1.23e7 m3, inclusive) until 0.17.33, so 997.2-1,000,
5,000-5,002 and 9,972-10,000 ac-ft fell in another class than in the Runner.

1. Fixed values (no Runner needed): the class on both sides of each limit.
2. Equality with the Runner's vtg_1990 (this repository's DamSafety_Runner through the
   nd_breach_runner test alias). The Runner is part of this repository, so a Runner that does
   not import is a FAILURE here, never a skip.

    python3 DamSafety_Screening/tests/test_vtg_parity.py
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(HERE, "qgis_mock"))
sys.path.insert(0, REPO)
from nd_dam_screening.core import breach_hydro as bh  # noqa: E402

ACFT = 1233.48183754752          # m3 per acre-foot (the Runner's core/units.py)
HW = 6.0                         # m; B = 2.5 hw + Cb = 15 m + Cb

# ac-ft -> Cb (m): each side of each limit, and the bands where the old m3 limits disagreed
EXPECT = [(50.0, 6.1), (997.0, 6.1), (997.2, 6.1), (998.0, 6.1), (999.9, 6.1), (999.999, 6.1),
          (1000.0, 18.3), (1000.5, 18.3), (2500.0, 18.3), (4999.9, 18.3),
          (5000.0, 42.7), (5001.0, 42.7), (5002.0, 42.7), (5002.5, 42.7),
          (9972.0, 42.7), (9980.0, 42.7), (9999.0, 42.7), (9999.999, 42.7),
          (10000.0, 54.9), (10001.0, 54.9), (1.0e5, 54.9)]


def check_fixed():
    for acft, cb in EXPECT:
        for erodible in (True, False):
            r = bh.von_thun_gillette(acft * ACFT, HW, erodible=erodible)
            assert abs(r["bavg_m"] - (2.5 * HW + cb)) < 1e-9, (acft, erodible, r["bavg_m"], cb)
            tf_h = 0.015 * HW if erodible else 0.020 * HW + 0.25
            assert abs(r["tf_s"] - tf_h * 3600.0) < 1e-9, (acft, erodible, r["tf_s"])
            assert r["z"] == 0.5
    print("OK (Cb on both sides of 1,000 / 5,000 / 10,000 ac-ft, {} volumes)".format(len(EXPECT)))


def check_runner():
    try:
        from nd_breach_runner.core import breach as runner
    except Exception as e:  # noqa: BLE001 - the Runner is in this repository: never a skip
        print("FAIL: the Runner's core/breach.py does not import ({}: {})".format(
            type(e).__name__, e))
        return False
    assert abs(runner.ACFT - ACFT) < 1e-9, runner.ACFT
    assert bh.VTG_ACFT_M3 == runner.ACFT, (bh.VTG_ACFT_M3, runner.ACFT)
    vols = [a for a, _cb in EXPECT]
    # a fine sweep through the three bands where the old limits disagreed, plus the limits
    # themselves in m3 as the Runner computes them
    for lo, hi in ((997.0, 1000.0), (5000.0, 5002.0), (9972.0, 10000.0)):
        vols += [lo + (hi - lo) * i / 200.0 for i in range(201)]
    n = 0
    for acft in vols:
        for vw in (acft * runner.ACFT, acft * runner.ACFT * (1 - 1e-12),
                   acft * runner.ACFT * (1 + 1e-12)):
            for hw in (2.0, HW, 15.0):
                for erodible in (True, False):
                    b, tf, z = runner.vtg_1990(vw, hw, erodible)
                    p = bh.von_thun_gillette(vw, hw, erodible=erodible)
                    assert p["bavg_m"] == b, (acft, vw, hw, erodible, p["bavg_m"], b)
                    assert p["tf_s"] == tf, (acft, vw, hw, erodible, p["tf_s"], tf)
                    assert p["z"] == z, (p["z"], z)
                    n += 1
    print("OK (von_thun_gillette == the Runner's vtg_1990 in {} cases, from {})".format(
        n, os.path.dirname(os.path.dirname(runner.__file__))))
    return True


if __name__ == "__main__":
    check_fixed()
    ok = check_runner()
    sys.stdout.flush()
    sys.exit(0 if ok else 1)
