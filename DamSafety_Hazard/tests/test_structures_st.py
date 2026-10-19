"""ST.1–ST.8 structure inventory fixes (Lumo structures audit).

    QT_QPA_PLATFORM=offscreen python3 DamSafety_Hazard/tests/test_structures_st.py

Needs PyQGIS (inits a headless QgsApplication).
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
HAZARD = os.path.dirname(HERE)
ROOT = os.path.dirname(HAZARD)
sys.path.insert(0, ROOT)

from qgis.core import QgsApplication  # noqa: E402
_app = QgsApplication([], False)
_app.setPrefixPath(os.environ.get("QGIS_PREFIX_PATH", "/home/box/miniforge3/envs/qgis334"), True)
_app.initQgis()

from DamSafety_Hazard import structures as ST  # noqa: E402
from DamSafety_Hazard import consequences as CQ  # noqa: E402
from DamSafety_Hazard import access as AC  # noqa: E402


def _rec(rid, *, habitable="Yes", persons=3, depth=2.0, camp=None, street="",
         geom=None, extra=False, access_cut=False):
    src = {}
    if street:
        src = {"addr_text": street, "nena": True}
    if camp:
        src = dict(src, camp=camp, nena=False)
    return {"id": rid, "habitable": habitable, "persons": persons, "max_depth": depth,
            "dv_max": 10.0, "arrival": 1.0, "dwelling_extra": extra, "geom": geom,
            "sources": [src] if src else [], "camp": camp, "zone": None,
            "access_cut": access_cut, "access_unknown": False, "access_raw_note": "",
            "area_ft2": 1000.0}


def test_st6_bool_coercion():
    r = ST.mobile_rules_from_cfg({"mobile_shape": "false", "mobile_lot_units": "0"})
    assert r["mobile_shape"] is False, r
    assert r["mobile_lot_units"] is False, r
    r = ST.mobile_rules_from_cfg({"mobile_shape": "true", "mobile_lot_units": 1})
    assert r["mobile_shape"] is True and r["mobile_lot_units"] is True
    assert ST.mobile_rules_from_cfg({"mobile_shape": True})["mobile_shape"] is True
    # unknown string keeps the default (True for mobile_shape)
    assert ST.mobile_rules_from_cfg({"mobile_shape": "maybe"})["mobile_shape"] is True


def test_st8_normalize_in_duplicate():
    class P(object):
        def __init__(self, x, y):
            self._x, self._y = x, y
        def x(self): return self._x
        def y(self): return self._y
    # stored pts are (x, y) tuples; the candidate is a point with .x()/.y()
    src = {"family": "OSM", "addr_key": "146th Ave SE", "nsi": None, "pt": (0.0, 0.0)}
    other = [{"family": "OSM", "addr_key": "146th Avenue Southeast", "nsi": None,
              "pt": (1.0, 0.0)}]
    assert ST._is_duplicate(other, src, P(1.0, 0.0), dup_dist=10.0)
    other2 = [{"family": "OSM", "addr_key": "12 Main St Apt 2", "nsi": None, "pt": (1.0, 0.0)}]
    assert not ST._is_duplicate(other2, src, P(1.0, 0.0), dup_dist=10.0)


def test_st1_consequences_and_access_skip_extras():
    keep = _rec(1, persons=3, depth=5.0, access_cut=True)
    extra = _rec(2, persons=3, depth=5.0, extra=True, access_cut=True)
    CQ.life_loss_estimate(keep, warning_issued_hr=0.0)
    CQ.life_loss_estimate(extra, warning_issued_hr=0.0)
    assert keep.get("par") and keep["par"] > 0
    assert extra.get("par") is None and extra.get("lol") is None
    tot = CQ.totals([keep, extra])
    assert abs(tot["par"] - (keep.get("par") or 0)) < 1e-9
    stats = AC.access_stats([keep, extra], edges=10, edges_cut=2)
    assert stats["structures"] == 1 and stats["cut"] == 1


def test_st3_camp_not_collapsed_by_footprint():
    class G(object):
        def intersects(self, other): return True
        def touches(self, other): return False
    house = _rec(1, street="", persons=3, geom=G())
    camp = _rec(2, street="", persons=3, camp={"sites": 5}, geom=G())
    n = ST.collapse_dwellings([house, camp])
    assert n == 0, n
    assert not house.get("dwelling_extra") and not camp.get("dwelling_extra")
    h1 = _rec(3, street="", persons=3, geom=G())
    h2 = _rec(4, street="", persons=3, geom=G())
    n = ST.collapse_dwellings([h1, h2])
    assert n >= 1
    assert h1.get("dwelling_extra") or h2.get("dwelling_extra")


def test_st1_collapse_zeros_persons_again():
    a = _rec(1, street="100 Main St", persons=3)
    b = _rec(2, street="100 Main Street", persons=3)
    b["dwelling_extra"] = True
    b["persons"] = 3
    ST.collapse_dwellings([a, b])
    extras = [r for r in (a, b) if r.get("dwelling_extra")]
    kept = [r for r in (a, b) if not r.get("dwelling_extra")]
    assert len(extras) == 1 and extras[0]["persons"] == 0
    assert len(kept) == 1 and kept[0]["persons"] == 3


def test_h4_evaluate_structures_no_address_collapse():
    """H4: evaluate_structures must NOT collapse habitable buildings by street
    address. Three distinct habitable buildings >30 ft apart sharing one 911
    address (mobile-home park / apartment complex / multi-home farmstead -
    routine in rural ND) must all keep their persons so TM-11 lives-in-jeopardy
    counts all three. The sibling ND_Dam_Hazard_Helper removed this call at
    2.16.0 (same-building duplicates are already merged spatially by
    build_inventory). The collapse_dwellings() definition is kept for
    compatibility / direct callers; it is simply no longer invoked here.
    """
    import re
    src = open(os.path.join(HAZARD, "structures.py"), encoding="utf-8").read()
    m = re.search(r"\ndef evaluate_structures\b", src)
    assert m, "evaluate_structures not found"
    nxt = re.search(r"\ndef ", src[m.start() + 1:])
    body = src[m.start():m.start() + 1 + nxt.start()] if nxt else src[m.start():]
    calls = [ln for ln in body.splitlines() if ln.strip().startswith("collapse_dwellings(")]
    assert not calls, "evaluate_structures still collapses dwellings: {}".format(calls)
    assert ("H4" in body) or ("not applied (audit)" in body), "H4 audit note missing"
    # definition kept for compatibility and direct callers (pipeline, tests)
    assert re.search(r"\ndef collapse_dwellings\b", src), "collapse_dwellings def removed"
    assert callable(ST.collapse_dwellings)


def test_st7_addr_not_street_is_documented():
    # source-level: _ADDR_NOT_STREET still lists addr_id / address_id, and describe
    # only sets address from street_idx / key_idx (regression lock)
    assert "addr_id" in ST._ADDR_NOT_STREET and "address_id" in ST._ADDR_NOT_STREET
    src = open(os.path.join(HAZARD, "structures.py"), encoding="utf-8").read()
    assert "street_parts or key_parts" in src
    assert "ST.7" in src or "addr_id" in src


def main():
    tests = [test_st6_bool_coercion, test_st8_normalize_in_duplicate,
             test_st1_consequences_and_access_skip_extras, test_st3_camp_not_collapsed_by_footprint,
             test_st1_collapse_zeros_persons_again,
             test_h4_evaluate_structures_no_address_collapse,
             test_st7_addr_not_street_is_documented]
    failed = 0
    for fn in tests:
        try:
            fn()
            print("ok   ", fn.__name__)
        except Exception as e:
            failed += 1
            print("FAIL ", fn.__name__, ":", e)
            import traceback; traceback.print_exc()
    print("\n{} tests, {} failed".format(len(tests), failed))
    return failed


if __name__ == "__main__":
    raise SystemExit(main())
