"""Suite 1.18.5: the suite's map services become QGIS Browser connections when a plugin starts
(damsuite/connections.py) - each added once, never put back after the user deletes it, the right
setting keys for QGIS 3.28 and 3.30+, and off with ndsuite/register_connections. Pure Python;
the last test uses a real QGIS when one imports.

    python3 DamSafety_Hazard/tests/test_browser_connections.py
"""
import os
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from DamSafety_Hazard.damsuite import connections as CN  # noqa: E402
from DamSafety_Hazard.damsuite import services as SV  # noqa: E402
from DamSafety_Hazard.damsuite import versions as VS  # noqa: E402

TESTS = []


def test(f):
    TESTS.append(f)
    return f


class Settings(object):
    """A QgsSettings stand-in: value() / setValue() / remove() on a dict."""

    def __init__(self, d=None):
        self.d = dict(d or {})

    def value(self, key, default=None):
        return self.d.get(key, default)

    def setValue(self, key, value):              # noqa: N802 - QGIS API
        self.d[key] = value

    def remove(self, key):
        for k in [k for k in self.d if k == key or k.startswith(key + "/")]:
            del self.d[k]


@test
def every_display_service_gets_one_connection():
    plan = CN.planned(prefix="DamSafety")
    kinds = {k for _n, k, _u in plan}
    assert kinds == {"wms", "xyz", "arcgis"}, kinds
    urls = [(k, u) for _n, k, u in plan]
    assert len(urls) == len(set(urls)), "a (kind, address) twice"
    names = [n for n, _k, _u in plan]
    assert all(n.startswith("DamSafety: ") for n in names)
    byname = {n: (k, u) for n, k, u in plan}
    # a FeatureServer layer -> its FeatureServer; an ImageServer -> its services folder
    k, u = byname["DamSafety: USGS road-stream crossings (Database of Stream Crossings)"]
    assert k == "arcgis" and u.endswith("/stream_crossings/FeatureServer"), u
    k, u = byname["DamSafety: USGS 3DEP elevation (ImageServer)"]
    assert u == "https://elevation.nationalmap.gov/arcgis/rest/services", u
    k, u = byname["DamSafety: ND DWR MapService (WMS)"]
    assert k == "wms" and u == "https://mapservice.dwr.nd.gov/swcwms", u
    assert byname["DamSafety: USGS topo (tiles)"][0] == "xyz"
    # services that are not map services (WCS, files) are not connections
    assert CN.connection_for({"kind": "wcs", "url": "https://x/wcs"}) is None
    assert CN.connection_for({"kind": "file", "url": "/tmp/a.gpkg"}) is None
    assert VS.CONNECTION_PREFIX == "DamSafety"


@test
def added_once_and_a_deleted_one_stays_deleted():
    s = Settings()
    first = CN.register(s, prefix="DamSafety", qgis_version_int=33400)
    n = len(CN.planned(prefix="DamSafety"))
    assert len(first["added"]) == n and first["kept"] == 0, first
    key = "connections/ows/items/wms/connections/items/DamSafety: USGS topo (WMS)/url"
    assert s.d[key].endswith("/USGSTopo/MapServer/WMSServer")
    xyz = "connections/xyz/items/DamSafety: USGS topo (tiles)/"
    assert "{z}" in s.d[xyz + "url"] and s.d[xyz + "zmax"] == 19
    arc = ("connections/arcgisfeatureserver/items/DamSafety: USACE National Inventory of "
           "Dams/url")
    assert s.d[arc].endswith("/National_Inventory_of_Dams_Public_Service/FeatureServer")
    # the user deletes one and edits another: a restart changes neither
    s.remove("connections/ows/items/wms/connections/items/DamSafety: USGS topo (WMS)")
    s.setValue(arc, "https://example.org/mine/FeatureServer")
    again = CN.register(s, prefix="DamSafety", qgis_version_int=33400)
    assert again["added"] == [] and again["kept"] == n, again
    assert key not in s.d
    assert s.d[arc] == "https://example.org/mine/FeatureServer"
    # a newer suite with one more service adds only that one
    more = list(SV.BUILTIN) + [{"id": "x", "name": "Extra WMS", "kind": "wms",
                                "url": "https://example.org/wms?service=WMS"}]
    third = CN.register(s, more, prefix="DamSafety", qgis_version_int=33400)
    assert third["added"] == ["DamSafety: Extra WMS"], third
    assert s.d["connections/ows/items/wms/connections/items/DamSafety: Extra WMS/url"] == \
        "https://example.org/wms"


@test
def qgis_3_28_keys_and_the_off_switch():
    s = Settings()
    CN.register(s, prefix="DamSafety", qgis_version_int=32815)
    assert s.d["qgis/connections-wms/DamSafety: USGS topo (WMS)/url"].endswith("WMSServer")
    assert "qgis/connections-xyz/DamSafety: USGS topo (tiles)/url" in s.d
    assert any(k.startswith("qgis/connections-arcgisfeatureserver/") for k in s.d)
    assert not any(k.startswith("connections/") for k in s.d)
    off = Settings({CN.ENABLED_KEY: "false"})
    res = CN.register(off, prefix="DamSafety", qgis_version_int=33400)
    assert res["off"] and res["added"] == [] and set(off.d) == {CN.ENABLED_KEY}


@test
def never_raises():
    class Broken(Settings):
        def setValue(self, key, value):          # noqa: N802
            raise RuntimeError("read-only profile")
    res = CN.register(Broken(), prefix="DamSafety", qgis_version_int=33400)
    assert res["added"] == [] and "read-only profile" in res["note"], res


@test
def real_qgis_lists_them_in_the_browser():
    try:
        from qgis.core import QgsApplication, QgsSettings
    except ImportError:
        print("skip (no QGIS)")
        return
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QgsApplication.instance() or QgsApplication([], False)
    app.initQgis()
    s = QgsSettings("ndsuite-test", "connections")       # a scratch settings file
    s.clear()
    res = CN.register(s, prefix="DamSafety")
    assert res["added"], res
    names = [k for k in s.allKeys() if "DamSafety: USGS topo (WMS)" in k]
    assert names, s.allKeys()[:5]
    s.clear()


def main():
    failed = 0
    for t in TESTS:
        try:
            t()
            print("ok   ", t.__name__)
        except Exception:                                        # noqa
            failed += 1
            print("FAIL ", t.__name__)
            traceback.print_exc()
    print("{} tests, {} failed".format(len(TESTS), failed))
    return failed


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
