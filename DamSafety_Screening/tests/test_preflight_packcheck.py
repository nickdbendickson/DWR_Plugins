"""0.17.45: the preflight's 'Offline pack vs manifest.json' row (core/preflight._pack_check,
damsuite.packcheck). A synthetic format-2 pack (manifest.json, LAYERS.txt, one GeoPackage
per layer, written with the suite's stdlib writer) in a temp suite folder: OK when every
layer matches, WARN naming the layer when a file is gone or its count differs; no row when
no pack is in use. Read-only on the pack.

Run: PYTHONPATH=tests/qgis_mock:<parent of the plugin> python3 tests/test_preflight_packcheck.py
"""
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "qgis_mock"))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
sys.path.insert(0, HERE)

from nd_dam_screening.core import preflight as pf, workflow as wf  # noqa: E402
from nd_dam_screening.damsuite import gpkgpack as GP, packcheck as PC, suite_fill as SF  # noqa: E402

TITLE = "Offline pack vs manifest.json"


class Net(pf.Env):
    def url_ok(self, url):
        return True, "answered (test)"

    def plugin_version(self, key):
        return None


def build(folder):
    rows = [{"type": "Feature", "geometry": {"type": "Point", "coordinates": [-100.5 - i / 100.0, 47.5]},
             "properties": {"nidid": "ND0000{}".format(i)}} for i in range(3)]
    fields = [["nidid", "nidid", "text"]]
    os.makedirs(os.path.join(folder, "dams"))
    GP.write_layer(os.path.join(folder, "dams", "NID_dams.gpkg"), "NID_dams", lambda: iter(rows),
                   fields=fields, geometry="POINT")
    m = {"format": "nd-dam-hazard-offline-pack", "format_version": 2, "layout": "gpkg",
         "created": "2026-10-25T00:00:00",
         "datasets": {"NID_dams": {"file": "dams/NID_dams.gpkg", "layer": "NID_dams", "count": 3,
                                   "fields": fields, "geometry": "POINT", "status": "complete"}}}
    with open(os.path.join(folder, "manifest.json"), "w") as fh:
        json.dump(m, fh)
    with open(os.path.join(folder, "LAYERS.txt"), "w") as fh:
        fh.write("test pack\n")


def row(S):
    SF._SCAN.clear()
    PC.clear_cache()
    return pf._pack_check(S)


d = tempfile.mkdtemp(prefix="pf_pack_")
try:
    pack = os.path.join(d, "Pack")
    build(pack)
    S = dict(wf.DEFAULT_SETTINGS)
    S["suite_folder"] = d
    r = row(S)
    assert r and r["status"] == pf.OK and r["title"] == TITLE and "OK - every layer" in r["detail"], r
    checks = pf.run_checks(os.path.join(d, "work"), S, env=Net())
    assert [c for c in checks if c["title"] == TITLE and c["status"] == pf.OK], \
        [c["title"] for c in checks]
    print("ok   valid pack: preflight row OK")

    with open(os.path.join(pack, "manifest.json")) as fh:
        m = json.load(fh)
    m["datasets"]["NID_dams"]["count"] = 4
    with open(os.path.join(pack, "manifest.json"), "w") as fh:
        json.dump(m, fh)
    r = row(S)
    assert r["status"] == pf.WARN and "NID_dams" in r["detail"] and "count" not in r["title"], r
    assert "3 feature(s) in the file, 4 in manifest.json" in r["detail"], r
    print("ok   count mismatch: WARN naming NID_dams")

    os.remove(os.path.join(pack, "dams", "NID_dams.gpkg"))
    r = row(S)
    assert r["status"] == pf.WARN and "missing: NID_dams" in r["detail"] and r["fix"], r
    print("ok   missing layer file: WARN naming NID_dams, with a fix")
    assert sorted(os.listdir(pack)) == ["LAYERS.txt", "dams", "manifest.json"]

    S2 = dict(wf.DEFAULT_SETTINGS)
    S2["suite_folder"] = os.path.join(d, "work")
    os.makedirs(S2["suite_folder"], exist_ok=True)
    assert row(S2) is None
    print("ok   no pack in use: no row")
finally:
    shutil.rmtree(d, ignore_errors=True)
print("all ok")
