"""Lumo vReal.17 DL.1 / DL.2 / LN.1 / LN.8 / LN.9 (Hazard 2.18.28).

    QT_QPA_PLATFORM=offscreen python3 DamSafety_Hazard/tests/test_lumo_real17.py
"""
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

from DamSafety_Hazard import download as DL  # noqa: E402
from DamSafety_Hazard import pipeline as PL  # noqa: E402
from DamSafety_Hazard import linear as LN  # noqa: E402
from DamSafety_Hazard import facility_review as FR  # noqa: E402

FEAT = {"type": "Feature", "properties": {"a": 1},
        "geometry": {"type": "Point", "coordinates": [-100.0, 46.0]}}


def test_dl1_atomic_write_and_truncated_recovery():
    d = tempfile.mkdtemp()
    p = os.path.join(d, "OSM_places_20261015_deadbeef.geojson")
    DL._write_fc(p, [FEAT] * 50)
    assert os.path.isfile(p) and not [f for f in os.listdir(d) if f.endswith(".part")]
    lyr = DL._todays_file(p, "x")
    assert lyr is not None and lyr.featureCount() == 50
    with open(p, "rb") as fh:
        data = fh.read()
    with open(p, "wb") as fh:
        fh.write(data[:len(data) // 2])                    # an interrupted old-style write
    assert DL._todays_file(p, "x") is None
    assert not os.path.exists(p)                           # deleted -> fetched again
    assert DL._todays_file(p, "x") is None                 # missing: fetch


def test_dl1_osm_paths_use_today_helper():
    import inspect
    src = inspect.getsource(DL)
    assert src.count("_todays_file(path,") >= 7
    assert "if os.path.exists(path):\n        lyr = _load(" not in src


def _cfg(out):
    c = dict(PL.DEFAULTS)
    c.update({"output_folder": out, "dam_folder": out, "scenarios": [], "point_sources": []})
    return c


def test_dl2_cache_key_pack_env():
    out = tempfile.mkdtemp()
    saved = {k: os.environ.get(k) for k in PL.EVAL_ENV_VARS}
    try:
        for k in PL.EVAL_ENV_VARS:
            os.environ.pop(k, None)
        k0 = PL.evaluation_key(_cfg(out))
        pack = os.path.join(tempfile.mkdtemp(), "pack.gpkg")
        with open(pack, "wb") as fh:
            fh.write(b"x" * 10)
        os.environ["ND_DAM_HAZARD_OFFLINE_PACK"] = pack
        k1 = PL.evaluation_key(_cfg(out))
        assert k1 != k0
        with open(pack, "wb") as fh:
            fh.write(b"x" * 20)                            # another pack build
        k2 = PL.evaluation_key(_cfg(out))
        assert k2 != k1
        os.environ["ND_DAM_HAZARD_OFFLINE_ONLY"] = "1"
        assert PL.evaluation_key(_cfg(out)) != k2
        for v in ("NDSUITE_HOME", "NDSUITE_SETTINGS"):
            assert v in PL.EVAL_ENV_VARS
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_ln1_motorists_shared():
    from DamSafety_Hazard.constants import vehicle_curve_available, CLASS_HIGH
    rec = {"paved": "Yes", "vehicle_zone": CLASS_HIGH, "length_ft": 2640.0}
    got = FR._motorists(rec, 4800.0, 1.5, 40.0)
    if vehicle_curve_available():
        assert abs(got - LN.motorists(4800.0, 2640.0, 40.0, 1.5)) < 1e-12
    else:
        assert got is None


class _Reader:
    def __init__(self):
        self.low = {"totaltrn": 0, "daythru": 1}

    def get(self, attrs, n):
        return attrs.get(n)

    def aadt_candidates(self, attrs, label):
        return [(attrs["aadt"], 2024, label)]


class _F:
    def __init__(self, a):
        self.a = a

    def attributes(self):
        return self.a


class _Idx:
    reader = _Reader()

    def __init__(self, feats):
        self.feats = feats

    def within(self, geom, d):
        return [(i, _F(a), None) for i, a in enumerate(self.feats)]


def test_ln8_next_nearest_crossing():
    idx = _Idx([{"street": "Oak Ave", "aadt": 100.0, "crossingid": "A"},
                {"street": "Main St", "aadt": 900.0, "crossingid": "B"}])
    c = LN.crossing_aadt_candidates(idx, None, 100.0, "Main Street")
    assert c and c[0][0] == 900.0 and "B" in c[0][2], c
    assert LN.crossing_aadt_candidates(_Idx([{"street": "Oak Ave", "aadt": 1.0}]), None, 1,
                                       "Main St") == []
    # no conflict: the nearest one, as before
    c = LN.crossing_aadt_candidates(idx, None, 100.0, "")
    assert c[0][0] == 100.0


def test_ln9_trains_single_read():
    r = _Reader()
    calls = []
    orig = r.get
    r.get = lambda a, n: (calls.append(n), orig(a, n))[1]
    assert LN.trains_per_day(r, {"totaltrn": 12}) == 12
    assert calls.count("totaltrn") == 1


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
