"""damsuite/loss.py: a flooded structure with no known arrival time must be counted as having
NO warning, not as the best (>60 min) band. The best band carries the smallest fatality rate,
so defaulting unknown arrivals to it understated loss of life - the one direction the suite's
own rule forbids. Unknown arrivals now go to UNKNOWN_WARNING_BAND ("under15", matching the
Runner's consequences.warning_class) and are counted in arrival_unknown / par_arrival_unknown so
the assumption is reported, never silent. Pure Python; no QGIS needed.

    python3 DamSafety_Hazard/tests/test_loss_unknown_arrival.py
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)

from DamSafety_Hazard.damsuite import loss as LO  # noqa: E402


def test_unknown_arrival_counts_as_no_warning():
    # the known-band behaviour is unchanged
    assert LO.warning_band(5) == "under15"
    assert LO.warning_band(30) == "15to60"
    assert LO.warning_band(600) == "over60"
    assert LO.warning_band(None) == ""
    # a NaN arrival used to slip through both < comparisons and land in "over60"
    assert LO.warning_band(float("nan")) == ""
    assert LO.UNKNOWN_WARNING_BAND == "under15"

    rows = [{"depth_ft": 14, "velocity_fts": 8, "pop2amu65": 3} for _ in range(10)]  # no arrival
    est = LO.estimate(rows)
    assert est["by_band"]["under15"] == 30.0, est["by_band"]
    assert est["by_band"]["over60"] == 0.0, est["by_band"]
    assert est["arrival_unknown"] == 10 and est["par_arrival_unknown"] == 30.0
    assert "warning time unknown" in "\n".join(LO.estimate_text(est))

    # with a signed table the unknown arrivals take the under15 (conservative) rate, not over60
    signed = {"signed_off_by": "eng", "rates": {("high", "under15"): 0.9,
              ("high", "15to60"): 0.5, ("high", "over60"): 0.2}}
    assert LO.estimate(rows, rates=signed)["fatalities"] == 27.0   # was 6.0 before the fix


if __name__ == "__main__":
    test_unknown_arrival_counts_as_no_warning()
    print("ok test_loss_unknown_arrival")
