"""The Boann gate's rules on made-up scores (no terrain, no runs).

    python tools/validation/lake_delhi_2010/test_boann_gate.py
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import boann_gate as G  # noqa: E402

BASE = {"band_ft": 0.5, "peak_band": 0.05,
        "runs": {"boann_acc": {"mean_abs_ft": 1.28, "largest_ft": 2.67, "peak_outflow_cfs": 140855}}}


def run(mean_abs, largest, peak=140855, dry=0, name="boann_acc"):
    return {"runs": [{"run": name, "summary": {"marks": 8, "dry": dry, "mean_abs_ft": mean_abs,
                                               "largest_ft": largest},
                      "facts": {"peak_outflow_cfs": peak}}]}


def ok(score):
    return G.check(score, BASE)[0][1]


def test_rules():
    assert ok(run(1.30, 2.70))                         # inside the band
    assert ok(run(1.70, 3.50))                         # at the edges (0.5 / 1.0 ft)
    assert not ok(run(1.90, 2.70))                     # mean size worse by 0.62 ft
    assert not ok(run(1.30, 3.80))                     # largest worse by 1.13 ft
    assert not ok(run(1.30, 2.70, dry=1))              # a mark dry
    assert not ok(run(1.30, 2.70, peak=160000))        # the breach moved 14%
    assert not ok(run(1.30, 2.70, name="other"))       # the baseline run is missing
    better = G.check(run(0.50, 1.00), BASE)[0]
    assert better[1] and any("re-record" in ln for ln in better[2]), better


def test_make_and_baseline_file():
    d = tempfile.mkdtemp(prefix="gate_")
    with open(os.path.join(d, "as_observed.json"), "w") as f:
        json.dump({"model": {"cell_size_m": 30.0}, "output": {"folder": "runs/as_observed"},
                   "_run": "observed"}, f)
    paths = G.make(d)
    assert [os.path.basename(p) for p in paths] == ["boann_acc.json", "boann_sgc.json",
                                                    "boann_subfv.json", "boann_subli.json"]
    with open(paths[1]) as f:
        c = json.load(f)
    assert c["model"]["lisflood_exe"] == "gpu-sgc" and c["model"]["lisflood_sgc"] is True
    assert c["output"]["folder"] == "runs/boann_sgc" and c["model"]["cell_size_m"] == 30.0
    with open(paths[2]) as f:
        c = json.load(f)
    assert c["model"]["lisflood_exe"] == "gpu-subfv" and "lisflood_sgc" not in c["model"]
    assert c["output"]["folder"] == "runs/boann_subfv" and c["model"]["cell_size_m"] == 30.0
    with open(paths[3]) as f:
        c = json.load(f)
    assert c["model"]["lisflood_exe"] == "gpu-subli" and "subgrid_upwind" not in c["model"]
    assert c["output"]["folder"] == "runs/boann_subli"
    with open(G.BASELINE) as f:
        b = json.load(f)
    assert set(b["runs"]) == set(G.RUNS), b["runs"].keys()
    for r in b["runs"].values():
        assert r["mean_abs_ft"] > 0 and r["largest_ft"] > 0 and r["peak_outflow_cfs"] > 0


if __name__ == "__main__":
    test_rules()
    test_make_and_baseline_file()
    print("ok")
