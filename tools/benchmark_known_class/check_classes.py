"""Known-class benchmark: classify the benchmark dams again and compare.

Pure Python (numpy + the plugins' pure modules); no QGIS, no GDAL. Three checks, each loud:

1. REPRODUCE - every fixture (fixtures/<dam_id>.json, the receptors tool 8 classified, written
   by DamSafety Screening with DAMSAFETY_RECEPTOR_DUMP set) classified again with the
   fixture's own settings gives the class tool 8 recorded in it. Fails if not.
2. REGRESSION - every classifier / preset below gives the class AND the drivers recorded in
   expected.json. Any change fails, up or down. This is the gate a rewrite (QGIS-free classify,
   screening or Runner prep) has to pass: it must change no class and no driver.
3. OFFICIAL - the ND class of record (dams.csv expected_nd_class, from the ND DWR dam database)
   against the suite's ND class ("nd" below, from the fixture) and against the end-to-end
   screening class (screening_results.csv, when given). A dam the suite puts BELOW its class of
   record is UNDER-CLASSIFIED and fails, every time, with no allowance list. A dam put above
   is reported (over-classification is conservative; it is listed, not failed).

    python3 check_classes.py                         all three checks, exit 0 only if all pass
    python3 check_classes.py --regression-only       1 and 2 (the CI gate; see the README)
    python3 check_classes.py --damit ~/damit         also every DamIT Catalog preset
    python3 check_classes.py --update                write expected.json from the current code
                                                     (only after the change is reviewed)

Exit status: 0 pass; 1 a fixture does not reproduce or a class / driver changed; 2 under-
classification against the class of record (and nothing else failed); 3 both.
"""
import argparse
import csv
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
FIXTURES = os.path.join(HERE, "fixtures")
EXPECTED = os.path.join(HERE, "expected.json")
DAMS_CSV = os.path.join(HERE, "dams.csv")
SCREENING_CSV = os.path.join(HERE, "screening_results.csv")

M_PER_FT = 0.3048
RANK = {"Low": 0, "Medium": 1, "Significant": 1, "High": 2}


def _screening_core():
    core = os.path.join(REPO, "DamSafety_Screening", "core")
    if core not in sys.path:
        sys.path.insert(0, core)
    import hazard as hz                     # noqa: E402  (core/hazard.py, pure)
    import nd_hazard as nh                  # noqa: E402
    return hz, nh


# ------------------------------------------------------------------ the DWR presets --
# Every lethality method of Screening tool 8 (core/hazard.METHODS) except the user-digitised
# curve (no curve ships), and the options that move a class: the judgment zone as High (the
# old reading), borderline-higher (ND 5.3(f)), and a missing AADT assumed Low instead of Medium.
# "nd" is the suite's default and the one compared with the class of record.
ND_PRESETS = {
    "nd": {},
    "nd_judgment_zone_high": {"judgment_zone_high": True},
    "nd_borderline_higher": {"borderline_higher": True},
    "nd_unknown_aadt_low": {"unknown_aadt": "Low", "unknown_trains": "Low"},
    "nd_all_wet": {"method": "all_wet"},
    "nd_1d_rule": {"method": "depth_rule_1d"},
    "cmp_feinberg": {"method": "feinberg"},
}


def nd_classify(fx, preset):
    """core/hazard.classify_nd on a fixture with the fixture's settings changed by `preset`."""
    hz, nh = _screening_core()
    s = dict(fx["settings"])
    s.update(preset)
    method = s.get("method") or nh.LETHALITY_CHARTS
    if method == "depth_rule_1d":
        method = nh.LETHALITY_1D
    charts = nh.load_charts() if method == nh.LETHALITY_CHARTS else None
    dd = [nh.DownstreamDam(**d) for d in fx.get("downstream_dams") or []]
    c = hz.classify_nd(fx["structures"], fx["roads"], fx["railroads"], method=method,
                       wet_m=float(s["wet_m"]), rule_depth_ft=float(s.get("rule_depth_ft") or 2.0),
                       unknown_aadt=s.get("unknown_aadt") or nh.MEDIUM, charts=charts,
                       lifelines=fx.get("lifelines") or [], downstream_dams=dd,
                       unknown_trains=s.get("unknown_trains") or nh.MEDIUM,
                       judgment_zone_high=bool(s.get("judgment_zone_high")),
                       borderline_higher=bool(s.get("borderline_higher")))
    return c


def nd_drivers(c):
    """The drivers of an ND classification: the governing findings as kind + section with a
    count, and the counts tool 8 writes (n_bldg_wet / lethal, roads, rail, lifelines, dams)."""
    gov = {}
    for f in c.governing:
        k = "{} {}".format(f.kind, f.section)
        gov[k] = gov.get(k, 0) + 1
    keep = ("habitable_wet", "habitable_lethal", "judgment_zone", "roads_overtopped",
            "railroads_overtopped", "lifelines_wet", "downstream_dams")
    return {"governing": dict(sorted(gov.items())),
            "counts": {k: int(c.counts.get(k, 0)) for k in keep},
            "data_gaps": len(c.data_gaps), "borderline_raised": bool(c.borderline_raised)}


# ------------------------------------------------------------------ DamIT presets --
ZONE_TO_M1 = {"high danger": "high_danger", "judgment": "judgment", "low danger": "low_danger"}


def damit_receptors(fx):
    """The fixture's receptors in the DamIT Catalog receptor language (evaluate.py docstring).

    The chart zone of each structure (m1_zone) is the zone the ND classifier read from the same
    REG_05.2026 table, so DamIT's M1 sees the same chart decision. Nothing is invented: no
    occupants (M5 lives-in-jeopardy then reports a missing input), rail 'active' = the ND rule
    (every overtopped railroad counts), a lifeline found wet = impacted."""
    hz, nh = _screening_core()
    c = nd_classify(fx, {})
    zone = {f.id: f.zone for f in c.findings if f.kind == "structure"}
    out = []
    for s in fx["structures"]:
        v = s.get("vel_ms")
        dv = s.get("dv_m2s")
        out.append({"id": "s:" + str(s["id"]), "receptor_type": "structure", "wet": True,
                    "habitable": bool(s.get("habitable", True)),
                    # DamIT's canonical token is "home" (methods.json); "house" matches no method
                    # and silently drops the structure from the home-keyed life-safety rules.
                    "structure_type": "mobile_home" if s.get("mobile") else "home",
                    "depth_ground_ft": s["depth_m"] / M_PER_FT,
                    "velocity_fps": None if v is None else v / M_PER_FT,
                    "dv_ft2s": None if dv is None else dv / (M_PER_FT * M_PER_FT),
                    "m1_zone": ZONE_TO_M1.get(zone.get(str(s["id"])) or "", None)})
    for r in fx["roads"]:
        rc = r.get("road_class") or ""
        name = r.get("name") or ""
        cls = "interstate" if hz.is_interstate(rc, name) else ("us_state_highway" if hz.is_us_state(rc, name)
                                                              else "")
        out.append({"id": "r:" + str(r["id"]), "receptor_type": "road", "wet": True,
                    "depth_ground_ft": r["depth_m"] / M_PER_FT, "road_class": cls,
                    "aadt": nh.aadt_value(r.get("aadt")), "sole_access": bool(r.get("sole_access"))})
    for r in fx["railroads"]:
        out.append({"id": "t:" + str(r["id"]), "receptor_type": "rail", "wet": True, "active": True,
                    "depth_ground_ft": r["depth_m"] / M_PER_FT,
                    "trains_per_day": nh.trains_value(r.get("trains_per_day")),
                    "passenger": bool(r.get("passenger"))})
    for r in fx.get("lifelines") or []:
        out.append({"id": "l:" + str(r["id"]), "receptor_type": "lifeline", "wet": True, "impacted": True,
                    "depth_ground_ft": r["depth_m"] / M_PER_FT, "kind": r.get("kind") or ""})
    return out


def damit_presets(damit_root):
    sys.path.insert(0, damit_root)
    from damit_catalog import catalog as C, evaluate as E      # noqa: E402
    cat = C.load()
    ids = sorted(cat.presets) if hasattr(cat, "presets") else []
    if not ids:
        with open(os.path.join(damit_root, "damit_catalog", "data", "presets.json"), encoding="utf-8") as fh:
            ids = sorted(json.load(fh)["presets"])
    return cat, E, ids


def damit_classify(cat, E, pid, fx):
    config = cat.resolve_preset(pid)
    sim = fx.get("sim") or {}
    summ = {"model_dimension": "2D", "hydraulics_source": "breach_model", "analysis": "breach_model",
            "storage_acft": sim.get("store_af")}
    res = E.evaluate(cat, config, damit_receptors(fx), summ, run_date="2026-11-23")
    drv = sorted({"{}:{}".format(d.get("method"), (str(d.get("receptor") or "")).split(":")[0])
                  for d in res.get("driving") or []})
    flags = sorted({"{}{}".format(f.get("code"), ":" + str(f["method"]) if f.get("method") else "")
                    for f in res.get("flags") or []})
    return res.get("suggested_label") or "Not determined", {"driving": drv, "flags": flags}


# ------------------------------------------------------------------ run --
def load_fixtures():
    out = {}
    if not os.path.isdir(FIXTURES):
        return out
    for n in sorted(os.listdir(FIXTURES)):
        if n.endswith(".json"):
            with open(os.path.join(FIXTURES, n), encoding="utf-8") as fh:
                fx = json.load(fh)
            out[fx["dam_id"]] = fx
    return out


def load_csv(path):
    if not path or not os.path.isfile(path):
        return {}
    with open(path, newline="", encoding="utf-8") as fh:
        return {r["dam_id"]: r for r in csv.DictReader(fh)}


def results(fixtures, damit_root=None):
    """{dam_id: {preset: {"class": .., "drivers": ..}}} for every fixture and preset."""
    out = {}
    dam = None
    if damit_root:
        dam = damit_presets(damit_root)
    for did, fx in fixtures.items():
        row = {}
        for pid, pre in ND_PRESETS.items():
            c = nd_classify(fx, pre)
            row[pid] = {"class": c.hazard_class, "drivers": nd_drivers(c)}
        if dam:
            cat, E, ids = dam
            for pid in ids:
                cls, drv = damit_classify(cat, E, pid, fx)
                row["damit:" + pid] = {"class": cls, "drivers": drv}
        out[did] = row
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--damit", default="", help="a DamIT checkout: also run every DamIT Catalog preset")
    ap.add_argument("--screening", default=SCREENING_CSV,
                    help="end-to-end screening classes (run_screening.py output); '' to skip")
    ap.add_argument("--regression-only", action="store_true",
                    help="checks 1 and 2 only; the under-classifications are still printed")
    ap.add_argument("--update", action="store_true", help="write expected.json from the current code")
    a = ap.parse_args(argv)

    fixtures = load_fixtures()
    dams = load_csv(DAMS_CSV)
    if not fixtures:
        print("no fixtures in {}".format(FIXTURES))
        return 1
    got = results(fixtures, a.damit or None)
    fail_reg, fail_under = [], []

    # 1 REPRODUCE
    for did, fx in fixtures.items():
        rec = (fx.get("result") or {}).get("hz_class")
        if rec != got[did]["nd"]["class"]:
            fail_reg.append("{}: fixture recorded {} from tool 8, classified again {}".format(
                did, rec, got[did]["nd"]["class"]))

    # 2 REGRESSION
    if a.update:
        old = {}
        if os.path.isfile(EXPECTED):
            with open(EXPECTED, encoding="utf-8") as fh:
                old = json.load(fh)
        keep_damit = not a.damit
        new = {did: dict(row) for did, row in got.items()}
        if keep_damit:                     # do not drop DamIT rows when updated without --damit
            for did, row in (old.get("dams") or {}).items():
                for pid, v in row.items():
                    if pid.startswith("damit:") and did in new:
                        new[did].setdefault(pid, v)
        payload = {"schema": "known-class-expected/1", "dams": new}
        with open(EXPECTED, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=1, sort_keys=True)
        print("expected.json written: {} dams".format(len(new)))
    else:
        exp = {}
        if os.path.isfile(EXPECTED):
            with open(EXPECTED, encoding="utf-8") as fh:
                exp = json.load(fh).get("dams") or {}
        for did, row in got.items():
            erow = exp.get(did)
            if erow is None:
                fail_reg.append("{}: not in expected.json (new fixture? run --update after review)".format(did))
                continue
            for pid, v in row.items():
                e = erow.get(pid)
                if e is None:
                    if not pid.startswith("damit:"):
                        fail_reg.append("{} {}: no expected value".format(did, pid))
                    continue
                if e["class"] != v["class"]:
                    fail_reg.append("{} {}: class CHANGED {} -> {}".format(did, pid, e["class"], v["class"]))
                elif e["drivers"] != v["drivers"]:
                    fail_reg.append("{} {}: drivers changed (class {} kept): {} -> {}".format(
                        did, pid, v["class"], json.dumps(e["drivers"], sort_keys=True),
                        json.dumps(v["drivers"], sort_keys=True)))
        # a baseline dam whose fixture is gone must fail even without --damit (the CI gate runs
        # --regression-only): a rewrite that stops classifying a dam would otherwise pass silently.
        for did in exp:
            if did not in got:
                fail_reg.append("{}: in expected.json, fixture missing (dam dropped out of the baseline)".format(did))

    # 3 OFFICIAL
    scr = load_csv(a.screening) if a.screening else {}
    lines, over = [], []
    for did, d in sorted(dams.items()):
        want = d.get("expected_nd_class")
        for label, cls in (("suite ND class (fixture)", got.get(did, {}).get("nd", {}).get("class")),
                           ("end-to-end screening", (scr.get(did) or {}).get("hz_class"))):
            if not cls:
                continue
            if cls not in RANK:
                lines.append("{} {}: {} gave no class ({}) - class of record {}".format(
                    did, d.get("name"), label, cls, want))
                fail_under.append(did)
            elif RANK[cls] < RANK[want]:
                lines.append("UNDER-CLASSIFIED {} {}: {} {} < class of record {} ({})".format(
                    did, d.get("name"), label, cls, want, d.get("dwr_hazard_class")))
                fail_under.append(did)
            elif RANK[cls] > RANK[want]:
                over.append("{} {}: {} {} > class of record {}".format(did, d.get("name"), label, cls, want))

    print("Known-class benchmark: {} fixtures, {} benchmark dams, {} end-to-end results".format(
        len(fixtures), len(dams), len(scr)))
    for s in fail_reg:
        print("FAIL " + s)
    for s in lines:
        print("!!!! " + s)
    if over:
        print("over-classified (conservative, listed only):")
        for s in over:
            print("     " + s)
    under = sorted(set(fail_under))
    print("under-classified dams: {}{}".format(len(under), (" - " + ", ".join(under)) if under else ""))
    code = (1 if fail_reg else 0) | (0 if a.regression_only or not under else 2)
    print("RESULT: " + ("PASS" if code == 0 else "FAIL ({})".format(
        ", ".join(x for x, b in (("regression", fail_reg), ("under-classification",
                                                             under and not a.regression_only)) if b))))
    return code


if __name__ == "__main__":
    sys.exit(main())
