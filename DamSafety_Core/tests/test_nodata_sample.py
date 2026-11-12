"""damsuite.nodata_sample: the 20 m NoData-receptor rule (Nick, 12 Nov 2026).

    python3 DamSafety_Core/tests/test_nodata_sample.py
"""
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))

from DamSafety_Core.damsuite import nodata_sample as ND  # noqa: E402

US_FT = 0.3048006096012192


def test_threshold_is_strictly_greater_than_20():
    assert ND.NODATA_NEIGHBOURHOOD_MIN_CELL_M == 20.0
    assert ND.neighbourhood_from_cell(20.0) is False
    assert ND.neighbourhood_from_cell(20.0000001) is True
    assert ND.neighbourhood_from_cell(19.999) is False
    assert ND.neighbourhood_from_cell(None) is False
    assert ND.nodata_policy(10.0)["neighbourhood_allowed"] is False
    assert ND.nodata_policy(20.0)["neighbourhood_allowed"] is False
    assert ND.nodata_policy(30.0)["neighbourhood_allowed"] is True


def test_finite_zero_is_not_nodata():
    assert ND.is_nodata(0.0) is False
    assert ND.is_nodata(0) is False
    assert ND.is_wet(0.0) is False
    assert ND.is_nodata(float("nan")) is True
    assert ND.is_nodata(None) is True
    assert ND.is_wet(1.5) is True


def test_option_c_peaks_from_different_cells():
    # east 3 ft / 1 fps, north 2 ft / 4 fps
    got = ND.peak_wet_cells([3.0, 2.0, float("nan"), 0.0], [1.0, 4.0, 9.0, 8.0])
    assert got == (3.0, 4.0, 2)


def test_resolve_10m_and_20m_score_dry_when_neighbour_wet():
    peak = (3.0, 4.0, 2)
    for cell in (10.0, 20.0):
        r = ND.resolve_nodata(True, peak, ND.nodata_policy(cell))
        assert r["sampling"] == ND.SAMPLING_CENTRE_NODATA_DRY
        assert r["flag"] == ND.FLAG_DRY_REVIEW
        assert r["neighbourhood"] is False
        assert r["depth"] == 0.0
        assert r["cell_m"] == cell


def test_resolve_30m_uses_neighbourhood_peak():
    peak = (3.0, 4.0, 2)
    r = ND.resolve_nodata(True, peak, ND.nodata_policy(30.0))
    assert r["sampling"] == ND.SAMPLING_NEIGHBOURHOOD
    assert r["flag"] == ND.FLAG_NEIGHBOURHOOD
    assert r["neighbourhood"] is True
    assert r["depth"] == 3.0 and r["velocity"] == 4.0


def test_resolve_no_wet_neighbour_is_dry_and_unflagged():
    for cell in (10.0, 20.0, 30.0):
        r = ND.resolve_nodata(True, None, ND.nodata_policy(cell), plain_sampling="plain")
        assert r["sampling"] == "plain"
        assert r["flag"] is None
        assert r["depth"] == 0.0


def test_finite_centre_is_not_resolved_here():
    r = ND.resolve_nodata(False, (3.0, 4.0, 2), ND.nodata_policy(30.0), plain_sampling="centre")
    assert r["sampling"] == "centre" and r["flag"] is None and r["depth"] is None


def test_nonsquare_cell_uses_the_larger_side():
    assert abs(ND.cell_size_m(15.0, 25.0) - 25.0) < 1e-12
    assert ND.neighbourhood_from_cell(ND.cell_size_m(15.0, 25.0)) is True
    assert ND.neighbourhood_from_cell(ND.cell_size_m(15.0, 20.0)) is False


def test_cell_size_in_us_survey_feet_is_converted_to_metres():
    m50 = ND.cell_size_m(50.0, 50.0, linear_units=US_FT)
    m80 = ND.cell_size_m(80.0, 80.0, linear_units=US_FT)
    assert abs(m50 - 50.0 * US_FT) < 1e-12
    assert abs(m80 - 80.0 * US_FT) < 1e-12
    assert ND.neighbourhood_from_cell(m50) is False
    assert ND.neighbourhood_from_cell(m80) is True


def test_geographic_cell_uses_metres_per_degree():
    # 0.0002 deg at 47 N: lon is shorter than lat
    dx = dy = 0.0002
    lat = 47.0
    w, h = ND.cell_sides_m(dx, dy, geographic=True, lat=lat)
    assert abs(h - dy * ND.M_PER_DEG_LAT) < 1e-9
    assert abs(w - dx * ND.M_PER_DEG_LON_EQ * math.cos(math.radians(lat))) < 1e-9
    assert ND.cell_size_m(dx, dy, geographic=True, lat=lat) == max(w, h)


def test_neighbourhood_centres_are_the_3x3():
    # 30 m cells, origin (0, 150), north-up, 5x5; point at centre of cell (2,2)
    gt = (0.0, 30.0, 0.0, 150.0, 0.0, -30.0)
    xs, ys = ND.neighbourhood_centres(gt, 5, 5, [(75.0, 75.0)])
    assert len(xs) == 9
    assert (75.0, 75.0) in list(zip(xs, ys))
    assert (45.0, 75.0) in list(zip(xs, ys))
    assert (105.0, 105.0) in list(zip(xs, ys))


def test_notes():
    n = ND.note_for_flag(ND.FLAG_NEIGHBOURHOOD, cell_m=30.0)
    assert "nodata_neighbourhood" in n and "30 m" in n
    n = ND.note_for_flag(ND.FLAG_DRY_REVIEW, cell_m=10.0)
    assert "nodata_dry_review" in n and "10 m" in n
    assert ND.note_for_flag(None) is None


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print("ok", t.__name__)
    print("{} tests ok".format(len(tests)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
