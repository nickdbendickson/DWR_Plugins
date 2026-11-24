"""Boann's real-dam gate: the Lake Delhi 2010 case run through Boann's engines (the CPU build of
the GPU kernels) must stay within a band of the recorded baseline at the USGS high-water marks.

    <QGIS python> tools/validation/lake_delhi_2010/boann_gate.py make CASE_FOLDER
        writes boann_acc.json, boann_sgc.json, boann_subfv.json and boann_subli.json beside the
        case's as_observed.json
    <QGIS python> tools/validation/lake_delhi_2010/boann_gate.py check CASE_FOLDER
        reads CASE_FOLDER/Lake_Delhi_2010_Score.json (score.py) and boann_baseline.json here

A run FAILS the gate when, over the 8 marks below the dam, its mean size (mean |model - surveyed|)
or its largest difference grows past the baseline by more than the band, a mark comes out dry,
or the breach peak moves more than PEAK_BAND. Getting closer to the survey by more than the band
passes with a note to re-record the baseline (an accuracy change should be seen, then kept).
The baseline is per engine at the case's 30 m cell; see boann_baseline.json for where each
number came from. Not LISFLOOD-FP: the LISFLOOD-FP runs are validation.yml's report."""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BASELINE = os.path.join(HERE, "boann_baseline.json")
RUNS = {
    "boann_acc": {"lisflood_exe": "gpu"},
    "boann_sgc": {"lisflood_exe": "gpu-sgc", "lisflood_sgc": True, "sgc_upstream_sqmi": 347.0,
                  "sgc_min_area_km2": 2.0, "sgc_channel_n": 0.035},
    # sub-grid FV at its default coarsening factor (r = 4: 120 m cells over the 30 m case)
    "boann_subfv": {"lisflood_exe": "gpu-subfv"},
    # sub-grid local inertial at its default r = 4 as well (flux weighting off, its default)
    "boann_subli": {"lisflood_exe": "gpu-subli"},
}


def make(case):
    with open(os.path.join(case, "as_observed.json"), encoding="utf-8") as f:
        base = json.load(f)
    out = []
    for name, model in RUNS.items():
        cfg = json.loads(json.dumps(base))
        cfg["model"].update(model)
        cfg["output"]["folder"] = "runs/" + name
        cfg["_run"] = "{} Boann engine: {}".format(base.get("_run", ""), model["lisflood_exe"]).strip()
        p = os.path.join(case, name + ".json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=1)
        out.append(p)
    return out


def check(score, baseline):
    """[(run, ok, lines)] for every baseline run; a run missing from the score fails."""
    runs = {r["run"]: r for r in (score.get("runs") or [])}
    res = []
    for name, b in baseline["runs"].items():
        band = float(baseline.get("band_ft", 0.5))
        lines, ok = [], True
        r = runs.get(name)
        if r is None:
            res.append((name, False, ["no score for this run (did it finish?)"]))
            continue
        s, facts = r.get("summary") or {}, r.get("facts") or {}
        if s.get("dry"):
            ok = False
            lines.append("{} mark(s) dry".format(s["dry"]))
        for key, label, lim in (("mean_abs_ft", "mean size", band),
                                ("largest_ft", "largest", 2.0 * band)):
            got, want = s.get(key), b.get(key)
            if got is None:
                ok = False
                lines.append("{}: not scored".format(label))
                continue
            d = abs(got) - abs(want)
            if d > lim:
                ok = False
                lines.append("{} {:+.2f} ft vs baseline {:+.2f} ft: worse by {:.2f} ft (band {:.2f})"
                             .format(label, got, want, d, lim))
            elif d < -lim:
                lines.append("{} {:+.2f} ft vs baseline {:+.2f} ft: closer to the survey by {:.2f} ft; "
                             "re-record boann_baseline.json".format(label, got, want, -d))
            else:
                lines.append("{} {:+.2f} ft (baseline {:+.2f})".format(label, got, want))
        pk, pk0 = facts.get("peak_outflow_cfs"), b.get("peak_outflow_cfs")
        if pk is not None and pk0:
            rel = abs(pk - pk0) / pk0
            if rel > float(baseline.get("peak_band", 0.05)):
                ok = False
                lines.append("breach peak {:,.0f} cfs vs baseline {:,.0f} ({:.0%} apart)".format(pk, pk0, rel))
        res.append((name, ok, lines))
    return res


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("action", choices=("make", "check"))
    ap.add_argument("case")
    a = ap.parse_args(argv)
    if a.action == "make":
        for p in make(a.case):
            print("wrote", p)
        return 0
    with open(os.path.join(a.case, "Lake_Delhi_2010_Score.json"), encoding="utf-8") as f:
        score = json.load(f)
    with open(BASELINE, encoding="utf-8") as f:
        baseline = json.load(f)
    bad = 0
    for name, ok, lines in check(score, baseline):
        print("{} {}".format("PASS" if ok else "FAIL", name))
        for ln in lines:
            print("    " + ln)
        bad += not ok
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
