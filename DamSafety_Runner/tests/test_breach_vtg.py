"""VTG (Von Thun & Gillette 1990) worked-value pin at the Absaraka inputs (H3).
Reference values are worked by hand from the formula and the Absaraka-like dam inputs, not from
the code under test. A pin on existing behaviour: it changes no computed number."""
from nd_breach_runner.core import breach as br
from nd_breach_runner.core.units import ACFT, FT
from nd_breach_runner.tests.registry import test


@test
def vtg_1990_absaraka_worked_values():
    # Absaraka (Swan Buffalo Detention Dam 12, DWR-2801): 28 ft head, ~1,615 acre-ft.
    # 1,000 <= 1,615 < 5,000 acre-ft -> Cb = 18.3 m; easily erodible -> tf = 0.015 hw.
    hw_m = 28.0 * FT
    vw_m3 = 1615.496 * ACFT
    b, tf_s, z = br.vtg_1990(vw_m3, hw_m, erodible=True)
    # Bave = 2.5 hw + Cb (m); tf = 0.015 hw (h); side slope 0.5H:1V
    assert abs(b - (2.5 * hw_m + 18.3)) < 1e-9, b
    assert abs(tf_s / 3600.0 - 0.015 * hw_m) < 1e-12, tf_s
    assert z == 0.5
    # worked once by hand: 130.0 ft average breach width, 0.128 h formation time
    assert abs(b / FT - 130.0) < 0.1, b / FT
    assert abs(tf_s / 3600.0 - 0.128) < 0.001, tf_s / 3600.0

    # the routed sunny-day run reports the same displayed values at these dam inputs
    d = br.DamData(dam_id="DWR-2801", name="Swan Buffalo Detention Dam 12",
                   dam_height_ft=28.0, storage_top_acft=1615.496,
                   top_of_dam_ft=1069.117, surface_area_top_ac=154.5)
    p = br.make_hydrograph(d, br.METHOD_VTG).params
    assert p["avg_breach_width_ft"] == 130.0, p["avg_breach_width_ft"]
    assert p["formation_time_hr"] == 0.128, p["formation_time_hr"]
    assert p["side_slope_h"] == 0.5
    assert p["breach_parameters"] == "Von Thun & Gillette (1990), easily erodible"
