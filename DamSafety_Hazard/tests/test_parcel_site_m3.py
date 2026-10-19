"""M3 (v2.20.1 parcel-site / 911 habitability model, ported from the sibling
ND_Dam_Hazard_Helper).

NSI 2026 copies a parcel's RES record onto every building on the parcel, and
FEMA USA Structures copies the parcel's occupancy and PROP_ADDR onto every
outline. A farmstead therefore reads as the farmhouse PLUS a machine shed and
a barn that each carry the parcel's "RES1" record. Before the fix DamSafety's
_interpret mapped any RES* occtype straight to CAT_DWELLING, so the farmstead
counted as 3 habitable dwellings. structures._mark_parcel_sites now finds the
one building the parcel's 911 (ND GIS Hub) address sits on; the others become
outbuildings and are downgraded, so the farmstead counts as 1 habitable
dwelling.

Runs without QGIS: a stand-in qgis module is installed when real PyQGIS is not
importable (the parcel/site logic here is pure dict manipulation - no geometry
is evaluated because every record in the scenario is a footprint).

    python3 DamSafety_Hazard/tests/test_parcel_site_m3.py
"""
import os
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
HAZARD = os.path.dirname(HERE)
ROOT = os.path.dirname(HAZARD)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _install_fake_qgis():
    """Stand-in qgis / qgis.PyQt / osgeo so the plugin modules import without
    QGIS; any attribute asked of them is a permissive Fake class. Returns
    "real" when real PyQGIS is present (then nothing is installed)."""
    try:
        import qgis.core  # noqa: F401
        return "real"
    except Exception:
        pass

    class _Meta(type):
        def __getattr__(cls, name):
            if name.startswith("__"):
                raise AttributeError(name)
            return Fake()

    class Fake(metaclass=_Meta):
        def __init__(self, *a, **k):
            pass

        def __getattr__(self, name):
            if name.startswith("__"):
                raise AttributeError(name)
            return Fake()

        def __call__(self, *a, **k):
            return Fake()

        def __iter__(self):
            return iter(())

        def __len__(self):
            return 0

        def __bool__(self):
            return False

    classes = {}

    def _class(qualname):
        if qualname not in classes:
            classes[qualname] = _Meta(qualname.rsplit(".", 1)[-1], (Fake,), {})
        return classes[qualname]

    def _module(name):
        m = types.ModuleType(name)
        m.__getattr__ = lambda attr, _n=name: _class(_n + "." + attr)
        m.__path__ = []
        sys.modules[name] = m
        return m

    qgis = _module("qgis")
    for sub in ("core", "gui", "utils", "analysis", "processing"):
        setattr(qgis, sub, _module("qgis." + sub))
    pyqt = _module("qgis.PyQt")
    qgis.PyQt = pyqt
    for sub in ("QtCore", "QtGui", "QtWidgets", "QtXml", "QtNetwork", "sip"):
        setattr(pyqt, sub, _module("qgis.PyQt." + sub))
    try:
        import osgeo  # noqa: F401
    except Exception:
        osgeo = _module("osgeo")
        for sub in ("gdal", "ogr", "osr", "gdal_array"):
            setattr(osgeo, sub, _module("osgeo." + sub))
    return "fake"


MODE = _install_fake_qgis()
from DamSafety_Hazard import structures as ST  # noqa: E402
from DamSafety_Hazard import review as RV  # noqa: E402

PARCEL = "123 FARM RD | 58000"


def _fema_src():
    return {"family": "FEMA_STRUCTURES", "texts": [], "address": False, "nsi": None,
            "addr_key": None, "unit": "", "placement": None, "place_kind": None,
            "nena": False, "place_type": "", "fema": "Residential - Single Family Dwelling",
            "parcel": PARCEL}


def _nsi_res_src(fd_id):
    return {"family": "NSI", "texts": [], "address": False, "place_kind": None,
            "nena": False, "place_type": "", "fema": "",
            "nsi": {"occtype": "RES1", "fd_id": fd_id}}


def _addr911_src():
    return {"family": "NDGISHUB", "texts": [], "address": True, "addr_key": "123 farm rd",
            "unit": "", "placement": None, "place_kind": None, "nena": True,
            "place_type": "", "fema": "", "snapped": None, "nguid": "ND-911-1"}


def _osm_house_src():
    """A genuine OSM building outline loaded in the primary FEMA_STRUCTURES slot:
    fema_parcel is False, so its own building=house / addr:* ARE habitability evidence
    (describe() keeps them) and must survive _interpret's parcel suppression."""
    return {"family": "FEMA_STRUCTURES", "fema_parcel": False, "texts": ["building=house"],
            "address": True, "addr_key": "osm-a", "unit": "", "placement": None,
            "place_kind": None, "nena": False, "place_type": "", "fema": "", "nsi": None}


def _usa_parcel_src():
    """A FEMA USA Structures outline: occupancy + PROP_ADDR are the PARCEL's
    (fema_parcel True), shown but not habitability evidence; carries the parcel key."""
    return {"family": "USA_STRUCTURES", "fema_parcel": True, "texts": [], "address": False,
            "nsi": None, "addr_key": None, "unit": "", "placement": None, "place_kind": None,
            "nena": False, "place_type": "", "fema": "Residential - Single Family Dwelling",
            "parcel": PARCEL}


def _footprint(rid, sources, area_ft2=1500.0):
    fams = {s["family"] for s in sources}
    return {"id": rid, "footprint": True, "area_ft2": area_ft2, "families": fams,
            "notes": [], "sources": sources}


def _farmstead():
    """Farmhouse (911 address) + machine shed + barn, all on one parcel; the
    shed and barn carry the parcel's FEMA RES record and an NSI RES1 record."""
    farmhouse = _footprint(1, [_fema_src(), _addr911_src()])
    shed = _footprint(2, [_fema_src(), _nsi_res_src(777)])
    barn = _footprint(3, [_fema_src(), _nsi_res_src(778)])
    return [farmhouse, shed, barn]


def test_farmstead_is_one_dwelling_not_three():
    assert hasattr(ST, "_mark_parcel_sites"), "parcel-site model not ported"
    structs = _farmstead()
    grid = ST._Grid(200.0)
    # footprint-only scenario: join/lone distances are unused (no stray points),
    # merge_dist only gates the farmhouse's own 911 point (snapped is None)
    ST._mark_parcel_sites(structs, grid, merge_dist=10.0, join_dist=150.0, lone_dist=80.0)
    # only the farmhouse (the 911 anchor) is free of a site_anchor
    farmhouse, shed, barn = structs
    assert "site_anchor" not in farmhouse, farmhouse.get("site_anchor")
    assert shed.get("site_anchor") and shed["site_anchor"]["ids"] == [1], shed.get("site_anchor")
    assert barn.get("site_anchor") and barn["site_anchor"]["ids"] == [1], barn.get("site_anchor")

    for r in structs:
        ST._interpret(r)

    habitable = [r for r in structs if r["habitable"] == "Yes"]
    assert len(habitable) == 1, "expected 1 habitable dwelling, got {}: {}".format(
        len(habitable), [(r["id"], r["category"], r["habitable"]) for r in structs])
    assert farmhouse["category"] == ST.CAT_DWELLING, farmhouse["category"]
    # the shed and barn: NSI calls them residential, but the parcel's 911
    # address is on the farmhouse -> unverified outbuildings, with a note
    for r in (shed, barn):
        assert r["category"] == ST.CAT_UNVERIFIED, (r["id"], r["category"])
        assert r["habitable"] == "Unverified", (r["id"], r["habitable"])
        assert r.get("site_note"), (r["id"], "missing site_note")
        assert "outbuilding" in r["site_note"].lower(), r["site_note"]


def test_nsi_res_without_911_anchor_stays_a_dwelling():
    """Guard the primary-class path: with no 911 address anywhere on the
    parcel there is no anchor, so an NSI RES building is NOT downgraded."""
    shed = _footprint(2, [_fema_src(), _nsi_res_src(777)])
    barn = _footprint(3, [_fema_src(), _nsi_res_src(778)])
    structs = [shed, barn]
    ST._mark_parcel_sites(structs, ST._Grid(200.0), 10.0, 150.0, 80.0)
    for r in structs:
        assert "site_anchor" not in r, r.get("site_anchor")
        ST._interpret(r)
        assert r["category"] == ST.CAT_DWELLING, (r["id"], r["category"])
        assert not r.get("site_note"), r.get("site_note")


def test_osm_house_parcel_sibling_stays_a_dwelling():
    """Round-4 regression: a genuine OSM house (building=house, fema_parcel False) that
    is a parcel sibling of a 911 anchor must stay a habitable dwelling. The old
    family-name parcel filter stripped the OSM building's own evidence (because it sits
    in the FEMA_STRUCTURES slot) and the M3 outbuilding branch then demoted it to Not
    habitable - an unsafe under-count. The per-source fema_parcel suppression must keep
    the OSM evidence, so it classifies as a Dwelling, matching nd_hazard."""
    house_a = _footprint(1, [_osm_house_src(), _usa_parcel_src()])
    anchor_b = _footprint(2, [_usa_parcel_src(), _addr911_src()])
    structs = [house_a, anchor_b]
    ST._mark_parcel_sites(structs, ST._Grid(200.0), 10.0, 150.0, 80.0)
    assert house_a.get("site_anchor") and house_a["site_anchor"]["ids"] == [2], \
        house_a.get("site_anchor")
    assert "site_anchor" not in anchor_b, anchor_b.get("site_anchor")
    for r in structs:
        ST._interpret(r)
    # the OSM house keeps its building=house evidence -> Dwelling, NOT a silent drop
    assert house_a["category"] == ST.CAT_DWELLING, (house_a["category"], house_a.get("site_note"))
    assert house_a["habitable"] == "Yes", house_a["habitable"]
    assert house_a["category"] != ST.CAT_NOT_HABITABLE


def test_review_reason_outbuilding():
    rec = {"id": 2, "outcome": "Probable loss of life", "category": ST.CAT_UNVERIFIED,
           "site_note": "outbuilding: no source says it is habitable and ...",
           "sources": []}
    reasons = RV.review_reasons(rec)
    assert RV.REASON_OUTBUILDING in reasons, reasons
    # a lone 911 address gives the other reason
    rec_lone = dict(rec, lone_address=True)
    assert RV.REASON_LONE_ADDRESS in RV.review_reasons(rec_lone)


def main():
    tests = [test_farmstead_is_one_dwelling_not_three,
             test_nsi_res_without_911_anchor_stays_a_dwelling,
             test_osm_house_parcel_sibling_stays_a_dwelling,
             test_review_reason_outbuilding]
    failed = 0
    print("QGIS: {}".format(MODE))
    for fn in tests:
        try:
            fn()
            print("ok   ", fn.__name__)
        except Exception as e:
            failed += 1
            print("FAIL ", fn.__name__, ":", e)
            import traceback
            traceback.print_exc()
    print("\n{} tests, {} failed".format(len(tests), failed))
    return failed


if __name__ == "__main__":
    raise SystemExit(main())
