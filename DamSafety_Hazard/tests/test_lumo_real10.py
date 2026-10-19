"""Lumo vReal.10 / vReal.11 fixes (Hazard 2.18.22).

    QT_QPA_PLATFORM=offscreen python3 DamSafety_Hazard/tests/test_lumo_real10.py
"""
import hashlib
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)

from qgis.core import QgsApplication  # noqa: E402
_app = QgsApplication([], False)
_app.setPrefixPath(os.environ.get("QGIS_PREFIX_PATH", "/home/box/miniforge3/envs/qgis334"), True)
_app.initQgis()

import numpy as np  # noqa: E402
from DamSafety_Hazard import sensitivity as SE  # noqa: E402
from DamSafety_Hazard import consequences as CQ  # noqa: E402
from DamSafety_Hazard import linear as LN  # noqa: E402
from DamSafety_Hazard import damage_curves as DC  # noqa: E402
from DamSafety_Hazard import rasimport as RI  # noqa: E402
from DamSafety_Hazard import classification as CL  # noqa: E402


def _rec(extra):
    return {"id": 2 if extra else 1, "habitable": "Yes", "dwelling_extra": extra,
            "max_depth": 4.0, "dv_max": 20.0, "arrival": 2.0, "persons": 2.5,
            "persons_basis": "NSI", "pop_night": 3, "pop_day": 3, "incremental": True,
            "area_ft2": 1500.0}


def test_ll_keys_and_extra_par():
    assert "dwelling_extra" in SE._LL_KEYS
    structs = [_rec(False), _rec(True)]
    cfg = {"lol": True, "warning_issued_hr": 0.0, "understanding": "vague",
           "severity_method": "graham", "population": "night", "high_severity": False}
    for r in structs:
        CQ.life_loss_estimate(r, 0.0)
    tot = CQ.totals(structs)
    assert tot["par"] == 3
    res = SE.run_consequences(structs, cfg)
    base = [r for r in res["rows"] if r["is_base"]][0]
    assert res["structures"] == 1
    assert abs(base["par"] - tot["par"]) < 1e-9, (base["par"], tot["par"])


def test_cn4_arrival_unknown_text():
    r = _rec(False)
    r["arrival"] = None
    CQ.life_loss_estimate(r, 0.0)
    assert "arrival unknown - treated as no warning" in r["severity"]
    assert r["warning_cat"] == CQ.WARN_NONE


def test_cn2_high_rate_on_full_par_documented():
    # no "people remaining" quantity exists: High rate applies to the full PAR
    r = _rec(False)
    CQ.life_loss_estimate(r, 0.0, high_severity=True)
    assert abs(r["lol"] - 3 * 0.75) < 1e-9


def test_cn1_tables_sanity_checksum():
    # ENGINEER: these checks only freeze the current values; check them against
    # USACE EGM 01-03 Tables 1-6 and Graham (1999) DSO-99-06 Table 7 themselves.
    assert len(CQ.EGM_DEPTHS) == 19 and np.all(np.diff(CQ.EGM_DEPTHS) > 0)
    assert set(CQ.EGM_0103) == {"one", "two", "split"}
    for s, c in CQ.EGM_0103.values():
        assert len(s) == len(c) == 19
        assert all(b >= a for a, b in zip(s, s[1:])) and all(b >= a for a, b in zip(c, c[1:]))
        assert all(0 <= v <= 100 for v in s + c)
    for (sev, warn, und), (rate, lo, hi) in CQ.GRAHAM_RATES.items():
        assert sev in (CQ.SEV_LOW, CQ.SEV_MEDIUM, CQ.SEV_HIGH)
        assert warn in (CQ.WARN_NONE, CQ.WARN_15_60, CQ.WARN_60)
        assert und in (None, "vague", "precise")
        assert 0 <= lo <= rate <= hi <= 1
    blob = json.dumps({"egm_depths": list(map(float, CQ.EGM_DEPTHS)), "egm": CQ.EGM_0103,
                       "graham": sorted([list(k) + list(v) for k, v in CQ.GRAHAM_RATES.items()],
                                        key=str)}, sort_keys=True, default=str)
    assert hashlib.sha256(blob.encode()).hexdigest() == \
        "3ecb8898049d0fca16a9717f9774fb2b6be01ec618f30bb6983143696a8a5443"


def test_cn3_damage_reason_excludes_extras():
    from DamSafety_Hazard.structures import OUT_JUDGMENT
    s = [{"outcome": OUT_JUDGMENT, "habitable": "Yes", "damage_usd": 100.0},
         {"outcome": OUT_JUDGMENT, "habitable": "Yes", "damage_usd": 50.0,
          "dwelling_extra": True}]
    nd = {"reasons": ["economic loss x"], "judgment": [], "struct_items": 0}
    try:
        CL.extra_criteria(nd, s, {}, {})
    except Exception:
        pass
    r = [x for x in nd["reasons"] if "economic loss" in x][0]
    assert "$100" in r and "$150" not in r, r


def test_ln1_motorists_same():
    r = {"aadt": 4800.0, "length_ft": 2640.0, "paved": "Yes"}
    opts = {"veh": True, "speed_mph": 40.0, "occupancy": 1.5}
    exp = 4800.0 / 24.0 * 0.5 / 40.0 * 1.5
    assert abs(LN.motorists(4800.0, 2640.0, 40.0, 1.5) - exp) < 1e-12
    assert abs(SE._persons(r, LN.CLASS_HIGH, opts) - exp) < 1e-12


def test_dc2_unsorted_warns():
    d = tempfile.mkdtemp()
    p = os.path.join(d, "c.csv")
    with open(p, "w") as fh:
        fh.write("occupancy,kind,depth_ft,percent\nCOM1,structure,4,40\nCOM1,structure,0,0\n")
    curves, probs = DC.load(p)
    assert curves is not None and any("not in increasing order" in x for x in probs)
    assert "deepest flooding" in DC.TEMPLATE


def test_rs1_plan_key_content():
    d = tempfile.mkdtemp()
    p = os.path.join(d, "p.hdf")
    with open(p, "wb") as fh:
        fh.write(b"A" * 5000)
    k1 = RI.plan_key(p)
    st = os.stat(p)
    with open(p, "wb") as fh:
        fh.write(b"B" * 5000)
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert RI.plan_key(p) != k1
    assert RI._CODE_DIGEST


if __name__ == "__main__":
    fails = 0
    for n, f in sorted(globals().items()):
        if n.startswith("test_") and callable(f):
            try:
                f()
                print("PASS", n)
            except Exception as e:
                import traceback
                traceback.print_exc()
                fails += 1
                print("FAIL", n, e)
    sys.stdout.flush()
    if fails:
        sys.exit(1)
    # all passed; os._exit(0) before the real-QgsApplication offscreen teardown segfaults at shutdown
    os._exit(0)
