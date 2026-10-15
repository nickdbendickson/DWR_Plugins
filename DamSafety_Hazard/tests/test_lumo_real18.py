"""Lumo vReal.18 OP.1/OP.3/OP.4, OS.1/OS.2/OS.3, EVC.1/EVC.5 (Hazard 2.18.29).

    QT_QPA_PLATFORM=offscreen python3 DamSafety_Hazard/tests/test_lumo_real18.py
"""
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

from DamSafety_Hazard import offline_pack as OP  # noqa: E402
from DamSafety_Hazard import osm_parse as OS  # noqa: E402
from DamSafety_Hazard import evacuation as EV  # noqa: E402


def _line(tail):
    pts = [[-100.0 + i * 1e-4, 46.0] for i in range(60)] + [tail]
    return {"type": "Feature", "properties": {},
            "geometry": {"type": "LineString", "coordinates": pts}}


def test_op4_op3_full_geometry_uid_and_skip_count():
    db = os.path.join(tempfile.mkdtemp(), "p.sqlite")
    w = OP.PackWriter(db, (-101, 45, -99, 47))
    w.begin("roads", "Roads", "http://x")
    a, b = _line([-99.5, 46.5]), _line([-99.6, 46.6])
    assert len(__import__("json").dumps(a["geometry"], sort_keys=True)[:400]) == 400
    n = w.add([a, b, a, {"type": "Feature", "properties": {}, "geometry": None}],
              lambda f: None)
    assert n == 2, n                        # two different lines kept, the repeat once
    w.finish()
    m = w.manifest()
    assert "1 feature(s) without geometry skipped" in m["datasets"]["roads"]["note"], m
    w.close()


def test_op1_demotion_logged_once():
    OP._DEMOTED.clear()
    logs = []
    orig = OP._log
    OP._log = logs.append
    try:
        OP._demoted("x", "why")
        OP._demoted("x", "why")
    finally:
        OP._log = orig
    assert len(logs) == 1 and "R*Tree" in logs[0]


def test_os1_semicolon_values():
    assert OS.tag_has({"amenity": "hospital;clinic"}, "amenity", "clinic")
    assert OS.tag_has({"amenity": "hospital"}, "amenity", "hospital")
    assert not OS.tag_has({"amenity": "hospitality"}, "amenity", "hospital")
    tags = [("amenity", "hospital", "Hospital")]
    data = {"elements": [{"type": "node", "id": 1, "lat": 46.0, "lon": -100.0,
                          "tags": {"amenity": "clinic;hospital", "name": "X"}}]}
    assert len(OS.overpass_to_features(data, tags)) == 1
    q = OS.overpass_query((-101, 45, -100, 46), tags)
    assert '["amenity"~"(^|;) *hospital *(;|$)"]' in q, q
    assert "(^|;)" in OS.camps_query((-101, 45, -100, 46))


def test_os2_dotted_acronym():
    for name in ("Sunset M.H.P.", "Sunset M. H. P.", "Sunset MHP"):
        assert OS.is_trailer_park({"landuse": "residential", "name": name}) == "name", name
    assert "m[.] ?h[.] ?p" in OS.trailer_parks_query((-101, 45, -100, 46))
    assert OS.is_trailer_park({"landuse": "residential", "name": "St. Mary's Park"}) is None


def test_os3_cap_zero_none():
    data = {"elements": [{"type": "node", "id": 1, "lat": 46.0, "lon": -100.0,
                          "tags": {"tourism": "camp_site", "capacity": "0"}},
                         {"type": "node", "id": 2, "lat": 46.0, "lon": -100.0,
                          "tags": {"tourism": "camp_site", "capacity": "-3"}},
                         {"type": "node", "id": 3, "lat": 46.0, "lon": -100.0,
                          "tags": {"tourism": "camp_site", "capacity": "12"}}]}
    feats = OS.camp_features(data)
    assert [f["properties"]["sites"] for f in feats] == [None, None, 12], feats


def test_evc5_limits_failing_field():
    import numpy as np
    from qgis.core import QgsCoordinateReferenceSystem
    names = [n for n, _t in EV.CLOSURE_FIELDS]
    assert names[-3:] == ["Limits_Structures", "Limits_Persons", "Limits_Failing"]

    class G:
        eclose = np.array([30.0])
        ex0, ey0, ex1, ey1 = [np.array([v]) for v in (0.0, 0.0, 10.0, 0.0)]
        emaxd, eunk, elen_ft = np.array([2.0]), np.array([False]), np.array([10.0])
        epart, part_names = np.array([0]), ["Main St"]
        crs = QgsCoordinateReferenceSystem("EPSG:26914")
    rows = [{"limiting_edge": 0, "margin_min": 5.0, "persons": 2.0},
            {"limiting_edge": 0, "margin_min": -3.0, "persons": 3.0},
            {"limiting_edge": 0, "margin_min": None, "persons": 9.0}]
    lyr = EV.closures_layer({"status": "ok", "_graph": G(), "rows": rows})
    f = next(lyr.getFeatures())
    assert f["Limits_Structures"] == 2 and abs(f["Limits_Persons"] - 5.0) < 1e-9
    assert f["Limits_Failing"] == 1


def test_evc1_guarded_import():
    import inspect
    src = inspect.getsource(EV)
    assert "_node_line_ends and _seg_dist" in src and "from .access import EXIT_MARGIN_FT" in src


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
    sys.exit(1 if fails else 0)
