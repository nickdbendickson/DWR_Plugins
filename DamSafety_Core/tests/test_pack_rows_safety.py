"""DamSafety Core security (audit 2026-11-18): an offline pack is a file someone may hand the user.

* A format-1 data set name holding a path ("HIFLD_/../../escaped/planted") is ignored, so its
  GeoPackage can never be written outside pack_cache_root().
* A zlib-packed feature row is inflated with a size cap (zipsafe.inflate), so one crafted row
  cannot run QGIS out of memory.

    python3 DamSafety_Core/tests/test_pack_rows_safety.py
"""
import os
import sqlite3
import sys
import tempfile
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from DamSafety_Core.damsuite import offline_pack as op
from DamSafety_Core.damsuite import suitedata as sd
from DamSafety_Core.damsuite import zipsafe


def test_inflate_ok():
    raw = b'{"type":"Feature","geometry":null,"properties":{}}' * 100
    assert zipsafe.inflate(zlib.compress(raw)) == raw
    assert op._inflate(zlib.compress(raw)) == raw


def test_inflate_bomb_refused():
    blob = zlib.compress(b"\0" * (8 * 1024 ** 2), 9)          # ~8 KB that inflates to 8 MiB
    for cap in (1024 ** 2, 8 * 1024 ** 2 - 1):
        raised = False
        try:
            zipsafe.inflate(blob, max_bytes=cap)
        except zipsafe.TooLarge:
            raised = True
        assert raised, "a row inflating past {} bytes must be refused".format(cap)
    assert len(zipsafe.inflate(blob, max_bytes=8 * 1024 ** 2)) == 8 * 1024 ** 2
    assert issubclass(zipsafe.TooLarge, ValueError)


def test_inflate_corrupt_still_zlib_error():
    raised = False
    try:
        zipsafe.inflate(b"not zlib")
    except zlib.error:
        raised = True
    assert raised


def test_name_ok():
    for good in ("HIFLD_local_eoc", "NHD_flowlines", "ND_roads_NG911", "FEMA_flood_zones"):
        assert sd._v1_name_ok(good), good
    for bad in ("HIFLD_/!a", "HIFLD_/../../../../escaped/planted", "..", "a/b", "a\\b", "C:x", "",
                None, "HIFLD_x.gpkg"):
        assert not sd._v1_name_ok(bad), bad


def _v1_pack(folder, names):
    db = os.path.join(folder, op.DB_NAME)
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE meta (key TEXT, value TEXT)")
    con.execute("INSERT INTO meta VALUES ('format', ?)", (op.PACK_FORMAT,))
    con.execute("CREATE TABLE datasets (name TEXT, count INTEGER, status TEXT, geometry TEXT)")
    for n in names:
        con.execute("INSERT INTO datasets VALUES (?, 1, 'complete', 'Point')", (n,))
    con.commit()
    con.close()
    return db


def test_manifest_drops_path_names_and_layer_stays_in_cache():
    with tempfile.TemporaryDirectory() as d:
        pack = os.path.join(d, "pack")
        cache = os.path.join(d, "cache")
        os.makedirs(pack)
        bad = "HIFLD_/../../../../escaped/planted"
        db = _v1_pack(pack, ["HIFLD_local_eoc", bad, "HIFLD_/!a"])
        old = os.environ.get(sd.ENV_PACK_CACHE)
        os.environ[sd.ENV_PACK_CACHE] = cache
        try:
            names = set(sd._v1_manifest(db)["datasets"])
            assert names == {"HIFLD_local_eoc"}, names
            assert sd._v1_layer(db, bad) == ""
        finally:
            if old is None:
                os.environ.pop(sd.ENV_PACK_CACHE, None)
            else:
                os.environ[sd.ENV_PACK_CACHE] = old
        for top, _dirs, files in os.walk(d):
            for f in files:
                assert not f.startswith("planted"), os.path.join(top, f)


def main():
    test_inflate_ok()
    test_inflate_bomb_refused()
    test_inflate_corrupt_still_zlib_error()
    test_name_ok()
    test_manifest_drops_path_names_and_layer_stays_in_cache()
    print("test_pack_rows_safety: all tests passed")


if __name__ == "__main__":
    main()
