"""Known-class benchmark gate (tools/benchmark_known_class): every fixture reproduces the class
Screening tool 8 recorded, and every DWR preset gives the class and drivers in expected.json.
A rewrite of classify must keep this green. Under-classifications against the ND DWR class of
record are printed by the check (run it without --regression-only to fail on them).

Also checks core/receptor_dump.py (the fixture writer) round-trips what tool 8 passes."""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(REPO, "tools", "benchmark_known_class"))
sys.path.insert(0, os.path.join(HERE, "..", "core"))

import receptor_dump as RD              # noqa: E402
import nd_hazard as nh                  # noqa: E402
import check_classes as CC              # noqa: E402

# --- the fixture writer ---------------------------------------------------------------------------
assert RD.folder() == "" or os.environ.get(RD.ENV)


class _Null(object):                    # a QVariant NULL stand-in
    def isNull(self):
        return True


with tempfile.TemporaryDirectory() as td:
    p = RD.write("DWR-1/x", {"method": "acer_tm11", "wet_m": 0.03}, [{"id": "1", "depth_m": 1.0, "vel_ms": None,
                                                                    "mobile": False, "habitable": True}],
                 [{"id": "r", "depth_m": 0.2, "aadt": _Null(), "road_class": "", "name": "US 2"}], [], [],
                 [nh.DownstreamDam("DWR-2", "High", True, "")], result={"hz_class": "High"}, out_dir=td)
    assert os.path.basename(p) == "DWR-1_x.json", p
    with open(p, encoding="utf-8") as fh:
        rec = json.load(fh)
    assert rec["schema"] == RD.SCHEMA and rec["roads"][0]["aadt"] is None
    assert rec["downstream_dams"][0] == {"id": "DWR-2", "hazard_class": "High", "affected": True, "note": ""}
    assert rec["structures"][0]["vel_ms"] is None
    assert RD.write("x", {}, [], [], [], [], [], out_dir="") == "" or RD.folder()
    # a written fixture classifies again with the check's own code
    c = CC.nd_classify(rec, {})
    assert c.hazard_class == "High", c.hazard_class      # unknown velocity at a house + a High dam below
    d = CC.nd_drivers(c)
    assert d["counts"]["downstream_dams"] == 1 and d["counts"]["roads_overtopped"] == 1, d
    rec["structures"], rec["downstream_dams"] = [], []
    assert CC.nd_classify(rec, {}).hazard_class == "Medium"     # US 2 overtopped (5.2.1.2(b))

# --- the gate ---------------------------------------------------------------------------------
if not CC.load_fixtures():
    print("SKIP: no fixtures in tools/benchmark_known_class/fixtures")
    sys.exit(0)
code = CC.main(["--regression-only", "--screening", ""])
assert code == 0, "known-class benchmark: a fixture does not reproduce or a class / driver changed (see above)"
print("ok known-class benchmark")
