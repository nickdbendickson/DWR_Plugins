"""Setup 1.0.17 / damsuite 1.18.3: Setup's suite_fill (its own copy, no damsuite beside it) fills the
DWR dams and the NID from a format-1 offline pack (nd_offline_pack.sqlite or its folder), through an
installed suite plugin's damsuite.suitedata; a format-2 pack is unchanged (its dwr/ layer only).

Tiny synthetic packs in a temp folder; no QGIS, no network.
    python -m pytest -q DamSafety_Setup/tests/test_suite_fill_v1.py   (from the repository root)"""
import json
import os
import sqlite3
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from DamSafety_Setup import suite_fill as SF  # noqa: E402
from DamSafety_Runner.damsuite import gpkgpack as GP  # noqa: E402
from DamSafety_Runner.damsuite import offline_pack as OP  # noqa: E402

BBOX = (-104.1, 45.9, -96.5, 49.1)


def _pt(i, x, y):
    return {"type": "Feature", "geometry": {"type": "Point", "coordinates": [x, y]},
            "properties": {"id": i, "DAM_NAME": "D%d" % i}}


@pytest.fixture
def packs(tmp_path, monkeypatch):
    for k in ("NDSUITE_SETTINGS", "NDSUITE_PACK", "NDSUITE_SOURCE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("NDSUITE_PACK_CACHE", str(tmp_path / "cache"))
    p1 = tmp_path / "nd_offline_pack"
    p1.mkdir()
    db = str(p1 / OP.DB_NAME)
    w = OP.PackWriter(db, BBOX)
    for name, feats in (("DWR_dams", [_pt(1, -100, 47), _pt(2, -99, 47)]),
                        ("NID_dams", [_pt(3, -100, 46.5)])):
        w.begin(name, name, "https://example.invalid/" + name)
        w.add(feats, lambda f: f["properties"]["id"])
        w.finish()
    man = w.manifest()
    w.close(finalize=True)
    (p1 / OP.MANIFEST_NAME).write_text(json.dumps(man))
    p2 = str(tmp_path / "ND_Dam_Suite_Pack")
    GP.write_manifest(p2, GP.convert_v1(db, p2))
    return str(p1), db, p2, str(tmp_path / "cache")


def _rows(uri):
    path, layer = uri.split("|layername=")
    con = sqlite3.connect(path)
    try:
        return con.execute('SELECT COUNT(*) FROM "{}"'.format(layer)).fetchone()[0]
    finally:
        con.close()


def test_setup_copy_has_no_suitedata_beside_it():
    assert not os.path.exists(os.path.join(ROOT, "DamSafety_Setup", "suitedata.py"))
    assert SF._suitedata() is not None


def test_format1_fills_dwr_and_nid(packs):
    p1, db, _p2, cache = packs
    for pk in (p1, db):
        got = SF._pack_dwr({"pack": pk}, [])
        assert set(got) == {"dwr", "nid"}, got
        assert got["dwr"].startswith(cache) and got["dwr"].endswith("|layername=DWR_dams")
        assert _rows(got["dwr"]) == 2 and _rows(got["nid"]) == 1
        lay = SF.layers_for_settings({"pack": pk})
        assert lay["dwr"] == got["dwr"] and lay["nid"] == got["nid"]
    found, notes = SF.scan(p1)
    assert found["pack"] == db and found["dwr"] and found["nid"], found
    assert any("(format-1 pack)" in n for n in notes)


def test_format2_unchanged(packs):
    _p1, _db, p2, cache = packs
    got = SF._pack_dwr({"pack": p2}, [])
    assert got == {"dwr": os.path.join(p2, "dwr", "DWR_dams.gpkg") + "|layername=DWR_dams"}, got
    assert not os.path.exists(cache)
