"""Offline tests for core/streamstats.py on saved USGS StreamStats responses (no network, no QGIS,
no GDAL: the module is loaded from its file, so core/__init__ is not imported).

Fixtures (fixtures_streamstats/), retrieved 2026-10-29 from the post-January-2026 services
(ss-delineate 1.2.0, ss-hydro 1.4.0, nssservices):
  both_dams/           DWR 4031 (48.2985029, -101.4370363) and DWR 2801 (46.98337, -97.39113),
                       outlets moved onto the stream line
  unsnapped_nid_2801/  the NID point of DWR 2801 (off the network: a 0.07 mi2 sliver basin)
  unsnapped_dwr_4031/  the DWR point of DWR 4031 (off the network: no snap at all)

Run:  python -m pytest DamSafety_PMP/tests/test_streamstats.py   (or run this file)
"""
import importlib.util
import json
import shutil
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
FIX = HERE / "fixtures_streamstats"
_spec = importlib.util.spec_from_file_location("pmp_streamstats", HERE.parent / "core" / "streamstats.py")
ss = importlib.util.module_from_spec(_spec)
sys.modules["pmp_streamstats"] = ss
_spec.loader.exec_module(ss)

EXPECT = {  # cfs, SIR 2015-5096 Region C (GC140) via NSS
    "DWR4031": {"PK50AEP": 11.3, "PK2AEP": 85.3, "PK1AEP": 106.0, "PK0_2AEP": 154.0},
    "DWR2801": {"PK50AEP": 66.0, "PK2AEP": 667.0, "PK1AEP": 847.0, "PK0_2AEP": 1300.0},
}
ENVELOPE = {"DWR4031": 1480.0, "DWR2801": 27400.0}
POINTS = {"DWR4031": (48.2985029, -101.4370363), "DWR2801": (46.98337, -97.39113)}


def _no_net(*a):
    raise AssertionError("network used in an offline test")


def _client(tmp_path, src="both_dams", **kw):
    d = tmp_path / "cache"
    shutil.copytree(FIX / src, d)
    kw.setdefault("offline", True)
    return ss.StreamStatsClient(d, fetcher=_no_net, min_interval_s=0, **kw)


def _run(tmp_path, label, **kw):
    return _client(tmp_path, **kw).run("ND", *POINTS[label], label + "_")


@pytest.mark.parametrize("label", ["DWR4031", "DWR2801"])
def test_offline_run_reproduces_peaks(tmp_path, label):
    r = _run(tmp_path, label)
    rows = {p["code"]: p for p in ss.peak_table(r)}
    assert [p["aep"] for p in ss.peak_table(r)] == ["50%", "20%", "10%", "4%", "2%", "1%", "0.2%"]
    for code, q in EXPECT[label].items():
        assert rows[code]["q_cfs"] == pytest.approx(q)
        assert rows[code]["rr_code"] == "GC140"
        assert 50 < rows[code]["asep_pct"] < 85
        lo, hi = rows[code]["pi90_lo_cfs"], rows[code]["pi90_hi_cfs"]
        assert lo < q < hi and lo * hi == pytest.approx(q * q, rel=1e-6)   # symmetric in log space
    assert ss.envelope_cfs(r) == pytest.approx(ENVELOPE[label])
    assert r["shape_area_sqmi_service"] == pytest.approx(r["geodesic_area_sqmi_local"], rel=0.01)
    assert all(x["cached"] for x in r["requests"])
    assert r["service_versions"]["ss-delineate"].startswith("not checked")


def test_2801_inputs_noncontributing_and_timing(tmp_path):
    r = _run(tmp_path, "DWR2801")
    miss = {s["rr_code"]: s["missing_basin_characteristics"] for s in r["skipped_regression_regions"]}
    assert "GC1928" in miss and "LAT_OUT" in miss["GC1928"]
    p = ss.pmf_inputs(r, cn=80)
    assert p["area_mi2"] == pytest.approx(16.6, abs=0.1)
    assert p["noncontrib_mi2_suggested"] == pytest.approx(4.46, abs=0.01)
    assert "ISOLAKESUM" in p["noncontrib_source"]
    assert p["contributing_mi2_est"] == pytest.approx(12.14, abs=0.05)
    k, n = ss.timing(p, "kirpich"), ss.timing(p, "nrcs")
    assert k["method"] == "kirpich" and k["lag_hr"] == pytest.approx(0.6 * k["tc_hr"])
    assert 5 < k["tc_hr"] < 15
    assert n["method"] == "nrcs" and n["tc_hr"] == pytest.approx(n["lag_hr"] / 0.6)
    assert "Kirpich" in k["note"] and "NRCS" in n["note"]
    nocn = ss.timing(ss.pmf_inputs(r, None), "nrcs")
    assert nocn["lag_hr"] is None and "curve number" in nocn["note"]


def test_timing_formulas():
    # Kirpich: Tc(min) = 0.0078 L^0.77 S^-0.385 (L ft, S ft/ft); 1 mi at 10 ft/mi
    L, S = 5280.0, 10.0 / 5280.0
    assert ss.kirpich_tc_hr(1.0, 10.0) == pytest.approx(0.0078 * L ** 0.77 * S ** -0.385 / 60)
    # NRCS lag (h) = l^0.8 (S+1)^0.7 / (1900 Y^0.5), S = 1000/CN - 10, l ft, Y %
    s = 1000 / 75 - 10
    assert ss.nrcs_lag_hr(1.0, 75, 2.0) == pytest.approx(L ** 0.8 * (s + 1) ** 0.7 / (1900 * 2 ** 0.5))


def test_pi90_from_asep():
    se = ss.asep_to_log_se(59.9)
    lo, hi = ss.approx_pi90(154.0, 59.9)
    assert hi / 154.0 == pytest.approx(10 ** (1.645 * se))
    assert lo == pytest.approx(154.0 / 10 ** (1.645 * se))


def test_pmf_check_levels():
    sanity = {"peaks": [{"code": "PK0_2AEP", "q_cfs": 1300.0, "pi90_hi_cfs": 3232.0}],
              "crippen_bue_envelope_cfs": 27400.0}
    low = ss.pmf_check(900.0, sanity)
    assert [lvl for lvl, _ in low] == ["WARNING", "WARNING"] and "BELOW" in low[0][1]
    near = ss.pmf_check(2000.0, sanity)
    assert near[0][0] == "WARNING" and "90%" in near[0][1]
    mid = ss.pmf_check(10000.0, sanity)
    assert mid[0][0] == "OK" and mid[1][0] == "WARNING" and "Crippen" in mid[1][1]
    high = ss.pmf_check(40000.0, sanity)
    assert [lvl for lvl, _ in high] == ["OK", "OK"]
    assert ss.pmf_check(10.0, {}) == []


def test_unsnapped_nid_point_is_not_on_stream(tmp_path):
    c = _client(tmp_path, "unsnapped_nid_2801")
    with pytest.raises(ss.NotOnStreamError, match="Move the point onto the channel") as ei:
        c.run("ND", 46.98438182, -97.39091754, "DWR2801_")
    msg = str(ei.value)
    assert msg.startswith("Not on the stream network") and len(msg) < 260
    # the service's own warning goes to .detail (for the log), once per sentence
    parts = [p for p in ei.value.detail.replace("StreamStats: ", "").split(". ") if p]
    assert ei.value.detail.startswith("StreamStats: ") and len(parts) == len(set(parts))


def test_unsnapped_dwr_point_fails_before_basin_characteristics(tmp_path):
    c = _client(tmp_path, "unsnapped_dwr_4031")
    with pytest.raises(ss.NotOnStreamError, match="Move the point onto the channel"):
        c.run("ND", 48.30256, -101.44042, "DWR4031_dwrpoint")
    assert len(c.log) == 1 and "delineate" in c.log[0]["url"]


def test_not_on_stream_is_a_streamstats_error():
    assert issubclass(ss.NotOnStreamError, ss.StreamStatsError)


def test_checksum_tamper_detected(tmp_path):
    c = _client(tmp_path)
    f = next(c.cache_dir.glob("DWR4031_delineate_*[0-9a-f].json"))
    f.write_bytes(f.read_bytes().replace(b"09010008", b"09010009"))
    with pytest.raises(ss.StreamStatsError, match="checksum"):
        c.run("ND", *POINTS["DWR4031"], "DWR4031_")


def test_offline_cache_miss(tmp_path):
    c = _client(tmp_path)
    with pytest.raises(ss.OfflineCacheMiss):
        c.run("ND", 47.5, -100.0, "NOPE_")


def test_live_path_with_mock_fetcher_caches_and_paces(tmp_path):
    """A fake 'network' that replays the saved responses: same answers, request log, cache."""
    src = FIX / "both_dams"
    calls = []

    def fake(method, url, body, headers):
        calls.append((method, url, headers.get("User-Agent")))
        key = ss.StreamStatsClient._key(method, url, body)
        hit = [p for p in src.glob("DWR2801_*_%s.json" % key)]
        if hit:
            return 200, hit[0].read_bytes()
        raise AssertionError("unexpected request " + url)

    c = ss.StreamStatsClient(tmp_path / "c", fetcher=fake, min_interval_s=0, plugin_version="test")
    r = c.run("ND", *POINTS["DWR2801"], "DWR2801_", check_versions=False)
    assert {p["code"]: p["q_cfs"] for p in ss.peak_table(r)}["PK0_2AEP"] == pytest.approx(1300.0)
    assert calls and all(ua and ua.startswith("DamSafety_PMP/test") for _m, _u, ua in calls)
    assert not any(e["cached"] for e in r["requests"])
    n = len(calls)
    c2 = ss.StreamStatsClient(tmp_path / "c", fetcher=_no_net, offline=True, min_interval_s=0)
    c2.run("ND", *POINTS["DWR2801"], "DWR2801_")       # second run: all from the new cache
    assert len(calls) == n


def test_cancel_stops_before_first_request(tmp_path):
    c = _client(tmp_path, should_cancel=lambda: True)
    with pytest.raises(ss.Cancelled):
        c.run("ND", *POINTS["DWR4031"], "DWR4031_")


def test_progress_reported(tmp_path):
    seen = []
    c = _client(tmp_path, progress=lambda m, pct: seen.append((m, pct)))
    c.run("ND", *POINTS["DWR4031"], "DWR4031_")
    assert seen and seen[-1][1] == 100 and [p for _m, p in seen] == sorted(p for _m, p in seen)


def test_save_run_writes_basin_layer_values_and_provenance(tmp_path):
    r = _run(tmp_path, "DWR2801")
    out = ss.save_run(r, tmp_path / "out", "DWR2801", cn=None, timing_method="kirpich",
                      plugin_version="0.4.0")
    ws = json.loads(out["watershed"].read_text())
    assert ws["type"] == "FeatureCollection" and ws["features"][0]["geometry"]["type"] in (
        "Polygon", "MultiPolygon")
    basin = json.loads(out["basin"].read_text())
    assert basin["timing"]["method"] == "kirpich"
    assert "not from StreamStats" in basin["curve_number"]
    assert basin["sanity"]["crippen_bue_envelope_cfs"] == pytest.approx(27400)
    prov = json.loads(out["provenance"].read_text())
    assert "2015-5096" in json.dumps(prov["citations"])
    assert prov["point_lonlat"] == [POINTS["DWR2801"][1], POINTS["DWR2801"][0]]
    assert prov["query_utc"] and prov["requests"] and all(q["response_sha256"] for q in prov["requests"])
    assert "2026" in prov["service_note"]
    assert set(prov["outputs_sha256"]) == {out["watershed"].name, out["basin"].name}


def test_check_pmf_run_writes_report_and_copies(tmp_path):
    r = _run(tmp_path, "DWR2801")
    out = ss.save_run(r, tmp_path / "ss", "DWR2801")
    rows = [{"storm_id": "local_72h", "peak_cfs": 18000.0}, {"storm_id": "gen_24h", "peak_cfs": 9000.0}]
    msgs, written = ss.check_pmf_run(rows, out["basin"], tmp_path / "run", area_mi2=10.0)
    levels = [lvl for lvl, _ in msgs]
    assert levels[0] == "OK"                        # 18000 is far above Q0.2 = 1300
    assert any("Crippen" in t for _l, t in msgs)    # below the 27400 envelope: a warning
    assert any("differs from the StreamStats basin" in t for _l, t in msgs)
    rep = json.loads(written["check"].read_text())
    assert rep["pmf_peak_cfs"] == 18000.0 and rep["pmf_storm_id"] == "local_72h"
    assert (tmp_path / "run" / out["basin"].name).is_file()
    assert (tmp_path / "run" / out["provenance"].name).is_file()
    msgs, _w = ss.check_pmf_run([], out["basin"], tmp_path / "run2")
    assert msgs[0][0] == "WARNING"


if __name__ == "__main__":
    sys.exit(pytest.main(["-q", "-p", "no:cacheprovider", __file__]))
