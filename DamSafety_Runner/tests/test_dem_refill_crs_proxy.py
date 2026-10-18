"""dem_refill.fetch_3dep: the 3DEP hole-refill must be requested in the model grid's working
CRS (not a hard-coded EPSG:5070, which fetched the wrong ground off-5070 and so silently did
nothing), and the download must go through damsuite.netproxy (QGIS's proxy / SSL), not raw
urllib (which bypasses the proxy and fails silently behind one).

Runnable standalone (no scipy / GDAL needed):
    python3 nd_breach_runner/tests/test_dem_refill_crs_proxy.py
or as part of the suite through the registry.
"""
import io
import os
import sys
import tempfile
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))          # the checkout root (holds nd_breach_runner)
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from nd_breach_runner.core import dem_refill            # noqa: E402
from nd_breach_runner.damsuite import netproxy          # noqa: E402
from nd_breach_runner.tests.registry import test as register  # noqa: E402

# A minimal but valid little-endian TIFF signature padded past the 1,000-byte floor fetch_3dep
# uses to reject an empty answer.
FAKE_TIFF = b"II*\x00" + b"\x00" * 2000


class _FakeResp(object):
    def __init__(self, data):
        self._data = data

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _capture_fetch(crs, bounds=(100.0, 200.0, 1100.0, 1200.0), cell=10.0, data=FAKE_TIFF):
    """Call fetch_3dep with netproxy.urlopen stubbed; return (requested query dict, out_path)."""
    seen = {}

    def fake_urlopen(req, timeout=60, settings=None):
        seen["url"] = req.full_url if hasattr(req, "full_url") else str(req)
        seen["timeout"] = timeout
        seen["req"] = req
        return _FakeResp(data)

    real = netproxy.urlopen
    netproxy.urlopen = fake_urlopen
    d = tempfile.mkdtemp()
    out = os.path.join(d, "refill.tif")
    try:
        dem_refill.fetch_3dep(bounds, cell, out, crs=crs)
    finally:
        netproxy.urlopen = real
    assert "url" in seen, "fetch_3dep did not go through damsuite.netproxy.urlopen"
    q = urllib.parse.parse_qs(urllib.parse.urlparse(seen["url"]).query)
    return seen, q, out


@register
def refill_requests_the_grids_working_crs_not_hardcoded_5070():
    # a North Dakota metric CRS (UTM 14N); before the fix bboxSR/imageSR were nailed to 5070
    seen, q, out = _capture_fetch(26914)
    assert q["bboxSR"] == ["26914"], q.get("bboxSR")
    assert q["imageSR"] == ["26914"], q.get("imageSR")
    # the bbox is sent verbatim (it is already in that CRS)
    assert q["bbox"] == ["100.0,200.0,1100.0,1200.0"], q.get("bbox")
    assert os.path.isfile(out) and os.path.getsize(out) >= 1000


@register
def refill_accepts_epsg_string_and_defaults_to_5070():
    _seen, q, _out = _capture_fetch("EPSG:26915")
    assert q["bboxSR"] == ["26915"] and q["imageSR"] == ["26915"], q
    # no CRS given -> the documented 5070 fall-back (so existing 5070 callers are unchanged)
    _seen, q2, _o2 = _capture_fetch(None)
    assert q2["bboxSR"] == ["5070"] and q2["imageSR"] == ["5070"], q2
    assert dem_refill._sr("epsg:2266") == 2266 and dem_refill._sr(3857) == 3857


@register
def refill_download_is_routed_through_the_qgis_proxy_layer():
    # netproxy.urlopen must be the transport (raw urllib.request.urlretrieve bypasses QGIS's
    # proxy). The stub records the call; a Request with a User-Agent is what reaches it.
    seen, _q, _out = _capture_fetch(26914)
    req = seen["req"]
    assert isinstance(req, dem_refill.urllib.request.Request), type(req)
    assert req.get_header("User-agent"), "no User-Agent header on the refill request"
    assert seen["url"].startswith(dem_refill.EXPORT_URL)


@register
def refill_rejects_a_non_tiff_answer():
    # an ArcGIS error comes back as JSON with HTTP 200; it must not be written as a "tile"
    try:
        _capture_fetch(26914, data=b'{"error":{"code":400}}' + b" " * 2000)
        raise AssertionError("a non-TIFF answer should raise")
    except IOError as e:
        assert "GeoTIFF" in str(e), str(e)


def _run():
    tests = [refill_requests_the_grids_working_crs_not_hardcoded_5070,
             refill_accepts_epsg_string_and_defaults_to_5070,
             refill_download_is_routed_through_the_qgis_proxy_layer,
             refill_rejects_a_non_tiff_answer]
    failed = 0
    for fn in tests:
        try:
            fn()
            print("ok    ", fn.__name__)
        except Exception as e:                           # noqa: BLE001
            failed += 1
            print("FAIL  ", fn.__name__, "->", repr(e))
    print("\n{} tests, {} failed".format(len(tests), failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run())
