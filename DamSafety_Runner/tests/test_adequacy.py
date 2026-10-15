"""Spillway adequacy (core/adequacy.py): CSV reading by header name (nd_pmp v0.2.0 / v0.3.0,
plain two-column), analytic routing checks on synthetic reservoirs, agreement with
flood.route_flood's no-breach routing, freeboard rules, bisection of the largest multiple."""
import math
import os
import shutil
import sys
import tempfile

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
from nd_breach_runner.core import adequacy as AD  # noqa: E402
from nd_breach_runner.core import breach as br, flood as fl  # noqa: E402
from nd_breach_runner.core.units import FT, ACFT, CFS  # noqa: E402
from nd_breach_runner.tests.registry import test  # noqa: E402

TMP = tempfile.mkdtemp(prefix="adequacy_")


def _w(name, text):
    p = os.path.join(TMP, name)
    with open(p, "w") as f:
        f.write(text)
    return p


def _const_area_table(z0, z1, area_ac):
    return _w("ss.csv", "elev_ft,storage_acft\n{},0\n{},{}\n".format(z0, z1, (z1 - z0) * area_ac))


@test
def adequacy_reads_by_header():
    v3 = _w("v3.csv", "# storm_id: s3\n# peak_cfs: 20\ntime_hr,flow_cfs,precip_in,excess_in\n"
            "1,10,0.5,0.1\n2,20,0.4,0.2\n3,5,0,0\n")
    v2 = _w("v2.csv", "# storm_id: s2\ntime_hr,precip_in,excess_in,flow_cfs\n"
            "1,0.5,0.1,10\n2,0.4,0.2,20\n3,0,0,5\n")
    plain = _w("plain.csv", "0,0\n1,10\n2,20\n3,5\n")
    named = _w("named.csv", "time_hr,flow_cfs\n0,0\n1,10\n2,20\n3,5\n")
    for p in (v3, v2, plain, named):
        t, q, meta = AD.read_inflow_csv(p)
        assert list(np.round(t / 3600.0, 6)) == [0, 1, 2, 3], (p, t)
        assert list(np.round(q / CFS, 6)) == [0, 10, 20, 5], (p, q / CFS)
    assert AD.read_inflow_csv(v3)[2]["storm_id"] == "s3"
    assert AD.read_inflow_csv(plain)[2]["storm_id"] == "plain"
    bad = _w("bad.csv", "time_hr,precip_in,excess_in\n1,1,1\n2,1,1\n")
    try:
        AD.read_inflow_csv(bad)
        raise AssertionError("no flow column accepted")
    except AD.AdequacyError:
        pass


@test
def adequacy_volume_balance_no_outflow():
    # constant-area reservoir, no spillway, no crest flow: rise = inflow volume / area
    area_ac, z0, top = 100.0, 1000.0, 1020.0
    ss = _const_area_table(z0 - 10, top + 30, area_ac)
    inflow = _w("tri.csv", "time_hr,flow_cfs\n0,0\n2,1000\n6,0\n")   # 3 h * 1000 cfs = 2000 cfs-hr
    res = AD.TableReservoir.from_csv(ss)
    t, q, _ = AD.read_inflow_csv(inflow)
    fi = fl.FloodInputs(t, q, None)
    r = AD.route_no_breach(res, fi, top * FT, z0 * FT)
    vol_acft = 1000 * 3 * 3600 * CFS / ACFT
    expect = z0 + vol_acft / area_ac
    assert abs(r["peak_wse_m"] / FT - expect) < 0.01, (r["peak_wse_m"] / FT, expect)


@test
def adequacy_linear_reservoir_analytic():
    # A dz/dt = Q - k (z - z0), z(0) = z0, constant Q: z = z0 + Q/k (1 - exp(-k t / A))
    area_ac, z0, top = 50.0, 500.0, 520.0
    k_cfs_per_ft = 200.0
    ss = _const_area_table(z0 - 5, top + 20, area_ac)
    rating = _w("lin.csv", "elev_ft,q_cfs\n{},0\n{},{}\n".format(z0, z0 + 40, 40 * k_cfs_per_ft))
    Q, hrs = 1000.0, 10.0
    inflow = _w("const.csv", "time_hr,flow_cfs\n0,{0}\n{1},{0}\n{2},0\n".format(Q, hrs, hrs + 0.001))
    res = AD.TableReservoir.from_csv(ss)
    t, q, _ = AD.read_inflow_csv(inflow)
    fi = fl.FloodInputs(t, q, fl.Spillway.from_csv(rating))
    r = AD.route_no_breach(res, fi, top * FT, z0 * FT)
    A = area_ac * 43560.0
    k = k_cfs_per_ft
    t_s = hrs * 3600.0
    expect = z0 + Q / k * (1 - math.exp(-k * t_s / A))
    assert abs(r["peak_wse_m"] / FT - expect) < 0.005, (r["peak_wse_m"] / FT, expect)


@test
def adequacy_matches_route_flood_no_breach():
    d = br.DamData(dam_id="SYN", dam_height_ft=30.0, storage_top_acft=1500.0, top_of_dam_ft=30.0,
                   surface_area_top_ac=120.0, crest_length_ft=800.0)
    rating = _w("weir.csv", "elev_ft,q_cfs\n" + "".join(
        "{},{}\n".format(20 + h, 3.0 * 100 * h ** 1.5) for h in np.arange(0, 15.01, 0.5)))
    inflow = _w("pmf.csv", "time_hr,flow_cfs\n0,0\n4,9000\n16,0\n")
    fi = fl.FloodInputs.from_files(inflow, rating, normal_pool_ft=20.0, crest_length_ft=800.0)
    ref = fl.route_flood(d, br.METHOD_PIPING if hasattr(br, "METHOD_PIPING") else "piping",
                         None, fi)
    out = AD.assess([inflow], 30.0, "high", spillway_csv=rating, crest_length_ft=800.0, dam=d,
                    bisect=False)
    row = out["rows"][0]
    assert abs(row["peak_pool_ft"] - ref.summary["peak_pool_ft_no_breach"]) <= 0.011, (
        row["peak_pool_ft"], ref.summary["peak_pool_ft_no_breach"])
    assert row["start_pool_ft"] == 20.0     # default: the rating's crest


@test
def adequacy_freeboard_rules_and_bisection():
    assert AD.required_freeboard_ft("high") == 1.0 and AD.required_freeboard_ft("low") == 3.0
    assert AD.required_freeboard_ft("medium", wind_ft=2.4) == 2.4
    assert AD.required_freeboard_ft("high", wind_ft=0.5) == 1.0
    assert AD.required_freeboard_ft("low", override_ft=2.0) == 2.0
    # constant area, no outflow: peak = z0 + m V / A, so the largest multiple is analytic
    area_ac, z0, top = 100.0, 1000.0, 1010.0
    ss = _const_area_table(z0 - 10, top + 30, area_ac)
    inflow = _w("tri2.csv", "time_hr,flow_cfs\n0,0\n2,4000\n6,0\n")
    vol_acft = 4000 * 3 * 3600 * CFS / ACFT
    out = AD.assess([inflow], top, "high", stage_storage_csv=ss, start_pool_ft=z0)
    row = out["rows"][0]
    expect_m = (top - 1.0 - z0) * area_ac / vol_acft
    assert abs(row["max_multiple_passed"] - expect_m) < 0.01, (row["max_multiple_passed"], expect_m)
    assert row["passes"] == (expect_m >= 1.0) and not row["max_multiple_capped"]
    v = out["verdict"]
    assert v["governing_storm"] == "tri2" and v["required_idf"].startswith("Probable Maximum")
    for p, f in (("a.csv", AD.write_csv), ("a.md", AD.write_report)):
        f(out, os.path.join(TMP, p))
    assert "DOES NOT PASS" in open(os.path.join(TMP, "a.md")).read() or row["passes"]


if __name__ == "__main__":
    from nd_breach_runner.tests.registry import TESTS
    failed = 0
    for fn in [f for f in TESTS if f.__module__ == __name__]:
        try:
            fn()
            print("ok  ", fn.__name__)
        except Exception as e:
            failed += 1
            import traceback
            traceback.print_exc()
            print("FAIL", fn.__name__, e)
    shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(1 if failed else 0)
