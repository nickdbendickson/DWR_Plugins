"""Core 1.5.0 / Data 1.11.1 (security audit pass 3): what the updater downloads is checked
before it replaces the data in use.

* NSI: USACE publishes no checksum, so the content is checked. Records that are not points
  inside North Dakota are left out (a few) or the answer is refused (many); an inventory much
  smaller than the one in use is refused and the one in use is kept; the SHA-256 of every API
  response is recorded in nsi_summary.json.
* A downloaded zip is checked before it is unpacked (damsuite.zipsafe): a "zip bomb", an entry
  that leaves the folder or a symbolic link stops the update, and nothing is written.
* An answer read into memory is bounded.

    python3 -m pytest DamSafety_Data/tests/test_security_pass3.py      (or run it directly)
"""
import hashlib
import json
import os
import shutil
import sys
import tempfile
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from DamSafety_Data.damsuite import updatecore as UC      # noqa: E402
from DamSafety_Data.damsuite import zipsafe as ZS         # noqa: E402

FIPS = ("38001", "38003")


def _pt(i, x=-100.0, y=47.0, **props):
    return {"type": "Feature", "geometry": {"type": "Point", "coordinates": [x, y]},
            "properties": dict({"fd_id": i, "occtype": "RES1"}, **props)}


class Http:
    """The NSI API: {fips: [features]} (or bytes / a callable) per county."""

    def __init__(self, counties):
        self.counties = counties
        self.urls = []

    def get_bytes(self, url, headers=None):
        self.urls.append(url)
        fips = url.split("fips=")[1].split("&")[0]
        got = self.counties[fips]
        if isinstance(got, bytes):
            return got
        return json.dumps({"type": "FeatureCollection", "features": got}).encode("utf-8")


class Feedback:
    def __init__(self):
        self.lines = []

    def log(self, msg):
        self.lines.append(msg)

    def check_cancelled(self):
        pass

    def fraction(self, f):
        pass

    def status(self, *a, **k):
        pass


def _source(**kw):
    return UC.NsiApiSource("nsi", "NSI", "nsi", county_fips=FIPS, mode="nsi2026", **kw)


def _fetch(data_dir, counties, src=None):
    src = src or _source()
    staging = os.path.join(data_dir, ".staging", "nsi-test")
    shutil.rmtree(staging, ignore_errors=True)
    fb = Feedback()
    version = src.fetch(Http(counties), staging, fb, None)
    return staging, version, fb


def _install(data_dir, staging):
    live = os.path.join(data_dir, "nsi")
    shutil.rmtree(live, ignore_errors=True)
    shutil.move(staging, live)
    return live


def test_feature_check():
    ok = UC.nsi_feature_ok
    b = UC.ND_NSI_BOUNDS
    assert ok(_pt(1), b) and ok(_pt(1, x=-104.05, y=45.94), b)
    assert not ok(_pt(1, x=-90.0), b) and ok(_pt(1, x=-90.0))          # outside ND / no box
    assert not ok(_pt(1, y=30.0), b)
    for bad in (None, "x", {}, {"geometry": None}, {"geometry": {"type": "Polygon"}},
                {"geometry": {"type": "Point", "coordinates": [1]}},
                {"geometry": {"type": "Point", "coordinates": ["a", "b"]}},
                {"geometry": {"type": "Point", "coordinates": [float("nan"), 47.0]}},
                {"geometry": {"type": "Point", "coordinates": [True, 47.0]}},
                {"geometry": {"type": "Point", "coordinates": [-100.0, 147.0]}},
                dict(_pt(1), properties="not an object")):
        assert not ok(bad), bad


def test_a_few_bad_records_are_left_out_and_counted(tmp_path=None):
    d = str(tmp_path) if tmp_path is not None else tempfile.mkdtemp()
    try:
        good = [_pt(i) for i in range(1000)]
        staging, version, fb = _fetch(d, {
            "38001": good + [_pt(9001, x=-80.0), {"type": "Feature", "geometry": None}],
            "38003": [_pt(5000 + i) for i in range(10)]})
        summary = json.load(open(os.path.join(staging, "nsi_summary.json")))
        assert summary["structures"] == 1010 and summary["invalid_dropped"] == 2
        assert summary["counties_with_structures"] == ["38001", "38003"]
        kept = json.load(open(os.path.join(staging, "counties", "38001.geojson")))["features"]
        assert len(kept) == 1000 and all(UC.nsi_feature_ok(f, UC.ND_NSI_BOUNDS) for f in kept)
        assert any("2 record(s)" in ln and "left out" in ln for ln in fb.lines), fb.lines
        assert version.startswith("sha256:")
    finally:
        if tmp_path is None:
            shutil.rmtree(d, ignore_errors=True)


def test_an_answer_that_is_not_the_countys_inventory_is_refused(tmp_path=None):
    d = str(tmp_path) if tmp_path is not None else tempfile.mkdtemp()
    try:
        # a whole county of points somewhere else (a wrong or hijacked answer)
        try:
            _fetch(d, {"38001": [_pt(i, x=-80.0, y=35.0) for i in range(200)], "38003": []})
            raise AssertionError("installed")
        except UC.UpdateError as e:
            assert "200 of 200" in str(e) and "not points inside North Dakota" in str(e), str(e)
            assert "kept" in str(e)
        for raw, word in ((b"<html>error</html>", "invalid JSON"),
                          (b'{"type":"FeatureCollection","features":{"a":1}}', "unexpected"),
                          (b'[1,2,3]', "unexpected")):
            try:
                _fetch(d, {"38001": raw, "38003": []})
                raise AssertionError("installed: " + word)
            except UC.UpdateError as e:
                assert word in str(e), str(e)
        old = UC.NSI_MAX_COUNTY_BYTES
        UC.NSI_MAX_COUNTY_BYTES = 100
        try:
            _fetch(d, {"38001": [_pt(i) for i in range(50)], "38003": []})
            raise AssertionError("installed an oversize answer")
        except UC.UpdateError as e:
            assert "far more than a county's structures" in str(e), str(e)
        finally:
            UC.NSI_MAX_COUNTY_BYTES = old
    finally:
        if tmp_path is None:
            shutil.rmtree(d, ignore_errors=True)


def test_response_hashes_are_recorded(tmp_path=None):
    d = str(tmp_path) if tmp_path is not None else tempfile.mkdtemp()
    try:
        a, b = [_pt(1), _pt(2)], [_pt(3)]
        staging, _v, _fb = _fetch(d, {"38001": a, "38003": b})
        summary = json.load(open(os.path.join(staging, "nsi_summary.json")))
        raw = json.dumps({"type": "FeatureCollection", "features": a}).encode("utf-8")
        assert summary["response_sha256"]["38001"] == {"nsi2026": hashlib.sha256(raw).hexdigest()}
        assert set(summary["response_sha256"]) == {"38001", "38003"}
        # what was there before is still there
        assert summary["mode"] == "nsi2026" and summary["totals"] == {"points_2026": 3}
    finally:
        if tmp_path is None:
            shutil.rmtree(d, ignore_errors=True)


def test_an_inventory_much_smaller_than_the_one_in_use_is_not_installed(tmp_path=None):
    d = str(tmp_path) if tmp_path is not None else tempfile.mkdtemp()
    saved = os.environ.pop(UC.NSI_ALLOW_DROP_ENV, None)
    try:
        full = {"38001": [_pt(i) for i in range(600)], "38003": [_pt(1000 + i) for i in range(400)]}
        staging, _v, _fb = _fetch(d, full)
        live = _install(d, staging)
        before = open(os.path.join(live, "nsi_summary.json")).read()
        # 1. a cut-short answer: 30 % fewer structures
        short = {"38001": full["38001"][:300], "38003": full["38003"]}
        try:
            _fetch(d, short)
            raise AssertionError("a cut-short inventory was accepted")
        except UC.UpdateError as e:
            msg = str(e)
        assert "700 structures, 30% fewer than the 1,000" in msg, msg
        assert "was kept" in msg and UC.NSI_ALLOW_DROP_ENV + "=1" in msg, msg
        assert open(os.path.join(live, "nsi_summary.json")).read() == before
        # 2. a county that had structures comes back empty (under the 20 % share)
        full2 = {"38001": [_pt(i) for i in range(950)], "38003": [_pt(2000 + i) for i in range(50)]}
        staging, _v, _fb = _fetch(d, full2, _source())      # same size: accepted
        live = _install(d, staging)
        try:
            _fetch(d, {"38001": full2["38001"], "38003": []})
            raise AssertionError("an empty county was accepted")
        except UC.UpdateError as e:
            assert "1 county with structures" in str(e) and "38003" in str(e), str(e)
        # 3. a small real change is fine; so is a larger inventory
        _fetch(d, {"38001": full2["38001"][:900], "38003": full2["38003"]})
        _fetch(d, {"38001": full2["38001"] + [_pt(9000 + i) for i in range(500)],
                   "38003": full2["38003"]})
        # 4. the user says the smaller inventory is real
        os.environ[UC.NSI_ALLOW_DROP_ENV] = "1"
        staging, _v, _fb = _fetch(d, {"38001": full2["38001"][:100], "38003": []})
        assert json.load(open(os.path.join(staging, "nsi_summary.json")))["structures"] == 100
        os.environ.pop(UC.NSI_ALLOW_DROP_ENV)
        # 5. nothing to compare with (first install, another mode, an older summary): accepted
        shutil.rmtree(live)
        _fetch(d, {"38001": [_pt(1)], "38003": []})
        os.makedirs(live)
        with open(os.path.join(live, "nsi_summary.json"), "w") as f:
            json.dump({"mode": "nsi2026", "totals": {"points_2026": 99999}}, f)
        _fetch(d, {"38001": [_pt(1)], "38003": []})
        with open(os.path.join(live, "nsi_summary.json"), "w") as f:
            json.dump({"mode": "merged", "structures": 99999}, f)
        _fetch(d, {"38001": [_pt(1)], "38003": []})
    finally:
        os.environ.pop(UC.NSI_ALLOW_DROP_ENV, None)
        if saved is not None:
            os.environ[UC.NSI_ALLOW_DROP_ENV] = saved
        if tmp_path is None:
            shutil.rmtree(d, ignore_errors=True)


def test_the_updater_keeps_the_inventory_in_use_when_the_new_one_is_refused(tmp_path=None):
    d = str(tmp_path) if tmp_path is not None else tempfile.mkdtemp()
    saved = os.environ.pop(UC.NSI_ALLOW_DROP_ENV, None)
    try:
        full = {"38001": [_pt(i) for i in range(100)], "38003": [_pt(500 + i) for i in range(100)]}
        src = _source(check_interval_hours=0)
        up = UC.Updater(d, [src], Http(full))
        r = up._update_one(src, Feedback(), True)
        assert r.status == "updated", (r.status, r.message)
        live = os.path.join(d, "nsi")
        files = sorted(os.listdir(os.path.join(live, "counties")))
        assert files == ["38001.geojson", "38003.geojson"]
        up.http = Http({"38001": full["38001"][:10], "38003": []})
        r = up._update_one(src, Feedback(), True)
        assert r.status == "error" and "was kept" in r.message, (r.status, r.message)
        assert sorted(os.listdir(os.path.join(live, "counties"))) == files
        n = len(json.load(open(os.path.join(live, "counties", "38001.geojson")))["features"])
        assert n == 100
        assert not os.listdir(os.path.join(d, ".staging"))      # the refused copy is gone
    finally:
        if saved is not None:
            os.environ[UC.NSI_ALLOW_DROP_ENV] = saved
        if tmp_path is None:
            shutil.rmtree(d, ignore_errors=True)


def test_a_downloaded_zip_is_checked_before_it_is_unpacked(tmp_path=None):
    d = str(tmp_path) if tmp_path is not None else tempfile.mkdtemp()
    old = (ZS.RATIO_FLOOR_BYTES, ZS.MAX_RATIO, ZS.MAX_DATASET_BYTES)
    try:
        dest = os.path.join(d, "out")
        good = os.path.join(d, "good.zip")
        with zipfile.ZipFile(good, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("layer.gpkg", b"data" * 100)
        UC.extract_zip(good, dest, Feedback())
        assert os.path.isfile(os.path.join(dest, "layer.gpkg"))
        shutil.rmtree(dest)
        bomb = os.path.join(d, "bomb.zip")
        with zipfile.ZipFile(bomb, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("zeros.bin", b"\0" * 4000000)
        ZS.RATIO_FLOOR_BYTES, ZS.MAX_RATIO = 1000000, 100
        for path, word in ((bomb, "compresses that hard"),):
            try:
                UC.extract_zip(path, dest, Feedback())
                raise AssertionError("unpacked")
            except UC.UpdateError as e:
                assert "was not unpacked" in str(e) and word in str(e), str(e)
            assert not os.path.isdir(dest) or not os.listdir(dest)
        ZS.RATIO_FLOOR_BYTES, ZS.MAX_RATIO = old[0], old[1]
        ZS.MAX_DATASET_BYTES = 1000
        try:
            UC.extract_zip(bomb, dest, Feedback())
            raise AssertionError("unpacked past the size limit")
        except UC.UpdateError as e:
            assert "unpacks to" in str(e), str(e)
        ZS.MAX_DATASET_BYTES = old[2]
        evil = os.path.join(d, "evil.zip")
        with zipfile.ZipFile(evil, "w") as z:
            z.writestr("ok.txt", b"ok")
            z.writestr("../outside.txt", b"x")
        try:
            UC.extract_zip(evil, dest, Feedback())
            raise AssertionError("unpacked")
        except UC.UpdateError as e:
            assert "climbs out" in str(e), str(e)
        assert not os.path.exists(os.path.join(d, "outside.txt"))
        assert not os.path.isdir(dest) or not os.listdir(dest)
        with open(os.path.join(d, "not.zip"), "wb") as f:
            f.write(b"<html>")
        try:
            UC.extract_zip(os.path.join(d, "not.zip"), dest, Feedback())
            raise AssertionError("unpacked")
        except UC.UpdateError as e:
            assert "not a valid zip" in str(e), str(e)
    finally:
        ZS.RATIO_FLOOR_BYTES, ZS.MAX_RATIO, ZS.MAX_DATASET_BYTES = old
        if tmp_path is None:
            shutil.rmtree(d, ignore_errors=True)


def test_an_answer_read_into_memory_is_bounded():
    from DamSafety_Data import http_urllib as HU

    class Resp:
        headers = {}

        def __init__(self, n):
            self.n = n

        def read(self, size=-1):
            return b"x" * (self.n if size < 0 else min(self.n, size))

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    client = HU.UrllibHttp()
    old = HU.MAX_GET_BYTES
    HU.MAX_GET_BYTES = 1000
    try:
        client._open = lambda url, method="GET", headers=None: Resp(1000)
        assert len(client.get_bytes("https://x.test/a")) == 1000
        client._open = lambda url, method="GET", headers=None: Resp(10 ** 9)
        try:
            client.get_bytes("https://x.test/a")
            raise AssertionError("an unbounded answer was read")
        except HU.UpdateError as e:
            assert "byte limit" in str(e), str(e)
    finally:
        HU.MAX_GET_BYTES = old


if __name__ == "__main__":
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            n += 1
            print("ok   " + name)
    print("{} tests ok".format(n))
