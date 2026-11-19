"""3.21.0: Xu & Zhang (2009) breach methods and the range of the peak-flow regressions.
Reference values are worked by hand from the coefficient tables in the HEC-RAS Hydraulic
Reference Manual ("Estimating Breach Parameters"), not from the code under test."""
import math

from nd_breach_runner.core import breach as br
from nd_breach_runner.core.units import ACFT, CFS
from nd_breach_runner.tests.registry import test

G = 9.80665


def _hand(a, p, r, e, vw, hw, hd):
    return a * (hd / 15.0) ** p * (vw ** (1.0 / 3.0) / hw) ** r * math.exp(e)


@test
def xu_zhang_2009_matches_hand_worked_values():
    vw, h = 1.0e6, 10.0
    # homogeneous fill, piping, medium erodibility
    bavg = h * _hand(0.787, 0.133, 0.652, -0.226 - 0.389 - 0.14, vw, h, h)
    btop = h * _hand(1.062, 0.092, 0.508, -0.089 - 0.239 - 0.062, vw, h, h)
    tf_h = _hand(0.304, 0.707, 1.228, -0.189 - 0.611 - 0.564, vw, h, h)
    b, z, tf = br.xu_zhang_2009(vw, h, h, h, "piping", "homogeneous", "medium")
    assert abs(b - bavg) < 1e-9 * bavg, (b, bavg)
    assert abs(z - round((btop - bavg) / h, 2)) < 1e-12, z
    assert abs(tf / 3600.0 - tf_h) < 1e-9, tf
    q = math.sqrt(G * vw ** (5.0 / 3.0)) * _hand(0.175, 0.199, -1.274,
                                                 -0.649 - 1.039 - 0.375, vw, h, h)
    assert abs(br.xu_zhang_2009_peak(vw, h, h) - q) < 1e-9 * q
    # 15.7 m, 0.66H:1V, 0.99 h, 342 m3/s (worked once by hand)
    assert abs(b - 15.73) < 0.01 and z == 0.66 and abs(tf_h - 0.986) < 0.001
    assert abs(q - 341.8) < 0.1
    # core wall, overtopping, high erodibility picks the other table columns
    b2, z2, _ = br.xu_zhang_2009(vw, h, h, h, "overtopping", "core_wall", "high")
    assert abs(b2 - h * _hand(0.787, 0.133, 0.652, -0.041 + 0.149 + 0.291, vw, h, h)) < 1e-9
    for bad in (dict(dam_type="earth"), dict(erodibility="very"), dict(mode="seepage")):
        kw = dict(mode="piping", dam_type="homogeneous", erodibility="medium")
        kw.update(bad)
        try:
            br.xu_zhang_2009(vw, h, h, h, **kw)
            assert False, bad
        except br.BreachInputError:
            pass


@test
def xu_zhang_methods_route_with_froehlich_formation_time():
    d = br.DamData(dam_id="XZ", dam_height_ft=32.8, storage_top_acft=1.0e6 / ACFT,
                   top_of_dam_ft=1000.0, surface_area_top_ac=80.0)
    f08 = br.make_hydrograph(d, br.METHOD_PIPING)
    hy = br.make_hydrograph(d, br.METHOD_XZ_PIPING)
    p = hy.params
    assert p["failure_mode"] == "piping"
    assert p["formation_time_hr"] == f08.params["formation_time_hr"]
    assert p["xu_zhang_2009_failure_time_hr"] > p["formation_time_hr"]
    assert "Xu & Zhang (2009)" in p["breach_parameters"]
    assert any("as HEC advises" in n for n in hy.notes)
    ov = br.BreachOverrides(xz_dam_type="core_wall", xz_erodibility="high")
    hy2 = br.make_hydrograph(d, br.METHOD_XZ_OVERTOPPING, ov)
    assert hy2.params["failure_mode"] == "overtopping"
    assert hy2.params["avg_breach_width_ft"] > p["avg_breach_width_ft"]
    assert br.METHOD_XZ_OVERTOPPING in br.OVERTOPPING_METHODS


@test
def regression_peak_range_is_reported_for_every_method():
    d = br.DamData(dam_id="R", dam_height_ft=25.0, storage_top_acft=500.0,
                   top_of_dam_ft=1850.0, surface_area_top_ac=60.0, crest_width_ft=12.0,
                   us_slope_h=3.0, ds_slope_h=3.0)
    for m in (br.METHOD_PIPING, br.METHOD_VTG, br.METHOD_XZ_PIPING, br.METHOD_TR60):
        hy = br.make_hydrograph(d, m)
        peaks = hy.params["regression_peaks_cfs"]
        assert set(peaks) >= {"Froehlich (1995)", "MacDonald & Langridge-Monopolis (1984)",
                              "NRCS TR-60"}, (m, peaks)
        lo, hi = min(peaks.values()), max(peaks.values())
        assert hy.params["regression_peak_range_cfs"].startswith("{:,.0f} to {:,.0f}".format(
            lo, hi)), hy.params["regression_peak_range_cfs"]
    vw, hw = 500.0 * ACFT, 25.0 * 0.3048
    assert abs(br.mlm_1984_peak(vw, hw) - 1.154 * (vw * hw) ** 0.412) < 1e-9
    assert hy.params["mlm_1984_peak_cfs"] == round(br.mlm_1984_peak(vw, hw) / CFS, 0)
    # USBR (1982) envelope is reported as a separate upper-bound ceiling, Q = 19.1 hw^1.85
    assert abs(br.usbr_1982_peak(hw) - 19.1 * hw ** 1.85) < 1e-9
    assert hy.params["usbr_1982_envelope_peak_cfs"] == round(br.usbr_1982_peak(hw) / CFS, 0)
    assert "USBR (1982) envelope" in hy.params["regression_peak_range_cfs"]
    # the envelope is a ceiling, not part of the best-fit range it is reported beside
    assert hy.params["usbr_1982_envelope_peak_cfs"] >= min(peaks.values())
    # Azmi (2026) data-fusion peak is in the best-fit range of a routed run (it needs the width,
    # so it is absent from the TR-60 run above but present for Froehlich piping)
    hp = br.make_hydrograph(d, br.METHOD_PIPING)
    assert "Azmi (2026) data fusion" in hp.params["regression_peaks_cfs"]
    assert hp.params["azmi_2026_peak_cfs"] == round(
        hp.params["regression_peaks_cfs"]["Azmi (2026) data fusion"], 0)


@test
def azmi_2026_data_fusion_blends_its_three_components():
    """Azmi (2026), Nat Hazards 122:499 Table 5: Qp = 0.3048 F16 + 0.4804 XZ9 + 0.1674 Z20.
    Reference values worked by hand from the component equations (Froehlich 2016b, Xu & Zhang
    2009, Zhong 2020), not from the code under test."""
    vw, hw, hb, hd, wave = 5000.0 * ACFT, 20.0, 20.0, 22.0, 80.0
    g = 9.80665
    # Froehlich 2016b, piping (km = 1), hb > 6.1 m so kh = (hb/6.1)^(1/8)
    kh = (hb / 6.1) ** (1.0 / 8.0)
    f16 = 0.0175 * 1.0 * kh * math.sqrt(g * hw * vw * hb ** 2 / wave)
    assert abs(br.froehlich_2016_peak(vw, hw, hb, wave, "piping") - f16) < 1e-6 * f16
    # Zhong 2020, homogeneous (HD): (a,b,c,d) = (-1.58,-0.76,0.1,-4.55)
    z20 = (math.sqrt(g) * hw ** -0.5 * vw * (vw ** (1.0 / 3.0) / hw) ** -1.58
           * (hw / hb) ** -0.76 * hd ** 0.1 * math.exp(-4.55))
    assert abs(br.zhong_2020_peak(vw, hw, hb, hd, core_wall=False) - z20) < 1e-6 * z20
    xz9 = br.xu_zhang_2009_peak(vw, hw, hd, "piping", "homogeneous", "medium")
    az = 0.3048 * f16 + 0.4804 * xz9 + 0.1674 * z20
    assert abs(br.azmi_2026_peak(vw, hw, hb, hd, wave, "piping", "homogeneous", "medium") - az) < 1e-6 * az
    # a core-wall dam picks the CD branch of Zhong and changes the result
    assert br.zhong_2020_peak(vw, hw, hb, hd, core_wall=True) != z20


@test
def older_breach_methods_keep_their_processing_index():
    """Saved Processing models store the METHOD enum index: the new methods go at the end."""
    old = ("piping", "piping_orifice", "overtopping", "froehlich1995_piping",
           "froehlich1995_overtopping", "mlm1984", "vtg1990", "tr60", "tr60_curvilinear", "user")
    assert br.METHODS[:len(old)] == old, br.METHODS
    assert br.METHODS[len(old):] == (br.METHOD_XZ_PIPING, br.METHOD_XZ_OVERTOPPING)


@test
def regression_range_names_equations_and_always_flags_routed_peak():
    """M7: the printed range names every equation, the routed peak is always compared against
    both ends of the range and the USBR (1982) envelope (not only past 1.5x), and a note states
    the Xu & Zhang (2009) dam-type / erodibility basis. Display/label text only: the
    regression_peaks_cfs keys and values do not change."""
    d = br.DamData(dam_id="R", dam_height_ft=25.0, storage_top_acft=500.0,
                   top_of_dam_ft=1850.0, surface_area_top_ac=60.0, crest_width_ft=12.0,
                   us_slope_h=3.0, ds_slope_h=3.0)
    hy = br.make_hydrograph(d, br.METHOD_PIPING)
    peaks = hy.params["regression_peaks_cfs"]
    rng = hy.params["regression_peak_range_cfs"]
    lo, hi = min(peaks.values()), max(peaks.values())
    # hard constraint: the named key set is unchanged (a key/value edit would show here)
    assert set(peaks) == {"Froehlich (1995)", "MacDonald & Langridge-Monopolis (1984)",
                          "Xu & Zhang (2009), piping", "NRCS TR-60",
                          "Azmi (2026) data fusion"}, peaks
    # the range still starts with "lo to hi" and still carries the USBR envelope
    assert rng.startswith("{:,.0f} to {:,.0f}".format(lo, hi)), rng
    assert "USBR (1982) envelope" in rng
    # every equation is now named in the printed range; the bare "(N equations)" is gone
    for name in peaks:
        assert name in rng, (name, rng)
    assert "equations)" not in rng, rng
    # the USBR "upper-bound screen" label is reworded
    assert "upper-bound screen" not in rng, rng
    assert "upper-bound ceiling" in rng, rng
    # this run's routed peak sits INSIDE 1.5x of the range, yet the routed-vs-range note fires
    q = hy.peak_cfs
    assert lo / 1.5 <= q <= 1.5 * hi, (q, lo, hi)
    rvr = [n for n in hy.notes if "routed breach peak" in n]
    assert len(rvr) == 1, hy.notes
    assert "the low end" in rvr[0] and "the high end" in rvr[0]
    assert "USBR (1982) envelope" in rvr[0]
    assert "1.5x" not in rvr[0]                      # the always-on line, not the 1.5x flag
    # a basis note states the default Xu & Zhang (2009) dam type and erodibility
    assert any("Xu & Zhang (2009)" in n and "homogeneous / zoned fill" in n
               and "medium erodibility" in n and "(the defaults)" in n
               for n in hy.notes), hy.notes
    # overriding the basis changes that note and drops "(the defaults)"
    ov = br.BreachOverrides(xz_dam_type="core_wall", xz_erodibility="high")
    hy2 = br.make_hydrograph(d, br.METHOD_XZ_OVERTOPPING, ov)
    basis = [n for n in hy2.notes if "regression range uses the Xu & Zhang (2009)" in n]
    assert len(basis) == 1, hy2.notes
    assert "core wall dam" in basis[0] and "high erodibility" in basis[0]
    assert "(the defaults)" not in basis[0]
