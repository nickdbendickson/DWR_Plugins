"""BoannGrid culvert ratings (core/metalterrain/culvert.py): the HDS-5 governing head-discharge
rating is monotone, the submerged-inlet form matches a hand computation, the rating inverts
cleanly (round-trip Q), a bigger barrel passes more flow at the same head, and the governing
control switches between inlet and outlet as geometry dictates. Pure math - no GPU, no compiler.
"""
import math

from nd_breach_runner.core.metalterrain import culvert as CV
from nd_breach_runner.tests.registry import test


@test
def culvert_submerged_inlet_matches_hand_calc():
    """The submerged inlet-control head equals HW/D = c*(Q/(A*sqrt(D)))^2 + Y - 0.5*S by hand
    (circular concrete groove end, D=1.5 m, Q=8 m^3/s, S=0.01), in native English units."""
    c = CV.Culvert.preset("circular_concrete_groove", diameter_m=1.5, length_m=20.0, slope=0.01)
    d_ft = 1.5 / CV.FT
    a_ft2 = math.pi * d_ft ** 2 / 4.0
    q_cfs = 8.0 / CV.CFS
    hw_d = 0.0292 * (q_cfs / (a_ft2 * math.sqrt(d_ft))) ** 2 + 0.74 - 0.5 * 0.01
    want_m = hw_d * d_ft * CV.FT
    got = c._inlet_ft(q_cfs) * CV.FT
    assert abs(got - want_m) < 1e-6, (got, want_m)
    # at this geometry the inlet governs
    assert c.control(8.0) == "inlet"
    assert abs(c.headwater(8.0) - want_m) < 1e-6


@test
def culvert_rating_is_monotone():
    """Headwater rises with discharge (so the rating inverts uniquely)."""
    c = CV.Culvert.preset("circular_cmp_headwall", diameter_m=1.2, length_m=30.0, slope=0.005)
    hs = [c.headwater(q) for q in (0.5, 1, 2, 4, 8, 16, 32)]
    assert all(b > a for a, b in zip(hs, hs[1:])), hs


@test
def culvert_rating_round_trips():
    """discharge(headwater(Q)) recovers Q (bisection on the monotone governing head)."""
    c = CV.Culvert.preset("circular_concrete_square", diameter_m=1.0, length_m=25.0, slope=0.01)
    for q in (1.0, 3.5, 9.0, 20.0):
        hw = c.headwater(q, tailwater_m=0.3)
        back = c.discharge(hw, tailwater_m=0.3)
        assert abs(back - q) < 1e-3 * max(1.0, q), (q, back)


@test
def culvert_bigger_barrel_passes_more_flow():
    """At the same headwater, a larger culvert carries more discharge."""
    small = CV.Culvert.preset("circular_concrete_groove", diameter_m=1.0, length_m=20.0, slope=0.01)
    big = CV.Culvert.preset("circular_concrete_groove", diameter_m=2.0, length_m=20.0, slope=0.01)
    hw = 2.5
    assert big.discharge(hw) > small.discharge(hw) * 1.5, (small.discharge(hw), big.discharge(hw))


@test
def culvert_outlet_control_can_govern():
    """A long, rough, flat barrel under high tailwater is outlet-controlled, and its headwater
    exceeds the tailwater (energy is lost pushing flow through the barrel)."""
    c = CV.Culvert.preset("circular_cmp_headwall", diameter_m=0.9, length_m=80.0, slope=0.0005)
    q, tw = 3.0, 1.6
    assert c.control(q, tailwater_m=tw) == "outlet"
    assert c.headwater(q, tailwater_m=tw) > tw


if __name__ == "__main__":
    import sys
    fails = 0
    for fn in (culvert_submerged_inlet_matches_hand_calc, culvert_rating_is_monotone,
               culvert_rating_round_trips, culvert_bigger_barrel_passes_more_flow,
               culvert_outlet_control_can_govern):
        try:
            fn()
            print("ok    ", fn.__name__)
        except Exception:
            import traceback
            fails += 1
            print("FAIL  ", fn.__name__)
            traceback.print_exc()
    sys.exit(1 if fails else 0)
