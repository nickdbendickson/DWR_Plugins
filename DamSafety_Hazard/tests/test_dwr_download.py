# -*- coding: utf-8 -*-
"""Hazard 2.19.2: build_nd_pack.download_dwr_shapefiles sends the DWR MapService download form.

The old request (layer=<index>, format=shapefile, extent=full) got HTTP 200 with an empty body,
so no DWR layer was ever downloaded. urllib.request.urlopen is mocked: no network. Checks

  * each of the four layers is one POST to downloadShape.php with qlay = the layer NAME
    (Dams, Damsline, Dugouts, DugoutsPoly), whichextent, srid 4269, downloadformat, mapext
    ('+'-separated EPSG:2266 feet) and map - and no layer / format / extent fields;
  * a bbox -> whichextent=partial and the EPSG:2266 box around it (to 2 ft of GDAL's answer);
    no bbox -> whichextent=all and the web map's full extent;
  * the zips are saved and extracted; an empty or non-zip answer, or a failed request, is
    logged, skipped and never saved as a .zip; a bad format is refused;
  * the --download-dwr command line passes --dwr-bbox / --dwr-format and exits 1 when a layer
    is missing.

    python3 DamSafety_Hazard/tests/test_dwr_download.py      (or with pytest)
"""
import io
import os
import shutil
import sys
import tempfile
import urllib.parse
import urllib.request
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from DamSafety_Hazard import build_nd_pack as B      # noqa: E402

BISMARCK = (-100.85, 46.75, -100.70, 46.85)
# osgeo.osr EPSG:4326 -> EPSG:2266 box of BISMARCK (corners), for the record
GDAL_BOX = (1880769.805, 395140.237, 1918463.512, 431741.401)


def _zip(name, ext):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(name + ext, b"data")
        if ext == ".shp":
            for e in (".dbf", ".shx", ".prj"):
                z.writestr(name + e, b"x")
    return buf.getvalue()


class _Resp(object):
    def __init__(self, raw):
        self.raw = raw

    def read(self):
        return self.raw

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Server(object):
    """Answers like downloadShape.php: a zip for a known qlay sent with the form's fields,
    an empty 200 otherwise (what the old request got)."""

    def __init__(self, fmt="shp", answers=None):
        self.calls, self.fmt, self.answers = [], fmt, answers or {}

    def __call__(self, req, timeout=None):
        assert isinstance(req, urllib.request.Request)
        form = dict(urllib.parse.parse_qsl(req.data.decode()))
        self.calls.append((req.full_url, req.get_method(), form, timeout,
                           req.get_header("User-agent")))
        q = form.get("qlay")
        if q in self.answers:
            a = self.answers[q]
            if isinstance(a, Exception):
                raise a
            return _Resp(a)
        ok = (q in ("Dams", "Damsline", "Dugouts", "DugoutsPoly")
              and {"whichextent", "srid", "downloadformat", "mapext", "map"} <= set(form))
        return _Resp(_zip(q, ".sqlite" if form.get("downloadformat") == "spat" else ".shp")
                     if ok else b"")


def _with(server, fn):
    old = urllib.request.urlopen
    urllib.request.urlopen = server
    try:
        return fn()
    finally:
        urllib.request.urlopen = old


def _tmp():
    return tempfile.mkdtemp(prefix="dwr_dl_")


def test_request_shape_full_data_set():
    d, srv, log = _tmp(), _Server(), []
    try:
        got = _with(srv, lambda: B.download_dwr_shapefiles(d, log=log.append))
        assert len(srv.calls) == 4, srv.calls
        qlays = [c[2]["qlay"] for c in srv.calls]
        assert qlays == ["Damsline", "Dams", "DugoutsPoly", "Dugouts"], qlays
        for url, method, form, timeout, ua in srv.calls:
            assert url == "https://mapservice.dwr.nd.gov/php/downloadShape.php"
            assert method == "POST"
            assert not {"layer", "format", "extent"} & set(form), form
            assert not form["qlay"].isdigit()
            assert form["whichextent"] == "all"
            assert form["srid"] == "4269"
            assert form["downloadformat"] == "shp"
            assert form["map"] == B.DWR_MAPFILE
            box = [float(v) for v in form["mapext"].split("+")]
            assert [round(v, 3) for v in box] == [round(v, 3) for v in B.DWR_FULL_EXTENT]
            assert timeout == B.TIMEOUT_S and ua and "offline-pack-builder" in ua
        assert sorted(os.path.basename(p) for p in got) == sorted(n for n, _, _ in B.DWR_DOWNLOADS)
        for name, _, q in B.DWR_DOWNLOADS:
            assert os.path.isfile(os.path.join(d, name + ".zip"))
            assert os.path.isfile(os.path.join(d, name, q + ".shp"))
        assert not [n for n in os.listdir(d) if n.endswith(".part")]
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_request_shape_bbox_and_spatialite():
    d, srv = _tmp(), _Server()
    try:
        got = _with(srv, lambda: B.download_dwr_shapefiles(d, bbox=BISMARCK, fmt="spat",
                                                           only=("DWR_dams", "Dugouts")))
        assert [c[2]["qlay"] for c in srv.calls] == ["Dams", "Dugouts"]
        for c in srv.calls:
            form = c[2]
            assert form["whichextent"] == "partial" and form["downloadformat"] == "spat"
            box = [float(v) for v in form["mapext"].split("+")]
            assert box[0] < box[2] and box[1] < box[3]
            # contains GDAL's corner box and is within 2 ft of it
            assert all(abs(a - b) < 2.0 for a, b in zip(box, GDAL_BOX)), box
        assert len(got) == 2
        assert os.path.isfile(os.path.join(d, "DWR_dams", "Dams.sqlite"))
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_empty_and_failed_answers_are_not_saved():
    d, log = _tmp(), []
    srv = _Server(answers={"Dams": b"", "Damsline": b"<html>error</html>",
                           "Dugouts": OSError("timed out"), "DugoutsPoly": _zip("x", ".txt")})
    try:
        got = _with(srv, lambda: B.download_dwr_shapefiles(d, log=log.append))
        assert got == [], got
        assert not os.path.exists(os.path.join(d, "DWR_dams.zip"))
        assert not os.path.exists(os.path.join(d, "DWR_dam_lines.zip"))
        text = "\n".join(log)
        assert "Dams: the DWR MapService sent no file (0 bytes)" in text, text
        assert "Dugouts failed: timed out" in text and "holds no .shp" in text, text
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_bad_format_refused():
    try:
        B.dwr_request("Dams", fmt="kml")
    except B.BuildError:
        return
    raise AssertionError("fmt=kml was accepted")


def test_command_line():
    d = _tmp()
    try:
        srv = _Server()
        rc = _with(srv, lambda: B.main(["--download-dwr", d, "--dwr-bbox=" +
                                        ",".join(str(v) for v in BISMARCK),
                                        "--dwr-format", "spat"]))
        assert rc == 0 and len(srv.calls) == 4
        assert all(c[2]["whichextent"] == "partial" and c[2]["downloadformat"] == "spat"
                   for c in srv.calls)
        srv = _Server(answers={"Dams": b""})
        assert _with(srv, lambda: B.main(["--download-dwr", d])) == 1
        assert B.main(["--download-dwr", d, "--dwr-bbox", "1,2,3"]) == 1
    finally:
        shutil.rmtree(d, ignore_errors=True)


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    bad = 0
    for t in tests:
        try:
            t()
            print("ok   " + t.__name__)
        except Exception as e:      # noqa: BLE001
            bad += 1
            import traceback
            traceback.print_exc()
            print("FAIL {}: {}".format(t.__name__, e))
    print("{} passed, {} failed".format(len(tests) - bad, bad))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
